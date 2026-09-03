"""Проверка подписки пользователя на обязательные каналы."""
import logging
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app import db

log = logging.getLogger(__name__)

SUBSCRIBED_STATUSES = {"creator", "administrator", "member"}

# user_id -> (timestamp, chat_id, [не подписан на ...]) — используется только если sub_cache_ttl > 0
_cache: dict[tuple[int, int], tuple[float, list[dict]]] = {}


def _field(ch, key: str):
    """Работает и с dict, и с aiosqlite.Row."""
    try:
        return ch[key]
    except (KeyError, IndexError):
        return None


def channel_link(ch) -> str:
    username = _field(ch, "username")
    invite = _field(ch, "invite_link")
    title = _field(ch, "title") or "канал"
    if username:
        return f'<a href="https://t.me/{username}">@{username}</a>'
    if invite:
        return f'<a href="{invite}">{title}</a>'
    return title


def channels_text(missing: list) -> str:
    return ", ".join(channel_link(ch) for ch in missing) or "—"


async def missing_channels(bot: Bot, user_id: int, chat_id: int) -> list:
    """Возвращает список обязательных каналов, на которые юзер НЕ подписан."""
    channels = await db.required_channels(chat_id)
    if not channels:
        return []

    ttl = await db.get_int("sub_cache_ttl")
    key = (user_id, chat_id)
    if ttl > 0:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]

    main_id = await db.get_int("ad_channel_id")
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            ok = member.status in SUBSCRIBED_STATUSES
            if not ok:
                missing.append(dict(ch))
            # ленивая синхронизация: результат живой проверки кладём в базу
            await db.record_subscription(user_id, ch["channel_id"], ok,
                                         is_main=ch["channel_id"] == main_id)
        except TelegramAPIError as exc:
            # Бот не в канале / канал недоступен — не наказываем юзера, но кричим в лог.
            log.warning("Не удалось проверить канал %s для %s: %s", ch["channel_id"], user_id, exc)

    if ttl > 0:
        _cache[key] = (time.time(), missing)
    return missing


def invalidate(user_id: int, chat_id: Optional[int] = None) -> None:
    for key in list(_cache):
        if key[0] == user_id and (chat_id is None or key[1] == chat_id):
            _cache.pop(key, None)


async def verify_channel(bot: Bot, ident: str) -> Optional[dict]:
    """Проверяет, что бот есть в канале, и возвращает его данные."""
    ident = ident.strip()
    target: str | int = ident
    if ident.lstrip("-").isdigit():
        target = int(ident)
    elif ident.startswith("https://t.me/"):
        target = "@" + ident.rsplit("/", 1)[-1]
    elif not ident.startswith("@"):
        target = "@" + ident
    try:
        chat = await bot.get_chat(target)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in {"administrator", "creator", "member"}:
            return None
        invite_link = chat.invite_link
        if not invite_link and not chat.username:
            try:
                invite_link = await bot.export_chat_invite_link(chat.id)
            except TelegramAPIError:
                invite_link = None
        return {
            "channel_id": chat.id,
            "title": chat.title,
            "username": chat.username,
            "invite_link": invite_link,
        }
    except TelegramAPIError as exc:
        log.warning("verify_channel(%s) failed: %s", ident, exc)
        return None


REQUIRED_GLOBAL = 0   # chat_id-заглушка для глобального списка обязательных каналов


async def ad_channel_link(bot: Optional[Bot] = None) -> Optional[str]:
    """Публичная ссылка на канал объявлений для кнопки «Канал Aff Bazaar».

    Берётся из настроек (/set_channel их сохраняет); если канал задан раньше и ссылки
    в настройках нет — запрашивается у Telegram и кэшируется в настройках.
    """
    username = await db.get_setting("ad_channel_username")
    if username:
        return f"https://t.me/{username}"
    link = await db.get_setting("ad_channel_link")
    if link:
        return link
    channel_id = await db.get_int("ad_channel_id")
    if not channel_id or bot is None:
        return None
    try:
        chat = await bot.get_chat(channel_id)
        if chat.username:
            await db.set_setting("ad_channel_username", chat.username)
            return f"https://t.me/{chat.username}"
        link = chat.invite_link or await bot.export_chat_invite_link(channel_id)
        if link:
            await db.set_setting("ad_channel_link", link)
        return link or None
    except TelegramAPIError as exc:
        log.warning("Не удалось получить ссылку на канал объявлений: %s", exc)
        return None


async def missing_for_ads(bot: Bot, user_id: int) -> list:
    """Каналы, на которые юзер не подписан, для публикации объявлений.

    Берётся глобальный список (chat_id = 0); если он пуст — сам канал объявлений.
    """
    channels = await db.required_channels(REQUIRED_GLOBAL)
    if not channels:
        channel_id = await db.get_int("ad_channel_id")
        if not channel_id:
            return []
        title = await db.get_setting("ad_channel_title")
        try:
            chat = await bot.get_chat(channel_id)
            info = {"channel_id": chat.id, "title": chat.title or title,
                    "username": chat.username, "invite_link": chat.invite_link}
        except TelegramAPIError:
            info = {"channel_id": channel_id, "title": title, "username": None,
                    "invite_link": None}
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            ok = member.status in SUBSCRIBED_STATUSES
            await db.record_subscription(user_id, channel_id, ok, is_main=True)
            return [] if ok else [info]
        except TelegramAPIError as exc:
            log.warning("Проверка канала объявлений не удалась: %s", exc)
            return []
    return await missing_channels(bot, user_id, REQUIRED_GLOBAL)
