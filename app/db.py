"""Основная база бота (SQLite/aiosqlite) + доступ к настройкам."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import aiosqlite

from app.config import MAIN_DB

log = logging.getLogger(__name__)

_conn: Optional[aiosqlite.Connection] = None

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS chats (
    chat_id           INTEGER PRIMARY KEY,
    title             TEXT,
    welcome_message   TEXT,
    post_mode         TEXT    DEFAULT 'direct',   -- direct | bot_only
    premoderate       INTEGER DEFAULT 0,
    repost_channel_id INTEGER,
    repost_mode       TEXT    DEFAULT 'manual',   -- manual | auto | off
    is_active         INTEGER DEFAULT 1,
    created_at        TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS required_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    title       TEXT,
    username    TEXT,
    invite_link TEXT,
    UNIQUE(chat_id, channel_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    username          TEXT,
    full_name         TEXT,
    first_name        TEXT,
    last_name         TEXT,
    bio               TEXT,
    tokens            INTEGER DEFAULT 0,
    activated         INTEGER DEFAULT 0,   -- выдан бонус за подписку (не «нажал /start» — см. started)
    activated_at      TEXT,
    started           INTEGER DEFAULT 0,   -- нажал /start хотя бы раз
    started_at        INTEGER,             -- unix, первый /start
    subscribed        INTEGER DEFAULT 0,   -- подписан на основной канал сейчас
    first_subscribed_at INTEGER,           -- unix, первая подписка на основной канал
    last_seen_at      INTEGER,             -- unix, последнее событие от юзера
    referrer_id       INTEGER,
    referral_rewarded INTEGER DEFAULT 0,
    messages_sent     INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);

-- Подписки на любые каналы (основной + обязательные). Заменяет «резервные»
-- колонки канал2-канал5 из ТЗ: количество каналов не ограничено.
CREATE TABLE IF NOT EXISTS channel_subs (
    user_id             INTEGER NOT NULL,
    channel_id          INTEGER NOT NULL,
    subscribed          INTEGER DEFAULT 0,
    first_subscribed_at INTEGER,           -- unix, ставится один раз
    updated_at          INTEGER,           -- unix, последнее обновление статуса
    PRIMARY KEY (user_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_subs_channel ON channel_subs(channel_id);

CREATE TABLE IF NOT EXISTS user_chat_state (
    user_id            INTEGER NOT NULL,
    chat_id            INTEGER NOT NULL,
    fail_streak        INTEGER DEFAULT 0,
    total_checks       INTEGER DEFAULT 0,
    subscribed         INTEGER DEFAULT 0,
    restricted_until   TEXT,
    last_prompt_msg_id INTEGER,
    last_check_at      TEXT,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS token_tx (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    amount        INTEGER NOT NULL,
    reason        TEXT,
    meta          TEXT,
    balance_after INTEGER,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON token_tx(user_id, id DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER,
    user_id       INTEGER NOT NULL,
    text          TEXT,
    media_type    TEXT,
    media_file_id TEXT,
    cost          INTEGER DEFAULT 0,
    refunded      INTEGER DEFAULT 0,
    posted_via_bot INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'published',  -- pending | published | rejected | deleted
    reposted      INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON chat_messages(chat_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_msg_user ON chat_messages(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_msg_status ON chat_messages(status);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER,          -- сколько заплачено (Stars / минимальные единицы валюты)
    currency    TEXT,
    tokens      INTEGER,
    charge_id   TEXT,
    payload     TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ad_types (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    tag          TEXT NOT NULL,
    has_vertical INTEGER DEFAULT 0,   -- показывать ли выбор вертикали
    note         TEXT,                -- пометка, показывается при выборе рубрики
    position     INTEGER DEFAULT 0,
    is_active    INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS verticals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    tag       TEXT NOT NULL,
    position  INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ads (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    channel_id         INTEGER,
    channel_message_id INTEGER,
    ad_type_id         INTEGER,
    ad_type_name       TEXT,
    ad_type_tag        TEXT,
    vertical_id        INTEGER,
    vertical_name      TEXT,
    vertical_tag       TEXT,
    text               TEXT,
    socials            TEXT,                       -- ссылки на соцсети (рубрика «Интро»)
    media_type         TEXT,
    media_file_id      TEXT,
    cost_base          INTEGER DEFAULT 0,
    cost_image         INTEGER DEFAULT 0,
    cost_pin           INTEGER DEFAULT 0,
    cost_total         INTEGER DEFAULT 0,
    pin_hours          INTEGER DEFAULT 0,
    pinned_until       TEXT,
    unpinned           INTEGER DEFAULT 0,
    status             TEXT DEFAULT 'published',   -- published | deleted
    deleted_by         INTEGER,
    delete_kind        TEXT,                       -- author | moderator (кто удалил)
    delete_comment     TEXT,
    deleted_at         TEXT,
    refunded           INTEGER DEFAULT 0,
    created_at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ads_user ON ads(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_ads_msg  ON ads(channel_id, channel_message_id);
CREATE INDEX IF NOT EXISTS idx_ads_pin  ON ads(pinned_until);

CREATE TABLE IF NOT EXISTS rules_accept (
    user_id     INTEGER PRIMARY KEY,
    version     TEXT,
    accepted_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS restrictions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    until      TEXT,
    reason     TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

RULES_TEXT_DEFAULT = (
    "<b>Правила публикации</b>\n\n"
    "1. Одно объявление — одна рубрика. Выбирайте её честно.\n"
    "2. Запрещены мошенничество, скам-схемы и обман участников.\n"
    "3. Запрещены оскорбления, разжигание вражды и политика.\n"
    "4. В рубрике «Интро/Знакомства» можно рассказывать о себе и оставлять свои соцсети, "
    "но <b>нельзя публиковать ссылки на Telegram-каналы</b>.\n"
    "5. Дубли и спам удаляются без возврата коинов.\n"
    "6. Администрация может удалить любое объявление, нарушающее правила. Если удаление "
    "произошло по инициативе администрации, коины возвращаются на баланс.\n\n"
    "Публикуя объявление, вы соглашаетесь с этими правилами."
)

# Значения глобальных настроек по умолчанию (все — строки в БД).
DEFAULT_SETTINGS: dict[str, str] = {
    "msg_ttl":          "45",     # сек. видимости сообщения бота
    "check_limit":      "10",     # подряд неудачных проверок подписки
    "restrict_hours":   "48",     # на сколько часов ограничивать
    "restricted_text":  "Вы заблокированы на 48 часов из-за превышения количества проверок. "
                        "Пожалуйста, свяжитесь с администратором.",
    "welcome_message":  "%USER%, приветствую тебя!\n"
                        "Чтобы иметь возможность писать в чат, необходимо подписаться на канал(ы) %CHANNEL_NAME%.",
    "signup_bonus":     "30",     # коинов за подписку (единоразово)
    "referral_bonus":   "10",     # коинов пригласившему, когда друг активировался
    "message_cost":     "1",      # стоимость одного сообщения в чат
    "no_tokens_text":   "%USER%, закончились коины. Пополни баланс в боте, чтобы писать в чат.",
    "bot_only_text":    "%USER%, в этом чате писать можно только через бота. Открой бота и отправь сообщение туда.",
    "token_packages":   json.dumps([{"stars": 50, "tokens": 50},
                                    {"stars": 100, "tokens": 120},
                                    {"stars": 250, "tokens": 350}], ensure_ascii=False),
    "sub_cache_ttl":    "0",      # 0 = проверять подписку всегда (требование п.1)

    # --- канал объявлений и цены (в коинах) ---
    "ad_channel_id":    "0",      # канал, куда бот публикует объявления
    "ad_channel_title": "",
    "price_post":       "10",     # базовая цена объявления
    "price_image":      "5",      # доплата за картинку
    "price_pin_4h":     "15",     # доплата за закреп на 4 часа
    "price_pin_8h":     "25",     # доплата за закреп на 8 часов
    "rules_version":    "1",      # смена версии заставит принять правила заново
    "rules_text":       RULES_TEXT_DEFAULT,
    "intro_note":       "В этой рубрике можно рассказать о себе и оставить свои соцсети, "
                        "но ссылки на Telegram-каналы публиковать нельзя.",

    # --- брендинг сайта (подставляется в шаблоны при отдаче страницы) ---
    "site_title":       "Aff Bazar",
    "site_tagline":     "Лента объявлений · биржа affiliate-рынка",
}


# Рубрики (тип объявления). has_vertical = 1 -> у рубрики спрашивается вертикаль.
AD_TYPES: list[tuple[str, int, str]] = [
    ("Прямой рекламодатель", 1, ""),
    ("CPA сеть", 1, ""),
    ("Медиабаинг / Рекламное агентство", 1, ""),
    ("Рекламные сети", 0, ""),
    ("HR Агентство", 0, ""),
    ("Агентские аккаунты", 0, ""),
    ("Безопасность", 0, ""),
    ("Разработка", 0, ""),
    ("Инфраструктура", 0, ""),
    ("Финансовые и платежные услуги", 0, ""),
    ("Юридические услуги", 0, ""),
    ("Рассылки sms, email, etc", 0, ""),
    ("Другие сервисы / Прочее", 0, ""),
    ("Интро/Знакомства", 0, "intro"),
    ("Резюме", 0, ""),
    ("Вакансия", 0, ""),
]

VERTICALS: list[str] = [
    "Все вертикали", "Гемблинг", "Покер", "Крипто / Web3", "ФинТех", "Игры / GameDev",
    "Форекс", "Бинарные опционы", "Дейтинг", "mVAS", "Адалт", "Нутра",
    "Утилиты / VPN / антивирусы", "Свипстейки", "e-Commerce", "Образование",
    "Фарма", "Туризм", "Мультивертикаль", "Прочее",
]


def make_tag(name: str) -> str:
    """«Медиабаинг / Рекламное агентство» -> «медиабаинг_рекламное_агентство»."""
    out: list[str] = []
    for ch in name.lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


async def _seed_rubrics(conn_: aiosqlite.Connection) -> None:
    """Заполняет справочники один раз; правки админа не затирает."""
    for position, (name, has_vertical, note_key) in enumerate(AD_TYPES):
        await conn_.execute(
            "INSERT OR IGNORE INTO ad_types(name, tag, has_vertical, note, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, make_tag(name), has_vertical, note_key or None, position))
    for position, name in enumerate(VERTICALS):
        await conn_.execute(
            "INSERT OR IGNORE INTO verticals(name, tag, position) VALUES (?, ?, ?)",
            (name, make_tag(name), position))


# Колонки users, добавленные после первого релиза. На проде базы уже существуют,
# а CREATE TABLE IF NOT EXISTS их не тронет — поэтому при init() догоняем ALTER-ами.
# Не путать: users.activated / activated_at (TEXT) — «выдан бонус за подписку» (tokens.py),
# а started / started_at (unix) — «нажал /start», это и есть «активация бота» из ТЗ.
USERS_ADDED_COLUMNS: dict[str, str] = {
    "first_name":          "TEXT",
    "last_name":           "TEXT",
    "bio":                 "TEXT",
    "started":             "INTEGER DEFAULT 0",
    "started_at":          "INTEGER",
    "subscribed":          "INTEGER DEFAULT 0",
    "first_subscribed_at": "INTEGER",
    "last_seen_at":        "INTEGER",
}


# Колонки ads, добавленные после первого релиза — тем же способом, что и users.
ADS_ADDED_COLUMNS: dict[str, str] = {
    "socials":     "TEXT",   # ссылки на соцсети (шаг мастера для рубрики «Интро»)
    "delete_kind": "TEXT",   # author | moderator; NULL у старых строк = удалял модератор
}


async def _migrate_table(conn_: aiosqlite.Connection, table: str,
                         columns: dict[str, str]) -> None:
    """Добавляет в таблицу недостающие колонки (идемпотентно)."""
    async with conn_.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for name, decl in columns.items():
        if name in existing:
            continue
        await conn_.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        log.info("Миграция: %s.%s добавлена", table, name)


async def _migrate(conn_: aiosqlite.Connection) -> None:
    await _migrate_table(conn_, "users", USERS_ADDED_COLUMNS)
    await _migrate_table(conn_, "ads", ADS_ADDED_COLUMNS)


def now_ts() -> int:
    """Текущее время в unix-секундах — формат всех новых полей времени."""
    return int(time.time())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


async def init() -> aiosqlite.Connection:
    global _conn
    _conn = await aiosqlite.connect(MAIN_DB)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _migrate(_conn)
    for key, value in DEFAULT_SETTINGS.items():
        await _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    await _seed_rubrics(_conn)
    await _conn.commit()
    log.info("Основная база готова: %s", MAIN_DB)
    return _conn


async def close() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("База не инициализирована — вызовите db.init()")
    return _conn


# ---------------------------------------------------------------- низкий уровень
async def fetchone(sql: str, args: Iterable = ()) -> Optional[aiosqlite.Row]:
    async with conn().execute(sql, tuple(args)) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, args: Iterable = ()) -> list[aiosqlite.Row]:
    async with conn().execute(sql, tuple(args)) as cur:
        return list(await cur.fetchall())


async def execute(sql: str, args: Iterable = ()) -> aiosqlite.Cursor:
    cur = await conn().execute(sql, tuple(args))
    await conn().commit()
    return cur


async def scalar(sql: str, args: Iterable = (), default: Any = 0) -> Any:
    row = await fetchone(sql, args)
    if row is None or row[0] is None:
        return default
    return row[0]


# ---------------------------------------------------------------- настройки
async def get_setting(key: str, default: Optional[str] = None) -> str:
    row = await fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    if row is not None:
        return row["value"]
    return default if default is not None else DEFAULT_SETTINGS.get(key, "")


async def get_int(key: str) -> int:
    try:
        return int(float(await get_setting(key)))
    except (TypeError, ValueError):
        return int(float(DEFAULT_SETTINGS.get(key, "0")))


async def set_setting(key: str, value: Any) -> None:
    await execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


async def all_settings() -> dict[str, str]:
    rows = await fetchall("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------- пользователи
PROFILE_FIELDS = ("username", "first_name", "last_name", "full_name", "bio")


async def upsert_user_profile(user_id: int, *, username: Optional[str] = None,
                              first_name: Optional[str] = None,
                              last_name: Optional[str] = None,
                              full_name: Optional[str] = None,
                              bio: Optional[str] = None,
                              seen: bool = True) -> None:
    """Создаёт/обновляет карточку пользователя.

    Поля, переданные как None, НЕ затираются: события приходят из разных мест
    (сообщение в чате, /start, chat_member) и знают разный набор данных.
    """
    values = {"username": username, "first_name": first_name, "last_name": last_name,
              "full_name": full_name, "bio": bio}
    fields = {k: v for k, v in values.items() if v is not None}
    if seen:
        fields["last_seen_at"] = now_ts()
    cols = ", ".join(("user_id", *fields))
    marks = ", ".join("?" for _ in range(len(fields) + 1))
    sets = "".join(f"{k} = excluded.{k}, " for k in fields) + "updated_at = datetime('now')"
    await execute(
        f"INSERT INTO users({cols}) VALUES ({marks}) "
        f"ON CONFLICT(user_id) DO UPDATE SET {sets}",
        (user_id, *fields.values()),
    )


async def upsert_user(user_id: int, username: Optional[str], full_name: Optional[str],
                      first_name: Optional[str] = None,
                      last_name: Optional[str] = None) -> None:
    """Совместимая обёртка: её вызывает весь старый код."""
    await upsert_user_profile(user_id, username=username, full_name=full_name,
                              first_name=first_name, last_name=last_name)


async def mark_started(user_id: int) -> bool:
    """Первое нажатие /start: started = 1 и started_at. Повторные не перезаписывают.

    Возвращает True, если это был первый /start.
    """
    await execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
    cur = await execute(
        "UPDATE users SET started = 1, started_at = COALESCE(started_at, ?), "
        "updated_at = datetime('now') "
        "WHERE user_id = ? AND (started IS NULL OR started = 0 OR started_at IS NULL)",
        (now_ts(), user_id))
    return bool(cur.rowcount)


async def record_subscription(user_id: int, channel_id: int, subscribed: bool,
                              is_main: Optional[bool] = None) -> None:
    """Единая точка записи подписки: channel_subs + (для основного канала) users.

    first_subscribed_at ставится один раз и при выходе из канала не сбрасывается.
    is_main=None -> определяем сами по настройке ad_channel_id.
    """
    if not channel_id:
        return
    ts = now_ts()
    flag = 1 if subscribed else 0
    first = ts if flag else None
    await execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
    await execute(
        """INSERT INTO channel_subs(user_id, channel_id, subscribed,
                                    first_subscribed_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, channel_id) DO UPDATE SET
               subscribed          = excluded.subscribed,
               first_subscribed_at = COALESCE(channel_subs.first_subscribed_at,
                                              excluded.first_subscribed_at),
               updated_at          = excluded.updated_at""",
        (user_id, channel_id, flag, first, ts),
    )
    if is_main is None:
        is_main = channel_id == await get_int("ad_channel_id")
    if is_main:
        await execute(
            "UPDATE users SET subscribed = ?, "
            "first_subscribed_at = COALESCE(first_subscribed_at, ?), "
            "updated_at = datetime('now') WHERE user_id = ?",
            (flag, first, user_id),
        )


async def channel_sub(user_id: int, channel_id: int) -> Optional[aiosqlite.Row]:
    return await fetchone(
        "SELECT * FROM channel_subs WHERE user_id = ? AND channel_id = ?",
        (user_id, channel_id))


async def user_card(user_id: int) -> Optional[dict]:
    """Полная карточка пользователя по ТЗ, реферер подтягивается JOIN-ом.

    referrer_id — TG ID реферера (он же его user_id в этой базе);
    referrer_db_id — его id в этой базе (NULL, если такого юзера у нас нет);
    referrer_username — никнейм. Если реферала нет, все три — None.
    """
    row = await fetchone(
        """SELECT u.user_id, u.username, u.first_name, u.last_name, u.full_name, u.bio,
                  u.tokens, u.messages_sent, u.activated, u.activated_at,
                  u.started, u.started_at, u.subscribed, u.first_subscribed_at,
                  u.last_seen_at, u.created_at, u.updated_at,
                  u.referrer_id,
                  r.user_id  AS referrer_db_id,
                  r.username AS referrer_username
             FROM users u
             LEFT JOIN users r ON r.user_id = u.referrer_id
            WHERE u.user_id = ?""",
        (user_id,))
    if row is None:
        return None
    card = dict(row)
    card["channels"] = [dict(c) for c in await fetchall(
        """SELECT channel_id, subscribed, first_subscribed_at, updated_at
             FROM channel_subs WHERE user_id = ? ORDER BY channel_id""", (user_id,))]
    card.update(await user_violations(user_id))
    return card


# ---------------------------------------------------------------- удаления и нарушения
async def user_violations(user_id: int) -> dict[str, int]:
    """Сколько объявлений пользователя удалено и сколько из них — модератором.

    Удаление модератором считаем нарушением. У строк, созданных до появления
    delete_kind, значение NULL — это всегда были удаления администрацией
    (самоудаления в боте не было), поэтому NULL приравниваем к 'moderator'.
    """
    row = await fetchone(
        """SELECT COUNT(*) AS deleted_total,
                  SUM(CASE WHEN COALESCE(delete_kind, 'moderator') = 'moderator'
                           THEN 1 ELSE 0 END) AS deleted_by_moderator
             FROM ads WHERE user_id = ? AND status = 'deleted'""",
        (user_id,))
    return {"deleted_total": int(row["deleted_total"] or 0) if row else 0,
            "deleted_by_moderator": int(row["deleted_by_moderator"] or 0) if row else 0}


async def deleted_ads(username: str = "", limit: int = 50,
                      offset: int = 0) -> tuple[list[dict], int]:
    """Удалённые объявления для вкладки админки. Фильтр — по @username или user_id."""
    where = ["a.status = 'deleted'"]
    args: list = []
    ident = (username or "").strip().lstrip("@")
    if ident:
        where.append("(lower(u.username) = ? OR CAST(a.user_id AS TEXT) = ?)")
        args += [ident.lower(), ident]
    clause = " AND ".join(where)
    rows = await fetchall(
        f"""SELECT a.*, u.username AS username, u.full_name AS full_name
              FROM ads a LEFT JOIN users u ON u.user_id = a.user_id
             WHERE {clause} ORDER BY a.id DESC LIMIT ? OFFSET ?""",
        (*args, max(1, min(int(limit), 200)), max(0, int(offset))))
    total = await scalar(
        f"""SELECT COUNT(*) FROM ads a LEFT JOIN users u ON u.user_id = a.user_id
             WHERE {clause}""", args)
    return [dict(r) for r in rows], int(total or 0)


async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    return await fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def get_user_by_username(username: str) -> Optional[aiosqlite.Row]:
    uname = username.lstrip("@").lower()
    return await fetchone("SELECT * FROM users WHERE lower(username) = ?", (uname,))


# ---------------------------------------------------------------- чаты и каналы
async def get_chat(chat_id: int) -> Optional[aiosqlite.Row]:
    return await fetchone("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))


async def active_chats() -> list[aiosqlite.Row]:
    return await fetchall("SELECT * FROM chats WHERE is_active = 1 ORDER BY created_at")


async def upsert_chat(chat_id: int, title: str) -> None:
    await execute(
        """INSERT INTO chats(chat_id, title) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, is_active = 1""",
        (chat_id, title),
    )


async def required_channels(chat_id: int) -> list[aiosqlite.Row]:
    return await fetchall("SELECT * FROM required_channels WHERE chat_id = ? ORDER BY id", (chat_id,))


async def set_required_channels(chat_id: int, channels: list[dict]) -> None:
    await conn().execute("DELETE FROM required_channels WHERE chat_id = ?", (chat_id,))
    for ch in channels:
        await conn().execute(
            """INSERT OR REPLACE INTO required_channels(chat_id, channel_id, title, username, invite_link)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, ch["channel_id"], ch.get("title"), ch.get("username"), ch.get("invite_link")),
        )
    await conn().commit()


# ---------------------------------------------------------------- состояние юзера в чате
async def get_state(user_id: int, chat_id: int) -> aiosqlite.Row:
    row = await fetchone(
        "SELECT * FROM user_chat_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
    )
    if row is None:
        await execute(
            "INSERT OR IGNORE INTO user_chat_state(user_id, chat_id) VALUES (?, ?)", (user_id, chat_id)
        )
        row = await fetchone(
            "SELECT * FROM user_chat_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
        )
    return row


async def update_state(user_id: int, chat_id: int, **fields) -> None:
    if not fields:
        return
    await get_state(user_id, chat_id)
    sets = ", ".join(f"{k} = ?" for k in fields)
    await execute(
        f"UPDATE user_chat_state SET {sets} WHERE user_id = ? AND chat_id = ?",
        (*fields.values(), user_id, chat_id),
    )


async def restrict_user(user_id: int, chat_id: int, hours: int, reason: str) -> datetime:
    until = utcnow() + timedelta(hours=hours)
    await update_state(user_id, chat_id, restricted_until=iso(until), fail_streak=0)
    await execute(
        "INSERT INTO restrictions(user_id, chat_id, until, reason) VALUES (?, ?, ?, ?)",
        (user_id, chat_id, iso(until), reason),
    )
    return until


async def is_restricted(user_id: int, chat_id: int) -> Optional[datetime]:
    state = await get_state(user_id, chat_id)
    until = parse_iso(state["restricted_until"])
    if until and until > utcnow():
        return until
    return None


# ---------------------------------------------------------------- рубрики
async def ad_types(only_active: bool = True) -> list[aiosqlite.Row]:
    where = "WHERE is_active = 1" if only_active else ""
    return await fetchall(f"SELECT * FROM ad_types {where} ORDER BY position, id")


async def verticals(only_active: bool = True) -> list[aiosqlite.Row]:
    where = "WHERE is_active = 1" if only_active else ""
    return await fetchall(f"SELECT * FROM verticals {where} ORDER BY position, id")


async def ad_type(type_id: int, only_active: bool = False) -> Optional[aiosqlite.Row]:
    where = " AND is_active = 1" if only_active else ""
    return await fetchone(f"SELECT * FROM ad_types WHERE id = ?{where}", (type_id,))


async def vertical(vertical_id: int, only_active: bool = False) -> Optional[aiosqlite.Row]:
    where = " AND is_active = 1" if only_active else ""
    return await fetchone(f"SELECT * FROM verticals WHERE id = ?{where}", (vertical_id,))


# ---------------------------------------------------------------- правила
async def rules_accepted(user_id: int) -> bool:
    version = await get_setting("rules_version")
    row = await fetchone("SELECT version FROM rules_accept WHERE user_id = ?", (user_id,))
    return bool(row and row["version"] == version)


async def accept_rules(user_id: int) -> None:
    version = await get_setting("rules_version")
    await execute(
        """INSERT INTO rules_accept(user_id, version, accepted_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET version = excluded.version,
                                              accepted_at = datetime('now')""",
        (user_id, version))
