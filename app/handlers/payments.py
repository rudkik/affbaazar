"""Покупка токенов внутри бота: Telegram Stars (XTR) или классический провайдер."""
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, LabeledPrice, Message, PreCheckoutQuery)

from app import db, tokens
from app.config import ADMINS, PAYMENT_PROVIDER_TOKEN

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
