"""Личка бота: рефералы, админ-команды, оплата, кнопка «Я подписался», автоудаление."""
import asyncio, os, sys, pathlib, itertools, json, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"
cfg.ADMINS = {999}; cfg.PAYMENT_PROVIDER_TOKEN = ""
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
import app.handlers.user as uh, app.handlers.admin as ah, app.handlers.payments as ph
uh.ADMINS = {999}; ah.ADMINS = {999}; ph.ADMINS = {999}

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, Chat, Message, SuccessfulPayment, Update, User)
from app.handlers import admin, chat_guard, payments, user as user_h

CHAT_ID, CHANNEL_ID, ADMIN, A, B = -100777, -100888, 999, 800001, 800002
_ids = itertools.count(9000)


class FakeBot:
    """Понимает и прямые вызовы (bot.send_message), и объекты-методы aiogram."""
    id = 42

    def __init__(self):
        self.subscribed = True
        self.sent, self.deleted, self.alerts, self.invoices, self.edits = [], [], [], [], []

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def get_chat_administrators(self, chat_id):
        class A_: user = User(id=ADMIN, is_bot=False, first_name="Admin")
        return [A_()]

    async def get_chat_member(self, chat_id, user_id):
        class M: status = "member" if self.subscribed else "left"
        return M()

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="private"))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id)); return True

    async def send_invoice(self, chat_id, title, description, payload, currency, prices, **kw):
        self.invoices.append((chat_id, title, currency, prices[0].amount, payload))
        return Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type="private"))

    async def answer_pre_checkout_query(self, *a, **kw): return True
    async def restrict_chat_member(self, *a, **kw): return True

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text)
        if name == "AnswerCallbackQuery":
            self.alerts.append(method.text or ""); return True
        if name == "EditMessageText":
            self.edits.append(method.text); return True
        if name == "DeleteMessage":
            return await self.delete_message(method.chat_id, method.message_id)
        if name == "AnswerPreCheckoutQuery":
            return True
        raise AssertionError("не смоделирован метод " + name)


def priv(text, uid, username="u"):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="U", username=username), text=text))


def group(text, uid, username="u"):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=CHAT_ID, type="supergroup", title="Чат"),
        from_user=User(id=uid, is_bot=False, first_name="U", username=username), text=text))


def cb(data, uid, chat_id, chat_type="supergroup"):
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data,
        from_user=User(id=uid, is_bot=False, first_name="U", username="u"),
        message=Message(message_id=next(_ids), date=0, chat=Chat(id=chat_id, type=chat_type))))


def paid(uid, tokens, stars):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="U", username="u"),
        successful_payment=SuccessfulPayment(
            currency="XTR", total_amount=stars,
            invoice_payload=json.dumps({"tokens": tokens, "index": 0}),
            telegram_payment_charge_id="charge_abc", provider_payment_charge_id="prov_1")))


async def main():
    await db.init(); await sdb.init()
    await db.upsert_chat(CHAT_ID, "Чат")
    await db.set_required_channels(CHAT_ID, [{"channel_id": CHANNEL_ID, "title": "К", "username": "ch"}])
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, payments.router, user_h.router, chat_guard.router):
        dp.include_router(r)
    bot = FakeBot()

    # --- /start и реферальная ссылка -------------------------------------
    await dp.feed_update(bot, priv("/start", A, "alice"))
    assert "Привет" in bot.sent[-1][1], bot.sent[-1]
    await dp.feed_update(bot, priv(f"/start ref_{A}", B, "bob"))
    row = await db.get_user(B)
    assert row["referrer_id"] == A, dict(row)
    print("реферальная ссылка OK: реферер B =", row["referrer_id"])

    # чужую ссылку уже активированному не подставляем
    await db.execute("UPDATE users SET activated = 1, referrer_id = NULL WHERE user_id = ?", (B,))
    await dp.feed_update(bot, priv("/start ref_777", B, "bob"))
    assert (await db.get_user(B))["referrer_id"] is None, "реферер только для новых"
    await db.execute("UPDATE users SET activated = 0, referrer_id = ? WHERE user_id = ?", (A, B))
    print("античит рефералов OK: активированному реферер не проставляется")

    # --- меню пользователя ------------------------------------------------
    for btn, marker in [("💰 Баланс", "Баланс"), ("📊 Мой профиль", "Профиль"),
                        ("👥 Пригласить друга", f"start=ref_{A}"), ("💎 Купить токены", "пакет")]:
        await dp.feed_update(bot, priv(btn, A, "alice"))
        assert marker in bot.sent[-1][1], (btn, bot.sent[-1][1])
    print("меню пользователя OK (баланс / профиль / реф-ссылка / пакеты)")

    # --- кнопка «✅ Я подписался» -----------------------------------------
    bot.subscribed = False
    await dp.feed_update(bot, cb("check_sub", B, CHAT_ID))
    assert "Подписка не найдена" in bot.alerts[-1], bot.alerts[-1]
    bot.subscribed = True
    await dp.feed_update(bot, cb("check_sub", B, CHAT_ID))
    assert "Можешь писать" in bot.alerts[-1], bot.alerts[-1]
    user = await db.get_user(B)
    assert user["activated"] == 1 and user["tokens"] == 30, dict(user)
    assert await tk.balance(A) == 10, "реферер получил бонус"
    print("кнопка «Я подписался» OK: B =", user["tokens"], "токенов, A =", await tk.balance(A))

    # --- админ-команды ----------------------------------------------------
    await dp.feed_update(bot, priv("/msg_ttl 60", ADMIN, "admin"))
    assert await db.get_int("msg_ttl") == 60, await db.get_int("msg_ttl")
    await dp.feed_update(bot, priv("/лимит 7", ADMIN, "admin"))          # русский алиас
    assert await db.get_int("check_limit") == 7, await db.get_int("check_limit")
    await dp.feed_update(bot, priv("/блокировка 24", ADMIN, "admin"))
    assert await db.get_int("restrict_hours") == 24
    await dp.feed_update(bot, priv("/restricted_text Стоп, слишком много попыток", ADMIN, "admin"))
    assert (await db.get_setting("restricted_text")).startswith("Стоп")
    print("настройки через команды OK: msg_ttl=60, лимит=7 (русский алиас), блокировка=24 ч")

    before = await tk.balance(B)
    await dp.feed_update(bot, priv("/give @bob 50", ADMIN, "admin"))
    assert await tk.balance(B) == before + 50, await tk.balance(B)
    assert any(s[0] == B and "начислил" in s[1] for s in bot.sent[-2:]), "юзер уведомлён"
    await dp.feed_update(bot, priv("/выдать @bob -20", ADMIN, "admin"))   # русский алиас, списание
    assert await tk.balance(B) == before + 30, await tk.balance(B)
    print("выдача токенов OK: баланс B =", await tk.balance(B))

    await dp.feed_update(bot, priv("/статистика", ADMIN, "admin"))
    assert "Статистика" in bot.sent[-1][1] and "Пользователей" in bot.sent[-1][1]
    await dp.feed_update(bot, priv("/help", ADMIN, "admin"))
    assert "/connect_chat" in bot.sent[-1][1]
    print("статистика и /help OK")

    # не-админ до админских команд не достаёт
    n = len(bot.sent)
    await dp.feed_update(bot, priv("/give @bob 1000", B, "bob"))
    assert await tk.balance(B) == before + 30, "не-админ не может выдавать токены"
    await dp.feed_update(bot, priv("/статистика", B, "bob"))
    assert all("Статистика" not in s[1] for s in bot.sent[n:]), bot.sent[n:]
    print("права админа OK: обычный юзер команды не получает")

    # --- покупка токенов ---------------------------------------------------
    await dp.feed_update(bot, cb("buy:0", B, B, chat_type="private"))
    assert bot.invoices, "счёт выставлен"
    chat_id, title, currency, amount, payload = bot.invoices[-1]
    assert chat_id == B and currency == "XTR" and amount == 50, bot.invoices[-1]
    print("счёт OK:", title, "/", amount, currency)

    before = await tk.balance(B)
    await dp.feed_update(bot, paid(B, tokens=50, stars=50))
    assert await tk.balance(B) == before + 50, await tk.balance(B)
    pay = await db.fetchone("SELECT * FROM payments WHERE charge_id = 'charge_abc'")
    assert pay and pay["tokens"] == 50 and pay["currency"] == "XTR", dict(pay) if pay else None
    assert any(s[0] == ADMIN and "Оплата" in s[1] for s in bot.sent), "админ уведомлён об оплате"
    print("оплата OK: начислено 50, баланс =", await tk.balance(B))

    # --- автоудаление сообщения бота по таймеру ---------------------------
    await db.set_setting("msg_ttl", 1)
    bot.subscribed = False
    upd = group("проверка ттл", B, "bob")
    await dp.feed_update(bot, upd)
    prompt_id = (await db.get_state(B, CHAT_ID))["last_prompt_msg_id"]
    assert prompt_id, "подсказка отправлена"
    assert (CHAT_ID, prompt_id) not in bot.deleted, "сразу не удаляется"
    await asyncio.sleep(1.6)
    assert (CHAT_ID, prompt_id) in bot.deleted, bot.deleted[-3:]
    print("автоудаление OK: подсказка", prompt_id, "снята через 1 с")

    print("FLOWS OK")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
