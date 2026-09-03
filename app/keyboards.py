"""Клавиатуры бота."""
import json
from typing import Optional

from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

from app import db, subscription


async def subscribe_kb(missing: list, bot_username: str) -> InlineKeyboardMarkup:
    rows = []
    for ch in missing:
        username = subscription._field(ch, "username")
        link = f"https://t.me/{username}" if username else subscription._field(ch, "invite_link")
        title = subscription._field(ch, "title") or "Канал"
        if link:
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
    rows.append([InlineKeyboardButton(text="✅ Я подписался",
                                      callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_kb(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 Пополнить токены",
                             url=f"https://t.me/{bot_username}?start=topup")
    ]])


BTN_CHANNEL = "📣 Канал Aff Bazaar"
BTN_SITE = "🌐 Наш сайт"


def main_menu() -> ReplyKeyboardMarkup:
    # Mini App пока убрана (кнопка web_app и Menu Button): вместо неё две ссылки — канал и сайт.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHANNEL), KeyboardButton(text=BTN_SITE)],
            [KeyboardButton(text="📢 Создать объявление")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="💎 Купить коины")],
            [KeyboardButton(text="👥 Пригласить друга"), KeyboardButton(text="📊 Мой профиль")],
            [KeyboardButton(text="📜 Правила")],
        ],
        resize_keyboard=True,
    )


def links_kb(channel_url: Optional[str], site_url: str) -> InlineKeyboardMarkup:
    """Кнопки-ссылки «в канал» и «на сайт» (reply-кнопки не умеют открывать URL)."""
    rows = []
    if channel_url:
        rows.append([InlineKeyboardButton(text=BTN_CHANNEL, url=channel_url)])
    rows.append([InlineKeyboardButton(text=BTN_SITE, url=site_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Канал и цены"), KeyboardButton(text="📋 Чаты")],
            [KeyboardButton(text="⚙️ Глобальные настройки")],
            [KeyboardButton(text="💰 Выдать коины"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🌐 Веб-панель"), KeyboardButton(text="🏠 Меню пользователя")],
        ],
        resize_keyboard=True,
    )


async def packages_kb() -> InlineKeyboardMarkup:
    raw = await db.get_setting("token_packages")
    try:
        packages = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        packages = []
    rows = [[InlineKeyboardButton(text=f"{p['tokens']} коинов — {p['stars']} ⭐",
                                  callback_data=f"buy:{i}")]
            for i, p in enumerate(packages)]
    return InlineKeyboardMarkup(inline_keyboard=rows or
                                [[InlineKeyboardButton(text="Пакеты не настроены",
                                                       callback_data="noop")]])


async def chats_kb(prefix: str = "chat") -> InlineKeyboardMarkup:
    rows = []
    for chat in await db.active_chats():
        rows.append([InlineKeyboardButton(
            text=f"{chat['title'] or chat['chat_id']}",
            callback_data=f"{prefix}:{chat['chat_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows or
                                [[InlineKeyboardButton(text="Нет подключённых чатов",
                                                       callback_data="noop")]])


def chat_settings_kb(chat_id: int, chat) -> InlineKeyboardMarkup:
    post_mode = "через бота" if chat["post_mode"] == "bot_only" else "напрямую"
    repost = {"manual": "вручную", "auto": "авто", "off": "выкл"}.get(chat["repost_mode"], "вручную")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Каналы для подписки", callback_data=f"setch:{chat_id}")],
        [InlineKeyboardButton(text="✏️ Приветственное сообщение", callback_data=f"setwelcome:{chat_id}")],
        [InlineKeyboardButton(text=f"📝 Режим постинга: {post_mode}", callback_data=f"togglemode:{chat_id}")],
        [InlineKeyboardButton(text=f"🔁 Репост в канал: {repost}", callback_data=f"togglerepost:{chat_id}")],
        [InlineKeyboardButton(text="📡 Канал для репостов", callback_data=f"setrepostch:{chat_id}")],
        [InlineKeyboardButton(text="🗑 Отключить чат", callback_data=f"offchat:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ К списку чатов", callback_data="chatlist")],
    ])


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Время видимости сообщений", callback_data="gs:msg_ttl")],
        [InlineKeyboardButton(text="🔢 Лимит проверок подписки", callback_data="gs:check_limit")],
        [InlineKeyboardButton(text="⛔️ Время блокировки (часы)", callback_data="gs:restrict_hours")],
        [InlineKeyboardButton(text="📄 Текст для заблокированных", callback_data="gs:restricted_text")],
        [InlineKeyboardButton(text="🎁 Бонус за подписку", callback_data="gs:signup_bonus")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="gs:referral_bonus")],
        [InlineKeyboardButton(text="💸 Стоимость сообщения в чате", callback_data="gs:message_cost")],
        [InlineKeyboardButton(text="📢 Цена объявления", callback_data="gs:price_post")],
        [InlineKeyboardButton(text="🖼 Доплата за картинку", callback_data="gs:price_image")],
        [InlineKeyboardButton(text="📌 Закреп 4 часа", callback_data="gs:price_pin_4h")],
        [InlineKeyboardButton(text="📌 Закреп 8 часов", callback_data="gs:price_pin_8h")],
        [InlineKeyboardButton(text="📜 Текст правил", callback_data="gs:rules_text")],
        [InlineKeyboardButton(text="💎 Пакеты токенов (JSON)", callback_data="gs:token_packages")],
    ])
