"""Учёт вступивших/вышедших в каналах.

Telegram присылает апдейт `chat_member` только если бот — администратор чата.
Здесь мы пишем каждое такое событие в базу: карточку пользователя, таблицу
channel_subs и (для основного канала объявлений) поля users.subscribed /
users.first_subscribed_at.
"""
import logging
from typing import Optional

from aiogram import Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from app import db

log = logging.getLogger(__name__)
router = Router(name="members")

# Статусы, при которых человек считается подписанным.
JOINED_STATUSES = {"member", "administrator", "creator"}


def _status(member) -> Optional[str]:
    """Строковый статус: у aiogram это enum, у фейкового бота в тестах — строка."""
    status = getattr(member, "status", None)
    return getattr(status, "value", status)


def is_subscribed(member) -> bool:
    """«restricted» считаем подпиской, только если is_member = True."""
    status = _status(member)
    if status in JOINED_STATUSES:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def _is_tracked(chat) -> bool:
    """Следим за каналами: основным, обязательными и вообще любым каналом.

    Группы сюда не попадают — членство в чате учитывается отдельно (user_chat_state).
    """
    if getattr(chat, "type", None) == "channel":
        return True
    chat_id = chat.id
    if chat_id == await db.get_int("ad_channel_id"):
        return True
    row = await db.fetchone(
        "SELECT 1 FROM required_channels WHERE channel_id = ? LIMIT 1", (chat_id,))
    return row is not None


async def handle_membership(event: ChatMemberUpdated) -> None:
    """Общая обработка входа и выхода."""
    user = event.new_chat_member.user
    if user is None or user.is_bot:
        return
    if not await _is_tracked(event.chat):
        return

    subscribed = is_subscribed(event.new_chat_member)
    await db.upsert_user_profile(
        user.id, username=user.username, first_name=user.first_name,
        last_name=user.last_name, full_name=user.full_name)

    main_id = await db.get_int("ad_channel_id")
    await db.record_subscription(user.id, event.chat.id, subscribed,
                                 is_main=event.chat.id == main_id)
    log.info("chat_member: user=%s channel=%s подписан=%s (статус %s)",
             user.id, event.chat.id, subscribed, _status(event.new_chat_member))


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER))
async def on_joined(event: ChatMemberUpdated) -> None:
    """Вступил (member / administrator / creator / restricted с is_member)."""
    await handle_membership(event)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER))
async def on_left(event: ChatMemberUpdated) -> None:
    """Вышел или удалён (left / kicked / restricted без is_member)."""
    await handle_membership(event)
