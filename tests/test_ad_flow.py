"""Сценарий объявления в боте целиком + кнопки модерации под постом в канале."""
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

CHANNEL, UID, ADMIN = -1005556667778, 300001, 999
_ids = itertools.count(20000)


class FakeBot:
    id = 42

    def __init__(self):
        self.subscribed = True
        self.dm, self.channel, self.alerts, self.pinned, self.deleted = [], [], [], [], []
        self.dm_blocked = False

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def get_chat(self, chat_id):
        return Chat(id=chat_id, type="channel", title="Объявления", username="adschannel")

    async def get_chat_member(self, chat_id, user_id):
        class M:
            status = ("administrator" if user_id == ADMIN
                      else ("member" if FakeBotState.subscribed else "left"))
        return M()

    async def send_message(self, chat_id, text, **kw):
        if chat_id > 0 and self.dm_blocked:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked")
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, text, mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, caption or "", mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.pinned.append(message_id); return True

    async def unpin_chat_message(self, chat_id, message_id=None): return True

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

    # тексты последних сообщений в личку
    def last_dm(self, n=1):
        return [d[1] for d in self.dm[-n:]]


class FakeBotState:
    subscribed = True


def priv(text, uid=UID, username="adman", photo=False):
    kw = {"caption": text, "photo": [{"file_id": "PH1", "file_unique_id": "u",
                                      "width": 100, "height": 100}]} if photo else {"text": text}
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Автор", username=username), **kw))


def cb(data, uid=UID, chat_id=None, chat_type="private"):
    chat_id = uid if chat_id is None else chat_id
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data,
        from_user=User(id=uid, is_bot=False, first_name="Автор", username="adman"),
        message=Message(message_id=next(_ids), date=0,
                        chat=Chat(id=chat_id, type=chat_type))))


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.set_setting("ad_channel_title", "Объявления")
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, moderation.router, post.router, payments.router,
              user_h.router, chat_guard.router):
        dp.include_router(r)
    bot = FakeBot()
    await db.upsert_user(UID, "adman", "Автор Объявлений")

    types = {r["name"]: r for r in await db.ad_types()}
    direct = types["Прямой рекламодатель"]          # с вертикалью
    intro = types["Интро/Знакомства"]               # с пометкой
    resume = types["Резюме"]                        # без вертикали
    gambling = next(r for r in await db.verticals() if r["name"] == "Гемблинг")

    # --- 1. Правила показываются до создания объявления --------------------
    await dp.feed_update(bot, priv("/post"))
    assert "Правила публикации" in bot.last_dm()[0], bot.last_dm()
    assert not await db.rules_accepted(UID)
    print("правила OK: показаны перед созданием объявления")

    # отдельная кнопка правил тоже работает
    await dp.feed_update(bot, priv("📜 Правила"))
    assert "Правила публикации" in bot.last_dm()[0]

    # --- 2. Приняли правила -> проверка подписки ---------------------------
    await dp.feed_update(bot, priv("/post"))
    FakeBotState.subscribed = False
    await dp.feed_update(bot, cb("ads_rules_ok"))
    assert await db.rules_accepted(UID), "принятие сохранено"
    assert "подпи" in " ".join(bot.last_dm(2)).lower(), bot.last_dm(2)
    print("подписка OK: без подписки дальше не пускает")

    # не подписан -> кнопка «Я подписался» отказывает
    await dp.feed_update(bot, cb("ads_sub_check"))
    assert bot.alerts and "подпис" in bot.alerts[-1].lower(), bot.alerts[-1]

    # --- 3. Подписался -> бонус за подписку и выбор рубрики -----------------
    FakeBotState.subscribed = True
    await dp.feed_update(bot, cb("ads_sub_check"))
    assert await tk.balance(UID) == 30, await tk.balance(UID)
    assert "рубрик" in " ".join(bot.last_dm(2)).lower(), bot.last_dm(2)
    print("активация OK: начислено", await tk.balance(UID), "коинов, спрашивает рубрику")

    # --- 4. Рубрика с вертикалью -------------------------------------------
    await dp.feed_update(bot, cb(f"adtype:{direct['id']}"))
    assert "вертикал" in " ".join(bot.last_dm(2)).lower(), bot.last_dm(2)
    await dp.feed_update(bot, cb(f"advert:{gambling['id']}"))
    assert "текст" in " ".join(bot.last_dm(2)).lower(), bot.last_dm(2)
    print("рубрика и вертикаль OK")

    # --- 5. Текст, картинка, закреп ----------------------------------------
    await dp.feed_update(bot, priv("Ищу байеров под гемблу, гео Tier-1"))
    assert "картинк" in " ".join(bot.last_dm(2)).lower(), bot.last_dm(2)
    await dp.feed_update(bot, cb("ads_img_yes"))
    await dp.feed_update(bot, priv("", photo=True))
    assert "закреп" in " ".join(bot.last_dm(2)).lower(), bot.last_dm(2)
    await dp.feed_update(bot, cb("ads_pin:4"))
    preview = " ".join(bot.last_dm(3))
    assert "#прямой_рекламодатель" in preview and "#гемблинг" in preview, preview
    assert "30" in preview, "в предпросмотре итоговая цена 10+5+15"
    print("предпросмотр OK: хэштеги и цена на месте")

    # --- 6. Публикация ------------------------------------------------------
    await tk.add(UID, 100, "test")
    balance_before = await tk.balance(UID)
    await dp.feed_update(bot, cb("ads_publish"))
    assert bot.channel and bot.channel[-1][0] == CHANNEL, bot.channel
    assert "#прямой_рекламодатель #гемблинг" in bot.channel[-1][1], bot.channel[-1][1]
    ad = await db.fetchone("SELECT * FROM ads ORDER BY id DESC LIMIT 1")
    assert ad["cost_total"] == 30 and ad["pin_hours"] == 4, dict(ad)
    assert ad["channel_message_id"] in bot.pinned, "пост закреплён"
    assert await tk.balance(UID) == balance_before - 30
    assert "t.me/adschannel/" in " ".join(bot.last_dm(2)), "в ответе ссылка на пост"
    posts, total = await sdb.query_posts(ad_type="прямой_рекламодатель")
    assert total == 1, total
    print(f"публикация OK: списано 30, закреп 4 ч, на сайте {total}")

    # --- 7. Рубрика без вертикали и пометка «Интро» -------------------------
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{resume['id']}"))
    assert "вертикал" not in " ".join(bot.last_dm(2)).lower(), "вертикаль не спрашиваем"
    print("рубрика без вертикали OK: шаг пропущен")

    await dp.feed_update(bot, cb("ads_cancel"))
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{intro['id']}"))
    assert "Telegram-канал" in " ".join(bot.last_dm(3)), bot.last_dm(3)
    print("пометка «Интро/Знакомства» OK")

    # --- 8. Не хватает коинов -> сразу предложение пополнить ----------------
    await dp.feed_update(bot, priv("Пара слов о себе и мои соцсети"))
    await dp.feed_update(bot, cb("ads_img_no"))
    await dp.feed_update(bot, cb("ads_pin:8"))
    await db.execute("UPDATE users SET tokens = 1 WHERE user_id = ?", (UID,))
    n = len(bot.channel)
    await dp.feed_update(bot, cb("ads_publish"))
    assert len(bot.channel) == n, "ничего не опубликовано"
    assert "не хватает" in bot.last_dm()[0].lower(), bot.last_dm()
    assert await db.scalar("SELECT COUNT(*) FROM ads") == 1, "черновик не создан"
    print("нехватка коинов OK:", bot.last_dm()[0].split(chr(10))[0][:70])

    # --- 9. Кнопки под постом: чужому нельзя --------------------------------
    ad_id = ad["id"]
    await dp.feed_update(bot, cb(f"ad_del:{ad_id}", uid=777, chat_id=CHANNEL,
                                 chat_type="channel"))
    assert "администрац" in bot.alerts[-1].lower(), bot.alerts[-1]
    assert (await db.fetchone("SELECT status FROM ads WHERE id=?", (ad_id,)))["status"] == "published"
    print("права на кнопки OK:", bot.alerts[-1])

    # --- 10. Удаление с комментом админом ------------------------------------
    await dp.feed_update(bot, cb(f"ad_delc:{ad_id}", uid=ADMIN, chat_id=CHANNEL,
                                 chat_type="channel"))
    assert any("коммент" in d.lower() for d in bot.last_dm(2)), bot.last_dm(2)
    balance_before = await tk.balance(UID)
    await dp.feed_update(bot, priv("Дубль, уже было такое объявление", uid=ADMIN,
                                   username="admin"))
    assert await tk.balance(UID) == balance_before + 30, "коины вернулись автору"
    assert (CHANNEL, ad["channel_message_id"]) in bot.deleted, bot.deleted
    assert any("Дубль" in d[1] for d in bot.dm if d[0] == UID), "автору ушёл комментарий"
    row = await db.fetchone("SELECT * FROM ads WHERE id = ?", (ad_id,))
    assert row["status"] == "deleted" and row["delete_comment"].startswith("Дубль")
    _, total = await sdb.query_posts()
    assert total == 0, "пост снят с сайта"
    print("удаление с комментом OK: вернули 30 коинов, пост убран из канала и с сайта")

    print("AD FLOW OK")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
