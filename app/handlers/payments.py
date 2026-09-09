"""Покупка токенов внутри бота: Telegram Stars (XTR), классический провайдер
или криптовалюта USDT/USDC через CryptoPay (app/cryptopay.py)."""
import html
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, LabeledPrice, Message, PreCheckoutQuery)

from app import cryptopay, db, keyboards, tokens
from app.config import ADMINS, PAYMENT_PROVIDER_TOKEN, cryptopay_enabled

log = logging.getLogger(__name__)
router = Router(name="payments")

CURRENCY = "XTR" if not PAYMENT_PROVIDER_TOKEN else "RUB"


async def packages() -> list[dict]:
    try:
        return json.loads(await db.get_setting("token_packages"))
    except (json.JSONDecodeError, TypeError):
        return []


@router.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery, bot: Bot) -> None:
    index = int(callback.data.split(":")[1])
    items = await packages()
    if index >= len(items):
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    pack = items[index]
    price = int(pack["stars"])
    amount = price if CURRENCY == "XTR" else price * 100  # для фиата — копейки
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"{pack['tokens']} коинов",
            description=f"Пакет из {pack['tokens']} коинов для публикации объявлений и сообщений.",
            payload=json.dumps({"tokens": int(pack["tokens"]), "index": index}),
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=[LabeledPrice(label=f"{pack['tokens']} коинов", amount=amount)],
        )
    except TelegramAPIError as exc:
        log.warning("send_invoice failed: %s", exc)
        await callback.answer("Не удалось выставить счёт. Проверьте настройки платежей.",
                              show_alert=True)
        return
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message) -> None:
    sp = message.successful_payment
    try:
        payload = json.loads(sp.invoice_payload)
        amount_tokens = int(payload["tokens"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.error("Некорректный payload платежа: %s", sp.invoice_payload)
        amount_tokens = 0

    await db.execute(
        """INSERT INTO payments(user_id, amount, currency, tokens, charge_id, payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message.from_user.id, sp.total_amount, sp.currency, amount_tokens,
         sp.telegram_payment_charge_id, sp.invoice_payload))

    balance = await tokens.add(message.from_user.id, amount_tokens, "purchase",
                              {"charge_id": sp.telegram_payment_charge_id,
                               "amount": sp.total_amount, "currency": sp.currency})
    await message.answer(f"✅ Оплата получена. Начислено <b>{amount_tokens}</b> коинов.\n"
                         f"Баланс: <b>{balance}</b>.")
    for admin_id in ADMINS:
        try:
            await message.bot.send_message(
                admin_id, f"💳 Оплата: @{message.from_user.username or message.from_user.id} — "
                          f"{sp.total_amount} {sp.currency} → {amount_tokens} коинов.")
        except TelegramAPIError:
            pass


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


@router.callback_query(F.data == "buy_menu")
async def back_to_packages(callback: CallbackQuery) -> None:
    await _show(callback, "💎 Выбери пакет коинов:", await keyboards.packages_kb())
    await callback.answer()


@router.callback_query(F.data == "crypto")
async def crypto_menu(callback: CallbackQuery) -> None:
    if not cryptopay_enabled():
        await callback.answer("Оплата криптой пока недоступна", show_alert=True)
        return
    items = await cryptopay.packages()
    await _show(callback, "🪙 Оплата в USDT или USDC (сети Tron, BSC, Ethereum).\n"
                          "Выбери пакет — я выставлю счёт со ссылкой на оплату:",
                keyboards.crypto_packages_kb(items))
    await callback.answer()


@router.callback_query(F.data.startswith("cbuy:"))
async def crypto_buy(callback: CallbackQuery, bot: Bot) -> None:
    if not cryptopay_enabled():
        await callback.answer("Оплата криптой пока недоступна", show_alert=True)
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
        await callback.answer("Не удалось выставить счёт, попробуйте позже.", show_alert=True)
        return
    await callback.message.answer(
        f"🪙 Счёт №{topup['id']}: <b>{topup['tokens']}</b> коинов за "
        f"<b>{topup['amount']} USDT/USDC</b>.\n\n"
        "Нажми «Оплатить», выбери сеть и монету и переведи точную сумму. "
        "Счёт действует 60 минут. Коины начислятся автоматически после подтверждения в сети; "
        "если сообщение не пришло — нажми «Проверить оплату».",
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
    text = cryptopay.describe(result)
    if result.get("action") == "credited":
        await callback.message.answer(text)
        await notify_admins(bot, f"🪙 Оплата криптой: @{callback.from_user.username or callback.from_user.id} — "
                                 f"{result['topup']['amount_credited']} {html.escape(str(invoice.get('currency') or 'USD'))} "
                                 f"→ {result['tokens']} коинов.")
        await callback.answer()
        return
    await callback.answer(text or f"Статус: {cryptopay.summary(result.get('topup') or topup)}",
                          show_alert=True)
