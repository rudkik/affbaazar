"""Модерация объявлений: кнопки под постом в канале «🗑 Удалить» / «💬 Удалить с комментом»."""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import ads

log = logging.getLogger(__name__)
router = Router(name="moderation")

MAX_COMMENT_LEN = 1000


class Mod(StatesGroup):
    """Ожидание комментария админа для удаления объявления «с комментом»."""
    comment = State()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="modc_cancel")]])


# ------------------------------------------------------------------ «🗑 Удалить»
@router.callback_query(F.data.startswith("ad_del:"))
async def cb_ad_del(callback: CallbackQuery, bot: Bot) -> None:
    """Кнопка видна всем в канале — права проверяем первым делом."""
    if not await ads.is_channel_admin(bot, callback.from_user.id):
        await callback.answer("Доступно только администрации", show_alert=True)
        return
    ad_id = int(callback.data.split(":", 1)[1])
    try:
        # кнопки под постом — всегда действие модерации, даже если админ удаляет своё
        res = await ads.delete_ad(bot, ad_id, by_admin_id=callback.from_user.id,
                                  delete_kind="moderator")
    except ads.AdError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if res["already"]:
        await callback.answer("Объявление уже было удалено.", show_alert=True)
        return
    text = "🗑 Объявление удалено."
    if res["refunded"]:
        text += f" Автору возвращено {res['refunded']} коинов."
    await callback.answer(text, show_alert=True)


# ------------------------------------------------------------------ «💬 Удалить с комментом»
@router.callback_query(F.data.startswith("ad_delc:"))
async def cb_ad_delc(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    if not await ads.is_channel_admin(bot, callback.from_user.id):
        await callback.answer("Доступно только администрации", show_alert=True)
        return
    ad_id = int(callback.data.split(":", 1)[1])
    admin_id = callback.from_user.id
    try:
        await bot.send_message(
            admin_id,
            f"Введите комментарий для автора объявления #{ad_id} "
            f"(до {MAX_COMMENT_LEN} символов, отправится автору вместе с уведомлением "
            f"об удалении).",
            reply_markup=_cancel_kb())
    except TelegramAPIError:
        me = await bot.get_me()
        await callback.answer(
            f"Откройте бота @{me.username} и нажмите Старт, чтобы оставить комментарий.",
            show_alert=True)
        return

    # Нажатие произошло в чате канала — там же лежит FSMContext по умолчанию.
    # Следующий текст админ пришлёт в личку боту, поэтому состояние ставим
    # явно на ключ его личного чата, а не чата, где случилось нажатие.
    admin_key = StorageKey(bot_id=state.key.bot_id, chat_id=admin_id, user_id=admin_id)
    admin_state = FSMContext(storage=state.storage, key=admin_key)
    await admin_state.set_state(Mod.comment)
    await admin_state.update_data(ad_id=ad_id)
    await callback.answer("Проверьте личные сообщения от бота.")


@router.callback_query(F.data == "modc_cancel")
async def cb_modc_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text("❌ Отменено.")
    except TelegramAPIError:
        pass
    await callback.answer()


@router.message(F.chat.type == ChatType.PRIVATE, Mod.comment)
async def comment_input(message: Message, bot: Bot, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Комментарий не может быть пустым. Отправьте текст "
                             "или нажмите «❌ Отмена» в сообщении выше.")
        return
    text = text[:MAX_COMMENT_LEN]
    data = await state.get_data()
    ad_id = data.get("ad_id")
    await state.clear()
    if ad_id is None:
        await message.answer("Не удалось определить объявление — начните заново кнопкой под постом.")
        return
    try:
        res = await ads.delete_ad(bot, int(ad_id), by_admin_id=message.from_user.id,
                                  comment=text, delete_kind="moderator")
    except ads.AdError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    if res["already"]:
        await message.answer("Объявление уже было удалено ранее.")
        return
    note = f"✅ Объявление #{ad_id} удалено с комментарием."
    if res["refunded"]:
        note += f" Автору возвращено {res['refunded']} коинов."
    await message.answer(note)
