"""Режим bot_only: публикация через бота, рефералы, премодерация."""
import asyncio, os, sys, pathlib, itertools, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
import app.handlers.user as uh, app.handlers.admin as ah, app.services as sv
uh.ADMINS = {999}; ah.ADMINS = {999}

from aiogram.types import Message, Chat, User
CHAT_ID, CHANNEL_ID, A, B = -100111, -100222, 700001, 700002
_ids = itertools.count(5000)

class FakeBot:
    id = 42
    def __init__(self):
        self.subscribed = True; self.sent = []; self.copied = []
    async def get_me(self): return User(id=42, is_bot=True, first_name="Bot", username="testbot")
    async def get_chat_member(self, chat_id, user_id):
        class M: status = "member" if self.subscribed else "left"
        return M()
    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="supergroup"))
    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        self.sent.append((chat_id, caption or ""))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="supergroup"))
    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.copied.append((chat_id, message_id))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="supergroup"))
    async def delete_message(self, *a, **kw): return True
    def __getattr__(self, name):
        if name.startswith("send_"):          # send_video, send_document, ...
            return self.send_photo
        raise AttributeError(name)

def dm(text, uid, username="u", media=None):
    kw = {}
    if media == "photo":
        kw["photo"] = [{"file_id": "PH", "file_unique_id": "u", "width": 1, "height": 1}]
        kw["caption"] = text
    else:
        kw["text"] = text
    return Message(message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
                   from_user=User(id=uid, is_bot=False, first_name="U", username=username), **kw)

# перехватываем ответы боту в личку
answers = []
async def fake_answer(self, text, **kw): answers.append(text); return None
Message.answer = fake_answer

async def main():
    await db.init(); await sdb.init()
    await db.upsert_chat(CHAT_ID, "Чат")
    await db.execute("UPDATE chats SET post_mode='bot_only' WHERE chat_id=?", (CHAT_ID,))
    await db.set_required_channels(CHAT_ID, [{"channel_id": CHANNEL_ID, "title": "К", "username": "ch"}])
    await db.set_setting("message_cost", 5)
    bot = FakeBot()

    await db.upsert_user(A, "alice", "Alice")
    await db.upsert_user(B, "bob", "Bob")
    await db.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (A, B))
    chat = await db.get_chat(CHAT_ID)

    # не подписан -> приглашение, публикации нет
    bot.subscribed = False
    await uh.publish(bot, dm("хочу написать", B), chat)
    assert "подписаться на канал" in answers[-1], answers[-1]
    assert not bot.sent, bot.sent
    print("bot_only / нет подписки OK")

    # подписан -> бонус 30, реферер +10, публикация, списание 5
    bot.subscribed = True
    msg = dm("моё первое сообщение", B)
    await uh.publish(bot, msg, chat)
    assert bot.sent and "моё первое сообщение" in bot.sent[-1][1], bot.sent
    assert bot.sent[-1][0] == CHAT_ID
    assert await tk.balance(B) == 30 - 5, await tk.balance(B)
    assert await tk.balance(A) == 10, await tk.balance(A)
    rows, total = await sdb.query_posts(q="первое сообщение")
    assert total == 1, total
    row = await db.fetchone("SELECT * FROM chat_messages WHERE user_id=?", (B,))
    assert row["posted_via_bot"] == 1 and row["cost"] == 5 and row["status"] == "published"
    print("bot_only / публикация OK, баланс B =", await tk.balance(B), "| A =", await tk.balance(A))

    # повторная активация бонус не даёт (античит)
    before = await tk.balance(B)
    await uh.publish(bot, dm("второе", B), chat)
    assert await tk.balance(B) == before - 5, (before, await tk.balance(B))
    assert await tk.balance(A) == 10, "реферальный бонус только один раз"
    print("античит OK: повторный бонус не начислен")

    # нет токенов -> не публикуем
    await db.execute("UPDATE users SET tokens=0 WHERE user_id=?", (B,))
    n = len(bot.sent)
    await uh.publish(bot, dm("без токенов", B), chat)
    assert len(bot.sent) == n and "коин" in answers[-1].lower(), answers[-1]
    print("bot_only / нет токенов OK")

    # премодерации больше нет (п.9 ТЗ): даже со старым флагом в базе публикуем сразу
    await db.execute("UPDATE users SET tokens=100 WHERE user_id=?", (B,))
    await db.execute("UPDATE chats SET premoderate=1 WHERE chat_id=?", (CHAT_ID,))
    chat = await db.get_chat(CHAT_ID)
    n = len(bot.sent)
    await uh.publish(bot, dm("без модерации", B), chat)
    assert any(x[0] == CHAT_ID and "без модерации" in x[1] for x in bot.sent[n:]), bot.sent[n:]
    assert await db.scalar("SELECT COUNT(*) FROM chat_messages WHERE status='pending'") == 0
    print("премодерация отключена OK: сообщение уходит в чат сразу")

    # репост в канал
    await db.execute("UPDATE chats SET repost_channel_id=? WHERE chat_id=?", (-100333, CHAT_ID))
    mid = row["message_id"]
    res = await sv.repost_to_channel(bot, CHAT_ID, mid)
    assert res and bot.copied[-1][0] == -100333, bot.copied
    posts, _ = await sdb.query_posts(only_reposted=True)
    assert len(posts) == 1 and posts[0]["channel_id"] == -100333, posts
    print("репост в канал OK")
    print("PUBLISH OK")

async def runner():
    try: await main()
    finally:
        await db.close(); await sdb.close()
asyncio.run(runner())
