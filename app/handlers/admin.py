"""Админ-часть бота: подключение чатов, каналы, настройки, токены, статистика."""
import html
import json
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import action_log, db, keyboards, services, site_db, subscription, tokens
from app.config import ADMINS, PUBLIC_URL

log = logging.getLogger(__name__)
router = Router(name="admin")


def is_bot_admin(user_id: int) -> bool:
    return user_id in ADMINS


class Adm(StatesGroup):
    connect_chat = State()
    channels = State()
    welcome = State()
    repost_channel = State()
    setting_value = State()
    give_tokens = State()


# ------------------------------------------------------------------ вход в меню
@router.message(F.chat.type == ChatType.PRIVATE, Command("admin"))
async def admin_entry(message: Message, state: FSMContext) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("🛠 Панель администратора", reply_markup=keyboards.admin_menu())


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "🏠 Меню пользователя")
async def user_menu(message: Message) -> None:
    await message.answer("Меню пользователя", reply_markup=await keyboards.main_menu())


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "🌐 Веб-панель")
async def web_panel(message: Message) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await message.answer(f"🌐 Лента: {PUBLIC_URL}/\n"
                         f"🛠 Админка: {PUBLIC_URL}/admin")


# ------------------------------------------------------------------ чаты
@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📋 Чаты")
@router.message(F.chat.type == ChatType.PRIVATE, Command("chats"))
async def list_chats(message: Message, state: FSMContext) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await state.clear()
    kb = await keyboards.chats_kb()
    await message.answer(
        "📋 Подключённые чаты.\n\n"
        "Чтобы подключить новый — добавьте бота в чат админом (он подключится сам) "
        "или отправьте /connect_chat &lt;id или @username&gt;.",
        reply_markup=kb)


@router.callback_query(F.data == "chatlist")
async def cb_chatlist(callback: CallbackQuery) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    await callback.message.edit_text("📋 Подключённые чаты:", reply_markup=await keyboards.chats_kb())
    await callback.answer()


async def chat_card(chat) -> str:
    channels = await db.required_channels(chat["chat_id"])
    ch_text = ", ".join(
        f"@{c['username']}" if c["username"] else (c["title"] or str(c["channel_id"]))
        for c in channels) or "не заданы"
    welcome = chat["welcome_message"] or await db.get_setting("welcome_message")
    repost = chat["repost_channel_id"] or "не задан"
    return (f"💬 <b>{html.escape(chat['title'] or str(chat['chat_id']))}</b>\n"
            f"ID: <code>{chat['chat_id']}</code>\n"
            f"Каналы для подписки: {html.escape(ch_text)}\n"
            f"Канал для репостов: <code>{repost}</code>\n\n"
            f"Приветствие:\n<i>{html.escape(welcome)}</i>")


@router.callback_query(F.data.startswith("chat:"))
async def cb_chat(callback: CallbackQuery) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    if not chat:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await callback.message.edit_text(await chat_card(chat),
                                     reply_markup=keyboards.chat_settings_kb(chat_id, chat))
    await callback.answer()


@router.message(F.chat.type == ChatType.PRIVATE, Command("connect_chat"))
async def cmd_connect(message: Message, command: CommandObject, bot: Bot,
                      state: FSMContext) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await state.set_state(Adm.connect_chat)
        await message.answer("Отправьте ID чата (например <code>-1001234567890</code>) "
                             "или @username чата. Бот уже должен быть в нём администратором.")
        return
    await do_connect(message, command.args, bot, state)


@router.message(Adm.connect_chat)
async def connect_input(message: Message, bot: Bot, state: FSMContext) -> None:
    await do_connect(message, message.text or "", bot, state)


async def do_connect(message: Message, raw: str, bot: Bot, state: FSMContext) -> None:
    raw = raw.strip()
    target: str | int = int(raw) if raw.lstrip("-").isdigit() else (
        raw if raw.startswith("@") else "@" + raw)
    try:
        chat = await bot.get_chat(target)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status != "administrator":
            await message.answer("⚠️ Бот должен быть <b>администратором</b> в этом чате "
                                 "(нужны права на удаление сообщений и ограничение участников).")
            return
    except TelegramAPIError as exc:
        await message.answer(f"⚠️ Не удалось получить чат: <code>{html.escape(str(exc))}</code>")
        return
    await db.upsert_chat(chat.id, chat.title or str(chat.id))
    await state.clear()
    row = await db.get_chat(chat.id)
    await message.answer(f"✅ Чат подключён.\n\n{await chat_card(row)}",
                         reply_markup=keyboards.chat_settings_kb(chat.id, row))


@router.callback_query(F.data.startswith("offchat:"))
async def cb_off(callback: CallbackQuery) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    await db.execute("UPDATE chats SET is_active = 0 WHERE chat_id = ?", (chat_id,))
    await callback.message.edit_text("Чат отключён.", reply_markup=await keyboards.chats_kb())
    await callback.answer()


# ------------------------------------------------------------------ каналы для подписки
@router.callback_query(F.data.startswith("setch:"))
async def cb_setch(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    await state.set_state(Adm.channels)
    await state.update_data(chat_id=chat_id)
    await callback.message.answer(
        "📢 Отправьте список каналов через пробел или запятую.\n"
        "Форматы: <code>@channel</code>, <code>https://t.me/channel</code>, "
        "<code>-1001234567890</code>.\n"
        "Бот должен быть участником/админом каждого канала — это проверяется.\n\n"
        "Чтобы очистить список — отправьте <code>-</code>.")
    await callback.answer()


@router.message(Adm.channels)
async def channels_input(message: Message, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    raw = (message.text or "").strip()
    if raw == "-":
        await db.set_required_channels(chat_id, [])
        await state.clear()
        await message.answer("Список каналов очищен.")
        return

    idents = [x for x in raw.replace(",", " ").split() if x]
    ok, bad = [], []
    for ident in idents:
        info = await subscription.verify_channel(bot, ident)
        (ok if info else bad).append(info or ident)
    if not ok:
        await message.answer("⚠️ Ни один канал не подтверждён. Добавьте бота в канал "
                             "(лучше администратором) и повторите.")
        return
    await db.set_required_channels(chat_id, ok)
    await state.clear()
    names = ", ".join(f"@{c['username']}" if c["username"] else c["title"] for c in ok)
    text = f"✅ Каналы сохранены: {html.escape(names)}"
    if bad:
        text += f"\n⚠️ Не удалось проверить: {html.escape(', '.join(map(str, bad)))}"
    chat = await db.get_chat(chat_id)
    await message.answer(text, reply_markup=keyboards.chat_settings_kb(chat_id, chat))


# ------------------------------------------------------------------ приветствие
@router.callback_query(F.data.startswith("setwelcome:"))
async def cb_setwelcome(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    await state.set_state(Adm.welcome)
    await state.update_data(chat_id=chat_id)
    await callback.message.answer(
        "✏️ Отправьте текст приветствия.\n"
        "Плейсхолдеры: <code>%USER%</code> — упоминание юзера, "
        "<code>%CHANNEL_NAME%</code> — список каналов.\n\n"
        "Отправьте <code>-</code>, чтобы использовать глобальный шаблон.")
    await callback.answer()


@router.message(Adm.welcome)
async def welcome_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    value = None if (message.text or "").strip() == "-" else (message.html_text or message.text)
    await db.execute("UPDATE chats SET welcome_message = ? WHERE chat_id = ?", (value, chat_id))
    await state.clear()
    chat = await db.get_chat(chat_id)
    await message.answer("✅ Приветствие сохранено.",
                         reply_markup=keyboards.chat_settings_kb(chat_id, chat))


# ------------------------------------------------------------------ переключатели чата
@router.callback_query(F.data.startswith("togglemode:"))
async def cb_mode(callback: CallbackQuery) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    new = "direct" if chat["post_mode"] == "bot_only" else "bot_only"
    await db.execute("UPDATE chats SET post_mode = ? WHERE chat_id = ?", (new, chat_id))
    chat = await db.get_chat(chat_id)
    await callback.message.edit_text(await chat_card(chat),
                                     reply_markup=keyboards.chat_settings_kb(chat_id, chat))
    await callback.answer("Режим постинга: " + ("через бота" if new == "bot_only" else "напрямую"))


@router.callback_query(F.data.startswith("togglerepost:"))
async def cb_repost_mode(callback: CallbackQuery) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    chat = await db.get_chat(chat_id)
    order = ["manual", "auto", "off"]
    new = order[(order.index(chat["repost_mode"] or "manual") + 1) % len(order)]
    await db.execute("UPDATE chats SET repost_mode = ? WHERE chat_id = ?", (new, chat_id))
    chat = await db.get_chat(chat_id)
    await callback.message.edit_text(await chat_card(chat),
                                     reply_markup=keyboards.chat_settings_kb(chat_id, chat))
    await callback.answer(f"Репост: {new}")


@router.callback_query(F.data.startswith("setrepostch:"))
async def cb_set_repost_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.split(":")[1])
    await state.set_state(Adm.repost_channel)
    await state.update_data(chat_id=chat_id)
    await callback.message.answer("📡 Отправьте @username или ID канала для репостов "
                                  "(бот должен быть его администратором). "
                                  "<code>-</code> — убрать канал.")
    await callback.answer()


@router.message(Adm.repost_channel)
async def repost_channel_input(message: Message, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    raw = (message.text or "").strip()
    if raw == "-":
        await db.execute("UPDATE chats SET repost_channel_id = NULL WHERE chat_id = ?", (chat_id,))
        await state.clear()
        await message.answer("Канал для репостов убран.")
        return
    info = await subscription.verify_channel(bot, raw)
    if not info:
        await message.answer("⚠️ Канал не найден или бот в нём не состоит.")
        return
    await db.execute("UPDATE chats SET repost_channel_id = ? WHERE chat_id = ?",
                     (info["channel_id"], chat_id))
    await state.clear()
    chat = await db.get_chat(chat_id)
    await message.answer(f"✅ Канал для репостов: {info['title']}",
                         reply_markup=keyboards.chat_settings_kb(chat_id, chat))


# ------------------------------------------------------------------ глобальные настройки
SETTING_TITLES = {
    "msg_ttl": "Время видимости сообщений бота (сек.)",
    "check_limit": "Лимит последовательных проверок подписки",
    "restrict_hours": "Время блокировки (часы)",
    "restricted_text": "Текст для заблокированного юзера",
    "signup_bonus": "Токенов за подписку",
    "referral_bonus": "Токенов за приглашённого друга",
    "message_cost": "Стоимость одного сообщения",
    "token_packages": "Пакеты токенов (JSON)",
    "welcome_message": "Глобальное приветствие",
    "no_tokens_text": "Текст при нехватке коинов",
    "bot_only_text": "Текст в режиме «только через бота»",
}
NUMERIC_SETTINGS = {"msg_ttl", "check_limit", "restrict_hours", "signup_bonus",
                    "referral_bonus", "message_cost"}


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "⚙️ Глобальные настройки")
@router.message(F.chat.type == ChatType.PRIVATE, Command("settings"))
async def show_settings(message: Message, state: FSMContext) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await state.clear()
    s = await db.all_settings()
    await message.answer(
        "⚙️ <b>Глобальные настройки</b>\n"
        f"⏱ Видимость сообщений: <b>{s['msg_ttl']}</b> сек.\n"
        f"🔢 Лимит проверок: <b>{s['check_limit']}</b>\n"
        f"⛔️ Блокировка: <b>{s['restrict_hours']}</b> ч.\n"
        f"🎁 Бонус за подписку: <b>{s['signup_bonus']}</b>\n"
        f"👥 Бонус за друга: <b>{s['referral_bonus']}</b>\n"
        f"💸 Стоимость сообщения: <b>{s['message_cost']}</b>\n\n"
        f"Текст блокировки:\n<i>{html.escape(s['restricted_text'])}</i>",
        reply_markup=keyboards.settings_kb())


@router.callback_query(F.data.startswith("gs:"))
async def cb_setting(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_bot_admin(callback.from_user.id):
        return
    key = callback.data.split(":")[1]
    current = await db.get_setting(key)
    await state.set_state(Adm.setting_value)
    await state.update_data(key=key)
    await callback.message.answer(
        f"<b>{SETTING_TITLES.get(key, key)}</b>\n"
        f"Текущее значение:\n<code>{html.escape(str(current))}</code>\n\n"
        f"Отправьте новое значение.")
    await callback.answer()


@router.message(Adm.setting_value)
async def setting_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data["key"]
    value = (message.html_text if key not in NUMERIC_SETTINGS else message.text or "").strip()
    if key in NUMERIC_SETTINGS:
        if not value.lstrip("-").isdigit():
            await message.answer("⚠️ Нужно число. Попробуйте ещё раз.")
            return
        value = str(max(0, int(value)))
    if key == "token_packages":
        try:
            parsed = json.loads(message.text)
            assert isinstance(parsed, list)
            value = json.dumps(parsed, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            await message.answer('⚠️ Нужен JSON вида '
                                 '<code>[{"stars": 50, "tokens": 50}]</code>')
            return
    await db.set_setting(key, value)
    await state.clear()
    await message.answer(f"✅ Сохранено: <b>{SETTING_TITLES.get(key, key)}</b> = "
                         f"<code>{html.escape(str(value))}</code>",
                         reply_markup=keyboards.admin_menu())


# --- короткие команды под спецификацию ---------------------------------
async def _set_numeric(message: Message, command: CommandObject, key: str, label: str) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer(f"Текущее значение {label}: <b>{await db.get_setting(key)}</b>\n"
                             f"Использование: /{key} 10")
        return
    await db.set_setting(key, max(0, int(command.args.strip())))
    await message.answer(f"✅ {label}: <b>{await db.get_setting(key)}</b>")


@router.message(F.chat.type == ChatType.PRIVATE, Command("msg_ttl"))
async def cmd_ttl(message: Message, command: CommandObject) -> None:
    await _set_numeric(message, command, "msg_ttl", "время видимости сообщений (сек.)")


@router.message(F.chat.type == ChatType.PRIVATE, Command("check_limit"))
async def cmd_limit(message: Message, command: CommandObject) -> None:
    await _set_numeric(message, command, "check_limit", "лимит проверок")


@router.message(F.chat.type == ChatType.PRIVATE, Command("restrict_hours"))
async def cmd_hours(message: Message, command: CommandObject) -> None:
    await _set_numeric(message, command, "restrict_hours", "время блокировки (ч.)")


@router.message(F.chat.type == ChatType.PRIVATE, Command("message_cost"))
async def cmd_cost(message: Message, command: CommandObject) -> None:
    await _set_numeric(message, command, "message_cost", "стоимость сообщения")


@router.message(F.chat.type == ChatType.PRIVATE, Command("signup_bonus"))
async def cmd_signup(message: Message, command: CommandObject) -> None:
    await _set_numeric(message, command, "signup_bonus", "бонус за подписку")


@router.message(F.chat.type == ChatType.PRIVATE, Command("referral_bonus"))
async def cmd_refbonus(message: Message, command: CommandObject) -> None:
    await _set_numeric(message, command, "referral_bonus", "бонус за друга")


@router.message(F.chat.type == ChatType.PRIVATE, Command("restricted_text"))
async def cmd_restricted_text(message: Message, command: CommandObject) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer(f"Текущий текст:\n<i>{html.escape(await db.get_setting('restricted_text'))}</i>\n\n"
                             f"Использование: /restricted_text ваш текст")
        return
    await db.set_setting("restricted_text", command.args)
    await message.answer("✅ Текст сохранён.")


# ------------------------------------------------------------------ выдача токенов
@router.message(F.chat.type == ChatType.PRIVATE, F.text == "💰 Выдать коины")
async def give_prompt(message: Message, state: FSMContext) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await state.set_state(Adm.give_tokens)
    await message.answer("Отправьте: <code>@username 100</code> или <code>123456789 100</code>.\n"
                         "Отрицательное число — списание.")


@router.message(Adm.give_tokens)
async def give_input(message: Message, state: FSMContext) -> None:
    await do_give(message, message.text or "")
    await state.clear()


@router.message(F.chat.type == ChatType.PRIVATE, Command("give"))
async def cmd_give(message: Message, command: CommandObject) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await do_give(message, command.args or "")


async def do_give(message: Message, raw: str, ) -> None:
    parts = raw.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: <code>@username 100</code>")
        return
    ident, amount = parts[0], int(parts[1])
    user = (await db.get_user(int(ident)) if ident.lstrip("-").isdigit()
            else await db.get_user_by_username(ident))
    if not user:
        await message.answer("Пользователь не найден. Он должен хотя бы раз написать боту "
                             "или в чат, чтобы попасть в базу.")
        return
    balance = await tokens.add(int(user["user_id"]), amount, "admin_grant",
                               {"by": message.from_user.id})
    await message.answer(f"✅ @{user['username'] or user['user_id']}: {amount:+d} коинов. "
                         f"Баланс: <b>{balance}</b>.")
    try:
        await message.bot.send_message(
            int(user["user_id"]),
            f"💰 Администратор {'начислил' if amount > 0 else 'списал'} "
            f"<b>{abs(amount)}</b> коин(ов). Баланс: <b>{balance}</b>.")
    except TelegramAPIError:
        pass


# ------------------------------------------------------------------ статистика
@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📊 Статистика")
@router.message(F.chat.type == ChatType.PRIVATE, Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    users = await db.scalar("SELECT COUNT(*) FROM users")
    activated = await db.scalar("SELECT COUNT(*) FROM users WHERE activated = 1")
    balance_sum = await db.scalar("SELECT COALESCE(SUM(tokens),0) FROM users")
    msgs = await db.scalar("SELECT COUNT(*) FROM chat_messages WHERE status = 'published'")
    deleted = await db.scalar("SELECT COUNT(*) FROM chat_messages WHERE status = 'deleted'")
    restricted = await db.scalar(
        "SELECT COUNT(*) FROM user_chat_state WHERE restricted_until > datetime('now')")
    paid = await db.scalar("SELECT COALESCE(SUM(tokens),0) FROM payments")
    revenue = await db.scalar("SELECT COALESCE(SUM(amount),0) FROM payments")
    refs = await db.scalar("SELECT COUNT(*) FROM users WHERE referral_rewarded = 1")
    chats = await db.scalar("SELECT COUNT(*) FROM chats WHERE is_active = 1")
    await message.answer(
        f"📊 <b>Статистика</b>\n"
        f"Чатов подключено: <b>{chats}</b>\n"
        f"Пользователей: <b>{users}</b> (активировались: {activated})\n"
        f"Сейчас ограничено: <b>{restricted}</b>\n"
        f"Сообщений опубликовано: <b>{msgs}</b> (удалено: {deleted})\n"
        f"Коинов на балансах: <b>{balance_sum}</b>\n"
        f"Куплено коинов: <b>{paid}</b> (оплат на {revenue})\n"
        f"Успешных рефералов: <b>{refs}</b>\n\n"
        f"Подробно: {PUBLIC_URL}/admin")


# ------------------------------------------------------------------ модерация в чате
@router.message(F.chat.type.in_({"group", "supergroup"}), Command("del"))
async def cmd_del(message: Message, bot: Bot) -> None:
    """Удалить сообщение (reply) и вернуть автору токены."""
    if not await services.is_chat_admin(bot, message.chat.id, message.from_user.id):
        return
    target = message.reply_to_message
    await services.delete_quiet(bot, message.chat.id, message.message_id)
    if not target:
        await services.send_temp(bot, message.chat.id, "Команду нужно отправить ответом "
                                                       "на удаляемое сообщение.")
        return
    refunded = await services.refund_message(bot, message.chat.id, target.message_id,
                                             reason=f"admin:{message.from_user.id}")
    await services.delete_quiet(bot, message.chat.id, target.message_id)
    await action_log.action(message.chat.id, message.from_user.id, message.from_user.username,
                            f"удалено сообщение {target.message_id}, возврат {refunded}",
                            event="admin_delete")
    await services.send_temp(bot, message.chat.id,
                             f"🗑 Сообщение удалено. Возвращено коинов: <b>{refunded}</b>.")


@router.message(F.chat.type.in_({"group", "supergroup"}), Command("repost"))
async def cmd_repost(message: Message, bot: Bot) -> None:
    """Ручной отбор сообщения в канал (режим repost_mode = manual)."""
    if not await services.is_chat_admin(bot, message.chat.id, message.from_user.id):
        return
    target = message.reply_to_message
    await services.delete_quiet(bot, message.chat.id, message.message_id)
    if not target:
        await services.send_temp(bot, message.chat.id, "Ответьте этой командой на сообщение.")
        return
    posted = await services.repost_to_channel(bot, message.chat.id, target.message_id)
    await services.send_temp(
        bot, message.chat.id,
        "✅ Отправлено в канал." if posted else
        "⚠️ Канал для репостов не задан или бот не может в него писать.")


@router.message(F.chat.type.in_({"group", "supergroup"}), Command("unrestrict"))
async def cmd_unrestrict(message: Message, bot: Bot) -> None:
    if not await services.is_chat_admin(bot, message.chat.id, message.from_user.id):
        return
    await services.delete_quiet(bot, message.chat.id, message.message_id)
    target = message.reply_to_message
    if not target or not target.from_user:
        return
    uid = target.from_user.id
    await db.update_state(uid, message.chat.id, restricted_until=None, fail_streak=0)
    try:
        from aiogram.types import ChatPermissions
        await bot.restrict_chat_member(
            message.chat.id, uid,
            permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True,
                                        can_send_polls=True, can_add_web_page_previews=True))
    except TelegramAPIError:
        pass
    await services.send_temp(bot, message.chat.id, "✅ Ограничение снято.")


# ------------------------------------------------------------------ русские алиасы команд
RU_ALIASES = {
    "подключить": "connect",     # /подключить <id|@username>
    "чаты": "chats",
    "каналы": "channels",        # /каналы — выбрать чат и задать каналы
    "приветствие": "welcome",
    "настройки": "settings",
    "время": "msg_ttl",          # /время 45
    "лимит": "check_limit",      # /лимит 10
    "блокировка": "restrict_hours",
    "текст": "restricted_text",
    "выдать": "give",            # /выдать @user 100
    "статистика": "stats",
    "помощь": "help",
    "канал": "set_channel",      # /канал @channel — канал объявлений
    "цены": "prices",            # /цены — канал, цены и статистика объявлений
    "правила": "rules",          # /правила — текущий текст правил
}

HELP_TEXT = (
    "🛠 <b>Команды администратора</b>\n\n"
    "<b>Чаты</b>\n"
    "/connect_chat &lt;id|@username&gt; — подключить чат (/подключить)\n"
    "/chats — список чатов и их настройки (/чаты)\n\n"
    "<b>Глобальные настройки</b>\n"
    "/settings — все настройки кнопками (/настройки)\n"
    "/msg_ttl 45 — время видимости сообщений бота (/время)\n"
    "/check_limit 10 — лимит проверок подписки (/лимит)\n"
    "/restrict_hours 48 — время блокировки (/блокировка)\n"
    "/restricted_text ... — текст для заблокированных (/текст)\n"
    "/signup_bonus 30, /referral_bonus 10, /message_cost 1 — экономика коинов\n\n"
    "<b>Токены и статистика</b>\n"
    "/give @user 100 — начислить или списать коины (/выдать)\n"
    "/stats — сводка (/статистика)\n"
    "/refund &lt;charge_id&gt; — возврат оплаты Stars\n\n"
    "<b>В самом чате, ответом на сообщение</b>\n"
    "/del — удалить сообщение и вернуть автору коины\n"
    "/repost — отправить сообщение в канал\n"
    "/unrestrict — снять ограничение с автора"
)


@router.message(F.chat.type == ChatType.PRIVATE, Command("help"))
async def cmd_help(message: Message) -> None:
    if not is_bot_admin(message.from_user.id):
        await message.answer("/start — меню, /balance — баланс, /buy — купить коины, "
                             "/ref — пригласить друга, /site — лента на сайте")
        return
    await message.answer(HELP_TEXT)


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"^/[а-яА-ЯёЁ]+"))
async def ru_alias(message: Message, state: FSMContext, bot: Bot) -> None:
    """Команды из ТЗ на русском: /подключить, /каналы, /время, /лимит, /выдать и т. д."""
    if not is_bot_admin(message.from_user.id):
        return
    head, _, args = (message.text or "").partition(" ")
    action = RU_ALIASES.get(head[1:].lower())
    if not action:
        await message.answer("Неизвестная команда. /help — список.")
        return

    args = args.strip()
    fake = CommandObject(prefix="/", command=action, args=args or None)

    if action == "connect":
        await cmd_connect(message, fake, bot, state)
    elif action == "chats":
        await list_chats(message, state)
    elif action in {"channels", "welcome"}:
        await list_chats(message, state)
        await message.answer("Выберите чат выше, затем нужный пункт настроек.")
    elif action == "settings":
        await show_settings(message, state)
    elif action in NUMERIC_SETTINGS:
        await _set_numeric(message, fake, action, SETTING_TITLES.get(action, action))
    elif action == "restricted_text":
        await cmd_restricted_text(message, fake)
    elif action == "give":
        await do_give(message, args)
    elif action == "stats":
        await cmd_stats(message)
    elif action == "set_channel":
        if not args:
            await message.answer("Использование: <code>/канал @channel</code> "
                                 "или <code>/канал -100...</code>")
            return
        await do_set_channel(message, args, bot)
    elif action in {"prices", "rules"}:
        await show_channel_prices(message, state)
    elif action == "help":
        await cmd_help(message)


# ------------------------------------------------------------------ канал объявлений и цены (п.15/ТЗ)
# Цены и текст правил редактируются уже существующим механизмом gs:<ключ> (см. cb_setting выше и
# keyboards.settings_kb()) — здесь только регистрируем подписи и признак «числовое значение».
SETTING_TITLES.update({
    "price_post": "Цена объявления",
    "price_image": "Доплата за картинку",
    "price_pin_4h": "Закреп на 4 часа",
    "price_pin_8h": "Закреп на 8 часов",
    "rules_text": "Текст правил",
})
NUMERIC_SETTINGS.update({"price_post", "price_image", "price_pin_4h", "price_pin_8h"})


@router.message(F.chat.type == ChatType.PRIVATE, Command("set_channel"))
async def cmd_set_channel(message: Message, command: CommandObject, bot: Bot) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Использование: <code>/set_channel @channel</code> "
                             "или <code>/set_channel -1001234567890</code>")
        return
    await do_set_channel(message, command.args.strip(), bot)


async def do_set_channel(message: Message, raw: str, bot: Bot) -> None:
    """Задаёт канал объявлений: проверяет, что бот в нём состоит и что он администратор."""
    info = await subscription.verify_channel(bot, raw)
    if not info:
        await message.answer("⚠️ Канал не найден или бот в нём не состоит. "
                             "Добавьте бота в канал и повторите команду.")
        return
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(info["channel_id"], me.id)
    except TelegramAPIError as exc:
        await message.answer(f"⚠️ Не удалось проверить права бота: <code>{html.escape(str(exc))}</code>")
        return
    if member.status != "administrator":
        await message.answer(
            "⚠️ Бот подключён к каналу, но не является в нём <b>администратором</b> — "
            "без этого он не сможет публиковать, закреплять и удалять объявления. "
            "Выдайте боту права администратора в канале и повторите команду.")
        return
    title = info["title"] or str(info["channel_id"])
    await db.set_setting("ad_channel_id", info["channel_id"])
    await db.set_setting("ad_channel_title", title)
    # ссылка для кнопки «Канал Aff Bazaar» в меню пользователя
    await db.set_setting("ad_channel_username", info.get("username") or "")
    await db.set_setting("ad_channel_link", info.get("invite_link") or "")
    await message.answer(f"✅ Канал объявлений: {html.escape(title)} "
                         f"(<code>{info['channel_id']}</code>)")


async def channel_prices_text() -> str:
    channel_id = await db.get_int("ad_channel_id")
    channel_title = await db.get_setting("ad_channel_title")
    channel_line = (f"{html.escape(channel_title or str(channel_id))} (<code>{channel_id}</code>)"
                    if channel_id else "не задан — /set_channel @channel (/канал)")
    published = await db.scalar("SELECT COUNT(*) FROM ads WHERE status = 'published'")
    deleted = await db.scalar("SELECT COUNT(*) FROM ads WHERE status = 'deleted'")
    refunded = await db.scalar(
        "SELECT COALESCE(SUM(cost_total), 0) FROM ads WHERE refunded = 1")
    rules_text = await db.get_setting("rules_text")
    return (
        "📢 <b>Канал и цены объявлений</b>\n\n"
        f"Канал: {channel_line}\n\n"
        f"💰 Объявление: <b>{await db.get_int('price_post')}</b> коинов\n"
        f"🖼 Доплата за картинку: <b>{await db.get_int('price_image')}</b> коинов\n"
        f"📌 Закреп на 4 часа: <b>{await db.get_int('price_pin_4h')}</b> коинов\n"
        f"📌 Закреп на 8 часов: <b>{await db.get_int('price_pin_8h')}</b> коинов\n\n"
        f"📊 Опубликовано объявлений: <b>{published}</b>\n"
        f"🗑 Удалено: <b>{deleted}</b>\n"
        f"↩️ Возвращено коинов: <b>{refunded}</b>\n\n"
        "Изменить цену или текст правил — кнопками ниже.\n\n"
        f"📜 <b>Текущий текст правил:</b>\n{rules_text}")


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "📢 Канал и цены")
async def show_channel_prices(message: Message, state: FSMContext) -> None:
    if not is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(await channel_prices_text(), reply_markup=keyboards.settings_kb())


# Примечание: отдельной команды /rules здесь нет — обычная (пользовательская) версия
# уже зарегистрирована в post.py (Command("rules")), а admin.router подключается в bot.py
# раньше post.router. Если бы /rules был продублирован и здесь, он бы перехватывал
# команду для всех приватных чатов (aiogram останавливает обход роутеров на первом
# сматчившемся хендлере независимо от его тела) и «глушил» её для обычных пользователей.
# Поэтому для админа правила показываются через меню «📢 Канал и цены» (см. выше) и
# алиас /правила (см. ru_alias).
