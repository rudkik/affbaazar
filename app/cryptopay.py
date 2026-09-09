"""CryptoPay (https://ubaduba.top): пополнение коинов за USDT/USDC. Схема — в INTEGRATION.md.

Поток: создаём счёт (create_topup) → пользователь платит на payment_url → процессинг шлёт
вебхук → apply_invoice начисляет коины. Та же apply_invoice работает и по кнопке
«Проверить оплату» (счёт перечитывается через API) — страховка, если вебхук не дошёл.

Начисление идемпотентно: один счёт зачисляется один раз (блокировка + статус в БД),
дубли доставок отсеиваются по delivery_id.
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Optional

import httpx

from app import config, db, locks, tokens

log = logging.getLogger(__name__)

TIMEOUT = 15.0
MAX_SKEW = 300                      # секунд: старее — считаем повтором
PAID_STATUSES = {"paid", "overpaid"}
CREDITED_STATUSES = {"paid", "partial", "reversed"}     # по счёту уже было начисление
FINAL_STATUSES = CREDITED_STATUSES | {"expired", "cancelled"}  # перепроверять через API незачем


class CryptoPayError(Exception):
    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


# ------------------------------------------------------------------ пакеты
async def packages() -> list[dict]:
    """[{"usd": "5", "tokens": 50}, …] из настроек; битые записи пропускаются."""
    try:
        raw = json.loads(await db.get_setting("crypto_packages"))
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            usd = Decimal(str(item["usd"]))
            tok = int(item["tokens"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if usd > 0 and tok > 0:
            out.append({"usd": fmt_amount(usd), "tokens": tok})
    return out


def fmt_amount(value: Decimal | str) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def tokens_for(amount_paid: str, amount: str, tokens_full: int) -> int:
    """Коинов за фактически оплаченную сумму, пропорционально пакету (вниз до целого)."""
    try:
        paid, full = Decimal(str(amount_paid)), Decimal(str(amount))
    except InvalidOperation:
        return 0
    if paid <= 0 or full <= 0:
        return 0
    return int((paid * tokens_full / full).to_integral_value(rounding=ROUND_DOWN))


# ------------------------------------------------------------------ HTTP API
def _headers(extra: Optional[dict] = None) -> dict:
    h = {"Authorization": f"Bearer {config.CRYPTOPAY_API_KEY}",
         "Content-Type": "application/json", "Accept": "application/json"}
    h.update(extra or {})
    return h


async def request(method: str, path: str, payload: Optional[dict] = None,
                  headers: Optional[dict] = None) -> dict:
    """Запрос к API; ошибки процессинга поднимаются как CryptoPayError."""
    if not config.cryptopay_enabled():
        raise CryptoPayError("disabled", "CRYPTOPAY_API_KEY не задан")
    url = f"{config.CRYPTOPAY_BASE_URL}/api/v1{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, json=payload, headers=_headers(headers))
    except httpx.HTTPError as exc:
        raise CryptoPayError("network", str(exc)) from exc
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        err = body.get("error") or {}
        raise CryptoPayError(err.get("code") or f"http_{resp.status_code}",
                             err.get("message") or resp.text[:200], resp.status_code)
    return body.get("data", body)


async def create_invoice(**fields) -> dict:
    return await request("POST", "/invoices", fields,
                         {"Idempotency-Key": fields.get("external_id") or str(uuid.uuid4())})


async def get_invoice(invoice_id: str) -> dict:
    return await request("GET", f"/invoices/{invoice_id}")


# ------------------------------------------------------------------ пополнение
async def create_topup(user_id: int, package: dict, *, success_url: str = "") -> dict:
    """Создаёт запись пополнения и счёт в CryptoPay. Возвращает строку crypto_topups."""
    amount, tok = fmt_amount(package["usd"]), int(package["tokens"])
    cur = await db.execute(
        "INSERT INTO crypto_topups(user_id, amount, tokens) VALUES (?, ?, ?)",
        (user_id, amount, tok))
    topup_id = cur.lastrowid
    external_id = f"topup-{topup_id}"
    fields = {
        "amount": amount,
        "external_id": external_id,
        "description": f"{tok} коинов",
        "customer_id": str(user_id),
        "metadata": {"user_id": user_id, "topup_id": topup_id, "tokens": tok, "kind": "topup"},
        "expires_in": 3600,
    }
    if success_url:
        fields["success_url"] = fields["cancel_url"] = success_url
    try:
        inv = await create_invoice(**fields)
    except CryptoPayError:
        await db.execute("UPDATE crypto_topups SET status = 'cancelled', "
                         "updated_at = datetime('now') WHERE id = ?", (topup_id,))
        raise
    await db.execute(
        """UPDATE crypto_topups SET invoice_id = ?, payment_url = ?, expires_at = ?,
                                    updated_at = datetime('now') WHERE id = ?""",
        (inv.get("id"), inv.get("payment_url"), inv.get("expires_at"), topup_id))
    log.info("cryptopay: счёт %s на %s USD (%s коинов) для user=%s",
             inv.get("id"), amount, tok, user_id)
    return await get_topup(topup_id)


async def get_topup(topup_id: int):
    return await db.fetchone("SELECT * FROM crypto_topups WHERE id = ?", (topup_id,))


async def find_topup(invoice: dict):
    """Запись пополнения по external_id (topup-<id>), а если его нет — по invoice_id."""
    ext = str(invoice.get("external_id") or "")
    if ext.startswith("topup-") and ext[6:].isdigit():
        row = await get_topup(int(ext[6:]))
        if row:
            return row
    if invoice.get("id"):
        return await db.fetchone("SELECT * FROM crypto_topups WHERE invoice_id = ?",
                                 (invoice["id"],))
    return None


# ------------------------------------------------------------------ вебхук
def signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body,
                      hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(timestamp: str, raw_body: bytes, given: str, *, secret: Optional[str] = None,
           now: Optional[float] = None) -> bool:
    """Подпись по сырому телу + защита от повтора (timestamp не старше 5 минут)."""
    secret = config.CRYPTOPAY_WEBHOOK_SECRET if secret is None else secret
    if not secret or not timestamp or not given:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > MAX_SKEW:
        return False
    return hmac.compare_digest(signature(secret, timestamp, raw_body), given)


async def apply_event(event: dict) -> dict:
    """Обработать тело вебхука. Возвращает результат apply_invoice или {"action": "duplicate"}."""
    delivery_id = str(event.get("id") or "")
    data = event.get("data") or {}
    invoice = data.get("invoice") or {}
    reversal = data.get("reversal")
    if not invoice.get("id"):
        return {"action": "ignored", "reason": "no invoice"}
    async with locks.named(f"cryptopay:{invoice['id']}"):
        if delivery_id and await db.fetchone(
                "SELECT 1 FROM cryptopay_deliveries WHERE delivery_id = ?", (delivery_id,)):
            return {"action": "duplicate"}
        result = await _apply_locked(invoice, reversal)
        if delivery_id:
            await db.execute(
                "INSERT OR IGNORE INTO cryptopay_deliveries(delivery_id, event, invoice_id) "
                "VALUES (?, ?, ?)", (delivery_id, event.get("event"), invoice["id"]))
    return result


async def apply_invoice(invoice: dict) -> dict:
    """Синхронизировать запись пополнения с объектом счёта из API (без вебхука)."""
    if not invoice.get("id"):
        return {"action": "ignored", "reason": "no invoice"}
    async with locks.named(f"cryptopay:{invoice['id']}"):
        return await _apply_locked(invoice, None)


async def _apply_locked(invoice: dict, reversal: Optional[dict]) -> dict:
    """Решение принимается по status/is_paid счёта, а не по имени события (INTEGRATION.md §4.2).

    Возвращает {"action": credited|reversed|status|noop|unknown, "topup": row, "tokens": n}.
    """
    topup = await find_topup(invoice)
    if not topup:
        log.warning("cryptopay: счёт %s (external_id=%s) не найден у нас",
                    invoice.get("id"), invoice.get("external_id"))
        return {"action": "unknown"}
    topup_id, status = int(topup["id"]), str(invoice.get("status") or "")
    if not topup["invoice_id"]:
        await db.execute("UPDATE crypto_topups SET invoice_id = ? WHERE id = ?",
                         (invoice["id"], topup_id))

    # Реверс: блокчейн забрал подтверждённый платёж — списываем то, что начисляли.
    if reversal or status == "reversed":
        if topup["status"] == "reversed" or not int(topup["tokens_credited"] or 0):
            return {"action": "noop", "topup": topup}
        credited = int(topup["tokens_credited"])
        amount = str((reversal or {}).get("amount") or topup["amount_credited"])
        back = min(credited, tokens_for(amount, topup["amount"], int(topup["tokens"])) or credited)
        await tokens.add(int(topup["user_id"]), -back, "crypto_reversal",
                         {"topup_id": topup_id, "invoice_id": invoice["id"], "amount": amount})
        await db.execute(
            """UPDATE crypto_topups SET status = 'reversed', tokens_credited = tokens_credited - ?,
                                        updated_at = datetime('now') WHERE id = ?""",
            (back, topup_id))
        log.warning("cryptopay: реверс счёта %s, списано %s коинов у user=%s",
                    invoice["id"], back, topup["user_id"])
        return {"action": "reversed", "topup": await get_topup(topup_id), "tokens": back}

    paid = bool(invoice.get("is_paid")) or status in PAID_STATUSES or status == "partially_paid"
    if paid:
        # Деньги пришли — зачисляем, даже если у нас счёт уже помечен истёкшим:
        # доверяем статусу счёта в процессинге, а не своему прошлому состоянию.
        if topup["status"] in CREDITED_STATUSES:
            return {"action": "noop", "topup": topup}
        confirmed = str(invoice.get("amount_confirmed") or "0")
        tok = tokens_for(confirmed, topup["amount"], int(topup["tokens"]))
        if tok <= 0:
            return {"action": "noop", "topup": topup}
        new_status = "partial" if (status == "partially_paid" and not invoice.get("is_paid")) else "paid"
        await db.execute(
            """UPDATE crypto_topups SET status = ?, amount_credited = ?, tokens_credited = ?,
                                        updated_at = datetime('now') WHERE id = ?""",
            (new_status, fmt_amount(confirmed), tok, topup_id))
        balance = await tokens.add(int(topup["user_id"]), tok, "crypto_purchase",
                                   {"topup_id": topup_id, "invoice_id": invoice["id"],
                                    "amount": confirmed, "currency": invoice.get("currency"),
                                    "network": invoice.get("network"), "status": status})
        # Дублируем в общий реестр оплат — его показывает админка.
        await db.execute(
            """INSERT INTO payments(user_id, amount, currency, tokens, charge_id, payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(topup["user_id"]), float(Decimal(confirmed)),
             invoice.get("currency") or "USD", tok, invoice["id"],
             json.dumps({"topup_id": topup_id, "network": invoice.get("network"),
                         "status": status}, ensure_ascii=False)))
        log.info("cryptopay: счёт %s оплачен (%s), начислено %s коинов user=%s",
                 invoice["id"], status, tok, topup["user_id"])
        return {"action": "credited", "topup": await get_topup(topup_id), "tokens": tok,
                "balance": balance, "partial": new_status == "partial"}

    if status in ("confirming", "expired", "cancelled") and topup["status"] not in CREDITED_STATUSES:
        if topup["status"] != status:
            await db.execute("UPDATE crypto_topups SET status = ?, updated_at = datetime('now') "
                             "WHERE id = ?", (status, topup_id))
            return {"action": "status", "topup": await get_topup(topup_id), "status": status}
    return {"action": "noop", "topup": topup}


def describe(result: dict) -> Optional[str]:
    """Текст для пользователя по результату apply_*; None — сообщать нечего."""
    action = result.get("action")
    if action == "credited":
        head = ("✅ Оплата получена частично." if result.get("partial")
                else "✅ Оплата получена.")
        return (f"{head} Начислено <b>{result['tokens']}</b> коинов.\n"
                f"Баланс: <b>{result['balance']}</b>.")
    if action == "reversed":
        return (f"⚠️ Платёж отменён сетью блокчейна. Списано обратно "
                f"<b>{result['tokens']}</b> коинов.")
    if action == "status":
        return {"confirming": "⏳ Платёж замечен, ждём подтверждения в сети.",
                "expired": "⌛ Срок оплаты счёта истёк. Создайте новый.",
                "cancelled": "✖️ Счёт отменён."}.get(result.get("status"))
    return None


def summary(topup: Any) -> str:
    """Короткое описание счёта для сообщений в боте."""
    status = {"pending": "ожидает оплаты", "confirming": "ждём подтверждения", "paid": "оплачен",
              "partial": "оплачен частично", "reversed": "отменён сетью", "expired": "истёк",
              "cancelled": "отменён"}.get(topup["status"], topup["status"])
    return f"{topup['tokens']} коинов за {topup['amount']} USDT/USDC — {status}"
