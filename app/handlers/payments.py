"""Покупка коинов внутри бота — только криптовалюта USDT/USDC через CryptoPay
(app/cryptopay.py). Покупка за Telegram Stars убрана (AffBazaar-13): остался лишь возврат
старых Stars-платежей командой /refund.
"""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from app import cryptopay, db, keyboards, texts, tokens
from app.config import ADMINS, cryptopay_enabled

log = logging.getLogger(__name__)
router = Router(name="payments")

STARS_OFF = "Оплата звёздами отключена. Коины можно купить за USDT/USDC."


@router.callback_query(F.data.startswith("buy:"))
async def buy_stars_disabled(callback: CallbackQuery) -> None:
    """Кнопки «N коинов — M ⭐» остались в старых сообщениях: объясняем и даём крипто-пакеты."""
    await callback.answer(STARS_OFF, show_alert=True)
    if cryptopay_enabled():
        await callback.message.answer(await texts.t("txt_buy_menu", callback.from_user),
                                      reply_markup=await keyboards.packages_kb())


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
    """Счёт в Stars, выставленный до отключения, оплатить нельзя: отклоняем до списания."""
    await bot.answer_pre_checkout_query(query.id, ok=False, error_message=STARS_OFF)


@router.message(Command("refund"))
async def refund_stars(message: Message, command: CommandObject, bot: Bot) -> None:
    """Возврат оплаты Stars: /refund <charge_id>. Только для админов бота."""
    if message.from_user.id not in ADMINS:
        return
    charge_id = (command.args or "").strip()
    if not charge_id:
        await message.answer("Использование: <code>/refund &lt;charge_id&gt;</code>")
        return
    row = await db.fetchone("SELECT * FROM payments WHERE charge_id = ?", (charge_id,))
    if not row:
        await message.answer("Платёж не найден.")
        return
    try:
        await bot.refund_star_payment(user_id=int(row["user_id"]), telegram_payment_charge_id=charge_id)
    except TelegramAPIError as exc:
        await message.answer(f"⚠️ Возврат не выполнен: <code>{exc}</code>")
        return
    await tokens.add(int(row["user_id"]), -int(row["tokens"] or 0), "purchase_refund",
                     {"charge_id": charge_id})
    await message.answer("✅ Возврат выполнен, коины списаны.")


# ------------------------------------------------------------------ крипта (CryptoPay)
async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            pass


async def _show(callback: CallbackQuery, text: str, kb) -> None:
    """Меняем текущее сообщение; если Telegram не даёт (старое, без текста) — шлём новое."""
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.in_({"buy_menu", "crypto"}))
async def crypto_menu(callback: CallbackQuery) -> None:
    """Список пакетов. Оба callback-а живут в старых сообщениях — ведут в одно меню."""
    if not cryptopay_enabled():
        await callback.answer(texts.plain(await texts.t("txt_buy_disabled")), show_alert=True)
        return
    await _show(callback, await texts.t("txt_buy_menu", callback.from_user),
                await keyboards.packages_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("cbuy:"))
async def crypto_buy(callback: CallbackQuery, bot: Bot) -> None:
    if not cryptopay_enabled():
        await callback.answer(texts.plain(await texts.t("txt_buy_disabled")), show_alert=True)
        return
    index = int(callback.data.split(":")[1])
    items = await cryptopay.packages()
    if index >= len(items):
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    pack = items[index]
    try:
        me = await bot.get_me()
        success_url = f"https://t.me/{me.username}" if me and me.username else ""
    except Exception:  # noqa: BLE001
        success_url = ""
    try:
        topup = await cryptopay.create_topup(callback.from_user.id, pack, success_url=success_url)
    except cryptopay.CryptoPayError as exc:
        log.warning("cryptopay: не удалось создать счёт: %s", exc)
        await callback.answer(await texts.t("txt_crypto_failed"), show_alert=True)
        return
    await callback.message.answer(
        await texts.t("txt_crypto_invoice", callback.from_user, id=topup["id"],
                      tokens=topup["tokens"], amount=topup["amount"]),
        reply_markup=keyboards.crypto_invoice_kb(topup))
    await callback.answer()


@router.callback_query(F.data.startswith("cstatus:"))
async def crypto_status(callback: CallbackQuery, bot: Bot) -> None:
    topup = await cryptopay.get_topup(int(callback.data.split(":")[1]))
    if not topup or int(topup["user_id"]) != callback.from_user.id:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if topup["status"] in cryptopay.FINAL_STATUSES or not topup["invoice_id"]:
        await callback.answer(cryptopay.summary(topup), show_alert=True)
        return
    try:
        invoice = await cryptopay.get_invoice(topup["invoice_id"])
    except cryptopay.CryptoPayError as exc:
        log.warning("cryptopay: не удалось проверить счёт %s: %s", topup["invoice_id"], exc)
        await callback.answer("Не удалось проверить оплату, попробуйте позже.", show_alert=True)
        return
    result = await cryptopay.apply_invoice(invoice)
    text = await cryptopay.describe(result)
    if result.get("action") == "credited":
        await callback.message.answer(text)
        await notify_admins(bot, f"🪙 Оплата криптой: @{callback.from_user.username or callback.from_user.id} — "
                                 f"{result['topup']['amount_credited']} {html.escape(str(invoice.get('currency') or 'USD'))} "
                                 f"→ {result['tokens']} коинов.")
        await callback.answer()
        return
    await callback.answer(
        texts.plain(text) if text
        else f"Статус: {cryptopay.summary(result.get('topup') or topup)}", show_alert=True)


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    """Кнопка-заглушка («временно недоступно»): просто гасим индикатор загрузки."""
    await callback.answer()
