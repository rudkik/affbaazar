"""Задачи трекера AffBazaar-18…20, 22, 23.

18 — бот поддержки: топик на пользователя в форум-группе, ответы из топика, /ban, кнопка в главном боте
19 — запрет дублей: то же объявление нельзя повторить раньше ad_dup_hours, бот говорит через сколько
20 — «Мои объявления»: список, карточка, продлить (повторная публикация), докупить закреп
22 — под балансом сразу кнопки покупки коинов
23 — объединённые кнопки меню: «Канал и сайт Aff Bazaar», «Баланс / Купить коины»
"""
import asyncio, itertools, os, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"; cfg.ADMINS = {999}
cfg.SUPPORT_CHAT_ID = -1005363518745
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al, app.ads as ads
import app.support as support
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR

from datetime import timedelta
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from app.handlers import admin, chat_guard, members, moderation, myads, payments, post, user as user_h

CHANNEL, UID, SUP = -1005556667778, 400001, cfg.SUPPORT_CHAT_ID
_ids = itertools.count(70000)


class FakeBot:
    id = 42

    def __init__(self):
        self.dm, self.channel, self.alerts, self.pinned, self.deleted, self.edits = [], [], [], [], [], []
        self.topics, self.copied, self.group, self.markups = [], [], [], []
        self.copy_fail = None

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def get_chat(self, chat_id):
        return Chat(id=chat_id, type="channel", title="Объявления", username="adschannel")

    async def get_chat_member(self, chat_id, user_id):
        class M: status = "member"
        return M()

    async def send_message(self, chat_id, text, **kw):
        mid = next(_ids)
        if chat_id == SUP:
            self.group.append((kw.get("message_thread_id"), text))
        else:
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

    # --- поддержка
    async def create_forum_topic(self, chat_id, name):
        class T: message_thread_id = 1000 + len(self.topics)
        self.topics.append(name); return T()

    async def copy_message(self, chat_id, from_chat_id, message_id, message_thread_id=None, **kw):
        if self.copy_fail:
            exc, self.copy_fail = self.copy_fail, None
            raise exc
        self.copied.append((chat_id, from_chat_id, message_id, message_thread_id)); return True

    async def delete_webhook(self, **kw): return True

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            self.markups.append(method.reply_markup)
            return await self.send_message(method.chat_id, method.text,
                                           message_thread_id=getattr(method, "message_thread_id", None))
        if name == "AnswerCallbackQuery":
            self.alerts.append(method.text or ""); return True
        if name == "EditMessageText":
            self.edits.append((method.text, method.reply_markup)); return True
        if name in {"EditMessageReplyMarkup", "DeleteMessage"}:
            return True
        raise AssertionError("не смоделирован метод " + name)

    def to(self, uid):
        return [t for c, t, _ in self.dm if c == uid]


def tg(uid, name="Автор"):
    return User(id=uid, is_bot=False, first_name=name, username=f"u{uid}", language_code="ru")


def priv(text, uid=UID):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"), from_user=tg(uid), text=text))


def group_msg(text, thread_id, uid=999):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=SUP, type="supergroup", title="Support",
                                                 is_forum=True),
        from_user=tg(uid, "Саппорт"), text=text, message_thread_id=thread_id, is_topic_message=True))


def cb(data, uid=UID):
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data, from_user=tg(uid),
        message=Message(message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"))))


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def wizard(dp, bot, text, uid=UID):
    """Проходим мастер до шага закрепа: /post → рубрика → текст → без картинки."""
    await dp.feed_update(bot, priv("/post", uid))
    types = {r["name"]: r for r in await db.ad_types()}
    await dp.feed_update(bot, cb(f"adtype:{types['Резюме']['id']}", uid))
    await dp.feed_update(bot, priv(text, uid))


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL); await db.set_setting("ad_channel_username", "adschannel")
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, moderation.router, post.router, myads.router, payments.router,
              user_h.router, chat_guard.router, members.router):
        dp.include_router(r)
    bot = FakeBot()
    await db.upsert_user(UID, "u400001", "Автор"); await db.accept_rules(UID)
    await db.execute("UPDATE users SET activated = 1 WHERE user_id = ?", (UID,))
    await tk.add(UID, 500, "test")
    types = {r["name"]: r for r in await db.ad_types()}
    resume = types["Резюме"]

    # ================= 19. дубли ===========================================================
    res = await ads.publish_ad(bot, tg(UID), text="Ищу работу медиабайером", ad_type_row=resume)
    first_id = res["ad_id"]
    # тот же текст (регистр и пробелы не спасают) — на шаге текста мастер останавливает
    await wizard(dp, bot, "  ищу   работу МЕДИАБАЙЕРОМ ")
    msg = bot.to(UID)[-1]
    assert "Нельзя постить дубли в течение 1 ч" in msg and "через <b>" in msg and "МСК" in msg, msg
    assert await db.scalar("SELECT COUNT(*) FROM ads") == 1
    # ядро тоже не пропустит (например, «продлить» сразу после публикации)
    try:
        await ads.publish_ad(bot, tg(UID), text="Ищу работу медиабайером", ad_type_row=resume)
        raise AssertionError("дубль опубликован")
    except ads.DuplicateError as exc:
        assert "Нельзя постить дубли" in str(exc)
    assert await tk.balance(UID) == 490, "за дубль не списано"
    # другой текст — можно; другой автор с тем же текстом — можно
    await ads.publish_ad(bot, tg(UID), text="Другое объявление", ad_type_row=resume)
    await tk.add(400002, 100, "test")
    await ads.publish_ad(bot, tg(400002), text="Ищу работу медиабайером", ad_type_row=resume)
    # окно прошло — можно; настройка 0 — проверка выключена
    await db.execute("UPDATE ads SET created_at = ? WHERE id = ?",
                     (db.iso(db.utcnow() - timedelta(hours=2)), first_id))
    assert await ads.find_duplicate(UID, "Ищу работу медиабайером") is None
    await db.set_setting("ad_dup_hours", 0)
    await db.execute("UPDATE ads SET created_at = datetime('now') WHERE id = ?", (first_id,))
    assert await ads.find_duplicate(UID, "Ищу работу медиабайером") is None
    await db.set_setting("ad_dup_hours", 1)
    assert ads.minutes_left_text(135) == "2 ч 15 мин" and ads.minutes_left_text(0) == "1 мин"
    await dp.feed_update(bot, cb("ads_cancel"))
    print("19 OK: дубль блокируется в мастере и в ядре, окно и выключение настройкой работают")

    # ================= 20. «Мои объявления» ================================================
    await dp.feed_update(bot, priv("📋 Мои объявления"))
    # список приходит новым сообщением с кнопками по объявлениям
    assert "Мои объявления" in bot.to(UID)[-1] and "(2)" in bot.to(UID)[-1], bot.to(UID)[-1]
    from app import keyboards
    kb = await keyboards.main_menu()
    labels = [b.text for row in kb.keyboard for b in row]
    assert "📋 Мои объявления" in labels and "🆘 Поддержка" in labels, labels

    # карточка активного объявления: продлить + закреп (закреп свободен)
    await dp.feed_update(bot, cb(f"my:ad:{first_id}"))
    text, markup = bot.edits[-1]
    assert f"#{first_id}" in text and "https://t.me/adschannel/" in text and "опубликовано" in text, text
    datas = [d for _, d in buttons(markup)]
    assert f"my:rep:{first_id}" in datas and f"my:pin:{first_id}:4" in datas and f"my:pin:{first_id}:8" in datas

    # докупить закреп: подтверждение → закреплено, списана цена закрепа
    before = await tk.balance(UID)
    await dp.feed_update(bot, cb(f"my:pin:{first_id}:4"))
    assert "Закрепить объявление" in bot.edits[-1][0] and "<b>15</b>" in bot.edits[-1][0]
    await dp.feed_update(bot, cb(f"my:pinok:{first_id}:4"))
    assert "закреплено до" in bot.to(UID)[-1] and await tk.balance(UID) == before - 15, bot.to(UID)[-1]
    ad = await db.fetchone("SELECT * FROM ads WHERE id = ?", (first_id,))
    assert ads.ad_pin_active(ad) and ad["cost_pin"] == 15 and ad["pin_hours"] == 4 and bot.pinned
    post_row = await sdb.conn().execute_fetchall(
        "SELECT pinned_until FROM posts WHERE source_message_id = ?", (ad["channel_message_id"],))
    assert post_row and post_row[0][0] == ad["pinned_until"], "закреп отмечен на сайте"
    # второй раз тому же — отказ; другому активному — отказ, потому что закреп занят
    await dp.feed_update(bot, cb(f"my:pinok:{first_id}:8"))
    assert "уже есть закреп" in bot.to(UID)[-1], bot.to(UID)[-1]
    second = await db.fetchone("SELECT * FROM ads WHERE user_id = ? AND id <> ?", (UID, first_id))
    await dp.feed_update(bot, cb(f"my:ad:{second['id']}"))
    assert not any(d.startswith("my:pin:") for _, d in buttons(bot.edits[-1][1])), "кнопок закрепа нет"
    await dp.feed_update(bot, cb(f"my:pinok:{second['id']}:4"))
    assert "Закреп сейчас занят" in bot.to(UID)[-1]
    print("20а OK: список, карточка, докупка закрепа, отказы")

    # продлить: сразу после публикации это дубль — отказ без списания
    before = await tk.balance(UID)
    await dp.feed_update(bot, cb(f"my:rep:{second['id']}"))
    assert "Опубликовать объявление" in bot.edits[-1][0] and "<b>10</b>" in bot.edits[-1][0]
    await dp.feed_update(bot, cb(f"my:repok:{second['id']}"))
    assert "Нельзя постить дубли" in bot.to(UID)[-1] and await tk.balance(UID) == before
    # прошёл час — продление публикует заново, старый пост снимается без возврата
    await db.execute("UPDATE ads SET created_at = ? WHERE id = ?",
                     (db.iso(db.utcnow() - timedelta(hours=2)), second["id"]))
    n_channel, n_deleted = len(bot.channel), len(bot.deleted)
    await dp.feed_update(bot, cb(f"my:repok:{second['id']}"))
    assert "Опубликовано" in bot.to(UID)[-1] and await tk.balance(UID) == before - 10, bot.to(UID)[-1]
    assert len(bot.channel) == n_channel + 1 and (CHANNEL, second["channel_message_id"]) in bot.deleted
    old = await db.fetchone("SELECT * FROM ads WHERE id = ?", (second["id"],))
    new = await db.fetchone("SELECT * FROM ads WHERE repost_of = ?", (second["id"],))
    assert old["status"] == "deleted" and old["delete_kind"] == "reposted" and not old["refunded"]
    assert new and new["text"] == second["text"] and new["ad_type_tag"] == second["ad_type_tag"]
    assert (await db.user_violations(UID))["deleted_total"] == 0, "продление — не нарушение"
    # продлить можно и давно удалённое (старое) объявление
    await ads.delete_ad(bot, new["id"], by_admin_id=999, comment="тест")
    await db.execute("UPDATE ads SET created_at = ? WHERE id = ?",
                     (db.iso(db.utcnow() - timedelta(hours=3)), new["id"]))
    res = await ads.repost_ad(bot, tg(UID), new["id"])
    assert res["ad_id"] > new["id"]
    # в списке статусы читаются
    await dp.feed_update(bot, cb("my:list:0"))
    titles = [t for t, _ in buttons(bot.edits[-1][1])]
    assert any("продлено" in t for t in titles) and any("закреплено" in t for t in titles), titles
    print("20б OK: продлить — дубль не раньше часа, потом новый пост, старый снят; старые тоже продлеваются")

    # ================= 18. поддержка =======================================================
    # кнопка в главном боте
    await dp.feed_update(bot, priv("🆘 Поддержка"))
    assert "поддерж" in bot.to(UID)[-1].lower()
    await db.set_setting("support_link", "")
    kb = await keyboards.main_menu()
    assert "🆘 Поддержка" not in [b.text for row in kb.keyboard for b in row], "без ссылки кнопки нет"
    await db.set_setting("support_link", "https://t.me/aff_bazaar_support_bot")

    # сам бот поддержки: отдельный диспетчер
    sdp = support.build_dispatcher()
    sbot = FakeBot()
    await sdp.feed_update(sbot, priv("/start", 500001))
    assert sbot.topics == ["Автор"] and sbot.group and "Telegram ID: <code>500001</code>" in sbot.group[0][1]
    assert "поддержка Aff Bazaar" in sbot.to(500001)[-1]
    ticket = await support.get_ticket(500001)
    assert ticket["topic_id"] == 1000
    # сообщение пользователя копируется в его топик; повторный /start топик не плодит
    await sdp.feed_update(sbot, priv("Не пришли коины", 500001))
    assert sbot.copied[-1][0] == SUP and sbot.copied[-1][3] == 1000, sbot.copied
    await sdp.feed_update(sbot, priv("/start", 500001))
    assert len(sbot.topics) == 1
    # без /start тоже работает: топик создаётся при первом сообщении
    await sdp.feed_update(sbot, priv("Здравствуйте", 500002))
    assert len(sbot.topics) == 2 and sbot.copied[-1][3] == 1001
    # ответ команды из топика уходит пользователю; команды и чужие топики — нет
    await sdp.feed_update(sbot, group_msg("Проверили, начислили", 1000))
    assert sbot.copied[-1][0] == 500001 and sbot.copied[-1][3] is None
    n = len(sbot.copied)
    await sdp.feed_update(sbot, group_msg("Ответ в никуда", 4242))
    assert len(sbot.copied) == n
    # топик удалили руками — создаётся новый и сообщение доходит
    sbot.copy_fail = TelegramBadRequest(method=None, message="Bad Request: message thread not found")
    await sdp.feed_update(sbot, priv("Ещё вопрос", 500001))
    assert len(sbot.topics) == 3 and (await support.get_ticket(500001))["topic_id"] == 1002
    assert sbot.copied[-1][3] == 1002
    # /ban в топике: пользователь больше не может писать, /unban возвращает
    await sdp.feed_update(sbot, group_msg("/ban", 1002))
    assert "заблокирован" in sbot.group[-1][1]
    n = len(sbot.copied)
    await sdp.feed_update(sbot, priv("Я тут", 500001))
    assert len(sbot.copied) == n and "заблокированы" in sbot.to(500001)[-1]
    await sdp.feed_update(sbot, group_msg("/unban", 1002))
    await sdp.feed_update(sbot, priv("Я тут", 500001))
    assert len(sbot.copied) == n + 1
    # без токена run() выходит сразу, не роняя процесс
    cfg.SUPPORT_BOT_TOKEN = ""
    await asyncio.wait_for(support.run(), 2)
    print("18 OK: топик на пользователя, ответы из топика, пересоздание топика, /ban и /unban, кнопка")

    # ================= 22 + 23. объединённые разделы ======================================
    cfg.CRYPTOPAY_API_KEY = "cp_test_key"
    kb = await keyboards.main_menu()
    labels = [b.text for row in kb.keyboard for b in row]
    assert "📣 Канал и сайт Aff Bazaar" in labels and "💰 Баланс / Купить коины" in labels, labels
    for old in ("📣 Канал Aff Bazaar", "🌐 Наш сайт", "💰 Баланс", "💎 Купить коины"):
        assert old not in labels, f"старая кнопка {old} осталась в меню"
    # два столбца в каждом ряду (AffBazaar-27), порядок из макета
    rows = [[b.text for b in row] for row in kb.keyboard]
    assert all(len(r) == 2 for r in rows), rows
    assert rows[0] == ["📣 Канал и сайт Aff Bazaar", "📋 Мои объявления"], rows[0]
    assert rows[1] == ["💰 Баланс / Купить коины", "📢 Создать объявление"], rows[1]
    assert rows[3] == ["📜 Правила", "🆘 Поддержка"], rows[3]
    # баланс: текст + пакеты коинов под ним
    await dp.feed_update(bot, priv("💰 Баланс / Купить коины"))
    assert "Баланс: <b>" in bot.to(UID)[-1] and "Пополнить баланс" in bot.to(UID)[-1], bot.to(UID)[-1]
    packs = buttons(bot.markups[-1])
    assert packs and all(d.startswith("cbuy:") for _, d in packs), packs
    # старые подписи из клавиатур до обновления работают так же
    await dp.feed_update(bot, priv("💰 Баланс"))
    assert buttons(bot.markups[-1]) == packs
    await dp.feed_update(bot, priv("💎 Купить коины"))
    assert buttons(bot.markups[-1]) == packs
    # канал и сайт: одно сообщение с двумя ссылками
    await dp.feed_update(bot, priv("📣 Канал и сайт Aff Bazaar"))
    text = bot.to(UID)[-1]
    assert "https://t.me/adschannel" in text and "Сайт:" in text, text
    urls = [b.url for row in bot.markups[-1].inline_keyboard for b in row]
    assert urls == ["https://t.me/adschannel", f"{cfg.PUBLIC_URL}/"], urls
    await dp.feed_update(bot, priv("🌐 Наш сайт"))
    assert cfg.PUBLIC_URL in bot.to(UID)[-1]
    print("22/23 OK: пакеты под балансом, объединённые кнопки, старые подписи живы")

    await db.close(); await sdb.close()
    print("TRACKER2 OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:
        import traceback; traceback.print_exc()
        os._exit(1)
