"""Задачи трекера AffBazaar-12…16.

12 — тексты бота правятся в админке сайта, шаблонизатор %USER% и т. п.
13 — покупка за звёзды убрана, остались только крипто-пакеты (см. также test_flows, test_cryptopay)
14 — цена публикации одна: price_post
15 — бонус за подписку: при /start, при создании объявления и по событию вступления в канал;
     повторно не начисляется даже после удаления бота и переписки
16 — закреп один на канал: пока он куплен, второй купить нельзя, бот называет точное время
"""
import asyncio, itertools, os, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"; cfg.ADMINS = {999}
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"
cfg.CRYPTOPAY_API_KEY = "cp_test_key"; cfg.CRYPTOPAY_WEBHOOK_SECRET = "whsec_test"
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al, app.ads as ads
import app.texts as texts
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR

from datetime import timedelta
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember,
                           ChatMemberUpdated, Message, Update, User)
from app.handlers import admin, chat_guard, members, moderation, payments, post, user as user_h

CHANNEL = -1005556667778
_ids = itertools.count(50000)


class FakeBot:
    id = 42

    def __init__(self):
        self.subs: set[int] = set()      # кто подписан на канал
        self.api_down = False            # get_chat_member падает (бот не админ канала)
        self.dm, self.channel, self.alerts, self.pinned = [], [], [], []

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def get_chat(self, chat_id):
        return Chat(id=chat_id, type="channel", title="Объявления", username="adschannel")

    async def get_chat_member(self, chat_id, user_id):
        if self.api_down:
            raise TelegramBadRequest(method=None, message="member list is inaccessible")
        class M:
            status = "member" if user_id in self.subs else "left"
        return M()

    async def send_message(self, chat_id, text, **kw):
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.channel).append((chat_id, text, mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="private"))

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.pinned.append(message_id); return True

    async def unpin_chat_message(self, chat_id, message_id=None): return True
    async def delete_message(self, chat_id, message_id): return True

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text)
        if name == "AnswerCallbackQuery":
            self.alerts.append(method.text or ""); return True
        if name in {"EditMessageText", "EditMessageReplyMarkup", "DeleteMessage"}:
            return True
        raise AssertionError("не смоделирован метод " + name)

    def to(self, uid):
        return [text for chat_id, text, _ in self.dm if chat_id == uid]


def tg(uid, name="Капитан <Очевидность>"):
    return User(id=uid, is_bot=False, first_name=name, username=f"u{uid}")


def priv(text, uid):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"),
        from_user=tg(uid), text=text))


def cb(data, uid):
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), chat_instance="ci", data=data, from_user=tg(uid),
        message=Message(message_id=next(_ids), date=0, chat=Chat(id=uid, type="private"))))


def joined(uid, left=False):
    old, new = ChatMemberLeft(status="left", user=tg(uid)), ChatMemberMember(status="member", user=tg(uid))
    if left:
        old, new = new, old
    return Update(update_id=next(_ids), chat_member=ChatMemberUpdated(
        chat=Chat(id=CHANNEL, type="channel", title="Канал"), from_user=tg(uid), date=0,
        old_chat_member=old, new_chat_member=new))


THANKS = "Спасибо, что подписались на наш канал"


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.set_setting("signup_bonus", 20)
    dp = Dispatcher(storage=MemoryStorage())
    for r in (admin.router, moderation.router, post.router, payments.router,
              user_h.router, chat_guard.router, members.router):
        dp.include_router(r)
    bot = FakeBot()

    # ================= 14. цена одна =======================================================
    assert "message_cost" not in db.DEFAULT_SETTINGS and "token_packages" not in db.DEFAULT_SETTINGS
    await db.set_setting("price_post", 7)
    await dp.feed_update(bot, priv("💰 Баланс / Купить коины", 101))
    assert "Стоимость объявления: <b>7</b>" in bot.to(101)[-1], bot.to(101)
    await dp.feed_update(bot, priv("💰 Баланс", 101))     # старая подпись тоже работает
    assert "Стоимость объявления: <b>7</b>" in bot.to(101)[-1]
    assert "одного сообщения" not in bot.to(101)[-1]
    await db.set_setting("price_post", 10)
    print("14 OK: одна настройка цены, в «Балансе» — цена объявления")

    # ================= 15. бонус за подписку ===============================================
    # а) уже подписан, коинов не получал -> /start начисляет и благодарит
    bot.subs.add(201)
    await dp.feed_update(bot, priv("/start", 201))
    msgs = bot.to(201)
    assert any(THANKS in m and "<b>20</b>" in m for m in msgs), msgs
    assert await tk.balance(201) == 20
    assert "Баланс: <b>20</b>" in msgs[-1], "приветствие идёт после начисления, баланс уже верный"
    # удалил бота и переписку, запустил заново — в базе всё сохранено, второго бонуса нет
    n = len(bot.dm)
    await dp.feed_update(bot, priv("/start", 201))
    assert await tk.balance(201) == 20 and not any(THANKS in t for _, t, _ in bot.dm[n:])
    # отписался и подписался снова — бонус всё равно один раз
    await dp.feed_update(bot, joined(201, left=True)); await dp.feed_update(bot, joined(201))
    assert await tk.balance(201) == 20
    assert await db.scalar("SELECT COUNT(*) FROM token_tx WHERE user_id=201 AND reason='signup_bonus'") == 1
    print("15а OK: /start подписанного — бонус и «спасибо» один раз, перезапуск не дублирует")

    # б) бот запущен, подписки нет -> потом вступил в канал: бонус по событию chat_member
    await dp.feed_update(bot, priv("/start", 202))
    assert await tk.balance(202) == 0 and not any(THANKS in m for m in bot.to(202))
    bot.subs.add(202)
    await dp.feed_update(bot, joined(202))
    assert await tk.balance(202) == 20 and THANKS in bot.to(202)[-1], bot.to(202)
    print("15б OK: вступил в канал при запущенном боте — бонус и сообщение сразу")

    # в) вступил, но бота не запускал: написать нельзя — бонус ждёт первого /start
    bot.subs.add(203)
    await dp.feed_update(bot, joined(203))
    assert await tk.balance(203) == 0 and not bot.to(203)
    row = await db.get_user(203)
    assert row["subscribed"] == 1, "сама подписка при этом учтена"
    await dp.feed_update(bot, priv("/start", 203))
    assert await tk.balance(203) == 20 and any(THANKS in m for m in bot.to(203))
    print("15в OK: бонус вступившему без бота начисляется при первом /start")

    # г) Telegram не подтвердил подписку (сбой проверки) — коины не раздаём, /start отвечает
    bot.api_down = True
    await dp.feed_update(bot, priv("/start", 204))
    assert await tk.balance(204) == 0 and bot.to(204), "приветствие пришло, бонуса нет"
    bot.api_down = False
    print("15г OK: при сбое проверки бонус не начисляется, бот не молчит")

    # д) подписан, бота запускал давно (до подписки), пришёл постить -> бонус на входе в мастер
    await db.upsert_user(205, "u205", "Старый Юзер")
    await db.accept_rules(205)
    bot.subs.add(205)
    await dp.feed_update(bot, priv("/post", 205))
    assert await tk.balance(205) == 20 and any(THANKS in m for m in bot.to(205)), bot.to(205)
    await dp.feed_update(bot, cb("ads_cancel", 205))
    print("15д OK: бонус при создании объявления")

    # ================= 12. тексты из админки ===============================================
    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"; web.set_bot(bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://t") as c:
        assert (await c.get("/admin/api/texts")).status_code == 401
        await c.post("/admin/login", data={"password": "pass"})
        reg = (await c.get("/admin/api/texts")).json()
        keys = {item["key"] for item in reg}
        assert {"txt_start", "txt_signup_bonus", "txt_ad_pin_busy", "txt_balance"} <= keys
        assert all(item["default"] and item["label"] for item in reg)
        settings = (await c.get("/admin/api/settings")).json()
        assert keys <= set(settings), "все тексты засеяны в базу и видны админке"

        r = await c.post("/admin/api/settings", json={
            "txt_start": "Йо, %USER%! У тебя <b>%BALANCE%</b> монет, бонус %BONUS%, цена %PRICE%."})
        assert r.status_code == 200, r.text
        await dp.feed_update(bot, priv("/start", 201))
        text = bot.to(201)[-1]
        assert text.startswith("Йо, <a href=\"tg://user?id=201\">") and "<b>20</b> монет" in text, text
        assert "&lt;Очевидность&gt;" in text, "имя пользователя экранировано"
        assert "бонус 20, цена 10" in text and "%" not in text, text

        # сломанная разметка не сохраняется: иначе Telegram отверг бы сообщение и бот замолчал
        r = await c.post("/admin/api/settings", json={"txt_balance": "<b>Баланс %BALANCE%",
                                                      "price_post": "99"})
        assert r.status_code == 400 and "не закрыт тег" in r.json()["error"], r.text
        assert await db.get_int("price_post") == 10, "при ошибке не сохраняется ничего"
        r = await c.post("/admin/api/settings", json={"txt_balance": "<div>x</div>"})
        assert r.status_code == 400
        r = await c.post("/admin/api/settings", json={"txt_start": "   "})
        assert r.status_code == 400 and "пустым" in r.json()["error"]
        r = await c.post("/admin/api/settings", json={"welcome_message": "%USER%, <i>привет"})
        assert r.status_code == 400, "старые текстовые настройки проверяются так же"
    # все значения по умолчанию сами проходят проверку
    for key, value in texts.defaults().items():
        assert texts.validate(key, value) is None, key
    assert texts.plain("<b>5</b> &lt; 7") == "5 < 7"
    print("12 OK: тексты правятся через админку, %USER% и переменные подставляются, "
          "битая разметка отклоняется")

    # ================= 13. только крипта ===================================================
    await dp.feed_update(bot, priv("💎 Купить коины", 201))
    assert "USDT" in bot.to(201)[-1], bot.to(201)[-1]
    from app import keyboards
    kb = await keyboards.packages_kb()
    assert all(b.callback_data.startswith("cbuy:") and "⭐" not in b.text
               for row in kb.inline_keyboard for b in row)
    print("13 OK: «Купить коины» — сразу пакеты за USDT/USDC, звёзд нет")

    # ================= 16. закреп один =====================================================
    types = {r["name"]: r for r in await db.ad_types()}
    resume = types["Резюме"]
    await tk.add(301, 500, "test"); await tk.add(302, 500, "test")
    res = await ads.publish_ad(bot, tg(301), text="Первое, с закрепом", ad_type_row=resume,
                               pin_hours=4)
    assert bot.pinned == [res["message_id"]]
    until = db.parse_iso((await ads.active_pin())["pinned_until"])

    # второй автор в мастере: кнопок закрепа нет, названо точное время
    bot.subs.add(302); await db.accept_rules(302); await db.upsert_user(302, "u302", "Второй")
    await dp.feed_update(bot, priv("/post", 302))
    await dp.feed_update(bot, cb(f"adtype:{resume['id']}", 302))
    await dp.feed_update(bot, priv("Второе объявление", 302))
    await dp.feed_update(bot, cb("ads_img_no", 302))
    busy = bot.to(302)[-1]
    assert "Закреп сейчас занят" in busy and ads.pin_until_text(until) in busy, busy
    assert "МСК" in busy and "UTC" in busy and "через 4 ч" in busy, busy
    # старая кнопка «📌 8 часов» из прошлого сообщения тоже не срабатывает
    n = len(bot.dm)
    await dp.feed_update(bot, cb("ads_pin:8", 302))
    assert "Закреп сейчас занят" in bot.dm[-1][1] and len(bot.dm) == n + 1
    # и ядро не продаст закреп в обход мастера; коины при отказе не списываются
    before = await tk.balance(302)
    try:
        await ads.publish_ad(bot, tg(302), text="в обход", ad_type_row=resume, pin_hours=8)
        raise AssertionError("закреп продан второй раз")
    except ads.PinBusyError as exc:
        assert "Закреп сейчас занят" in str(exc)
    assert await tk.balance(302) == before and len(bot.pinned) == 1
    assert await db.scalar("SELECT COUNT(*) FROM ads") == 1, "черновик не остался"
    # без закрепа публиковать можно
    await dp.feed_update(bot, cb("ads_pin:0", 302))
    await dp.feed_update(bot, cb("ads_publish", 302))
    assert await db.scalar("SELECT COUNT(*) FROM ads WHERE status='published'") == 2
    print("16а OK: занятый закреп не продаётся, время окончания:", ads.pin_until_text(until))

    # двое одновременно покупают свободный закреп — достаётся одному
    await db.execute("UPDATE ads SET pinned_until = ? WHERE pinned_until IS NOT NULL",
                     (db.iso(db.utcnow() - timedelta(minutes=1)),))
    assert await ads.active_pin() is None, "истёкший закреп продажу не блокирует"
    results = await asyncio.gather(
        ads.publish_ad(bot, tg(301), text="гонка 1", ad_type_row=resume, pin_hours=4),
        ads.publish_ad(bot, tg(302), text="гонка 2", ad_type_row=resume, pin_hours=8),
        return_exceptions=True)
    assert sum(isinstance(r, ads.PinBusyError) for r in results) == 1, results
    assert sum(isinstance(r, dict) for r in results) == 1, results
    print("16б OK: истёкший закреп освобождает слот; при гонке закреп достаётся одному")

    assert ads.pin_left_text(until, until - timedelta(minutes=135)) == "2 ч 15 мин"
    assert ads.pin_left_text(until, until - timedelta(seconds=20)) == "1 мин"
    assert ads.pin_left_text(until, until - timedelta(hours=3)) == "3 ч"

    await db.close(); await sdb.close()
    print("TRACKER OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:
        import traceback; traceback.print_exc()
        os._exit(1)
