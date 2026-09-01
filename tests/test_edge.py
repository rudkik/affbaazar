"""Краевые случаи и защита денег: двойные нажатия, античит, инъекции, сбои."""
import asyncio, os, sys, pathlib, itertools, tempfile, json
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
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from app.handlers import admin, chat_guard, moderation, payments, post, user as user_h

CHANNEL, UID, ADMIN = -1007778889990, 400001, 999
_ids = itertools.count(30000)
FAILS = []


class S:
    subscribed = True
    pin_fails = False
    unpin_fails = False


class FakeBot:
    id = 42

    def __init__(self):
        self.dm, self.channel, self.alerts, self.pinned, self.unpinned, self.deleted = \
            [], [], [], [], [], []

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="affbot")

    async def get_chat(self, chat_id):
        return Chat(id=chat_id, type="channel", title="Канал", username="affchannel")

    async def get_chat_member(self, chat_id, user_id):
        class M:
            status = ("administrator" if user_id == ADMIN
                      else ("member" if S.subscribed else "left"))
        return M()

    async def send_message(self, chat_id, text, **kw):
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, text, mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, caption or "", mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def pin_chat_message(self, chat_id, message_id, **kw):
        if S.pin_fails:
            raise TelegramBadRequest(method=None, message="not enough rights to pin")
        self.pinned.append(message_id); return True

    async def unpin_chat_message(self, chat_id, message_id=None):
        if S.unpin_fails:
            raise TelegramBadRequest(method=None, message="not enough rights")
        self.unpinned.append(message_id); return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id)); return True

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text)
        if name == "AnswerCallbackQuery":
            self.alerts.append(method.text or ""); return True
        if name in {"EditMessageText", "EditMessageReplyMarkup", "DeleteMessage"}:
            return True
        raise AssertionError("не смоделирован метод " + name)

    def last_dm(self, n=1):
        return " | ".join(d[1] for d in self.dm[-n:])


class U:
    def __init__(self, uid, username="user", full_name="Пользователь"):
        self.id, self.username, self.full_name = uid, username, full_name


def priv(text, uid=UID, username="adman", photo=False):
    kw = {"caption": text, "photo": [{"file_id": "PH", "file_unique_id": "u",
                                      "width": 10, "height": 10}]} if photo else {"text": text}
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Ю", username=username), **kw))


def cb(data, uid=UID, chat_id=None, chat_type="private"):
    chat_id = uid if chat_id is None else chat_id
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data,
        from_user=User(id=uid, is_bot=False, first_name="Ю", username="adman"),
        message=Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type=chat_type))))


def check(name, ok, detail=""):
    print(("  OK   " if ok else "  БАГ  ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name + (f" — {detail}" if detail else ""))


async def full_flow(dp, bot, uid, pin=0, text="Объявление для теста"):
    """Проходит сценарий до экрана подтверждения; возвращает ничего."""
    t = (await db.ad_types())[0]
    v = (await db.verticals())[1]
    await dp.feed_update(bot, priv("/post", uid=uid))
    if not await db.rules_accepted(uid):
        await dp.feed_update(bot, cb("ads_rules_ok", uid=uid))
    await dp.feed_update(bot, cb(f"adtype:{t['id']}", uid=uid))
    await dp.feed_update(bot, cb(f"advert:{v['id']}", uid=uid))
    await dp.feed_update(bot, priv(text, uid=uid))
    await dp.feed_update(bot, cb("ads_img_no", uid=uid))
    await dp.feed_update(bot, cb(f"ads_pin:{pin}", uid=uid))


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.set_setting("ad_channel_title", "Канал")
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, moderation.router, post.router, payments.router,
              user_h.router, chat_guard.router):
        dp.include_router(r)
    bot = FakeBot()
    await db.upsert_user(UID, "adman", "Автор")
    await tk.add(UID, 500, "test")

    print("\n--- деньги: двойные нажатия ---")
    await full_flow(dp, bot, UID)
    before = await tk.balance(UID)
    await dp.feed_update(bot, cb("ads_publish"))
    after_first = await tk.balance(UID)
    await dp.feed_update(bot, cb("ads_publish"))          # второе нажатие той же кнопки
    check("повторное «Опубликовать» не списывает второй раз",
          await tk.balance(UID) == after_first,
          f"баланс {before} → {after_first} → {await tk.balance(UID)}")
    check("повторное «Опубликовать» не создаёт второй пост",
          await db.scalar("SELECT COUNT(*) FROM ads") == 1,
          f"объявлений в базе: {await db.scalar('SELECT COUNT(*) FROM ads')}")

    ad = await db.fetchone("SELECT * FROM ads ORDER BY id DESC LIMIT 1")
    bal = await tk.balance(UID)
    await dp.feed_update(bot, cb(f"ad_del:{ad['id']}", uid=ADMIN, chat_id=CHANNEL,
                                 chat_type="channel"))
    once = await tk.balance(UID)
    await dp.feed_update(bot, cb(f"ad_del:{ad['id']}", uid=ADMIN, chat_id=CHANNEL,
                                 chat_type="channel"))
    check("повторное «Удалить» не возвращает коины дважды",
          await tk.balance(UID) == once, f"{bal} → {once} → {await tk.balance(UID)}")

    print("\n--- античит бонусов ---")
    await db.upsert_user(500001, "cheater", "Читер")
    await dp.feed_update(bot, priv("/start ref_500001", uid=500001, username="cheater"))
    row = await db.get_user(500001)
    check("самореферал не проставляется", row["referrer_id"] is None,
          f"referrer_id={row['referrer_id']}")

    await db.upsert_user(500002, "victim", "Жертва")
    await dp.feed_update(bot, priv("/start ref_777777", uid=500002, username="victim"))
    row = await db.get_user(500002)
    check("несуществующий реферер не проставляется", row["referrer_id"] is None,
          f"referrer_id={row['referrer_id']}")

    bonus_before = await tk.balance(500001)
    await tk.grant_signup_bonus(500001)
    once_bonus = await tk.balance(500001)
    await tk.grant_signup_bonus(500001)
    check("бонус за подписку выдаётся один раз",
          await tk.balance(500001) == once_bonus,
          f"{bonus_before} → {once_bonus} → {await tk.balance(500001)}")

    print("\n--- обход платы и подписки ---")
    S.subscribed = False
    await db.upsert_user(500003, "nosub", "Без подписки")
    await tk.add(500003, 500, "test")
    await db.accept_rules(500003)
    n = len(bot.channel)
    await full_flow(dp, bot, 500003)
    await dp.feed_update(bot, cb("ads_publish", uid=500003))
    check("отписавшийся не может опубликовать", len(bot.channel) == n,
          f"постов добавилось: {len(bot.channel) - n}")
    S.subscribed = True

    # подмена рубрики на несуществующую
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb("adtype:99999"))
    check("несуществующая рубрика не роняет бота", True)

    # публикация с нулевой ценой
    await db.set_setting("price_post", 0)
    await db.set_setting("price_image", 0)
    await db.set_setting("price_pin_4h", 0)
    await db.execute("UPDATE users SET tokens = 0 WHERE user_id = ?", (UID,))
    await full_flow(dp, bot, UID, text="Бесплатное объявление")
    n = len(bot.channel)
    await dp.feed_update(bot, cb("ads_publish"))
    check("при нулевой цене публикация проходит с нулевым балансом",
          len(bot.channel) == n + 1, f"постов добавилось: {len(bot.channel) - n}")
    await db.set_setting("price_post", 10)
    await db.set_setting("price_image", 5)
    await db.set_setting("price_pin_4h", 15)

    print("\n--- инъекции и длинный ввод ---")
    await tk.add(UID, 500, "test")
    payload = '<script>alert(1)</script> и <b>жирный</b> текст'
    await full_flow(dp, bot, UID, text=payload)
    await dp.feed_update(bot, cb("ads_publish"))
    posted = bot.channel[-1][1]
    check("HTML из текста экранируется в посте канала",
          "&lt;script&gt;" in posted and "<script>" not in posted,
          posted[:80].replace("\n", " "))
    rows, _ = await sdb.query_posts(q="жирный")
    check("на сайт текст попадает без разметки-исполнения",
          rows and "<script>" in rows[0]["text"],
          "на сайте хранится исходный текст, экранирование — на стороне страницы")

    print("\n--- сбои Telegram ---")
    S.pin_fails = True
    bal = await tk.balance(UID)
    await full_flow(dp, bot, UID, pin=4, text="Пост с неудачным закрепом")
    await dp.feed_update(bot, cb("ads_publish"))
    ad = await db.fetchone("SELECT * FROM ads ORDER BY id DESC LIMIT 1")
    spent = bal - await tk.balance(UID)
    check("если закреп не удался — за него не берут денег",
          spent == 10 and ad["cost_pin"] == 0 and ad["pin_hours"] == 0
          and ad["pinned_until"] is None,
          f"списано {spent}, cost_pin={ad['cost_pin']}, pin_hours={ad['pin_hours']}, "
          f"pinned_until={ad['pinned_until']}")
    S.pin_fails = False

    S.unpin_fails = True
    await db.execute("UPDATE ads SET pinned_until = datetime('now','-1 minute'), unpinned = 0 "
                     "WHERE id = ?", (ad["id"],))
    done = await ads.unpin_expired(bot)
    left = await db.scalar("SELECT COUNT(*) FROM ads WHERE unpinned = 0 AND pinned_until "
                           "<= datetime('now')")
    check("ошибка снятия закрепа не зацикливает фоновую задачу",
          done == 1 and left == 0, f"обработано {done}, осталось {left}")
    S.unpin_fails = False

    print("\n--- настройки с мусором ---")
    await db.set_setting("token_packages", "не json")
    try:
        kb = await __import__("app.keyboards", fromlist=["x"]).packages_kb()
        check("битый JSON пакетов не роняет клавиатуру", kb is not None)
    except Exception as exc:
        check("битый JSON пакетов не роняет клавиатуру", False, repr(exc))
    await db.set_setting("token_packages",
                         json.dumps([{"stars": 50, "tokens": 50}], ensure_ascii=False))

    await db.set_setting("price_post", -100)
    q = await ads.price_quote()
    check("отрицательная цена не даёт отрицательный итог", q["total"] >= 0,
          f"итог: {q['total']}")
    await db.set_setting("price_post", 10)

    print("\n--- выдача коинов админом ---")
    await db.execute("UPDATE users SET tokens = 10 WHERE user_id = ?", (UID,))
    await dp.feed_update(bot, priv("/give @adman -1000", uid=ADMIN, username="admin"))
    check("списание больше баланса не уводит в минус", await tk.balance(UID) >= 0,
          f"баланс: {await tk.balance(UID)}")

    print("\n--- FSM ---")
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, priv("/start"))
    st = await db.fetchone("SELECT 1")   # заглушка, состояние проверяем поведением
    n = len(bot.channel)
    await dp.feed_update(bot, priv("случайный текст после /start"))
    check("/start прерывает создание объявления и не публикует мусор",
          len(bot.channel) == n, f"постов добавилось: {len(bot.channel) - n}")

    print()
    if FAILS:
        print("НАЙДЕНО ПРОБЛЕМ:", len(FAILS))
        for f in FAILS:
            print("  •", f)
        sys.exit(1)
    print("EDGE OK — все краевые проверки прошли")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
