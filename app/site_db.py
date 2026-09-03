"""Независимая база-дублёр для сайта (лайв-лента репостов)."""
import logging
from typing import Optional

import aiosqlite

from app.config import SITE_DB

log = logging.getLogger(__name__)

_conn: Optional[aiosqlite.Connection] = None

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS posts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id     INTEGER NOT NULL,
    source_chat_title  TEXT,
    source_message_id  INTEGER,
    channel_id         INTEGER,
    channel_message_id INTEGER,
    author_id          INTEGER,
    author_username    TEXT,
    author_name        TEXT,
    text               TEXT,
    socials            TEXT,          -- ссылки на соцсети (рубрика «Интро»)
    media_type         TEXT,
    media_file_id      TEXT,
    ad_type_name       TEXT,
    ad_type_tag        TEXT,
    vertical_name      TEXT,
    vertical_tag       TEXT,
    pinned_until       TEXT,
    search_blob        TEXT,          -- всё в нижнем регистре: LIKE в SQLite не знает кириллицы
    is_reposted        INTEGER DEFAULT 0,
    is_deleted         INTEGER DEFAULT 0,
    created_at         TEXT DEFAULT (datetime('now')),
    UNIQUE(source_chat_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author  ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_chat    ON posts(source_chat_id);
CREATE INDEX IF NOT EXISTS idx_posts_type    ON posts(ad_type_tag);
CREATE INDEX IF NOT EXISTS idx_posts_vert    ON posts(vertical_tag);
"""

# Колонки, добавленные после первого релиза, — досоздаются на существующей базе.
MIGRATIONS = [
    ("ad_type_name", "TEXT"), ("ad_type_tag", "TEXT"),
    ("vertical_name", "TEXT"), ("vertical_tag", "TEXT"),
    ("pinned_until", "TEXT"), ("search_blob", "TEXT"),
    ("socials", "TEXT"),
]


def _blob(*parts) -> str:
    """Нормализованная строка для поиска: нижний регистр, без решёток."""
    return " ".join(str(p) for p in parts if p).lower().replace("#", "")


async def _migrate(conn_: aiosqlite.Connection) -> None:
    async with conn_.execute("PRAGMA table_info(posts)") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for name, sql_type in MIGRATIONS:
        if name not in existing:
            await conn_.execute(f"ALTER TABLE posts ADD COLUMN {name} {sql_type}")
            log.info("База сайта: добавлена колонка %s", name)
    await conn_.commit()


async def init() -> aiosqlite.Connection:
    global _conn
    _conn = await aiosqlite.connect(SITE_DB)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _migrate(_conn)
    await _conn.commit()
    log.info("База сайта готова: %s", SITE_DB)
    return _conn


async def close() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("База сайта не инициализирована")
    return _conn


async def mirror_post(**kw) -> None:
    """Зеркалирует сообщение чата в базу сайта (лайв-лента)."""
    await conn().execute(
        """INSERT INTO posts(source_chat_id, source_chat_title, source_message_id, channel_id,
                             channel_message_id, author_id, author_username, author_name,
                             text, socials, media_type, media_file_id, ad_type_name,
                             ad_type_tag, vertical_name, vertical_tag, pinned_until,
                             search_blob, is_reposted, created_at)
           VALUES (:source_chat_id, :source_chat_title, :source_message_id, :channel_id,
                   :channel_message_id, :author_id, :author_username, :author_name,
                   :text, :socials, :media_type, :media_file_id, :ad_type_name,
                   :ad_type_tag, :vertical_name, :vertical_tag, :pinned_until,
                   :search_blob, :is_reposted, datetime('now'))
           ON CONFLICT(source_chat_id, source_message_id) DO UPDATE SET
               text = excluded.text,
               socials = COALESCE(excluded.socials, posts.socials),
               channel_message_id = COALESCE(excluded.channel_message_id, posts.channel_message_id),
               is_reposted = MAX(posts.is_reposted, excluded.is_reposted),
               ad_type_name = COALESCE(excluded.ad_type_name, posts.ad_type_name),
               ad_type_tag = COALESCE(excluded.ad_type_tag, posts.ad_type_tag),
               vertical_name = COALESCE(excluded.vertical_name, posts.vertical_name),
               vertical_tag = COALESCE(excluded.vertical_tag, posts.vertical_tag),
               pinned_until = COALESCE(excluded.pinned_until, posts.pinned_until),
               search_blob = excluded.search_blob""",
        {
            "source_chat_id": kw.get("source_chat_id"),
            "source_chat_title": kw.get("source_chat_title"),
            "source_message_id": kw.get("source_message_id"),
            "channel_id": kw.get("channel_id"),
            "channel_message_id": kw.get("channel_message_id"),
            "author_id": kw.get("author_id"),
            "author_username": kw.get("author_username"),
            "author_name": kw.get("author_name"),
            "text": kw.get("text"),
            "socials": kw.get("socials"),
            "media_type": kw.get("media_type"),
            "media_file_id": kw.get("media_file_id"),
            "ad_type_name": kw.get("ad_type_name"),
            "ad_type_tag": kw.get("ad_type_tag"),
            "vertical_name": kw.get("vertical_name"),
            "vertical_tag": kw.get("vertical_tag"),
            "pinned_until": kw.get("pinned_until"),
            "search_blob": _blob(kw.get("text"), kw.get("author_username"),
                                 kw.get("author_name"), kw.get("ad_type_name"),
                                 kw.get("ad_type_tag"), kw.get("vertical_name"),
                                 kw.get("vertical_tag")),
            "is_reposted": int(kw.get("is_reposted", 0)),
        },
    )
    await conn().commit()


async def mark_deleted(source_chat_id: int, source_message_id: int) -> None:
    await conn().execute(
        "UPDATE posts SET is_deleted = 1 WHERE source_chat_id = ? AND source_message_id = ?",
        (source_chat_id, source_message_id),
    )
    await conn().commit()


async def mark_reposted(source_chat_id: int, source_message_id: int,
                        channel_id: int, channel_message_id: int) -> None:
    await conn().execute(
        """UPDATE posts SET is_reposted = 1, channel_id = ?, channel_message_id = ?
           WHERE source_chat_id = ? AND source_message_id = ?""",
        (channel_id, channel_message_id, source_chat_id, source_message_id),
    )
    await conn().commit()


async def query_posts(q: str = "", chat_id: Optional[int] = None, author: str = "",
                      media: str = "", ad_type: str = "", vertical: str = "",
                      only_pinned: bool = False, only_reposted: bool = False,
                      sort: str = "created_at", order: str = "desc",
                      limit: int = 50, offset: int = 0,
                      after_id: Optional[int] = None) -> tuple[list[dict], int]:
    where = ["is_deleted = 0"]
    args: list = []
    if q:
        where.append("search_blob LIKE ?")
        args.append("%" + _blob(q) + "%")
    if ad_type:
        where.append("ad_type_tag = ?")
        args.append(ad_type.lstrip("#"))
    if vertical:
        where.append("vertical_tag = ?")
        args.append(vertical.lstrip("#"))
    if only_pinned:
        where.append("pinned_until > datetime('now')")
    if chat_id:
        where.append("source_chat_id = ?")
        args.append(chat_id)
    if author:
        where.append("(lower(author_username) = ? OR CAST(author_id AS TEXT) = ?)")
        args += [author.lstrip("@").lower(), author]
    if media:
        where.append("media_type = ?" if media != "text" else "(media_type IS NULL OR media_type = 'text')")
        if media != "text":
            args.append(media)
    if only_reposted:
        where.append("is_reposted = 1")
    if after_id:
        where.append("id > ?")
        args.append(after_id)

    sort = sort if sort in {"created_at", "id", "author_username"} else "created_at"
    order = "ASC" if str(order).lower() == "asc" else "DESC"
    clause = " AND ".join(where)

    total = 0
    async with conn().execute(f"SELECT COUNT(*) FROM posts WHERE {clause}", args) as cur:
        row = await cur.fetchone()
        total = row[0] if row else 0

    sql = (f"SELECT * FROM posts WHERE {clause} ORDER BY {sort} {order}, id {order} "
           f"LIMIT ? OFFSET ?")
    async with conn().execute(sql, (*args, min(int(limit), 200), int(offset))) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    return rows, total
