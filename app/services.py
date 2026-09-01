"""Вспомогательные сервисы: временные сообщения, шаблоны, права, медиа, репост."""
import asyncio
import html
import logging
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app import db, site_db, subscription

log = logging.getLogger(__name__)

ADMIN_STATUSES = {"creator", "administrator"}
_admin_cache: dict[int, tuple[float, set[int]]] = {}
_ADMIN_TTL = 300  # сек.


# ------------------------------------------------------------------ права
async def chat_admin_ids(bot: Bot, chat_id: int, force: bool = False) -> set[int]:
    cached = _admin_cache.get(chat_id)
    if cached and not force and time.time() - cached[0] < _ADMIN_TTL:
        return cached[1]
    ids: set[int] = set()
    try:
        for admin in await bot.get_chat_administrators(chat_id):
            ids.add(admin.user.id)
    except TelegramAPIError as exc:
        log.warning("Не удалось получить админов чата %s: %s", chat_id, exc)
        if cached:
            return cached[1]
    _admin_cache[chat_id] = (time.time(), ids)
    return ids


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    return user_id in await chat_admin_ids(bot, chat_id)


# ------------------------------------------------------------------ шаблоны
def user_mention(user) -> str:
    name = html.escape(getattr(user, "full_name", None) or getattr(user, "first_name", "") or "друг")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def render(template: str, user=None, missing_channels: Optional[list] = None,
                 **extra) -> str:
    text = template or ""
    if user is not None:
        text = text.replace("%USER%", user_mention(user))
    text = text.replace("%CHANNEL_NAME%", subscription.channels_text(missing_channels or []))
    for key, value in extra.items():
        text = text.replace(f"%{key.upper()}%", str(value))
    return text


# ------------------------------------------------------------------ временные сообщения
async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    try:
        await asyncio.sleep(max(1, delay))
        await bot.delete_message(chat_id, message_id)
    except (TelegramAPIError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("Ошибка автоудаления сообщения %s/%s", chat_id, message_id)


async def delete_quiet(bot: Bot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


async def send_temp(bot: Bot, chat_id: int, text: str, user_id: Optional[int] = None,
                    reply_markup=None, ttl: Optional[int] = None) -> Optional[Message]:
    """Отправить сообщение в чат, удалив предыдущее такое же для этого юзера,
    и запланировать автоудаление через N секунд (настройка msg_ttl)."""
    if ttl is None:
        ttl = await db.get_int("msg_ttl")

    if user_id is not None:
        state = await db.get_state(user_id, chat_id)
        await delete_quiet(bot, chat_id, state["last_prompt_msg_id"])
        await db.update_state(user_id, chat_id, last_prompt_msg_id=None)

    try:
        msg = await bot.send_message(chat_id, text, reply_markup=reply_markup,
                                     disable_web_page_preview=True)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить сообщение в %s: %s", chat_id, exc)
        return None

    if user_id is not None:
        await db.update_state(user_id, chat_id, last_prompt_msg_id=msg.message_id)
    if ttl > 0:
        asyncio.create_task(_delete_later(bot, chat_id, msg.message_id, ttl))
    return msg


# ------------------------------------------------------------------ медиа
MEDIA_FIELDS = (
    ("photo", "photo"), ("video", "video"), ("animation", "animation"),
    ("document", "document"), ("audio", "audio"), ("voice", "voice"),
    ("video_note", "video_note"), ("sticker", "sticker"),
)


def media_info(message: Message) -> tuple[str, Optional[str]]:
    for attr, name in MEDIA_FIELDS:
        value = getattr(message, attr, None)
        if value:
            if attr == "photo":
                return name, value[-1].file_id
            return name, getattr(value, "file_id", None)
    return "text", None


def message_text(message: Message) -> str:
    return message.text or message.caption or ""


# ------------------------------------------------------------------ учёт сообщений
async def record_message(message: Message, cost: int, posted_via_bot: bool = False,
                         status: str = "published", chat_title: str = "") -> int:
    media_type, file_id = media_info(message)
    user = message.from_user
    cur = await db.execute(
        """INSERT INTO chat_messages(chat_id, message_id, user_id, text, media_type,
                                     media_file_id, cost, posted_via_bot, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(chat_id, message_id) DO UPDATE SET text = excluded.text""",
        (message.chat.id, message.message_id, user.id if user else 0,
         message_text(message), media_type, file_id, cost, int(posted_via_bot), status),
    )
    await db.execute(
        "UPDATE users SET messages_sent = messages_sent + 1 WHERE user_id = ?",
        (user.id if user else 0,),
    )
    await site_db.mirror_post(
        source_chat_id=message.chat.id,
        source_chat_title=chat_title or message.chat.title,
        source_message_id=message.message_id,
        author_id=user.id if user else None,
        author_username=user.username if user else None,
        author_name=user.full_name if user else None,
        text=message_text(message),
        media_type=media_type,
        media_file_id=file_id,
    )
    return cur.lastrowid


# ------------------------------------------------------------------ репост в канал
async def repost_to_channel(bot: Bot, chat_id: int, message_id: int) -> Optional[int]:
    """Копирует сообщение чата в привязанный канал. Возвращает message_id в канале."""
    chat = await db.get_chat(chat_id)
    if not chat or not chat["repost_channel_id"]:
        return None
    try:
        copied = await bot.copy_message(
            chat_id=chat["repost_channel_id"], from_chat_id=chat_id, message_id=message_id
        )
    except TelegramAPIError as exc:
        log.warning("Репост в канал не удался (%s/%s): %s", chat_id, message_id, exc)
        return None
    await db.execute(
        "UPDATE chat_messages SET reposted = 1 WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    await site_db.mark_reposted(chat_id, message_id, chat["repost_channel_id"], copied.message_id)
    return copied.message_id


async def refund_message(bot: Optional[Bot], chat_id: int, message_id: int,
                         reason: str = "admin_delete") -> int:
    """Помечает сообщение удалённым и возвращает токены автору. -> сколько вернули.

    От бота не зависит: возврат и снятие поста с сайта работают и без него."""
    from app import tokens  # локальный импорт, чтобы избежать цикла

    row = await db.fetchone(
        "SELECT * FROM chat_messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
    )
    await site_db.mark_deleted(chat_id, message_id)
    if row is None:
        return 0
    await db.execute(
        "UPDATE chat_messages SET status = 'deleted' WHERE id = ?", (row["id"],)
    )
    if row["refunded"] or not row["cost"]:
        return 0
    await db.execute("UPDATE chat_messages SET refunded = 1 WHERE id = ?", (row["id"],))
    await tokens.add(row["user_id"], int(row["cost"]), "refund",
                     {"chat_id": chat_id, "message_id": message_id, "reason": reason})
    return int(row["cost"])
