"""Проверки текста объявления в боте, шаг соцсетей для «Интро», учёт удалений и рефералы."""
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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, Chat, Message, MessageEntity, Update, User)
from app import keyboards
from app.handlers import admin, chat_guard, moderation, payments, post, user as user_h

CHANNEL, UID, ADMIN = -1007778889990, 400001, 999
_ids = itertools.count(50000)


class FakeBot:
    id = 42

    def __init__(self):
        self.dm, self.channel, self.alerts, self.deleted = [], [], [], []

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def get_chat(self, chat_id):
        return Chat(id=chat_id, type="channel", title="Объявления", username="adschannel")

    async def get_chat_member(self, chat_id, user_id):
        class M:
            status = "administrator" if user_id == ADMIN else "member"
        return M()

    async def send_message(self, chat_id, text, **kw):
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, text, mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, caption or "", mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def pin_chat_message(self, chat_id, message_id, **kw): return True
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

    def last_dm(self, n=1):
        return [d[1] for d in self.dm[-n:]]


def priv(text, uid=UID, username="adman", photo=False, entities=None):
    kw = {"caption": text, "caption_entities": entities,
          "photo": [{"file_id": "PH1", "file_unique_id": "u",
                     "width": 100, "height": 100}]} if photo else {
        "text": text, "entities": entities}
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


class U:
    """Минимальный «пользователь» для прямых вызовов ads.publish_ad."""
    def __init__(self, uid, username, full_name):
        self.id, self.username, self.full_name = uid, username, full_name


OLD_UID = 400099        # автор «старого» объявления, созданного до миграции


def seed_old_dbs():
    """Базы, какими они лежат на проде: без ads.socials/delete_kind и posts.socials."""
    import sqlite3
    con = sqlite3.connect(cfg.MAIN_DB)
    con.execute("""CREATE TABLE ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER,
        channel_message_id INTEGER, ad_type_id INTEGER, ad_type_name TEXT, ad_type_tag TEXT,
        vertical_id INTEGER, vertical_name TEXT, vertical_tag TEXT, text TEXT,
        media_type TEXT, media_file_id TEXT, cost_base INTEGER DEFAULT 0,
        cost_image INTEGER DEFAULT 0, cost_pin INTEGER DEFAULT 0, cost_total INTEGER DEFAULT 0,
        pin_hours INTEGER DEFAULT 0, pinned_until TEXT, unpinned INTEGER DEFAULT 0,
        status TEXT DEFAULT 'published', deleted_by INTEGER, delete_comment TEXT,
        deleted_at TEXT, refunded INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    con.execute("INSERT INTO ads(user_id, text, status, cost_total, deleted_by, deleted_at) "
                "VALUES (?, ?, 'deleted', 10, 999, datetime('now'))",
                (OLD_UID, "Старое объявление, удалено до миграции"))
    con.commit(); con.close()
    # posts — как на проде сейчас: рубрики уже есть (на них висят индексы из SCHEMA),
    # а socials и search_blob ещё нет — их и должна догнать миграция.
    con = sqlite3.connect(cfg.SITE_DB)
    con.execute("""CREATE TABLE posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_chat_id INTEGER NOT NULL,
        source_chat_title TEXT, source_message_id INTEGER, channel_id INTEGER,
        channel_message_id INTEGER, author_id INTEGER, author_username TEXT,
        author_name TEXT, text TEXT, media_type TEXT, media_file_id TEXT,
        ad_type_name TEXT, ad_type_tag TEXT, vertical_name TEXT, vertical_tag TEXT,
        pinned_until TEXT, is_reposted INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(source_chat_id, source_message_id))""")
    con.commit(); con.close()


async def check_migration():
    ads_cols = {r[1] for r in await db.fetchall("PRAGMA table_info(ads)")}
    assert {"socials", "delete_kind"} <= ads_cols, ads_cols
    rows = await sdb.conn().execute_fetchall("PRAGMA table_info(posts)")
    posts_cols = {r[1] for r in rows}
    assert {"socials", "search_blob"} <= posts_cols, posts_cols
    old = await db.fetchone("SELECT * FROM ads WHERE user_id = ?", (OLD_UID,))
    assert old and old["text"].startswith("Старое"), "старые данные целы"
    assert old["delete_kind"] is None and old["socials"] is None, dict(old)
    print("миграция OK: ads.socials/delete_kind и posts.socials добавлены, данные целы")


async def main():
    seed_old_dbs()
    await db.init(); await sdb.init()
    await check_migration()
    await db.upsert_user(OLD_UID, "olduser", "Старый Автор")
    # удаление без delete_kind (до миграции) считаем нарушением модератора
    assert await db.user_violations(OLD_UID) == {"deleted_total": 1,
                                                 "deleted_by_moderator": 1}
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.set_setting("ad_channel_title", "Объявления")
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    for r in (admin.router, moderation.router, post.router, payments.router,
              user_h.router, chat_guard.router):
        dp.include_router(r)
    bot = FakeBot()
    await db.upsert_user(UID, "adman", "Автор Объявлений")
    await db.accept_rules(UID)
    await tk.add(UID, 500, "test")

    state = FSMContext(storage=storage,
                       key=StorageKey(bot_id=bot.id, chat_id=UID, user_id=UID))

    types = {r["name"]: r for r in await db.ad_types()}
    intro = types["Интро/Знакомства"]
    resume = types["Резюме"]

    # ================================================================= А. текст
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{resume['id']}"))
    assert await state.get_state() == post.Post.text.state, await state.get_state()

    # ссылка в тексте -> отказ, остаёмся на шаге текста
    await dp.feed_update(bot, priv("Ищу работу, пиши https://t.me/mynick"))
    assert "нельзя использовать ссылки" in bot.last_dm()[0], bot.last_dm()
    assert await state.get_state() == post.Post.text.state, "состояние не должно уходить"

    # голый домен — тоже отказ
    await dp.feed_update(bot, priv("Портфолио на mysite.ru, звоните"))
    assert "нельзя использовать ссылки" in bot.last_dm()[0], bot.last_dm()
    assert "mysite.ru" in bot.last_dm()[0], bot.last_dm()

    # форматирование (bold) — отказ
    await dp.feed_update(bot, priv("Опытный байер, ищу команду",
                                   entities=[MessageEntity(type="bold", offset=0, length=8)]))
    assert "жирный шрифт" in bot.last_dm()[0], bot.last_dm()
    assert await state.get_state() == post.Post.text.state

    # ссылка в подписи к фото — тоже ловится
    await dp.feed_update(bot, priv("Резюме тут example.com", photo=True))
    assert "нельзя использовать ссылки" in bot.last_dm()[0], bot.last_dm()
    assert await state.get_state() == post.Post.text.state
    print("проверка текста OK: ссылка, домен, bold и подпись к фото отклонены")

    # чистый текст проходит, и шага соцсетей у «Резюме» нет
    await dp.feed_update(bot, priv("Байер с опытом 3.5 года, гемблинг и т.д."))
    assert "картинк" in bot.last_dm()[0].lower(), bot.last_dm()
    assert await state.get_state() == post.Post.image_choice.state, await state.get_state()
    print("чистый текст OK: пропущен, для «Резюме» шага соцсетей нет")
    await dp.feed_update(bot, cb("ads_cancel"))

    # ============================================================ Б. соцсети
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{intro['id']}"))
    await dp.feed_update(bot, priv("Пара слов о себе: медиабайер, 5 лет в нутре"))
    prompt = bot.last_dm()[0]
    assert await state.get_state() == post.Post.socials.state, await state.get_state()
    assert "соцсети" in prompt.lower(), prompt
    assert "<b>Нельзя указывать корпоративный сайт или канал!" in prompt, prompt
    print("шаг соцсетей OK: появился у «Интро», предупреждение жирным")

    # bold в соцсетях -> отказ, остаёмся на шаге
    await dp.feed_update(bot, priv("https://vk.com/id1",
                                   entities=[MessageEntity(type="bold", offset=0, length=5)]))
    assert "жирный шрифт" in bot.last_dm()[0], bot.last_dm()
    assert await state.get_state() == post.Post.socials.state

    # мусор -> отказ
    await dp.feed_update(bot, priv("расскажу о себе при встрече"))
    assert "не похоже на адрес" in bot.last_dm()[0], bot.last_dm()
    assert await state.get_state() == post.Post.socials.state

    # ссылки принимаются
    await dp.feed_update(bot, priv("https://vk.com/id1\n@mynick\nt.me/myblog"))
    assert await state.get_state() == post.Post.image_choice.state, await state.get_state()
    print("соцсети OK: bold и мусор отклонены, ссылки приняты")

    await dp.feed_update(bot, cb("ads_img_no"))
    await dp.feed_update(bot, cb("ads_pin:0"))
    preview = " ".join(bot.last_dm(2))
    assert "🔗 Соцсети:" in preview and "t.me/myblog" in preview, preview
    await dp.feed_update(bot, cb("ads_publish"))

    channel_text = bot.channel[-1][1]
    assert "🔗 Соцсети:" in channel_text, channel_text
    assert "https://vk.com/id1" in channel_text and "@mynick" in channel_text, channel_text
    # блок соцсетей идёт после основного текста и перед подписью автора
    assert channel_text.index("медиабайер") < channel_text.index("🔗 Соцсети:") \
        < channel_text.index("Автор: @adman"), channel_text
    intro_ad = await db.fetchone("SELECT * FROM ads ORDER BY id DESC LIMIT 1")
    assert intro_ad["socials"] == "https://vk.com/id1\n@mynick\nt.me/myblog", intro_ad["socials"]
    site_row = await sdb.conn().execute_fetchall(
        "SELECT socials FROM posts WHERE channel_message_id = ?",
        (intro_ad["channel_message_id"],))
    assert site_row and site_row[0][0] == intro_ad["socials"], site_row
    print("публикация с соцсетями OK: блок в канале, ads.socials и posts.socials заполнены")

    # «Пропустить» -> соцсетей нет
    await dp.feed_update(bot, priv("/post"))
    await dp.feed_update(bot, cb(f"adtype:{intro['id']}"))
    await dp.feed_update(bot, priv("Второе интро, без соцсетей"))
    await dp.feed_update(bot, cb("ads_soc_skip"))
    assert await state.get_state() == post.Post.image_choice.state, await state.get_state()
    await dp.feed_update(bot, cb("ads_img_no"))
    await dp.feed_update(bot, cb("ads_pin:0"))
    await dp.feed_update(bot, cb("ads_publish"))
    skipped = await db.fetchone("SELECT * FROM ads ORDER BY id DESC LIMIT 1")
    assert skipped["socials"] is None, skipped["socials"]
    assert "🔗 Соцсети:" not in bot.channel[-1][1], bot.channel[-1][1]
    print("«Пропустить» OK: socials пустой, блока в посте нет")

    # =========================================================== В. удаления
    author = U(UID, "adman", "Автор Объявлений")
    other = U(400002, "otherguy", "Другой Автор")
    await db.upsert_user(other.id, other.username, other.full_name)
    await tk.add(other.id, 200, "test")

    # 1) удалил модератор кнопкой под постом
    await dp.feed_update(bot, cb(f"ad_del:{intro_ad['id']}", uid=ADMIN, chat_id=CHANNEL,
                                 chat_type="channel"))
    row = await db.fetchone("SELECT * FROM ads WHERE id = ?", (intro_ad["id"],))
    assert row["status"] == "deleted" and row["delete_kind"] == "moderator", dict(row)
    assert row["deleted_at"], "время удаления проставлено"
    # обе базы помечены
    posts, total = await sdb.query_posts(ad_type=intro["tag"])
    assert all(p["channel_message_id"] != intro_ad["channel_message_id"] for p in posts), posts

    # админ удаляет собственное объявление кнопкой под постом — это всё равно модерация
    await db.upsert_user(ADMIN, "admin", "Админ")
    await tk.add(ADMIN, 100, "test")
    own = await ads.publish_ad(bot, U(ADMIN, "admin", "Админ"), text="Своё",
                               ad_type_row=resume)
    await dp.feed_update(bot, cb(f"ad_del:{own['ad_id']}", uid=ADMIN, chat_id=CHANNEL,
                                 chat_type="channel"))
    row = await db.fetchone("SELECT delete_kind FROM ads WHERE id = ?", (own["ad_id"],))
    assert row["delete_kind"] == "moderator", dict(row)

    # 2) удалил сам автор
    out = await ads.delete_ad(bot, skipped["id"], by_admin_id=UID)
    assert out["delete_kind"] == "author", out
    row = await db.fetchone("SELECT delete_kind FROM ads WHERE id = ?", (skipped["id"],))
    assert row["delete_kind"] == "author", dict(row)

    # 3) веб-админка зовёт без by_admin_id — это всё равно модератор
    res = await ads.publish_ad(bot, author, text="Третье", ad_type_row=resume)
    await ads.delete_ad(bot, res["ad_id"], by_admin_id=None, comment="Скам")
    row = await db.fetchone("SELECT delete_kind FROM ads WHERE id = ?", (res["ad_id"],))
    assert row["delete_kind"] == "moderator", dict(row)
    print("delete_kind OK: модератор / автор / веб-админка")

    stats = await db.user_violations(UID)
    assert stats == {"deleted_total": 3, "deleted_by_moderator": 2}, stats
    assert await db.user_violations(other.id) == {"deleted_total": 0,
                                                  "deleted_by_moderator": 0}
    # старые строки без delete_kind считаем удалением модератора
    await db.execute("UPDATE ads SET delete_kind = NULL WHERE id = ?", (skipped["id"],))
    assert (await db.user_violations(UID))["deleted_by_moderator"] == 3, "NULL = модератор"
    await db.execute("UPDATE ads SET delete_kind = 'author' WHERE id = ?", (skipped["id"],))
    print("user_violations OK:", stats)

    card = await db.user_card(UID)
    assert card["deleted_total"] == 3 and card["deleted_by_moderator"] == 2, card
    print("user_card OK: удаления и нарушения в карточке")

    # deleted_ads: фильтр по username и по user_id
    rows, total = await db.deleted_ads()
    # 3 объявления adman + «старое» до миграции + собственное объявление админа
    assert total == 5 and len(rows) == 5, (total, len(rows))
    assert rows[0]["username"] == "adman", rows[0]
    rows, total = await db.deleted_ads("olduser")
    assert total == 1 and rows[0]["delete_kind"] is None, (total, rows)
    rows, total = await db.deleted_ads("@ADMAN")          # без @ и регистронезависимо
    assert total == 3, total
    rows, total = await db.deleted_ads("adman")
    assert total == 3, total
    rows, total = await db.deleted_ads(str(UID))
    assert total == 3, "фильтр по user_id"
    rows, total = await db.deleted_ads("otherguy")
    assert total == 0 and rows == [], (total, rows)
    rows, total = await db.deleted_ads("adman", limit=2)
    assert total == 3 and len(rows) == 2, (total, len(rows))
    rows, _ = await db.deleted_ads("adman", limit=2, offset=2)
    assert len(rows) == 1, rows
    print("deleted_ads OK: всего 3, фильтры по нику и id работают")

    # профиль показывает удаления
    await dp.feed_update(bot, priv("📊 Мой профиль"))
    assert "Удалено объявлений: 3 (нарушений: 2)" in bot.last_dm()[0], bot.last_dm()
    print("профиль OK:", [l for l in bot.last_dm()[0].split("\n") if "Удалено" in l][0])

    # =========================================================== Г. рефералы
    await db.upsert_user(400010, "ref1", "Реф Первый")
    await db.upsert_user(400011, "ref2", "Реф Второй")
    await db.execute("UPDATE users SET referrer_id = ? WHERE user_id IN (?, ?)",
                     (UID, 400010, 400011))
    await db.execute("UPDATE users SET activated = 1 WHERE user_id = ?", (400010,))

    await dp.feed_update(bot, priv("👥 Пригласить друга"))
    msg = bot.last_dm()[0]
    assert "Вы пригласили: 2 участников" in msg, msg
    assert "Из них активировались: 1" in msg, msg
    print("реферальное сообщение OK: 2 приглашено, 1 активировался")

    # кнопка меню подписана бонусом и реагирует на нажатие
    assert await db.get_int("referral_bonus") == 10
    kb = await keyboards.main_menu()
    texts = [b.text for row in kb.keyboard for b in row]
    ref_btn = next(t for t in texts if t.startswith(keyboards.BTN_REFERRAL))
    assert ref_btn == "👥 Пригласить друга (Получи 10 коинов!)", ref_btn

    await dp.feed_update(bot, priv(ref_btn))
    assert "реферальная ссылка" in bot.last_dm()[0], bot.last_dm()
    assert user_h.is_menu_button(ref_btn), "кнопка с суффиксом — всё ещё кнопка меню"
    assert user_h.is_menu_button("📊 Мой профиль") and not user_h.is_menu_button("привет")

    await db.set_setting("referral_bonus", 25)
    kb = await keyboards.main_menu()
    texts = [b.text for row in kb.keyboard for b in row]
    assert "👥 Пригласить друга (Получи 25 коинов!)" in texts, texts
    await dp.feed_update(bot, priv("👥 Пригласить друга (Получи 25 коинов!)"))
    assert "25</b> коинов" in bot.last_dm()[0], bot.last_dm()
    print("кнопка рефералов OK: текст следует за настройкой referral_bonus")

    print("POST RULES OK")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
