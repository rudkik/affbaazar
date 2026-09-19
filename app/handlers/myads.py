"""Раздел «📋 Мои объявления» в личке бота (AffBazaar-20).

Список всех объявлений пользователя — опубликованных и старых. Из карточки можно
«продлить» объявление (опубликовать заново по текущей цене) или докупить закреп
к активному. Деньги списываются только после отдельного подтверждения.
"""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import ads, db, keyboards, locks, texts, tokens

log = logging.getLogger(__name__)
router = Router(name="myads")
router.message.filter(F.chat.type == ChatType.PRIVATE)

BTN_MY_ADS = "📋 Мои объявления"
PAGE = 8
PREVIEW_LEN = 300

STATUS_ICON = {"published": "🟢", "deleted": "⚪️"}
STATUS_TEXT = {"published": "опубликовано", "deleted": "снято"}


def _status(ad) -> str:
    if ad["status"] == "deleted" and ad["delete_kind"] == "reposted":
        return "🔁 продлено"
    if ad["status"] == "published" and ads.ad_pin_active(ad):
        return "📌 закреплено"
    return f"{STATUS_ICON.get(ad['status'], '⚪️')} {STATUS_TEXT.get(ad['status'], ad['status'])}"


def _date(ad) -> str:
    dt = db.parse_iso(ad["created_at"])
    return f"{dt.astimezone(ads.MSK):%d.%m.%Y %H:%M}" if dt else "—"


def _button_title(ad) -> str:
    text = (ad["text"] or "").strip().replace("\n", " ")
    short = text[:28] + "…" if len(text) > 28 else text
    return f"#{ad['id']} {_status(ad)} · {short or ad['ad_type_name'] or ''}"


async def list_kb(user_id: int, offset: int) -> InlineKeyboardMarkup:
    rows_ = await ads.user_ads(user_id, PAGE, offset)
    total = await ads.user_ads_count(user_id)
    rows = [[InlineKeyboardButton(text=_button_title(ad), callback_data=f"my:ad:{ad['id']}")]
            for ad in rows_]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Новее", callback_data=f"my:list:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="Старее ➡️", callback_data=f"my:list:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, text: str, kb, edit: bool) -> None:
    """Правим текущее сообщение (навигация по списку) или шлём новое."""
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except TelegramAPIError:
            pass
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def show_list(message: Message, user, offset: int = 0, edit: bool = False) -> None:
    total = await ads.user_ads_count(user.id)
    if not total:
        await _show(message, await texts.t("txt_my_ads_empty", user), None, edit)
        return
    await _show(message, await texts.t("txt_my_ads_list", user, total=total),
                await list_kb(user.id, offset), edit)


@router.message(F.text == BTN_MY_ADS)
@router.message(Command("myads"))
async def cmd_my_ads(message: Message) -> None:
    await show_list(message, message.from_user)


@router.callback_query(F.data.startswith("my:list:"))
async def cb_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_list(callback.message, callback.from_user,
                    int(callback.data.split(":")[2]), edit=True)


# ------------------------------------------------------------------ карточка
async def _own_ad(callback: CallbackQuery, ad_id: int):
    ad = await db.fetchone("SELECT * FROM ads WHERE id = ? AND user_id = ?",
                           (ad_id, callback.from_user.id))
    if ad is None:
        await callback.answer("Объявление не найдено", show_alert=True)
    return ad


async def card_kb(ad) -> InlineKeyboardMarkup:
    price = await ads.price_quote(bool(ad["media_file_id"]) and ad["media_type"] != "text")
    rows = [[InlineKeyboardButton(text=f"🔁 Продлить (+{price['total']} коинов)",
                                  callback_data=f"my:rep:{ad['id']}")]]
    if ads.ad_is_active(ad) and not ads.ad_pin_active(ad) and not await ads.active_pin():
        pin4, pin8 = await db.get_int("price_pin_4h"), await db.get_int("price_pin_8h")
        rows.append([InlineKeyboardButton(text=f"📌 Закреп 4 ч (+{pin4})",
                                          callback_data=f"my:pin:{ad['id']}:4"),
                     InlineKeyboardButton(text=f"📌 Закреп 8 ч (+{pin8})",
                                          callback_data=f"my:pin:{ad['id']}:8")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="my:list:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def card_text(bot: Bot, user, ad) -> str:
    link = await ads.ad_link(bot, ad)
    text = (ad["text"] or "").strip()
    if len(text) > PREVIEW_LEN:
        text = text[:PREVIEW_LEN].rstrip() + "…"
    price = await ads.price_quote(bool(ad["media_file_id"]) and ad["media_type"] != "text")
    if ads.ad_pin_active(ad):
        pin = f"закреплено до {ads.pin_until_text(db.parse_iso(ad['pinned_until']))}"
    elif ads.ad_is_active(ad):
        pin = "без закрепа"
    else:
        pin = "—"
    return await texts.t(
        "txt_my_ad", user, id=ad["id"], status=_status(ad),
        type=html.escape(ad["ad_type_name"] or "без рубрики"), date=_date(ad),
        link=f"🔗 {link}" if link else "", text=html.escape(text),
        repost_price=price["total"], pin=pin)


@router.callback_query(F.data.startswith("my:ad:"))
async def cb_card(callback: CallbackQuery, bot: Bot) -> None:
    ad = await _own_ad(callback, int(callback.data.split(":")[2]))
    if ad is None:
        return
    await callback.answer()
    await _show(callback.message, await card_text(bot, callback.from_user, ad),
                await card_kb(ad), edit=True)


# ------------------------------------------------------------------ продлить
@router.callback_query(F.data.startswith("my:rep:"))
async def cb_repost_ask(callback: CallbackQuery) -> None:
    ad = await _own_ad(callback, int(callback.data.split(":")[2]))
    if ad is None:
        return
    await callback.answer()
    price = await ads.price_quote(bool(ad["media_file_id"]) and ad["media_type"] != "text")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, опубликовать", callback_data=f"my:repok:{ad['id']}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"my:ad:{ad['id']}")]])
    await _show(callback.message, await texts.t(
        "txt_my_repost_confirm", callback.from_user, id=ad["id"], price=price["total"],
        balance=await tokens.balance(callback.from_user.id)), kb, edit=True)


@router.callback_query(F.data.startswith("my:repok:"))
async def cb_repost(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    ad_id = int(callback.data.split(":")[2])
    await callback.answer()
    async with locks.named(f"publish:{user.id}"):     # двойной тап = одна публикация
        try:
            res = await ads.repost_ad(bot, user, ad_id)
        except ads.AdError as exc:
            await _fail(callback, bot, exc)
            return
    link = await ads.ad_link(bot, await db.fetchone("SELECT * FROM ads WHERE id = ?", (res["ad_id"],)))
    await callback.message.answer(await texts.t(
        "txt_ad_published", user, cost=res["cost"], balance=res["balance"],
        link=f"\n🔗 {link}" if link else ""))


# ------------------------------------------------------------------ закреп
@router.callback_query(F.data.startswith("my:pin:"))
async def cb_pin_ask(callback: CallbackQuery) -> None:
    _, _, ad_id, hours = callback.data.split(":")
    ad = await _own_ad(callback, int(ad_id))
    if ad is None:
        return
    await callback.answer()
    hours = int(hours)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, закрепить", callback_data=f"my:pinok:{ad['id']}:{hours}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"my:ad:{ad['id']}")]])
    await _show(callback.message, await texts.t(
        "txt_my_pin_confirm", callback.from_user, id=ad["id"], hours=hours,
        price=await db.get_int(f"price_pin_{hours}h"),
        balance=await tokens.balance(callback.from_user.id)), kb, edit=True)


@router.callback_query(F.data.startswith("my:pinok:"))
async def cb_pin(callback: CallbackQuery, bot: Bot) -> None:
    _, _, ad_id, hours = callback.data.split(":")
    user = callback.from_user
    await callback.answer()
    try:
        res = await ads.add_pin(bot, user, int(ad_id), int(hours))
    except ads.AdError as exc:
        await _fail(callback, bot, exc)
        return
    await callback.message.answer(await texts.t(
        "txt_my_pin_done", user, until=ads.pin_until_text(db.parse_iso(res["pinned_until"])),
        cost=res["cost"], balance=res["balance"]))


async def _fail(callback: CallbackQuery, bot: Bot, exc: ads.AdError) -> None:
    """Готовые сообщения (дубль, закреп занят) шлём как есть; остальные ошибки — текстом."""
    if isinstance(exc, (ads.DuplicateError, ads.PinBusyError)):
        await callback.message.answer(str(exc))
        return
    text = f"⚠️ {html.escape(str(exc))}"
    if "коин" in str(exc).lower():
        await callback.message.answer(text + "\nПополни баланс:",
                                      reply_markup=await keyboards.packages_kb())
    else:
        await callback.message.answer(text)
