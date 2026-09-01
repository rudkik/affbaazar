"""Гонки: параллельные нажатия кнопок не должны дублировать деньги и посты."""
import asyncio, os, sys, pathlib, itertools, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al, app.ads as ads
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
import app.handlers.admin as ah, app.handlers.user as uh, app.handlers.post as ph
import app.handlers.moderation as mh, app.handlers.payments as pay
for mod in (ah, uh, ph, mh, pay, ads):
    if hasattr(mod, "ADMINS"):
        mod.ADMINS = {999}

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from app.handlers import admin, chat_guard, moderation, payments, post, user as user_h

CHANNEL, UID, ADMIN = -1009990001112, 700900, 999
_ids = itertools.count(50000)
FAILS = []


class FakeBot:
    """Сетевые вызовы уступают управление — как настоящие, чтобы гонка была реальной."""
    id = 42

    def __init__(self):
        self.dm, self.channel, self.alerts, self.pinned, self.deleted = [], [], [], [], []

    async def _io(self):
        await asyncio.sleep(0.01)

    async def get_me(self):
        await self._io()
        return User(id=self.id, is_bot=True, first_name="Bot", username="affbot")

    async def get_chat(self, chat_id):
        await self._io()
        return Chat(id=chat_id, type="channel", title="Канал", username="affchannel")

    async def get_chat_member(self, chat_id, user_id):
        await self._io()
        class M: status = "administrator" if user_id == ADMIN else "member"
        return M()

    async def send_message(self, chat_id, text, **kw):
        await self._io()
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, text, mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        return await self.send_message(chat_id, caption or "")

    async def pin_chat_message(self, chat_id, message_id, **kw):
        await self._io(); self.pinned.append(message_id); return True

    async def unpin_chat_message(self, chat_id, message_id=None):
        await self._io(); return True

    async def delete_message(self, chat_id, message_id):
        await self._io(); self.deleted.append((chat_id, message_id)); return True

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text)
        if name == "AnswerCallbackQuery":
            await self._io(); self.alerts.append(method.text or ""); return True
        if name in {"EditMessageText", "EditMessageReplyMarkup", "DeleteMessage"}:
            await self._io(); return True
        raise AssertionError("не смоделирован метод " + name)


def priv(text, uid=UID, username="racer"):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Ю", username=username), text=text))


def cb(data, uid=UID, chat_id=None, chat_type="private"):
    chat_id = uid if chat_id is None else chat_id
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data,
        from_user=User(id=uid, is_bot=False, first_name="Ю", username="racer"),
        message=Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type=chat_type))))


def check(name, ok, detail=""):
    print(("  OK   " if ok else "  БАГ  ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name} — {detail}")


async def to_confirm(dp, bot, uid):
    t = (await db.ad_types())[0]
    v = (await db.verticals())[1]
    await dp.feed_update(bot, priv("/post", uid=uid))
    if not await db.rules_accepted(uid):
        await dp.feed_update(bot, cb("ads_rules_ok", uid=uid))
    await dp.feed_update(bot, cb(f"adtype:{t['id']}", uid=uid))
    await dp.feed_update(bot, cb(f"advert:{v['id']}", uid=uid))
    await dp.feed_update(bot, priv("Объявление про гонки", uid=uid))
    await dp.feed_update(bot, cb("ads_img_no", uid=uid))
    await dp.feed_update(bot, cb("ads_pin:0", uid=uid))


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.set_setting("ad_channel_title", "Канал")
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, moderation.router, post.router, payments.router,
              user_h.router, chat_guard.router):
        dp.include_router(r)
    bot = FakeBot()

    print("\n--- двойной тап «Опубликовать» (денег ровно на одно объявление) ---")
    await db.upsert_user(UID, "racer", "Гонщик")
    await db.execute("UPDATE users SET tokens = 10 WHERE user_id = ?", (UID,))
    await to_confirm(dp, bot, UID)
    n = len(bot.channel)
    await asyncio.gather(dp.feed_update(bot, cb("ads_publish")),
                         dp.feed_update(bot, cb("ads_publish")))
    posts = len(bot.channel) - n
    bal = await tk.balance(UID)
    ads_cnt = await db.scalar("SELECT COUNT(*) FROM ads WHERE channel_message_id IS NOT NULL")
    check("параллельные нажатия дают ровно один пост", posts == 1, f"постов: {posts}")
    check("баланс не уходит в минус", bal >= 0, f"баланс: {bal}")
    check("списано ровно за одно объявление", bal == 0, f"баланс: {bal} (ожидался 0)")
    check("в базе одно объявление", ads_cnt == 1, f"объявлений: {ads_cnt}")

    print("\n--- двойной тап «Опубликовать» при большом балансе ---")
    await db.execute("UPDATE users SET tokens = 500 WHERE user_id = ?", (UID,))
    await to_confirm(dp, bot, UID)
    n, before = len(bot.channel), await tk.balance(UID)
    await asyncio.gather(dp.feed_update(bot, cb("ads_publish")),
                         dp.feed_update(bot, cb("ads_publish")),
                         dp.feed_update(bot, cb("ads_publish")))
    posts = len(bot.channel) - n
    spent = before - await tk.balance(UID)
    check("тройной тап не публикует три раза", posts == 1, f"постов: {posts}")
    check("списано за одно объявление", spent == 10, f"списано: {spent}")

    print("\n--- двойной тап «Удалить» под постом ---")
    ad = await db.fetchone("SELECT * FROM ads WHERE channel_message_id IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1")
    before = await tk.balance(UID)
    await asyncio.gather(
        dp.feed_update(bot, cb(f"ad_del:{ad['id']}", uid=ADMIN, chat_id=CHANNEL,
                               chat_type="channel")),
        dp.feed_update(bot, cb(f"ad_del:{ad['id']}", uid=ADMIN, chat_id=CHANNEL,
                               chat_type="channel")))
    got = await tk.balance(UID) - before
    check("параллельное удаление возвращает коины один раз", got == ad["cost_total"],
          f"вернулось {got}, стоило {ad['cost_total']}")

    print("\n--- параллельная активация: бонус за подписку ---")
    await db.upsert_user(800900, "newbie", "Новичок")
    await asyncio.gather(*[tk.grant_signup_bonus(800900) for _ in range(3)])
    check("бонус за подписку начислен один раз", await tk.balance(800900) == 30,
          f"баланс: {await tk.balance(800900)}")

    print("\n--- параллельная награда рефереру ---")
    await db.upsert_user(800901, "friend", "Друг")
    await db.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (800900, 800901))
    before = await tk.balance(800900)
    await asyncio.gather(*[tk.reward_referrer(800901) for _ in range(3)])
    got = await tk.balance(800900) - before
    check("реферальный бонус начислен один раз", got == 10, f"начислено: {got}")

    print("\n--- параллельные списания ---")
    await db.execute("UPDATE users SET tokens = 10 WHERE user_id = ?", (UID,))
    res = await asyncio.gather(*[tk.charge(UID, 10, "test") for _ in range(3)])
    check("из трёх параллельных списаний проходит одно", sum(res) == 1, f"успешных: {sum(res)}")
    check("баланс не ушёл в минус", await tk.balance(UID) == 0,
          f"баланс: {await tk.balance(UID)}")

    print("\n--- /start посреди ввода текста объявления ---")
    await db.execute("UPDATE users SET tokens = 100 WHERE user_id = ?", (UID,))
    t = (await db.ad_types())[3]     # рубрика без вертикали
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{t['id']}"))
    await dp.feed_update(bot, priv("/start"))
    n = len(bot.channel)
    await dp.feed_update(bot, cb("ads_img_no"))     # если состояние не сброшено — пойдёт дальше
    await dp.feed_update(bot, cb("ads_pin:0"))
    await dp.feed_update(bot, cb("ads_publish"))
    published = [c[1] for c in bot.channel[n:]]
    check("/start не становится текстом объявления",
          not any("/start" in p for p in published), f"опубликовано: {published}")

    print("\n--- выключенная рубрика ---")
    t = (await db.ad_types())[-1]
    await db.execute("UPDATE ad_types SET is_active = 0 WHERE id = ?", (t["id"],))
    await dp.feed_update(bot, priv("/post"))
    n = len(bot.dm)
    await dp.feed_update(bot, cb(f"adtype:{t['id']}"))
    answer = " ".join(d[1] for d in bot.dm[n:]) + " " + " ".join(bot.alerts[-2:])
    check("выключенную рубрику нельзя выбрать подменой кнопки",
          "не найдена" in answer.lower() or "недоступна" in answer.lower(),
          answer[:90].replace("\n", " "))
    await db.execute("UPDATE ad_types SET is_active = 1 WHERE id = ?", (t["id"],))

    v = (await db.verticals())[-1]
    await db.execute("UPDATE verticals SET is_active = 0 WHERE id = ?", (v["id"],))
    t = (await db.ad_types())[0]          # рубрика с вертикалью
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{t['id']}"))
    n = len(bot.dm)
    await dp.feed_update(bot, cb(f"advert:{v['id']}"))
    answer = " ".join(d[1] for d in bot.dm[n:]) + " " + " ".join(bot.alerts[-2:])
    check("выключенную вертикаль нельзя выбрать подменой кнопки",
          "не найдена" in answer.lower() or "недоступна" in answer.lower(),
          answer[:90].replace("\n", " "))
    await db.execute("UPDATE verticals SET is_active = 1 WHERE id = ?", (v["id"],))
    await dp.feed_update(bot, cb("ads_cancel"))

    print()
    if FAILS:
        print("НАЙДЕНО ПРОБЛЕМ:", len(FAILS))
        for f in FAILS:
            print("  •", f)
        sys.exit(1)
    print("RACES OK — гонок не осталось")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
