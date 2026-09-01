"""Создание объявления в личке бота.

Мастер (FSM): правила -> подписка -> рубрика -> вертикаль -> текст -> картинка ->
закреп -> подтверждение -> публикация. Каждый шаг можно отменить кнопкой «❌ Отмена».
"""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from app import ads, db, keyboards, locks, subscription, tokens

log = logging.getLogger(__name__)
router = Router(name="post")
router.message.filter(F.chat.type == ChatType.PRIVATE)

MAX_TEXT_LEN = 3500

CANCEL_BTN = InlineKeyboardButton(text="❌ Отмена", callback_data="ads_cancel")


class Post(StatesGroup):
    rules = State()
    subscribe = State()
    ad_type = State()
    vertical = State()
    text = State()
    image_choice = State()
    image = State()
    pin = State()
    confirm = State()


# ------------------------------------------------------------------ клавиатуры
def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows + [[CANCEL_BTN]])


def _cols(items, prefix: str) -> InlineKeyboardMarkup:
    """Раскладка рубрик/вертикалей в 2 колонки + отмена."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for item in items:
        row.append(InlineKeyboardButton(text=item["name"], callback_data=f"{prefix}:{item['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return _kb(rows)


# ------------------------------------------------------------------ точки входа
@router.message(F.text == "📜 Правила")
@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    """Просто показывает текст правил — без FSM."""
    await message.answer(await db.get_setting("rules_text"))


@router.message(F.text == "📢 Создать объявление")
@router.message(Command("post"))
async def cmd_post(message: Message, state: FSMContext, bot: Bot) -> None:
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.full_name)
    await state.clear()
    await _start_flow(message.chat.id, state, bot, user)


# ------------------------------------------------------------------ шаги мастера
async def _start_flow(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    if not await db.rules_accepted(user.id):
        await _step_rules(chat_id, state, bot, user)
        return
    await _step_subscription(chat_id, state, bot, user)


async def _step_rules(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    await state.set_state(Post.rules)
    text = await db.get_setting("rules_text")
    kb = _kb([[InlineKeyboardButton(text="✅ Принимаю", callback_data="ads_rules_ok")]])
    await bot.send_message(chat_id, text, reply_markup=kb)


async def _step_subscription(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    missing = await subscription.missing_for_ads(bot, user.id)
    if not missing:
        await _step_ad_type(chat_id, state, bot, user)
        return
    await state.set_state(Post.subscribe)
    rows: list[list[InlineKeyboardButton]] = []
    for ch in missing:
        username = subscription._field(ch, "username")
        link = f"https://t.me/{username}" if username else subscription._field(ch, "invite_link")
        title = subscription._field(ch, "title") or "Канал"
        if link:
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="ads_sub_check")])
    text = ("Чтобы публиковать объявления, подпишись на канал(ы):\n"
            f"{subscription.channels_text(missing)}")
    await bot.send_message(chat_id, text, reply_markup=_kb(rows))


async def _step_ad_type(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    await state.set_state(Post.ad_type)
    types = await db.ad_types()
    await bot.send_message(chat_id, "📢 Выбери рубрику объявления:", reply_markup=_cols(types, "adtype"))


async def _step_vertical(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    await state.set_state(Post.vertical)
    verts = await db.verticals()
    await bot.send_message(chat_id, "🎯 Выбери вертикаль:", reply_markup=_cols(verts, "advert"))


async def _step_text(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    await state.set_state(Post.text)
    await bot.send_message(
        chat_id,
        "✏️ Пришли текст объявления. Можно отправить сразу фото с подписью — "
        f"текст возьмётся из подписи, а картинка добавится автоматически.\n"
        f"Максимум {MAX_TEXT_LEN} символов.",
        reply_markup=_kb([]))


async def _step_image(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    data = await state.get_data()
    if data.get("media_file_id"):
        await _step_pin(chat_id, state, bot, user)
        return
    await state.set_state(Post.image_choice)
    price_image = await db.get_int("price_image")
    kb = _kb([
        [InlineKeyboardButton(text=f"🖼 Добавить картинку (+{price_image} коинов)",
                              callback_data="ads_img_yes")],
        [InlineKeyboardButton(text="Без картинки", callback_data="ads_img_no")],
    ])
    await bot.send_message(chat_id, "🖼 Добавить картинку к объявлению?", reply_markup=kb)


async def _step_pin(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    await state.set_state(Post.pin)
    pin4 = await db.get_int("price_pin_4h")
    pin8 = await db.get_int("price_pin_8h")
    kb = _kb([
        [InlineKeyboardButton(text="Без закрепа", callback_data="ads_pin:0")],
        [InlineKeyboardButton(text=f"📌 4 часа (+{pin4} коинов)", callback_data="ads_pin:4")],
        [InlineKeyboardButton(text=f"📌 8 часов (+{pin8} коинов)", callback_data="ads_pin:8")],
    ])
    await bot.send_message(chat_id, "📌 Закрепить объявление в канале?", reply_markup=kb)


async def _step_confirm(chat_id: int, state: FSMContext, bot: Bot, user) -> None:
    await state.set_state(Post.confirm)
    data = await state.get_data()
    body = ads.format_ad(data.get("text", ""), data.get("ad_type_tag"), data.get("vertical_tag"),
                         user.username)
    has_image = bool(data.get("media_file_id"))
    pin_hours = int(data.get("pin_hours") or 0)

    if has_image and data.get("media_type") == "photo":
        await bot.send_photo(chat_id, data["media_file_id"], caption=body)
    else:
        await bot.send_message(chat_id, body, disable_web_page_preview=True)

    price = await ads.price_line(has_image, pin_hours)
    balance = await tokens.balance(user.id)
    kb = _kb([
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="ads_publish")],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="ads_edit_text")],
    ])
    await bot.send_message(chat_id, f"Стоимость: {price}\nБаланс: <b>{balance}</b> коинов.",
                           reply_markup=kb)


# ------------------------------------------------------------------ отмена (любой шаг)
@router.callback_query(F.data == "ads_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer("Отменено.")


# ------------------------------------------------------------------ правила
@router.callback_query(Post.rules, F.data == "ads_rules_ok")
async def cb_rules_ok(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await db.accept_rules(callback.from_user.id)
    await callback.answer()
    await _step_subscription(callback.message.chat.id, state, bot, callback.from_user)


# ------------------------------------------------------------------ подписка
@router.callback_query(Post.subscribe, F.data == "ads_sub_check")
async def cb_sub_check(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = callback.from_user
    missing = await subscription.missing_for_ads(bot, user.id)
    if missing:
        await callback.answer("Подписка не найдена. Проверь, что подписан на все каналы.",
                              show_alert=True)
        return
    from app.handlers.chat_guard import activate_if_needed
    await activate_if_needed(bot, user)
    await callback.answer("Готово!")
    await _step_ad_type(callback.message.chat.id, state, bot, user)


# ------------------------------------------------------------------ рубрика
@router.callback_query(Post.ad_type, F.data.startswith("adtype:"))
async def cb_ad_type(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    type_id = int(callback.data.split(":")[1])
    row = await db.ad_type(type_id, only_active=True)
    await callback.answer()
    if not row:
        await callback.message.answer("Рубрика не найдена, попробуй ещё раз.")
        return
    await state.update_data(ad_type_id=row["id"], ad_type_name=row["name"], ad_type_tag=row["tag"])
    chat_id = callback.message.chat.id
    if row["note"] == "intro":
        await bot.send_message(chat_id, await db.get_setting("intro_note"))
    user = callback.from_user
    if row["has_vertical"]:
        await _step_vertical(chat_id, state, bot, user)
    else:
        await state.update_data(vertical_id=None, vertical_name=None, vertical_tag=None)
        await _step_text(chat_id, state, bot, user)


# ------------------------------------------------------------------ вертикаль
@router.callback_query(Post.vertical, F.data.startswith("advert:"))
async def cb_vertical(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    vert_id = int(callback.data.split(":")[1])
    row = await db.vertical(vert_id, only_active=True)
    await callback.answer()
    if not row:
        await callback.message.answer("Вертикаль не найдена, попробуй ещё раз.")
        return
    await state.update_data(vertical_id=row["id"], vertical_name=row["name"], vertical_tag=row["tag"])
    await _step_text(callback.message.chat.id, state, bot, callback.from_user)


# ------------------------------------------------------------------ текст
def _not_command(message: Message) -> bool:
    """Команды (/start, /balance, …) не должны становиться текстом объявления —
    пропускаем их дальше, к их собственным обработчикам."""
    return not (message.text or "").startswith("/")


@router.message(Post.text, _not_command, F.content_type.in_({"text", "photo"}))
async def on_text(message: Message, state: FSMContext, bot: Bot) -> None:
    is_photo = message.content_type == "photo"
    body = (message.caption if is_photo else message.text) or ""
    body = body.strip()
    if not body:
        await message.answer("Текст объявления не может быть пустым. Пришли текст"
                             + (" в подписи к фото." if is_photo else "."))
        return
    if len(body) > MAX_TEXT_LEN:
        await message.answer(f"Слишком длинный текст ({len(body)} символов из {MAX_TEXT_LEN}). "
                             f"Сократи и пришли ещё раз.")
        return
    update = {"text": body}
    if is_photo:
        update["media_type"] = "photo"
        update["media_file_id"] = message.photo[-1].file_id
    await state.update_data(**update)
    await _step_image(message.chat.id, state, bot, message.from_user)


@router.message(Post.text, _not_command)
async def on_text_invalid(message: Message) -> None:
    await message.answer("Пришли текст объявления сообщением, либо фото с подписью-текстом.")


# ------------------------------------------------------------------ картинка
@router.callback_query(Post.image_choice, F.data == "ads_img_no")
async def cb_image_no(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await _step_pin(callback.message.chat.id, state, bot, callback.from_user)


@router.callback_query(Post.image_choice, F.data == "ads_img_yes")
async def cb_image_yes(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(Post.image)
    await callback.answer()
    await bot.send_message(callback.message.chat.id, "Пришли картинку (фото).", reply_markup=_kb([]))


@router.message(Post.image, F.photo)
async def on_image(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(media_type="photo", media_file_id=message.photo[-1].file_id)
    await _step_pin(message.chat.id, state, bot, message.from_user)


@router.message(Post.image)
async def on_image_invalid(message: Message) -> None:
    await message.answer("Пришли картинку (фото).")


# ------------------------------------------------------------------ закреп
@router.callback_query(Post.pin, F.data.startswith("ads_pin:"))
async def cb_pin(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    hours = int(callback.data.split(":")[1])
    await state.update_data(pin_hours=hours)
    await callback.answer()
    await _step_confirm(callback.message.chat.id, state, bot, callback.from_user)


# ------------------------------------------------------------------ подтверждение / публикация
@router.callback_query(Post.confirm, F.data == "ads_edit_text")
async def cb_edit_text(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await _step_text(callback.message.chat.id, state, bot, callback.from_user)


@router.callback_query(Post.confirm, F.data == "ads_publish")
async def cb_publish(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = callback.from_user
    chat_id = callback.message.chat.id
    await callback.answer()
    # Блокировка + повторная проверка состояния: двойной тап по кнопке не должен
    # публиковать два объявления. Второй заход увидит, что состояние уже сброшено.
    async with locks.named(f"publish:{user.id}"):
        if await state.get_state() != Post.confirm.state:
            return
        data = await state.get_data()
        await _do_publish(callback, state, bot, user, chat_id, data)


async def _do_publish(callback: CallbackQuery, state: FSMContext, bot: Bot, user,
                      chat_id: int, data: dict) -> None:

    ad_type_row = await db.ad_type(data["ad_type_id"], only_active=True) \
        if data.get("ad_type_id") else None
    vertical_row = await db.vertical(data["vertical_id"], only_active=True) \
        if data.get("vertical_id") else None
    media_type = data.get("media_type") or "text"
    media_file_id = data.get("media_file_id")
    pin_hours = int(data.get("pin_hours") or 0)
    text = data.get("text", "")

    has_image = bool(media_file_id)
    quote = await ads.price_quote(has_image, pin_hours)
    balance = await tokens.balance(user.id)
    if balance < quote["total"]:
        need = quote["total"] - balance
        await bot.send_message(
            chat_id,
            f"⚠️ Не хватает коинов: нужно <b>{quote['total']}</b>, на балансе <b>{balance}</b> "
            f"(не хватает {need}). Пополни баланс:",
            reply_markup=await keyboards.packages_kb())
        return

    try:
        res = await ads.publish_ad(bot, user, text=text, ad_type_row=ad_type_row,
                                   vertical_row=vertical_row, media_type=media_type,
                                   media_file_id=media_file_id, pin_hours=pin_hours)
    except ads.AdError as exc:
        msg = html.escape(str(exc))
        if "коин" in str(exc).lower():
            await bot.send_message(chat_id, f"⚠️ {msg}\nПополни баланс:",
                                   reply_markup=await keyboards.packages_kb())
        else:
            await bot.send_message(chat_id, f"⚠️ {msg}")
        return

    await state.clear()
    link_line = ""
    try:
        channel = await bot.get_chat(res["channel_id"])
        if channel.username:
            link_line = f"\n🔗 https://t.me/{channel.username}/{res['message_id']}"
    except TelegramAPIError:
        pass
    await bot.send_message(
        chat_id,
        f"✅ Опубликовано!\nСписано: <b>{res['cost']}</b> коинов.\n"
        f"Баланс: <b>{res['balance']}</b> коинов.{link_line}")
