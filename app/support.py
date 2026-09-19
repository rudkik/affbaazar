"""Бот поддержки (@aff_bazzar_support_bot) — второй бот в том же процессе (AffBazaar-18).

Перенос старого feedback-бота (legacy_feedback_bot.py.bak) на aiogram 3:
- пользователь пишет боту в личку → на каждого создаётся топик в форум-группе поддержки
  (SUPPORT_CHAT_ID), сообщения копируются туда;
- ответ команды в топике копируется пользователю в личку;
- /ban и /unban в топике блокируют и разблокируют пользователя.

Тикеты хранятся в основной базе (support_tickets). Запускается из bot.py, если в .env
задан SUPPORT_BOT_TOKEN; без токена run() просто ничего не делает.
"""
import html
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app import config, db, texts

log = logging.getLogger(__name__)
router = Router(name="support")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


async def run() -> None:
    if not config.SUPPORT_BOT_TOKEN:
        log.info("Бот поддержки не запущен: SUPPORT_BOT_TOKEN пуст")
        return
    if not config.SUPPORT_CHAT_ID:
        log.error("Бот поддержки не запущен: SUPPORT_CHAT_ID пуст (id форум-группы поддержки)")
        return
    bot = Bot(token=config.SUPPORT_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        log.info("Бот поддержки запущен: @%s → группа %s", me.username, config.SUPPORT_CHAT_ID)
        await bot.delete_webhook(drop_pending_updates=False)
        await build_dispatcher().start_polling(bot, handle_signals=False)
    except Exception:  # noqa: BLE001 — падение поддержки не должно ронять основной бот
        log.exception("Бот поддержки остановился с ошибкой")
    finally:
        await bot.session.close()


# ------------------------------------------------------------------ тикеты
async def get_ticket(user_id: int):
    return await db.fetchone("SELECT * FROM support_tickets WHERE user_id = ?", (user_id,))


async def ticket_by_topic(topic_id: int):
    return await db.fetchone("SELECT * FROM support_tickets WHERE topic_id = ?", (topic_id,))


def _card(user) -> str:
    username = f"@{user.username}" if user.username else "N/A"
    return (f"{html.escape(user.full_name or str(user.id))}\n"
            f"├── Наличие бана: нет\n"
            f"├── Telegram ID: <code>{user.id}</code>\n"
            f"├── Юзернейм: {html.escape(username)}\n"
            f"└── Язык: {html.escape(user.language_code or 'N/A')}")


async def ensure_topic(bot: Bot, user) -> Optional[int]:
    """Топик пользователя в группе поддержки; создаётся при первом обращении."""
    ticket = await get_ticket(user.id)
    if ticket and ticket["topic_id"]:
        return int(ticket["topic_id"])
    return await _create_topic(bot, user)


async def _create_topic(bot: Bot, user) -> Optional[int]:
    try:
        topic = await bot.create_forum_topic(chat_id=config.SUPPORT_CHAT_ID,
                                             name=(user.full_name or str(user.id))[:128])
    except TelegramAPIError as exc:
        log.error("Не удалось создать топик поддержки для %s: %s", user.id, exc)
        return None
    topic_id = topic.message_thread_id
    await db.execute(
        """INSERT INTO support_tickets(user_id, topic_id) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET topic_id = excluded.topic_id,
                                             updated_at = datetime('now')""",
        (user.id, topic_id))
    try:
        await bot.send_message(config.SUPPORT_CHAT_ID, _card(user), message_thread_id=topic_id)
    except TelegramAPIError as exc:
        log.warning("Карточка пользователя %s в топик не отправлена: %s", user.id, exc)
    log.info("support: топик %s создан для user=%s", topic_id, user.id)
    return topic_id


def _thread_gone(exc: TelegramBadRequest) -> bool:
    """Топик удалили или закрыли руками — создаём новый."""
    text = str(exc).lower()
    return "thread not found" in text or "topic_deleted" in text or "topic_closed" in text


# ------------------------------------------------------------------ личка
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    await db.upsert_user_profile(user.id, username=user.username, first_name=user.first_name,
                                 last_name=user.last_name, full_name=user.full_name)
    if await ensure_topic(bot, user) is None:
        await message.answer(await texts.t("txt_support_error", user))
        return
    await message.answer(await texts.t("txt_support_start", user))


@router.message(F.chat.type == ChatType.PRIVATE)
async def from_user(message: Message, bot: Bot) -> None:
    """Любое сообщение пользователя копируется в его топик."""
    if message.text and message.text.startswith("/"):
        return
    user = message.from_user
    ticket = await get_ticket(user.id)
    if ticket and ticket["banned"]:
        await message.answer(await texts.t("txt_support_banned", user))
        return
    topic_id = await ensure_topic(bot, user)
    if topic_id is None:
        await message.answer(await texts.t("txt_support_error", user))
        return
    try:
        await bot.copy_message(chat_id=config.SUPPORT_CHAT_ID, from_chat_id=message.chat.id,
                               message_id=message.message_id, message_thread_id=topic_id)
    except TelegramBadRequest as exc:
        if not _thread_gone(exc):
            log.exception("support: не удалось передать сообщение user=%s", user.id)
            await message.answer(await texts.t("txt_support_error", user))
            return
        topic_id = await _create_topic(bot, user)
        try:
            await bot.copy_message(chat_id=config.SUPPORT_CHAT_ID, from_chat_id=message.chat.id,
                                   message_id=message.message_id, message_thread_id=topic_id)
        except TelegramAPIError:
            log.exception("support: не удалось передать сообщение user=%s", user.id)
            await message.answer(await texts.t("txt_support_error", user))
    except TelegramAPIError:
        log.exception("support: не удалось передать сообщение user=%s", user.id)
        await message.answer(await texts.t("txt_support_error", user))


# ------------------------------------------------------------------ группа поддержки
def _in_support_chat(message: Message) -> bool:
    return message.chat.id == config.SUPPORT_CHAT_ID and bool(message.message_thread_id)


@router.message(_in_support_chat, Command("ban", "unban"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    ticket = await ticket_by_topic(message.message_thread_id)
    if ticket is None:
        await message.reply("Для этого топика нет обращения.")
        return
    ban = (message.text or "").startswith("/ban")
    await db.execute("UPDATE support_tickets SET banned = ?, updated_at = datetime('now') "
                     "WHERE user_id = ?", (int(ban), ticket["user_id"]))
    await message.reply("Пользователь заблокирован и больше не может писать в поддержку."
                        if ban else "Пользователь разблокирован и снова может писать.")


@router.message(_in_support_chat)
async def from_team(message: Message, bot: Bot) -> None:
    """Ответ команды в топике уходит пользователю. Команды и сервисные сообщения не копируем."""
    if message.from_user is None or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return
    if message.content_type not in {"text", "photo", "video", "animation", "document",
                                    "audio", "voice", "video_note", "sticker"}:
        return
    ticket = await ticket_by_topic(message.message_thread_id)
    if ticket is None:
        return
    try:
        await bot.copy_message(chat_id=int(ticket["user_id"]), from_chat_id=message.chat.id,
                               message_id=message.message_id)
    except TelegramAPIError as exc:
        log.warning("support: ответ пользователю %s не доставлен: %s", ticket["user_id"], exc)
        await message.reply(f"⚠️ Не доставлено: <code>{html.escape(str(exc))}</code>. "
                            f"Возможно, пользователь заблокировал бота.")
