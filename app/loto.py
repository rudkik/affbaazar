"""Бонус-Лото: оплаченные пополнения → билеты, вебхуки о выигрышах, ссылка входа.

Схема (loto/docs/INTEGRATION-AFFBAZAAR.md):
- магазин → лото: `order.paid` после зачисления коинов за крипту, `order.refunded` при реверсе.
  Заказы кладутся в таблицу loto_orders и уходят фоновым воркером с повторами, поэтому
  недоступность лото не влияет на оплату и переживает перезапуск бота;
- лото → магазин: POST /webhooks/loto (подпись HMAC как у CryptoPay), `prize.won` начисляет
  коины по курсу LOTO_COINS_PER_USD, остальное — уведомления;
- вход покупателя: SSO-ссылка с короткоживущим токеном (sso_link / sso_token).

Всё выключено, пока не заданы LOTO_URL, LOTO_PUBLIC_KEY и LOTO_SECRET.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

import httpx

from app import config, db, tokens

log = logging.getLogger(__name__)

TIMEOUT = 10.0
MAX_SKEW = 300
RETRY_DELAYS = (60, 300, 1800, 7200, 43200)          # как у лото: 1 мин, 5 мин, 30 мин, 2 ч, 12 ч
CREDIT_PRIZES = {"bonus", "jackpot"}                  # начисляем коинами; promo/physical — админам

_wake = asyncio.Event()


# ------------------------------------------------------------------ подписи
def _hmac(msg: bytes, secret: Optional[str] = None) -> str:
    key = (config.LOTO_SECRET if secret is None else secret).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_body(raw: bytes, secret: Optional[str] = None) -> str:
    """X-Signature исходящего запроса: HMAC ровно той строки, что уходит в теле."""
    return "sha256=" + _hmac(raw, secret)


def sign_webhook(timestamp: str, raw: bytes, secret: Optional[str] = None) -> str:
    return "sha256=" + _hmac(timestamp.encode() + b"." + raw, secret)


def verify(timestamp: str, raw: bytes, given: str, *, secret: Optional[str] = None,
           now: Optional[float] = None) -> bool:
    """Подпись входящего вебхука по сырому телу + защита от повтора (5 минут)."""
    secret = config.LOTO_SECRET if secret is None else secret
    if not secret or not timestamp or not given or not timestamp.isdigit():
        return False
    if abs((now if now is not None else time.time()) - int(timestamp)) > MAX_SKEW:
        return False
    return hmac.compare_digest(sign_webhook(timestamp, raw, secret), given)


# ------------------------------------------------------------------ вход покупателя
def sso_token(user_id: int, name: Optional[str] = None, *, ttl: int = 600,
              now: Optional[int] = None) -> str:
    """payload.signature — короткоживущий, генерировать при каждом показе."""
    payload = base64.urlsafe_b64encode(json.dumps(
        {"id": str(user_id), "name": name, "exp": int(now if now is not None else time.time()) + ttl},
        ensure_ascii=False, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return payload + "." + _hmac(payload.encode())


def sso_link(user_id: int, name: Optional[str] = None, to: str = "/tickets") -> str:
    return (f"{config.LOTO_URL}/sso?key={config.LOTO_PUBLIC_KEY}"
            f"&token={sso_token(user_id, name)}&to={to}")


def widget(user_id: int, name: Optional[str] = None) -> Optional[dict]:
    """Данные для <script src=…/widget.js> на сайте; None — лото выключено."""
    if not config.loto_enabled():
        return None
    return {"url": f"{config.LOTO_URL}/widget.js", "key": config.LOTO_PUBLIC_KEY,
            "token": sso_token(user_id, name)}


# ------------------------------------------------------------------ магазин → лото
async def enqueue(event: str, order_id: str, user_id: int, amount: str,
                  name: Optional[str] = None) -> bool:
    """Поставить order.paid / order.refunded в очередь. HTTP здесь не ходит: воркер отправит
    сразу (его будят) или позже с повторами. False — лото выключено."""
    if not config.loto_enabled():
        return False
    await db.execute(
        """INSERT INTO loto_orders(order_id, event, user_id, amount, name)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(order_id) DO UPDATE SET event = excluded.event, status = 'pending',
               attempts = 0, next_at = 0, updated_at = datetime('now')""",
        (str(order_id), event, int(user_id), str(amount), name))
    _wake.set()
    return True


async def post_order(row) -> tuple[str, dict]:
    """Один HTTP-запрос в лото. -> ("ok" | "retry" | "fail", тело ответа).

    retry — сеть или 5xx/429: попробуем позже; fail — лото ответило 4xx, повтор не поможет."""
    body = json.dumps({
        "event": row["event"], "order_id": str(row["order_id"]),
        "customer": {"id": str(row["user_id"]), "email": None, "name": row["name"]},
        "amount": float(row["amount"]), "currency": "USD",
        "paid_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, ensure_ascii=False).encode()
    headers = {"Authorization": f"Bearer {config.LOTO_SECRET}",
               "Content-Type": "application/json", "X-Signature": sign_body(body)}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{config.LOTO_URL}/api/v1/orders", content=body,
                                     headers=headers)
    except httpx.HTTPError as exc:
        log.warning("loto: %s %s недоступно: %s", row["event"], row["order_id"], exc)
        return "retry", {}
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    if resp.status_code >= 400:
        log.warning("loto: %s %s → %s %s", row["event"], row["order_id"], resp.status_code,
                    resp.text[:200])
        return ("retry" if resp.status_code == 429 or resp.status_code >= 500 else "fail"), data
    return "ok", data


async def flush(notify=None, *, now: Optional[int] = None) -> int:
    """Отправить все заказы, чей срок подошёл. notify(row, response) зовётся по доставленным.
    Возвращает число доставленных."""
    if not config.loto_enabled():
        return 0
    ts = int(now if now is not None else time.time())
    rows = await db.fetchall(
        "SELECT * FROM loto_orders WHERE status = 'pending' AND next_at <= ? ORDER BY created_at "
        "LIMIT 50", (ts,))
    sent = 0
    for row in rows:
        outcome, data = await post_order(row)
        attempts = int(row["attempts"] or 0) + 1
        if outcome == "ok":
            await db.execute(
                """UPDATE loto_orders SET status = 'sent', attempts = ?, response = ?,
                                          updated_at = datetime('now') WHERE order_id = ?""",
                (attempts, json.dumps(data, ensure_ascii=False), row["order_id"]))
            sent += 1
            log.info("loto: %s %s доставлен, билеты: %s", row["event"], row["order_id"],
                     (data or {}).get("ticket_ids"))
            if notify:
                try:
                    await notify(row, data or {})
                except Exception:  # noqa: BLE001 — уведомление не должно ломать очередь
                    log.exception("loto: не удалось уведомить о заказе %s", row["order_id"])
            continue
        if outcome == "fail" or attempts > len(RETRY_DELAYS):
            await db.execute("UPDATE loto_orders SET status = 'failed', attempts = ?, response = ?, "
                             "updated_at = datetime('now') WHERE order_id = ?",
                             (attempts, json.dumps(data, ensure_ascii=False), row["order_id"]))
            log.error("loto: %s %s не доставлен (%s, попыток: %s)", row["event"],
                      row["order_id"], outcome, attempts)
            continue
        await db.execute("UPDATE loto_orders SET attempts = ?, next_at = ?, "
                         "updated_at = datetime('now') WHERE order_id = ?",
                         (attempts, ts + RETRY_DELAYS[attempts - 1], row["order_id"]))
    return sent


async def worker(notify=None) -> None:
    """Фоновая задача: шлёт очередь сразу после enqueue и раз в минуту — отложенные повторы."""
    while True:
        try:
            await flush(notify)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("loto: ошибка очереди заказов")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), 60)
        except asyncio.TimeoutError:
            pass


# ------------------------------------------------------------------ лото → магазин
async def apply_event(event: dict) -> dict[str, Any]:
    """Обработать вебхук. Идемпотентно по id доставки и по билету (prize.won).

    Возвращает {"action": duplicate|credited|manual|noted|ignored, …}.
    """
    delivery = str(event.get("id") or "")
    kind = str(event.get("event") or "")
    data = event.get("data") or {}
    if not delivery or not kind:
        return {"action": "ignored", "reason": "no id/event"}
    cur = await db.execute("INSERT OR IGNORE INTO loto_deliveries(id, event) VALUES (?, ?)",
                           (delivery, kind))
    if not cur.rowcount:
        return {"action": "duplicate"}
    if kind != "prize.won":
        return {"action": "noted", "kind": kind, "data": data}

    prize = data.get("prize") or {}
    try:
        user_id = int(data.get("customer_id"))
    except (TypeError, ValueError):
        log.warning("loto: prize.won без customer_id: %s", data)
        return {"action": "ignored", "reason": "bad customer_id", "data": data}
    if prize.get("type") not in CREDIT_PRIZES:
        # promo / physical — выдаёт человек, боту начислять нечего
        return {"action": "manual", "user_id": user_id, "kind": kind, "data": data}
    try:
        coins = int(round(float(prize.get("value") or 0) * config.LOTO_COINS_PER_USD))
    except (TypeError, ValueError):
        coins = 0
    ticket = str(data.get("ticket_id") or delivery)
    claim = await db.execute("INSERT OR IGNORE INTO loto_prizes(ticket_id, delivery, coins) "
                             "VALUES (?, ?, ?)", (ticket, delivery, coins))
    if not claim.rowcount:
        return {"action": "duplicate", "reason": "ticket already credited"}
    balance = await tokens.add(user_id, coins, "loto_prize",
                               {"ticket": ticket, "draw": data.get("draw"), "delivery": delivery,
                                "prize": prize}) if coins > 0 else await tokens.balance(user_id)
    log.info("loto: выигрыш билет %s тираж %s → %s коинов user=%s", ticket, data.get("draw"),
             coins, user_id)
    return {"action": "credited", "user_id": user_id, "coins": coins, "balance": balance,
            "data": data}
