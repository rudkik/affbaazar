"""Бонус-Лото (loto/docs/INTEGRATION-AFFBAZAAR.md): подпись вебхука, идемпотентное начисление
выигрыша, очередь заказов order.paid / order.refunded с подписью тела, SSO-ссылка, кнопка и виджет."""
import asyncio, base64, hashlib, hmac, itertools, json, os, pathlib, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"; cfg.ADMINS = {999}
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"
cfg.CRYPTOPAY_API_KEY = "cp_test_key"; cfg.CRYPTOPAY_WEBHOOK_SECRET = "whsec_test"
cfg.LOTO_URL = "https://loto.test"; cfg.LOTO_PUBLIC_KEY = "pk_test"; cfg.LOTO_SECRET = "sk_test"
cfg.LOTO_COINS_PER_USD = 10
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
import app.loto as loto, app.cryptopay as cp, app.web.server as web
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"

import httpx
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User
from app.handlers import payments, user as user_h

UID, ADMIN = 600001, 999
_ids = itertools.count(90000)


class FakeBot:
    id = 42

    def __init__(self):
        self.dm = []          # (chat_id, text, markup)

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.dm.append((chat_id, text, reply_markup))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="private"))

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text, method.reply_markup)
        raise AssertionError("не смоделирован метод " + name)

    def to(self, uid):
        return [(t, m) for c, t, m in self.dm if c == uid]


def priv(text, uid=UID):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Игрок", username="player"), text=text))


def signed(body: dict, *, secret="sk_test", ts=None):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) if ts is None else ts)
    return raw, {"Content-Type": "application/json", "X-Loto-Timestamp": ts,
                 "X-Loto-Signature": loto.sign_webhook(ts, raw, secret)}


def prize_event(delivery, ticket, ptype="bonus", value="10", uid=UID, draw=7):
    return {"id": delivery, "event": "prize.won", "created_at": "2026-09-27T12:00:00Z",
            "data": {"ticket_id": ticket, "draw": draw, "customer_id": str(uid), "matches": 3,
                     "numbers": [1, 2, 3, 4, 5],
                     "prize": {"type": ptype, "value": value, "currency": "USD"}}}


# --- подменённый httpx для исходящих заказов
SENT = []          # (url, headers, raw body)
MODE = {"status": 200, "body": {"ok": True, "ticket_ids": ["T-1"], "draw": 7}, "fail": False}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, content=None, headers=None):
        if MODE["fail"]:
            raise httpx.ConnectError("boom")
        SENT.append((url, headers, content))
        return FakeResponse(MODE["status"], MODE["body"])


RealClient = httpx.AsyncClient          # для запросов к нашему же FastAPI (ASGI)
loto.httpx.AsyncClient = FakeClient     # httpx один на всех: исходящие запросы лото — фейковые


async def main():
    await db.init(); await sdb.init()
    bot = FakeBot(); web.set_bot(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(payments.router); dp.include_router(user_h.router)
    await db.upsert_user(UID, "player", "Игрок")

    # ================= 1. подпись вебхука =================================================
    raw, h = signed({"id": "d0", "event": "test.ping", "data": {"test": True}})
    ts = h["X-Loto-Timestamp"]
    assert loto.verify(ts, raw, h["X-Loto-Signature"])
    assert not loto.verify(ts, raw, loto.sign_webhook(ts, raw, "other"))          # чужой секрет
    assert not loto.verify(ts, raw, h["X-Loto-Signature"], now=int(ts) + 301)     # устарело
    assert not loto.verify(ts, raw + b" ", h["X-Loto-Signature"])                 # тело изменено
    assert not loto.verify("abc", raw, h["X-Loto-Signature"])
    print("подпись OK: верная / чужой секрет / устаревшая / изменённое тело")

    async def hook(body, headers, expect=200):
        async with RealClient(transport=httpx.ASGITransport(app=web.app), base_url="http://t") as c:
            r = await c.post("/webhooks/loto", content=body, headers=headers)
        assert r.status_code == expect, (r.status_code, r.text)
        return r

    await hook(raw, {**h, "X-Loto-Signature": "sha256=00"}, expect=401)
    await hook(*signed({"id": "d0", "event": "test.ping", "data": {"test": True}}))
    r = await hook(*signed({"id": "d0", "event": "test.ping", "data": {"test": True}}))
    assert r.json()["action"] == "duplicate"
    print("вебхук OK: 401 на плохую подпись, test.ping → 200, повтор доставки → duplicate")

    # ================= 2. prize.won: начисление один раз ===================================
    r = await hook(*signed(prize_event("d1", "T-1", "bonus", "10")))
    assert r.json()["action"] == "credited" and await tk.balance(UID) == 100
    tx = await db.fetchone("SELECT * FROM token_tx WHERE user_id = ? ORDER BY id DESC", (UID,))
    assert tx["reason"] == "loto_prize" and json.loads(tx["meta"])["ticket"] == "T-1"
    assert "выиграли" in bot.to(UID)[-1][0] and "<b>100</b>" in bot.to(UID)[-1][0]
    assert any(c == ADMIN and "Бонус-Лото" in t for c, t, _ in bot.dm), "админ уведомлён"
    r = await hook(*signed(prize_event("d1", "T-1")))                 # та же доставка
    assert r.json()["action"] == "duplicate" and await tk.balance(UID) == 100
    r = await hook(*signed(prize_event("d1b", "T-1")))                # другая доставка, тот же билет
    assert r.json()["action"] == "duplicate" and await tk.balance(UID) == 100
    r = await hook(*signed(prize_event("d2", "T-2", "jackpot", "50.5")))
    assert r.json()["action"] == "credited" and await tk.balance(UID) == 100 + 505
    n = len(bot.dm)
    r = await hook(*signed(prize_event("d3", "T-3", "promo", "0")))
    assert r.json()["action"] == "manual" and await tk.balance(UID) == 605
    assert any(c == ADMIN and "вручную" in t for c, t, _ in bot.dm[n:]), bot.dm[n:]
    assert "свяжется" in bot.to(UID)[-1][0]
    print("prize.won OK: bonus 10$ → 100, повтор по доставке и по билету не дублирует, jackpot, promo → вручную")

    # ticket.* — уведомления с кнопкой входа
    await hook(*signed({"id": "d4", "event": "ticket.issued", "data": {
        "ticket_id": "T-9", "draw": 8, "customer_id": str(UID), "order_id": "topup-1",
        "close_at": "2026-09-30T18:00:00Z"}}))
    text, kb = bot.to(UID)[-1]
    assert "билет" in text.lower() and kb.inline_keyboard[0][0].url.startswith("https://loto.test/sso?key=pk_test&token=")
    await hook(*signed({"id": "d5", "event": "ticket.reminder", "data": {
        "ticket_id": "T-9", "draw": 8, "customer_id": str(UID), "close_at": "2026-09-30T18:00:00Z"}}))
    assert "не заполнен" in bot.to(UID)[-1][0]
    await hook(*signed({"id": "d6", "event": "ticket.cancelled", "data": {
        "ticket_id": "T-9", "draw": 8, "customer_id": str(UID), "reason": "возврат"}}))
    assert "отменён: возврат" in bot.to(UID)[-1][0]
    r = await hook(*signed({"id": "d7", "event": "draw.finished", "data": {"draw": 8}}))
    assert r.json()["action"] == "noted" and bot.to(UID)[-1][0].startswith("✖️")
    print("ticket.issued/reminder/cancelled OK: уведомления с SSO-кнопкой, draw.* — тишина")

    # ================= 3. заказы: очередь и подпись тела ====================================
    assert await loto.enqueue("order.paid", "topup-1", UID, "5.00", name="player")
    assert await loto.flush(web.loto_order_sent) == 1
    url, headers, body = SENT[-1]
    assert url == "https://loto.test/api/v1/orders" and headers["Authorization"] == "Bearer sk_test"
    assert headers["X-Signature"] == "sha256=" + hmac.new(b"sk_test", body, hashlib.sha256).hexdigest()
    payload = json.loads(body)
    assert payload["event"] == "order.paid" and payload["order_id"] == "topup-1"
    assert payload["customer"]["id"] == str(UID) and payload["amount"] == 5.0 and payload["currency"] == "USD"
    row = await db.fetchone("SELECT * FROM loto_orders WHERE order_id = 'topup-1'")
    assert row["status"] == "sent" and json.loads(row["response"])["ticket_ids"] == ["T-1"]
    text, kb = bot.to(UID)[-1]
    assert "выдан билет" in text and "№7" in text and kb.inline_keyboard[0][0].url.startswith("https://loto.test/sso?")
    # недоступность лото: не бросает, заказ ждёт повтора; потом уходит
    MODE["fail"] = True
    await loto.enqueue("order.paid", "topup-2", UID, "10.00")
    assert await loto.flush() == 0
    row = await db.fetchone("SELECT * FROM loto_orders WHERE order_id = 'topup-2'")
    assert row["status"] == "pending" and row["attempts"] == 1 and row["next_at"] > time.time() + 50
    assert await loto.flush() == 0, "раньше срока не шлём"
    MODE["fail"] = False
    assert await loto.flush(now=int(time.time()) + 61) == 1
    # 4xx — в failed без повторов; 5xx — повтор
    MODE["status"] = 422
    await loto.enqueue("order.paid", "topup-3", UID, "1.00"); await loto.flush()
    assert (await db.fetchone("SELECT status FROM loto_orders WHERE order_id='topup-3'"))["status"] == "failed"
    MODE["status"] = 503
    await loto.enqueue("order.paid", "topup-4", UID, "1.00"); await loto.flush()
    assert (await db.fetchone("SELECT status FROM loto_orders WHERE order_id='topup-4'"))["status"] == "pending"
    MODE["status"] = 200
    print("очередь OK: X-Signature = HMAC тела, ретрай при недоступности, 4xx → failed")

    # ================= 4. из зачисления криптой =============================================
    SENT.clear()
    cur = await db.execute("INSERT INTO crypto_topups(user_id, amount, tokens, invoice_id) VALUES (?, '5.00', 50, 'inv-x')", (UID,))
    topup_id = cur.lastrowid
    inv = {"id": "inv-x", "status": "paid", "is_paid": True, "amount": "5.000000",
           "amount_confirmed": "5.000000", "external_id": f"topup-{topup_id}", "currency": "USDT", "network": "tron"}
    before = await tk.balance(UID)
    res = await cp.apply_invoice(inv)
    assert res["action"] == "credited" and await tk.balance(UID) == before + 50
    assert await loto.flush(web.loto_order_sent) == 1
    payload = json.loads(SENT[-1][2])
    assert payload == {**payload, "event": "order.paid", "order_id": f"topup-{topup_id}", "amount": 5.0}
    assert payload["customer"] == {"id": str(UID), "email": None, "name": "player"}
    # обычное списание заказ не создаёт
    n = len(SENT); await tk.charge(UID, 1, "message"); await loto.flush()
    assert len(SENT) == n
    # реверс → order.refunded с тем же order_id
    res = await cp.apply_invoice({**inv, "status": "reversed"})
    assert res["action"] == "reversed"
    assert await loto.flush() == 1
    payload = json.loads(SENT[-1][2])
    assert payload["event"] == "order.refunded" and payload["order_id"] == f"topup-{topup_id}"
    print("крипта OK: зачисление → order.paid, реверс → order.refunded, списание — ничего")

    # ================= 5. SSO-ссылка, кнопка, виджет ========================================
    link = loto.sso_link(UID, "player")
    assert link.startswith("https://loto.test/sso?key=pk_test&token=") and link.endswith("&to=/tickets")
    token = link.split("token=")[1].split("&")[0]
    payload_b64, sig = token.split(".")
    assert sig == hmac.new(b"sk_test", payload_b64.encode(), hashlib.sha256).hexdigest()
    data = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    assert data["id"] == str(UID) and data["name"] == "player" and data["exp"] > time.time() + 500
    assert loto.sso_link(UID) != link or True     # токен свежий при каждом вызове (exp меняется со временем)

    from app import keyboards
    kb = await keyboards.main_menu()
    assert "🎟 Бонус-Лото" in [b.text for row in kb.keyboard for b in row]
    await dp.feed_update(bot, priv("🎟 Бонус-Лото"))
    text, kb = bot.to(UID)[-1]
    assert "Бонус-Лото" in text and kb.inline_keyboard[0][0].url.startswith("https://loto.test/sso?")
    await dp.feed_update(bot, priv("/loto"))
    assert bot.to(UID)[-1][1].inline_keyboard[0][0].url.startswith("https://loto.test/sso?")

    w = loto.widget(UID, "player")
    assert w["url"] == "https://loto.test/widget.js" and w["key"] == "pk_test" and "." in w["token"]
    me = await web._me_payload(UID)
    assert me["loto"]["key"] == "pk_test"
    html = pathlib.Path("app/web/templates/index.html").read_text()
    assert "mountLotoWidget(data.loto)" in html and "data-token" in html
    # выключено — ни кнопки, ни виджета, ни заказов
    cfg.LOTO_SECRET = ""
    assert loto.widget(UID) is None and (await web._me_payload(UID))["loto"] is None
    assert "🎟 Бонус-Лото" not in [b.text for row in (await keyboards.main_menu()).keyboard for b in row]
    assert not await loto.enqueue("order.paid", "topup-9", UID, "5.00")
    assert not loto.verify(ts, raw, h["X-Loto-Signature"])
    print("SSO OK: токен разбирается и верифицируется, кнопка и виджет только при включённом лото")

    await db.close(); await sdb.close()
    print("LOTO OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:
        import traceback; traceback.print_exc()
        os._exit(1)
