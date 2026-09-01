"""Прогон сообщений через реальный Dispatcher с фейковым Bot."""
import asyncio, os, sys, pathlib, itertools, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp / "bot.db"; cfg.SITE_DB = tmp / "site.db"
cfg.LOG_DIR = tmp / "logs"; cfg.RESTRICTED_LOG_DIR = tmp / "logs-restricted"
cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update, Message, Chat, User
from app.handlers import admin, chat_guard, payments, user as user_h

CHAT_ID, CHANNEL_ID, UID = -1001234567890, -1009876543210, 555001

class FakeBot:
    id = 42
    def __init__(self):
        self.subscribed = True
        self.sent, self.deleted, self.restricted = [], [], []
        self.session = None
    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")
    async def get_chat_administrators(self, chat_id):
        class A: user = User(id=999, is_bot=False, first_name="Admin")
        return [A()]
    async def get_chat_member(self, chat_id, user_id):
        class M: status = "member" if self.subscribed else "left"
        return M()
    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return Message(message_id=90000 + len(self.sent), date=0,
                       chat=Chat(id=chat_id, type="supergroup"),
                       from_user=User(id=self.id, is_bot=True, first_name="Bot"))
    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id)); return True
    async def restrict_chat_member(self, chat_id, user_id, **kw):
        self.restricted.append(user_id); return True
    async def copy_message(self, **kw):
        class R: message_id = 77777
        return R()

_ids = itertools.count(1000)
def group_msg(text, uid=UID, username="tester"):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0,
        chat=Chat(id=CHAT_ID, type="supergroup", title="Тестовый чат"),
        from_user=User(id=uid, is_bot=False, first_name="Тест", username=username),
        text=text))

async def main():
    await db.init(); await sdb.init()
    await db.upsert_chat(CHAT_ID, "Тестовый чат")
    await db.set_required_channels(CHAT_ID, [{"channel_id": CHANNEL_ID, "title": "Канал",
                                              "username": "testchannel"}])
    await db.set_setting("check_limit", 3)
    await db.set_setting("msg_ttl", 0)          # без фоновых таймеров в тесте
    await db.set_setting("message_cost", 10)

    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, payments.router, user_h.router, chat_guard.router):
        dp.include_router(r)
    bot = FakeBot()

    # --- 1б) не подписан: сообщение удаляется, шлётся приглашение --------
    bot.subscribed = False
    upd = group_msg("привет")
    await dp.feed_update(bot, upd)
    assert bot.deleted and bot.deleted[-1][1] == upd.message.message_id, bot.deleted
    assert "подписаться на канал" in bot.sent[-1][1], bot.sent[-1]
    assert "@testchannel" in bot.sent[-1][1]
    st = await db.get_state(UID, CHAT_ID); assert st["fail_streak"] == 1, dict(st)
    print("1b OK:", bot.sent[-1][1].replace("\n", " / "))

    # повтор: старое сообщение бота удаляется, новое отправляется
    before_deleted = len(bot.deleted)
    await dp.feed_update(bot, group_msg("ещё раз"))
    st = await db.get_state(UID, CHAT_ID); assert st["fail_streak"] == 2, dict(st)
    assert len(bot.deleted) - before_deleted == 2, "удалено и сообщение юзера, и старая подсказка"

    # --- лимит проверок -> ограничение ----------------------------------
    await dp.feed_update(bot, group_msg("третий раз"))
    assert UID in bot.restricted, bot.restricted
    assert "заблокированы" in bot.sent[-1][1].lower(), bot.sent[-1]
    assert await db.is_restricted(UID, CHAT_ID) is not None
    rlog = list((cfg.RESTRICTED_LOG_DIR / str(CHAT_ID)).glob("restricted_*.log"))
    assert rlog and "превышен лимит" in rlog[0].read_text()
    print("limit OK:", rlog[0].read_text().strip())

    # заблокированному сразу удаляем и показываем текст блокировки
    n = len(bot.sent)
    await dp.feed_update(bot, group_msg("я вернулся"))
    assert len(bot.sent) == n + 1 and "заблокированы" in bot.sent[-1][1].lower()

    # --- 1а) подписан: бонус, списание, сообщение остаётся ---------------
    await db.update_state(UID, CHAT_ID, restricted_until=None, fail_streak=0)
    bot.subscribed = True
    deleted_before = len(bot.deleted)
    upd = group_msg("полезное сообщение")
    await dp.feed_update(bot, upd)
    assert len(bot.deleted) == deleted_before, "сообщение подписчика не удаляем"
    user = await db.get_user(UID)
    assert user["activated"] == 1 and user["tokens"] == 30 - 10, dict(user)
    row = await db.fetchone("SELECT * FROM chat_messages WHERE message_id = ?",
                            (upd.message.message_id,))
    assert row and row["cost"] == 10 and row["status"] == "published"
    posts, total = await sdb.query_posts(q="полезное")
    assert total == 1, (total, posts)
    print("1a OK: баланс", user["tokens"], "| в ленте сайта:", posts[0]["text"])

    # --- нет токенов -> удаляем и предлагаем пополнить -------------------
    await db.execute("UPDATE users SET tokens = 0 WHERE user_id = ?", (UID,))
    upd = group_msg("а теперь без токенов")
    await dp.feed_update(bot, upd)
    assert upd.message.message_id in [d[1] for d in bot.deleted], bot.deleted[-3:]
    assert "коин" in bot.sent[-1][1].lower(), bot.sent[-1]
    print("no-tokens OK:", bot.sent[-1][1].replace("\n", " / "))

    # --- админа чата не трогаем -----------------------------------------
    bot.subscribed = False
    d, s = len(bot.deleted), len(bot.sent)
    await dp.feed_update(bot, group_msg("я админ", uid=999, username="admin"))
    assert len(bot.deleted) == d and len(bot.sent) == s, "админ проходит без проверок"
    print("admin-bypass OK")

    # --- возврат токенов при удалении админом ---------------------------
    import app.services as sv
    await db.execute("UPDATE users SET tokens = 0 WHERE user_id = ?", (UID,))
    refunded = await sv.refund_message(bot, CHAT_ID, row["message_id"], "admin")
    assert refunded == 10 and await tk.balance(UID) == 10, refunded
    posts, total = await sdb.query_posts(q="полезное")
    assert total == 0, "удалённое сообщение уходит из ленты сайта"
    print("refund OK: вернули", refunded)

    # --- лог действий ----------------------------------------------------
    logf = list((cfg.LOG_DIR / str(CHAT_ID)).glob("*.log"))[0]
    lines = [l for l in logf.read_text().splitlines() if l]
    assert all(l.count("|") == 3 for l in lines), lines
    print("action log OK, строк:", len(lines))
    print(lines[0]); print(lines[-1])

    print("GATE OK")

async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
