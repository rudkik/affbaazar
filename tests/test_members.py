"""Учёт подписчиков канала: миграция схемы, chat_member-события, /start, user_card."""
import asyncio, os, sys, pathlib, itertools, sqlite3, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp / "bot.db"; cfg.SITE_DB = tmp / "site.db"
cfg.LOG_DIR = tmp / "logs"; cfg.RESTRICTED_LOG_DIR = tmp / "logs-restricted"
cfg.ADMINS = {999}; cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"
import app.db as db, app.site_db as sdb, app.action_log as al
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
import app.handlers.user as uh
uh.ADMINS = {999}

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember,
                           ChatMemberOwner, ChatMemberRestricted, ChatMemberUpdated, Message,
                           Update, User)
from app.handlers import admin, chat_guard, members, payments, user as user_h

MAIN_CH, OTHER_CH = -1001111111111, -1002222222222
A, B, REF = 700001, 700002, 700003
_ids = itertools.count(5000)


class FakeBot:
    """Минимальный бот: /start печатает приветствие, get_chat отдаёт био."""
    id = 42

    def __init__(self):
        self.sent = []
        self.bios = {}

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def get_chat(self, chat_id):
        return Chat(id=chat_id, type="private", bio=self.bios.get(chat_id))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="private"))

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text)
        raise AssertionError("не смоделирован метод " + name)


def tg_user(uid, first="Иван", last="Петров", username="ivan"):
    return User(id=uid, is_bot=False, first_name=first, last_name=last, username=username)


def member_update(chat_id, user, old_status, new_status, chat_type="channel",
                  is_member=None):
    """Собирает апдейт chat_member (вход/выход в канале)."""
    def make(status):
        u = user
        if status == "member":
            return ChatMemberMember(status="member", user=u)
        if status == "administrator":
            return ChatMemberAdministrator(
                status="administrator", user=u, can_be_edited=False, is_anonymous=False,
                can_manage_chat=True, can_delete_messages=False, can_manage_video_chats=False,
                can_restrict_members=False, can_promote_members=False, can_change_info=False,
                can_invite_users=False, can_post_stories=False, can_edit_stories=False,
                can_delete_stories=False, can_send_welcome_messages=False)
        if status == "creator":
            return ChatMemberOwner(status="creator", user=u, is_anonymous=False)
        if status == "restricted":
            return ChatMemberRestricted(
                status="restricted", user=u, is_member=bool(is_member), until_date=0,
                can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                can_send_voice_notes=False, can_send_polls=False,
                can_send_other_messages=False, can_add_web_page_previews=False,
                can_change_info=False, can_invite_users=False, can_pin_messages=False,
                can_manage_topics=False, can_react_to_messages=False, can_edit_tag=False)
        return ChatMemberLeft(status="left", user=u)

    return Update(update_id=next(_ids), chat_member=ChatMemberUpdated(
        chat=Chat(id=chat_id, type=chat_type, title="Канал"),
        from_user=user, date=0,
        old_chat_member=make(old_status), new_chat_member=make(new_status)))


def priv(text, user):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=user.id, type="private"),
        from_user=user, text=text))


async def sub_row(user_id, channel_id):
    row = await db.channel_sub(user_id, channel_id)
    return dict(row) if row else None


# ---------------------------------------------------------------- 1. миграция
async def test_migration():
    """Старая база без новых колонок должна доехать до новой схемы через ALTER."""
    old_db = tmp / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript("""
        CREATE TABLE users (
            user_id           INTEGER PRIMARY KEY,
            username          TEXT,
            full_name         TEXT,
            tokens            INTEGER DEFAULT 0,
            activated         INTEGER DEFAULT 0,
            activated_at      TEXT,
            referrer_id       INTEGER,
            referral_rewarded INTEGER DEFAULT 0,
            messages_sent     INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now')),
            updated_at        TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO users(user_id, username, full_name, tokens, activated)
        VALUES (555, 'old', 'Старый Юзер', 77, 1);
    """)
    con.commit(); con.close()

    db.MAIN_DB = old_db
    await db.init()
    cols = {r[1] for r in await db.fetchall("PRAGMA table_info(users)")}
    for name in db.USERS_ADDED_COLUMNS:
        assert name in cols, (name, sorted(cols))
    row = await db.get_user(555)
    assert row["tokens"] == 77 and row["activated"] == 1, dict(row)
    assert row["started"] == 0 and row["started_at"] is None, dict(row)
    assert await db.fetchone("SELECT 1 FROM channel_subs LIMIT 1") is None, "таблица создана и пуста"
    # повторный init на уже мигрированной базе не падает
    await db.close(); await db.init(); await db.close()
    db.MAIN_DB = cfg.MAIN_DB
    print("миграция OK: добавлено колонок", len(db.USERS_ADDED_COLUMNS), "| данные целы")


# ---------------------------------------------------------------- 2. основной канал
async def test_main_channel(dp, bot):
    user = tg_user(A)
    await dp.feed_update(bot, member_update(MAIN_CH, user, "left", "member"))

    row = await db.get_user(A)
    assert row["subscribed"] == 1, dict(row)
    first_ts = row["first_subscribed_at"]
    assert isinstance(first_ts, int) and first_ts > 0, first_ts
    assert row["first_name"] == "Иван" and row["last_name"] == "Петров"
    assert row["username"] == "ivan" and row["full_name"] == "Иван Петров"
    cs = await sub_row(A, MAIN_CH)
    assert cs["subscribed"] == 1 and cs["first_subscribed_at"] == first_ts, cs
    print("вступление в основной канал OK: subscribed=1, first_subscribed_at =", first_ts)

    # выход: флаг снимается, дата первой подписки остаётся
    await dp.feed_update(bot, member_update(MAIN_CH, user, "member", "left"))
    row = await db.get_user(A)
    assert row["subscribed"] == 0, dict(row)
    assert row["first_subscribed_at"] == first_ts, "дата первой подписки не сбрасывается"
    cs = await sub_row(A, MAIN_CH)
    assert cs["subscribed"] == 0 and cs["first_subscribed_at"] == first_ts, cs
    print("выход OK: subscribed=0, first_subscribed_at сохранён")

    # повторный вход: дата не перезаписывается
    await asyncio.sleep(1.05)          # чтобы новый unix-таймстамп точно отличался
    await dp.feed_update(bot, member_update(MAIN_CH, user, "left", "administrator"))
    row = await db.get_user(A)
    assert row["subscribed"] == 1 and row["first_subscribed_at"] == first_ts, dict(row)
    cs = await sub_row(A, MAIN_CH)
    assert cs["first_subscribed_at"] == first_ts and cs["updated_at"] > first_ts, cs
    print("повторный вход OK: first_subscribed_at =", first_ts, "updated_at =", cs["updated_at"])

    # restricted с is_member=True — это подписка, без него — нет
    await dp.feed_update(bot, member_update(MAIN_CH, user, "member", "restricted",
                                            is_member=True))
    assert (await db.get_user(A))["subscribed"] == 1, "restricted+is_member = подписан"
    await dp.feed_update(bot, member_update(MAIN_CH, user, "restricted", "restricted",
                                            is_member=False))
    assert (await db.get_user(A))["subscribed"] == 0, "restricted без is_member = не подписан"
    print("статусы restricted OK")

    # creator тоже считается подписчиком
    await dp.feed_update(bot, member_update(MAIN_CH, user, "left", "creator"))
    assert (await db.get_user(A))["subscribed"] == 1
    print("creator OK")


# ---------------------------------------------------------------- 3. чужой канал
async def test_other_channel(dp, bot):
    user = tg_user(B, first="Мария", last="Сидорова", username="masha")
    await dp.feed_update(bot, member_update(OTHER_CH, user, "left", "member"))

    row = await db.get_user(B)
    assert row["subscribed"] == 0 and row["first_subscribed_at"] is None, dict(row)
    assert row["first_name"] == "Мария", dict(row)
    cs = await sub_row(B, OTHER_CH)
    assert cs and cs["subscribed"] == 1 and cs["first_subscribed_at"], cs
    assert await sub_row(B, MAIN_CH) is None, "в основной канал ничего не пишем"
    print("не основной канал OK: только channel_subs, users.subscribed остался 0")

    # бота в канале не учитываем
    bot_user = User(id=999999, is_bot=True, first_name="SomeBot", username="somebot")
    await dp.feed_update(bot, member_update(OTHER_CH, bot_user, "left", "member"))
    assert await db.get_user(999999) is None, "боты в базу не попадают"
    print("боты игнорируются OK")


# ---------------------------------------------------------------- 4. /start
async def test_start(dp, bot):
    user = tg_user(A)
    bot.bios[A] = "Медиабайер, Латам"
    await dp.feed_update(bot, priv("/start", user))

    row = await db.get_user(A)
    assert row["started"] == 1 and isinstance(row["started_at"], int), dict(row)
    started_at = row["started_at"]
    assert row["bio"] == "Медиабайер, Латам", dict(row)
    assert row["first_name"] == "Иван" and row["last_name"] == "Петров", dict(row)
    assert isinstance(row["last_seen_at"], int) and row["last_seen_at"] > 0
    print("/start OK: started_at =", started_at, "| bio =", row["bio"])

    # повторный /start дату не двигает, но обновляет профиль
    await asyncio.sleep(1.05)
    bot.bios[A] = "Новое био"
    await dp.feed_update(bot, priv("/start", tg_user(A, first="Иоанн", last="П.",
                                                     username="ivan2")))
    row = await db.get_user(A)
    assert row["started_at"] == started_at, (row["started_at"], started_at)
    assert row["bio"] == "Новое био" and row["first_name"] == "Иоанн", dict(row)
    assert row["last_seen_at"] > started_at, dict(row)
    print("повторный /start OK: started_at не изменился, профиль обновлён")


# ---------------------------------------------------------------- 5. user_card
async def test_card(dp, bot):
    card = await db.user_card(A)
    assert card["user_id"] == A and card["username"] == "ivan2"
    assert card["referrer_id"] is None and card["referrer_username"] is None
    assert card["referrer_db_id"] is None
    assert card["subscribed"] == 1 and card["first_subscribed_at"]
    assert any(c["channel_id"] == MAIN_CH for c in card["channels"]), card["channels"]
    print("user_card без реферала OK: реферер =", card["referrer_id"], card["referrer_username"])

    await db.upsert_user(REF, "referrer_nick", "Реферер")
    await db.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (REF, A))
    card = await db.user_card(A)
    assert card["referrer_id"] == REF, card
    assert card["referrer_db_id"] == REF, card
    assert card["referrer_username"] == "referrer_nick", card
    print("user_card с рефералом OK:", card["referrer_id"], "/", card["referrer_username"])

    assert await db.user_card(123456789) is None, "несуществующий юзер -> None"


# ---------------------------------------------------------------- 6. ленивая синхронизация
async def test_lazy_sync():
    """Живая проверка подписки через get_chat_member тоже пишется в базу."""
    import app.subscription as sub

    class SubBot:
        def __init__(self, ok): self.ok = ok
        async def get_chat_member(self, chat_id, user_id):
            class M: status = "member" if self.ok else "left"
            return M()

    await db.set_required_channels(sub.REQUIRED_GLOBAL, [
        {"channel_id": MAIN_CH, "title": "Основной", "username": "main"},
        {"channel_id": OTHER_CH, "title": "Второй", "username": "second"}])

    missing = await sub.missing_for_ads(SubBot(True), B)
    assert missing == [], missing
    assert (await db.get_user(B))["subscribed"] == 1, "основной канал попал в users"
    assert (await sub_row(B, OTHER_CH))["subscribed"] == 1
    print("ленивая синхронизация OK: подписка записана без chat_member-события")

    missing = await sub.missing_for_ads(SubBot(False), B)
    assert len(missing) == 2, missing
    row = await db.get_user(B)
    assert row["subscribed"] == 0 and row["first_subscribed_at"], dict(row)
    print("отписка через проверку OK: subscribed=0, first_subscribed_at сохранён")
    await db.set_required_channels(sub.REQUIRED_GLOBAL, [])


# ---------------------------------------------------------------- 7. веб-админка
async def test_web_api():
    """Список пользователей и карточка отдают новые поля."""
    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://t") as c:
        assert (await c.get("/admin/api/users")).status_code == 401, "без входа закрыто"
        r = await c.post("/admin/login", data={"password": "pass"}, follow_redirects=False)
        assert r.status_code in (200, 303, 307), r.status_code

        r = await c.get("/admin/api/users", params={"q": "ivan"})
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        me = [u for u in items if u["user_id"] == A]
        assert me, [u["user_id"] for u in items]
        me = me[0]
        for field in ("first_name", "last_name", "bio", "started", "started_at",
                      "subscribed", "first_subscribed_at", "last_seen_at",
                      "referrer_username", "referrer_db_id", "invited"):
            assert field in me, (field, sorted(me))
        assert me["referrer_username"] == "referrer_nick", me
        print("GET /admin/api/users OK: новые поля и реферер в выдаче")

        r = await c.get(f"/admin/api/users/{A}/card")
        card = r.json()
        assert card["user_id"] == A and card["started"] == 1, card
        assert any(ch["channel_id"] == MAIN_CH for ch in card["channels"]), card["channels"]
        assert (await c.get("/admin/api/users/424242/card")).status_code == 404
        print("GET /admin/api/users/{id}/card OK: каналов в карточке", len(card["channels"]))


async def main():
    await test_migration()

    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", MAIN_CH)
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, payments.router, user_h.router, chat_guard.router, members.router):
        dp.include_router(r)
    assert "chat_member" in dp.resolve_used_update_types(), dp.resolve_used_update_types()
    print("dp.resolve_used_update_types() содержит chat_member OK")

    bot = FakeBot()
    await test_main_channel(dp, bot)
    await test_other_channel(dp, bot)
    await test_start(dp, bot)
    await test_card(dp, bot)
    await test_lazy_sync()
    await test_web_api()
    print("MEMBERS OK")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
