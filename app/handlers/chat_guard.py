"""Проверка сообщений в подключённых чатах: подписка, лимиты, токены."""
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message

from app import action_log, db, keyboards, services, site_db, subscription, tokens

log = logging.getLogger(__name__)
router = Router(name="chat_guard")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def guard(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    chat = await db.get_chat(chat_id)
    if not chat or not chat["is_active"]:
        return  # чат не подключён — бот молчит

    user = message.from_user
    if user is None or user.is_bot:
        return
    if message.sender_chat is not None:
        return  # анонимный админ / от имени канала

    # Сервисные сообщения (вход/выход) не проверяем, но чистим за собой не нужно.
    if message.content_type in {"new_chat_members", "left_chat_member",
                                "pinned_message", "forum_topic_created"}:
        return

    await db.upsert_user(user.id, user.username, user.full_name,
                         user.first_name, user.last_name)
    text = services.message_text(message)
    await action_log.action(chat_id, user.id, user.username, text)

    if await services.is_chat_admin(bot, chat_id, user.id):
        return  # админов не трогаем

    # --- 0. Уже ограничен -------------------------------------------------
    until = await db.is_restricted(user.id, chat_id)
    if until:
        await services.delete_quiet(bot, chat_id, message.message_id)
        body = await services.render(await db.get_setting("restricted_text"), user)
        await services.send_temp(bot, chat_id, body, user.id)
        await action_log.action(chat_id, user.id, user.username, text, event="удалено: ограничен")
        return

    # --- 1. Проверка подписки --------------------------------------------
    missing = await subscription.missing_channels(bot, user.id, chat_id)
    state = await db.get_state(user.id, chat_id)

    if missing:
        await services.delete_quiet(bot, chat_id, message.message_id)
        streak = int(state["fail_streak"] or 0) + 1
        limit = await db.get_int("check_limit")
        await db.update_state(user.id, chat_id, fail_streak=streak, subscribed=0,
                              total_checks=int(state["total_checks"] or 0) + 1,
                              last_check_at=db.iso(db.utcnow()))

        if streak >= limit:
            hours = await db.get_int("restrict_hours")
            until = await db.restrict_user(user.id, chat_id, hours, "лимит проверок подписки")
            try:
                await bot.restrict_chat_member(
                    chat_id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
            except TelegramAPIError as exc:
                log.warning("restrict_chat_member не удался: %s", exc)
            await action_log.restricted(chat_id, user.id, user.username, db.iso(until), streak)
            body = await services.render(await db.get_setting("restricted_text"), user,
                                         hours=hours, until=db.iso(until))
            await services.send_temp(bot, chat_id, body, user.id)
            return

        welcome = chat["welcome_message"] or await db.get_setting("welcome_message")
        body = await services.render(welcome, user, missing)
        me = await bot.get_me()
        kb = await keyboards.subscribe_kb(missing, me.username)
        await services.send_temp(bot, chat_id, body, user.id, reply_markup=kb)
        await action_log.action(chat_id, user.id, user.username, text,
                                event=f"удалено: нет подписки ({streak}/{limit})")
        return

    # --- подписан: сбрасываем счётчик, выдаём бонус при первой активации ---
    await db.update_state(user.id, chat_id, fail_streak=0, subscribed=1,
                          total_checks=int(state["total_checks"] or 0) + 1,
                          last_check_at=db.iso(db.utcnow()))
    await activate_if_needed(bot, user)

    # --- 2. Режим "писать только через бота" ------------------------------
    if chat["post_mode"] == "bot_only":
        await services.delete_quiet(bot, chat_id, message.message_id)
        me = await bot.get_me()
        body = await services.render(await db.get_setting("bot_only_text"), user)
        await services.send_temp(bot, chat_id, body, user.id,
                                 reply_markup=keyboards.topup_kb(me.username))
        return

    # --- 3. Списание токенов ----------------------------------------------
    cost = await db.get_int("message_cost")
    if cost > 0:
        ok = await tokens.charge(user.id, cost, "message",
                                 {"chat_id": chat_id, "message_id": message.message_id})
        if not ok:
            await services.delete_quiet(bot, chat_id, message.message_id)
            me = await bot.get_me()
            body = await services.render(await db.get_setting("no_tokens_text"), user)
            await services.send_temp(bot, chat_id, body, user.id,
                                     reply_markup=keyboards.topup_kb(me.username))
            await action_log.action(chat_id, user.id, user.username, text,
                                    event="удалено: нет коинов")
            return

    # --- 4. Сообщение остаётся: учёт + зеркало на сайт --------------------
    await services.record_message(message, cost, chat_title=chat["title"])

    if chat["repost_mode"] == "auto":
        await services.repost_to_channel(bot, chat_id, message.message_id)


async def activate_if_needed(bot: Bot, user) -> None:
    """Первая успешная проверка подписки: бонус юзеру и награда пригласившему."""
    row = await db.get_user(user.id)
    if row and row["activated"]:
        return
    bonus = await tokens.grant_signup_bonus(user.id)
    if bonus:
        try:
            await bot.send_message(
                user.id,
                f"🎉 Подписка подтверждена! Начислено <b>{bonus}</b> коинов.\n"
                f"Ими оплачиваются сообщения в чате.")
        except TelegramAPIError:
            pass
    referrer_id, ref_bonus = await tokens.reward_referrer(user.id)
    if referrer_id and ref_bonus:
        try:
            await bot.send_message(
                referrer_id,
                f"👥 Твой друг {services.user_mention(user)} активировался — "
                f"начислено <b>{ref_bonus}</b> коинов.")
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "check_sub")
async def check_sub(callback, bot: Bot) -> None:
    """Кнопка «Я подписался» под сообщением в чате."""
    chat_id = callback.message.chat.id if callback.message else None
    user = callback.from_user
    if chat_id is None:
        await callback.answer()
        return
    subscription.invalidate(user.id, chat_id)
    missing = await subscription.missing_channels(bot, user.id, chat_id)
    if missing:
        await callback.answer("Подписка не найдена. Проверь, что подписан на все каналы.",
                              show_alert=True)
        return
    await db.update_state(user.id, chat_id, fail_streak=0, subscribed=1)
    await activate_if_needed(bot, user)
    balance = await tokens.balance(user.id)
    await callback.answer(f"Готово! Можешь писать. Баланс: {balance} коинов.", show_alert=True)
    state = await db.get_state(user.id, chat_id)
    if state["last_prompt_msg_id"]:
        await services.delete_quiet(bot, chat_id, state["last_prompt_msg_id"])
        await db.update_state(user.id, chat_id, last_prompt_msg_id=None)


@router.my_chat_member()
async def on_bot_added(event, bot: Bot) -> None:
    """Бот добавлен в группу — регистрируем чат, если добавил админ бота."""
    from app.config import ADMINS

    chat = event.chat
    if chat.type not in {"group", "supergroup"}:
        return
    if event.new_chat_member.status in {"administrator", "member"}:
        if event.from_user and event.from_user.id in ADMINS:
            await db.upsert_chat(chat.id, chat.title or str(chat.id))
            try:
                await bot.send_message(
                    event.from_user.id,
                    f"✅ Чат <b>{chat.title}</b> (<code>{chat.id}</code>) подключён.\n"
                    f"Теперь задайте список каналов: «📋 Чаты» → выбрать чат → «Каналы для подписки».")
            except TelegramAPIError:
                pass
    elif event.new_chat_member.status in {"left", "kicked"}:
        await db.execute("UPDATE chats SET is_active = 0 WHERE chat_id = ?", (chat.id,))
