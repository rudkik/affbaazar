"""Личка бота: профиль, баланс, рефералы, отправка сообщений в чат через бота."""
import html
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from app import action_log, db, keyboards, services, subscription, texts, tokens
from app.config import ADMINS, PUBLIC_URL

log = logging.getLogger(__name__)
router = Router(name="user")
router.message.filter(F.chat.type == ChatType.PRIVATE)

MENU_BUTTONS = {"💰 Баланс", "💎 Купить коины", "💎 Купить токены", keyboards.BTN_REFERRAL,
                keyboards.BTN_CHANNEL_SITE, keyboards.BTN_BALANCE_BUY,
                "📊 Мой профиль", "📢 Создать объявление", "📜 Правила", "📢 Канал и цены",
                "📋 Чаты", "⚙️ Глобальные настройки", "💰 Выдать токены", "💰 Выдать коины", "📊 Статистика",
                "🌐 Веб-панель", "🏠 Меню пользователя", keyboards.BTN_CHANNEL, keyboards.BTN_SITE,
                keyboards.BTN_MY_ADS, keyboards.BTN_SUPPORT}


def is_menu_button(text: Optional[str]) -> bool:
    """Текст кнопки меню? Кнопка рефералов подписана бонусом («… (Получи 10 коинов!)»),
    поэтому её проверяем по префиксу — иначе нажатие уедет в постинг через бота."""
    if not text:
        return False
    return text in MENU_BUTTONS or text.startswith(keyboards.BTN_REFERRAL)


def _parse_ref(payload: Optional[str]) -> Optional[int]:
    if not payload:
        return None
    payload = payload.strip()
    for prefix in ("ref_", "ref"):
        if payload.startswith(prefix):
            tail = payload[len(prefix):]
            if tail.isdigit():
                return int(tail)
    return None


async def fetch_bio(bot, user_id: int) -> Optional[str]:
    """Био пользователя: у приватного чата get_chat возвращает поле bio.

    Метода может не быть вовсе (фейковый бот в тестах) — тогда просто None.
    """
    getter = getattr(bot, "get_chat", None)
    if getter is None:
        return None
    try:
        chat = await getter(user_id)
    except TelegramAPIError as exc:
        log.debug("Не удалось получить bio для %s: %s", user_id, exc)
        return None
    except Exception:  # noqa: BLE001 — чужая реализация может кинуть что угодно
        return None
    return getattr(chat, "bio", None)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext,
                    bot: Bot) -> None:
    await state.clear()
    user = message.from_user
    bio = await fetch_bio(bot, user.id)
    await db.upsert_user_profile(user.id, username=user.username, first_name=user.first_name,
                                 last_name=user.last_name, full_name=user.full_name, bio=bio)
    # started/started_at фиксируются один раз — при первом /start
    await db.mark_started(user.id)

    referrer_id = _parse_ref(command.args if command else None)
    if referrer_id and referrer_id != user.id:
        row = await db.get_user(user.id)
        # реферер фиксируется один раз и только для нового, ещё не активированного юзера
        if row and not row["referrer_id"] and not row["activated"]:
            if await db.get_user(referrer_id):
                await db.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?",
                                 (referrer_id, user.id))

    # Уже подписан на канал, а коинов за подписку ещё не получал — начисляем при запуске
    # и благодарим (AffBazaar-15). Делаем это до приветствия, чтобы баланс в нём был верным.
    from app.handlers.chat_guard import activate_if_subscribed
    try:
        await activate_if_subscribed(bot, user)
    except Exception:  # noqa: BLE001 — /start обязан ответить при любой ошибке проверки
        log.exception("Проверка подписки при /start не удалась для %s", user.id)

    is_admin = user.id in ADMINS
    kb = keyboards.admin_menu() if is_admin else await keyboards.main_menu()
    chats = await db.active_chats()
    chat_lines = "\n".join(f"• {html.escape(c['title'] or str(c['chat_id']))}" for c in chats) or "—"

    await message.answer(
        await texts.t("txt_start", user, balance=await tokens.balance(user.id),
                      bonus=await db.get_int("signup_bonus"),
                      price=await db.get_int("price_post"), chats=chat_lines),
        reply_markup=kb,
    )
    if command and command.args == "topup":
        await show_packages(message)


@router.message(F.text.in_({keyboards.BTN_BALANCE_BUY, "💰 Баланс"}))
async def show_balance(message: Message) -> None:
    """Баланс и сразу под ним пакеты коинов (AffBazaar-22/23): один раздел вместо двух."""
    from app.config import cryptopay_enabled
    balance = await tokens.balance(message.from_user.id)
    kb = await keyboards.packages_kb() if cryptopay_enabled() else None
    await message.answer(await texts.t("txt_balance", message.from_user, balance=balance,
                                       price=await db.get_int("price_post")), reply_markup=kb)


@router.message(F.text == "📊 Мой профиль")
async def show_profile(message: Message) -> None:
    uid = message.from_user.id
    row = await db.get_user(uid)
    invited = await db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (uid,))
    activated_invited = await db.scalar(
        "SELECT COUNT(*) FROM users WHERE referrer_id = ? AND activated = 1", (uid,))
    spent = await db.scalar(
        "SELECT COALESCE(SUM(-amount),0) FROM token_tx WHERE user_id = ? AND amount < 0", (uid,))
    earned = await db.scalar(
        "SELECT COALESCE(SUM(amount),0) FROM token_tx WHERE user_id = ? AND amount > 0", (uid,))
    violations = await db.user_violations(uid)
    await message.answer(await texts.t(
        "txt_profile", message.from_user, id=uid, balance=row["tokens"] if row else 0,
        sent=row["messages_sent"] if row else 0, earned=earned, spent=spent,
        invited_active=activated_invited, invited=invited,
        deleted=violations["deleted_total"], violations=violations["deleted_by_moderator"],
        activated="да" if row and row["activated"] else "нет"))


@router.message(F.text.startswith(keyboards.BTN_REFERRAL))
async def show_referral(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    uid = message.from_user.id
    bonus = await db.get_int("referral_bonus")
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    invited = await db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (uid,))
    activated_invited = await db.scalar(
        "SELECT COUNT(*) FROM users WHERE referrer_id = ? AND activated = 1", (uid,))
    await message.answer(await texts.t("txt_referral", message.from_user, link=link, bonus=bonus,
                                       invited_active=activated_invited, invited=invited))


@router.message(F.text.in_({"💎 Купить коины", "💎 Купить токены"}))
async def show_packages(message: Message) -> None:
    # Коины продаются только за крипту (Stars убраны, AffBazaar-13): сразу список пакетов.
    from app.config import cryptopay_enabled
    if not cryptopay_enabled():
        await message.answer(await texts.t("txt_buy_disabled", message.from_user))
        return
    await message.answer(await texts.t("txt_buy_menu", message.from_user),
                         reply_markup=await keyboards.packages_kb())


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    await show_balance(message)


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await show_packages(message)


@router.message(Command("ref"))
async def cmd_ref(message: Message, bot: Bot) -> None:
    await show_referral(message, bot)


@router.message(Command("site"))
@router.message(F.text == keyboards.BTN_SITE)
async def cmd_site(message: Message) -> None:
    await message.answer(await texts.t("txt_site", message.from_user, url=f"{PUBLIC_URL}/"),
                         reply_markup=keyboards.links_kb(None, f"{PUBLIC_URL}/"))


@router.message(Command("support"))
@router.message(F.text == keyboards.BTN_SUPPORT)
async def btn_support(message: Message) -> None:
    """Кнопка «Поддержка»: ссылка на бот поддержки (настройка support_link)."""
    link = (await db.get_setting("support_link")).strip()
    if not link:
        await message.answer(await texts.t("txt_channel_none", message.from_user))
        return
    await message.answer(await texts.t("txt_support", message.from_user),
                         reply_markup=keyboards.support_kb(link))


@router.message(F.text == keyboards.BTN_CHANNEL_SITE)
async def btn_channel_site(message: Message, bot: Bot) -> None:
    """Канал и сайт одним сообщением с двумя кнопками-ссылками (AffBazaar-23)."""
    link = await subscription.ad_channel_link(bot)
    site = f"{PUBLIC_URL}/"
    if not link:
        await cmd_site(message)
        return
    title = await db.get_setting("ad_channel_title") or "Aff Bazaar"
    await message.answer(await texts.t("txt_channel_site", message.from_user,
                                       title=html.escape(title), url=link, site=site),
                         reply_markup=keyboards.links_kb(link, site))


@router.message(F.text == keyboards.BTN_CHANNEL)
async def btn_channel(message: Message, bot: Bot) -> None:
    link = await subscription.ad_channel_link(bot)
    if not link:
        await message.answer(await texts.t("txt_channel_none", message.from_user))
        return
    title = await db.get_setting("ad_channel_title") or "Aff Bazaar"
    await message.answer(await texts.t("txt_channel", message.from_user,
                                       title=html.escape(title), url=link),
                         reply_markup=keyboards.links_kb(link, f"{PUBLIC_URL}/"))


# ------------------------------------------------------------------ постинг через бота
# исходные сообщения, ожидающие выбора чата: (user_id, message_id) -> Message
_pending: dict[tuple[int, int], Message] = {}


async def bot_only_chats() -> list:
    return [c for c in await db.active_chats() if c["post_mode"] == "bot_only"]


@router.message(F.content_type.in_({"text", "photo", "video", "animation", "document",
                                    "audio", "voice", "video_note"}))
async def post_via_bot(message: Message, bot: Bot, state: FSMContext) -> None:
    """Сообщение в личке = заявка на публикацию в чат (режим «только через бота»)."""
    if message.text and message.text.startswith("/"):
        return
    if is_menu_button(message.text):
        return
    if await state.get_state() is not None:
        return  # идёт диалог настроек

    chats = await bot_only_chats()
    if not chats:
        await message.answer(await texts.t("txt_post_no_chats", message.from_user))
        return
    if len(chats) == 1:
        await publish(bot, message, chats[0])
        return

    _pending[(message.from_user.id, message.message_id)] = message
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=c["title"] or str(c["chat_id"]),
                              callback_data=f"pub:{c['chat_id']}:{message.message_id}")]
        for c in chats])
    await message.answer("В какой чат опубликовать?", reply_markup=kb)


@router.callback_query(F.data.startswith("pub:"))
async def choose_chat(callback: CallbackQuery, bot: Bot) -> None:
    _, chat_id, message_id = callback.data.split(":")
    chat = await db.get_chat(int(chat_id))
    original = _pending.pop((callback.from_user.id, int(message_id)), None)
    if not chat or original is None:
        await callback.answer("Заявка устарела — отправь сообщение заново.", show_alert=True)
        return
    await services.delete_quiet(bot, callback.message.chat.id, callback.message.message_id)
    await publish(bot, original, chat)
    await callback.answer()


async def publish(bot: Bot, message: Message, chat, source_message_id: Optional[int] = None,
                  author=None) -> None:
    user = author or message.from_user
    chat_id = int(chat["chat_id"])
    await db.upsert_user(user.id, user.username, user.full_name,
                         user.first_name, user.last_name)

    until = await db.is_restricted(user.id, chat_id)
    if until:
        await message.answer(await services.render(await db.get_setting("restricted_text"), user))
        return

    missing = await subscription.missing_channels(bot, user.id, chat_id)
    if missing:
        state = await db.get_state(user.id, chat_id)
        streak = int(state["fail_streak"] or 0) + 1
        limit = await db.get_int("check_limit")
        await db.update_state(user.id, chat_id, fail_streak=streak, subscribed=0)
        if streak >= limit:
            hours = await db.get_int("restrict_hours")
            new_until = await db.restrict_user(user.id, chat_id, hours, "лимит проверок подписки")
            await action_log.restricted(chat_id, user.id, user.username, db.iso(new_until), streak)
            await message.answer(await services.render(
                await db.get_setting("restricted_text"), user, hours=hours))
            return
        me = await bot.get_me()
        await message.answer(
            await services.render(chat["welcome_message"] or await db.get_setting("welcome_message"),
                                  user, missing),
            reply_markup=await keyboards.subscribe_kb(missing, me.username))
        return

    await db.update_state(user.id, chat_id, fail_streak=0, subscribed=1)
    from app.handlers.chat_guard import activate_if_needed
    await activate_if_needed(bot, user)

    cost = await db.get_int("price_post")     # цена одна: объявление = сообщение в чат
    if cost > 0 and await tokens.balance(user.id) < cost:
        me = await bot.get_me()
        await message.answer(await services.render(await db.get_setting("no_tokens_text"), user),
                             reply_markup=await keyboards.packages_kb())
        return

    src_msg_id = source_message_id or message.message_id
    media_type, file_id = services.media_info(message)
    body = services.message_text(message)

    posted = await deliver(bot, chat, user, message, src_msg_id, cost)
    if posted:
        await message.answer(await texts.t("txt_post_published", user, cost=cost,
                                           balance=await tokens.balance(user.id)))


async def deliver(bot: Bot, chat, user, message: Message, src_msg_id: int, cost: int) -> bool:
    """Публикует сообщение пользователя в чат от имени бота."""
    chat_id = int(chat["chat_id"])
    header = f"💬 От {services.user_mention(user)}"
    body = services.message_text(message)
    try:
        if message.content_type == "text":
            sent = await bot.send_message(chat_id, f"{header}:\n\n{html.escape(body)}",
                                          disable_web_page_preview=True)
        else:
            sent = await bot.copy_message(
                chat_id=chat_id, from_chat_id=message.chat.id, message_id=src_msg_id,
                caption=f"{header}" + (f":\n\n{html.escape(body)}" if body else ""))
    except TelegramAPIError as exc:
        log.warning("Публикация в чат %s не удалась: %s", chat_id, exc)
        await message.answer(await texts.t("txt_post_failed", user))
        return False

    if cost > 0:
        await tokens.charge(user.id, cost, "message",
                            {"chat_id": chat_id, "message_id": sent.message_id})

    media_type, file_id = services.media_info(message)
    await db.execute(
        """INSERT INTO chat_messages(chat_id, message_id, user_id, text, media_type,
                                     media_file_id, cost, posted_via_bot, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'published')
           ON CONFLICT(chat_id, message_id) DO UPDATE SET text = excluded.text""",
        (chat_id, sent.message_id, user.id, body, media_type, file_id, cost))
    await db.execute("UPDATE users SET messages_sent = messages_sent + 1 WHERE user_id = ?",
                     (user.id,))
    from app import site_db
    await site_db.mirror_post(
        source_chat_id=chat_id, source_chat_title=chat["title"],
        source_message_id=sent.message_id, author_id=user.id,
        author_username=user.username, author_name=user.full_name,
        text=body, media_type=media_type, media_file_id=file_id)
    await action_log.action(chat_id, user.id, user.username, body, event="опубликовано через бота")

    if chat["repost_mode"] == "auto":
        await services.repost_to_channel(bot, chat_id, sent.message_id)
    return True
