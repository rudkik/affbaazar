"""CryptoPay: создание счёта из бота, вебхук (подпись, дедупликация, зачисление, реверс),
кнопка «Проверить оплату». Сеть подменяется: API процессинга не вызывается."""
import asyncio, json, os, sys, pathlib, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"; cfg.ADMINS = {999}
cfg.CRYPTOPAY_API_KEY = "cp_test_key"; cfg.CRYPTOPAY_WEBHOOK_SECRET = "whsec_test"
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
import app.cryptopay as cp
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR

import httpx
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from app.handlers import payments, user as user_h
import app.web.server as web

UID, ADMIN = 700001, 999
_ids = iter(range(50000, 99999))

# ------------------------------------------------------------------ подмена API процессинга
API_CALLS = []
INVOICES = {}


async def fake_request(method, path, payload=None, headers=None):
    API_CALLS.append((method, path, payload, headers))
    if method == "POST" and path == "/invoices":
        inv_id = f"inv-{len(INVOICES) + 1}"
        INVOICES[inv_id] = {
            "id": inv_id, "status": "pending", "is_paid": False, "amount": f"{payload['amount']}0000",
            "amount_received": "0", "amount_confirmed": "0", "currency": None, "network": None,
            "payment_url": f"https://ubaduba.top/pay/{inv_id}", "external_id": payload["external_id"],
            "customer_id": payload["customer_id"], "metadata": payload.get("metadata"),
            "expires_at": "2026-09-09T12:00:00+00:00"}
        return INVOICES[inv_id]
    if method == "POST" and path.endswith("/cancel"):
        inv = INVOICES.get(path.split("/")[-2])
        if inv and inv["status"] == "pending":
            inv["status"] = "cancelled"
        return inv or {}
    if method == "GET" and path.startswith("/invoices/"):
        inv = INVOICES.get(path.split("/")[-1])
        if not inv:
            raise cp.CryptoPayError("not_found", "no", 404)
        return inv
    raise AssertionError(f"неожиданный вызов {method} {path}")

cp.request = fake_request


class FakeBot:
    id = 42

    def __init__(self):
        self.dm, self.alerts = [], []

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def send_message(self, chat_id, text, **kw):
        mid = next(_ids)
        self.dm.append((chat_id, text))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text,
                                           reply_markup=method.reply_markup)
        if name == "AnswerCallbackQuery":
            self.alerts.append(method.text or ""); return True
        if name in {"EditMessageText", "EditMessageReplyMarkup"}:
            return True
        raise AssertionError("не смоделирован метод " + name)


def cb(data, uid=UID):
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data,
        from_user=User(id=uid, is_bot=False, first_name="Юзер", username="payer"),
        message=Message(message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"))))


def signed(body: dict, *, secret="whsec_test", ts=None):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) if ts is None else ts)
    return raw, {"Content-Type": "application/json", "X-CryptoPay-Timestamp": ts,
                 "X-CryptoPay-Signature": cp.signature(secret, ts, raw),
                 "X-CryptoPay-Event": body.get("event", ""), "X-CryptoPay-Delivery": body.get("id", "")}


def event(name, invoice, delivery, reversal=None):
    return {"id": delivery, "event": name, "created_at": "2026-09-09T11:05:00+00:00",
            "data": {"invoice": dict(invoice), "reversal": reversal}}


async def main():
    await db.init(); await sdb.init()
    bot = FakeBot()
    web.set_bot(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(payments.router); dp.include_router(user_h.router)
    feed = lambda u: dp.feed_update(bot, u)  # noqa: E731

    # --- чистые функции
    assert cp.tokens_for("5", "5", 50) == 50
    assert cp.tokens_for("2.5", "5", 50) == 25
    assert cp.tokens_for("7.31", "5", 50) == 73           # переплата: пропорционально, вниз
    assert cp.tokens_for("0", "5", 50) == 0 and cp.tokens_for("x", "5", 50) == 0
    raw = b'{"a":1}'
    sig = cp.signature("s", "100", raw)
    assert sig.startswith("sha256=") and cp.verify("100", raw, sig, secret="s", now=200)
    assert not cp.verify("100", raw, sig, secret="s", now=500)       # старше 5 минут
    assert not cp.verify("100", raw, sig, secret="other", now=200)
    assert not cp.verify("100", raw + b" ", sig, secret="s", now=200)
    assert not cp.verify("100", raw, sig, secret="", now=200)         # секрет не задан
    assert not cp.verify("abc", raw, sig, secret="s", now=200)

    # --- пакеты: меню покупки — сразу крипто-пакеты, звёзд нет (AffBazaar-13)
    from app import keyboards
    kb = await keyboards.packages_kb()
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["cbuy:0", "cbuy:1", "cbuy:2"], buttons
    assert all("USDT" in b.text and "⭐" not in b.text for b in buttons), buttons
    packs = await cp.packages()
    assert packs[0] == {"usd": "5.00", "tokens": 50}
    await db.set_setting("crypto_packages", json.dumps([{"usd": "5", "tokens": 50},
                                                        {"usd": "bad"}, {"usd": "10", "tokens": 120}]))
    assert [p["tokens"] for p in await cp.packages()] == [50, 120]

    # --- меню и выставление счёта из бота
    await feed(cb("crypto"))
    await feed(cb("cbuy:0"))
    assert API_CALLS[-1][0] == "POST" and API_CALLS[-1][2]["amount"] == "5.00"
    assert API_CALLS[-1][2]["customer_id"] == str(UID)
    assert API_CALLS[-1][2]["external_id"] == "topup-1"
    assert API_CALLS[-1][3]["Idempotency-Key"] == "topup-1"
    assert API_CALLS[-1][2]["success_url"] == "https://t.me/testbot"
    topup = await cp.get_topup(1)
    assert topup["invoice_id"] == "inv-1" and topup["status"] == "pending"
    assert topup["payment_url"] == "https://ubaduba.top/pay/inv-1"
    assert "Счёт №1" in bot.dm[-1][1] and "5.00 USDT" in bot.dm[-1][1]
    assert await tk.balance(UID) == 0
    await feed(cb("cbuy:9"))
    assert bot.alerts[-1] == "Пакет недоступен"

    # --- «Проверить оплату», пока не оплачен: только статус, ничего не начисляется
    await feed(cb("cstatus:1"))
    assert "ожидает оплаты" in bot.alerts[-1] and await tk.balance(UID) == 0
    await feed(cb("cstatus:1", uid=UID + 1))
    assert bot.alerts[-1] == "Счёт не найден"

    async def hook(body, headers, expect=200):
        transport = httpx.ASGITransport(app=web.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/webhooks/cryptopay", content=body, headers=headers)
        assert r.status_code == expect, (r.status_code, r.text)
        return r

    inv = INVOICES["inv-1"]

    # --- плохая подпись / старый timestamp / нет заголовков → 401, состояние не меняется
    raw, h = signed(event("invoice.paid", {**inv, "status": "paid", "is_paid": True,
                                           "amount_confirmed": "5.000000"}, "d-bad"),
                    secret="wrong")
    await hook(raw, h, 401)
    raw, h = signed(event("invoice.paid", inv, "d-old"), ts=int(time.time()) - 600)
    await hook(raw, h, 401)
    await hook(b"{}", {"Content-Type": "application/json"}, 401)
    assert await tk.balance(UID) == 0
    assert (await cp.get_topup(1))["status"] == "pending"
    assert await db.scalar("SELECT COUNT(*) FROM cryptopay_deliveries") == 0

    # --- confirming: статус и уведомление
    raw, h = signed(event("invoice.confirming", {**inv, "status": "confirming"}, "d-1"))
    r = await hook(raw, h)
    assert r.json()["action"] == "status"
    assert (await cp.get_topup(1))["status"] == "confirming"
    assert "ждём подтверждения" in bot.dm[-1][1] and bot.dm[-1][0] == UID

    # --- paid: зачисление, запись в payments, уведомление юзеру и админу
    paid = {**inv, "status": "paid", "is_paid": True, "amount_received": "5.000000",
            "amount_confirmed": "5.000000", "currency": "USDT", "network": "tron"}
    raw, h = signed(event("invoice.paid", paid, "d-2"))
    r = await hook(raw, h)
    assert r.json()["action"] == "credited"
    assert await tk.balance(UID) == 50
    t = await cp.get_topup(1)
    assert t["status"] == "paid" and t["tokens_credited"] == 50 and t["amount_credited"] == "5.00"
    assert "Начислено <b>50</b>" in bot.dm[-2][1] and bot.dm[-2][0] == UID
    assert bot.dm[-1][0] == ADMIN and "50 коинов" in bot.dm[-1][1]
    pay = await db.fetchone("SELECT * FROM payments WHERE charge_id = 'inv-1'")
    assert pay and pay["tokens"] == 50 and pay["currency"] == "USDT" and float(pay["amount"]) == 5.0
    tx = await db.fetchone("SELECT * FROM token_tx WHERE user_id = ? ORDER BY id DESC", (UID,))
    assert tx["reason"] == "crypto_purchase"

    # --- повтор той же доставки (Retry из админки) и другая доставка того же события: один раз
    r = await hook(raw, h)
    assert r.json()["action"] == "duplicate"
    raw2, h2 = signed(event("invoice.paid", paid, "d-3"))
    r = await hook(raw2, h2)
    assert r.json()["action"] == "noop"
    assert await tk.balance(UID) == 50
    assert await db.scalar("SELECT COUNT(*) FROM payments") == 1

    # --- «Проверить оплату» после зачисления тоже не дублирует
    INVOICES["inv-1"] = paid
    await feed(cb("cstatus:1"))
    assert "оплачен" in bot.alerts[-1] and await tk.balance(UID) == 50

    # --- параллельные вебхуки по одному счёту (второй счёт): зачисление ровно одно
    await feed(cb("cbuy:1"))
    inv2 = INVOICES["inv-2"]
    assert not any(c[0] == "POST" and c[1].endswith("/cancel") for c in API_CALLS), \
        "оплаченный первый счёт при выставлении второго не отменяется"
    paid2 = {**inv2, "status": "overpaid", "is_paid": True, "amount_confirmed": "12.000000",
             "currency": "USDC", "network": "bsc"}
    reqs = [signed(event("invoice.overpaid", paid2, f"d-par-{i}")) for i in range(5)]
    results = await asyncio.gather(*(hook(r_, h_) for r_, h_ in reqs))
    actions = sorted(r_.json()["action"] for r_ in results)
    assert actions == ["credited", "noop", "noop", "noop", "noop"], actions
    assert await tk.balance(UID) == 50 + 144                # 12 USD по курсу 120/10, переплата учтена

    # --- reversed: списание обратно, повтор не списывает дважды
    raw, h = signed(event("invoice.reversed", {**paid2, "status": "reversed"}, "d-rev",
                          reversal={"amount": "12.000000", "reason": "reorg"}))
    r = await hook(raw, h)
    assert r.json()["action"] == "reversed" and await tk.balance(UID) == 50
    t2 = await cp.get_topup(2)
    assert t2["status"] == "reversed" and t2["tokens_credited"] == 0
    assert "Списано обратно" in bot.dm[-2][1] and "Реверс" in bot.dm[-1][1]
    raw, h = signed(event("invoice.reversed", {**paid2, "status": "reversed"}, "d-rev2",
                          reversal={"amount": "12.000000"}))
    r = await hook(raw, h)
    assert r.json()["action"] == "noop" and await tk.balance(UID) == 50
    assert (await db.fetchone("SELECT * FROM token_tx WHERE reason = 'crypto_reversal'"))["amount"] == -144

    # --- partially_paid: зачисляем фактическую сумму, статус partial; expired для нового
    await feed(cb("cbuy:0"))
    inv3 = INVOICES["inv-3"]
    raw, h = signed(event("invoice.partially_paid", {**inv3, "status": "partially_paid",
                                                      "amount_confirmed": "2.500000"}, "d-part"))
    r = await hook(raw, h)
    assert r.json()["action"] == "credited" and await tk.balance(UID) == 75
    assert (await cp.get_topup(3))["status"] == "partial"
    assert "частично" in bot.dm[-2][1]

    await feed(cb("cbuy:0"))
    inv4 = INVOICES["inv-4"]
    raw, h = signed(event("invoice.expired", {**inv4, "status": "expired"}, "d-exp"))
    r = await hook(raw, h)
    assert r.json()["action"] == "status" and (await cp.get_topup(4))["status"] == "expired"
    # после истечения оплата «задним числом» через вебхук paid всё равно зачисляется —
    # доверяем статусу счёта, а не нашему прошлому состоянию
    raw, h = signed(event("invoice.paid", {**inv4, "status": "paid", "is_paid": True,
                                           "amount_confirmed": "5.000000"}, "d-late"))
    r = await hook(raw, h)
    assert r.json()["action"] == "credited" and await tk.balance(UID) == 125

    # --- неизвестный счёт: 200 (чтобы процессинг не долбил повторами), ничего не меняется
    raw, h = signed(event("invoice.paid", {"id": "inv-x", "external_id": "topup-777",
                                           "status": "paid", "is_paid": True,
                                           "amount_confirmed": "5"}, "d-unk"))
    r = await hook(raw, h)
    assert r.json()["action"] == "unknown" and await tk.balance(UID) == 125

    # --- «Проверить оплату» через API, когда вебхук не дошёл
    await feed(cb("cbuy:1"))
    INVOICES["inv-5"] = {**INVOICES["inv-5"], "status": "paid", "is_paid": True,
                         "amount_confirmed": "10.000000", "currency": "USDT"}
    await feed(cb("cstatus:5"))
    assert await tk.balance(UID) == 245
    assert "Начислено <b>120</b>" in bot.dm[-2][1] and bot.dm[-1][0] == ADMIN
    assert API_CALLS[-1] == ("GET", "/invoices/inv-5", None, None)

    # --- сбой API при создании счёта: запись помечается cancelled, юзеру алерт
    async def broken(method, path, payload=None, headers=None):
        raise cp.CryptoPayError("no_free_address", "later", 503)
    cp.request = broken
    await feed(cb("cbuy:0"))
    assert "Не удалось выставить счёт" in bot.alerts[-1]
    assert (await cp.get_topup(6))["status"] == "cancelled"
    cp.request = fake_request

    # --- забытый счёт: выставил, не оплатил, выставил новый и оплатил его. Старый
    # отменяется в процессинге сразу, а если он всё же истечёт — пользователю об этом не пишем
    await feed(cb("cbuy:0"))                      # topup 7, inv-7 — забыт
    old = await db.fetchone("SELECT * FROM crypto_topups WHERE user_id = ? ORDER BY id DESC", (UID,))
    await feed(cb("cbuy:1"))                      # topup 8, inv-8 — оплачивается
    assert (await cp.get_topup(old["id"]))["status"] == "cancelled", "старый pending отменён локально"
    assert INVOICES[old["invoice_id"]]["status"] == "cancelled", "и в процессинге"
    new = await db.fetchone("SELECT * FROM crypto_topups WHERE user_id = ? ORDER BY id DESC", (UID,))
    paid_new = {**INVOICES[new["invoice_id"]], "status": "paid", "is_paid": True,
                "amount_confirmed": "10.000000", "currency": "USDT", "network": "tron"}
    balance_before = await tk.balance(UID)
    await hook(*signed(event("invoice.paid", paid_new, "d-old-1")))
    assert await tk.balance(UID) == balance_before + 120
    n = len(bot.dm)
    # процессинг всё же прислал «истёк» по старому (например, отмена не прошла) — тишина
    expired_old = {**INVOICES[old["invoice_id"]], "status": "expired"}
    await db.execute("UPDATE crypto_topups SET status = 'pending' WHERE id = ?", (old["id"],))
    r = await hook(*signed(event("invoice.expired", expired_old, "d-old-2")))
    assert r.json()["action"] == "status"
    assert (await cp.get_topup(old["id"]))["status"] == "expired"
    assert len(bot.dm) == n, f"лишнее сообщение: {bot.dm[n:]}"
    # а по последнему счёту «истёк» по-прежнему сообщается
    await feed(cb("cbuy:0"))
    last = await db.fetchone("SELECT * FROM crypto_topups WHERE user_id = ? ORDER BY id DESC", (UID,))
    expired_last = {**INVOICES[last["invoice_id"]], "status": "expired"}
    await hook(*signed(event("invoice.expired", expired_last, "d-old-3")))
    assert "истёк" in bot.dm[-1][1], bot.dm[-1]
    print("забытый счёт: отменён при новом, истечение старого не беспокоит OK")

    # --- без ключа пакетов нет: вместо них заглушка «временно недоступна»
    cfg.CRYPTOPAY_API_KEY = ""
    kb = await keyboards.packages_kb()
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["noop"]
    await feed(cb("crypto"))
    assert "недоступна" in bot.alerts[-1]

    await db.close(); await sdb.close()
    print("test_cryptopay: OK")


asyncio.run(main())
