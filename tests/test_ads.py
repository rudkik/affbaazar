"""Ядро объявлений: цена, публикация в канал, закреп, удаление с возвратом коинов."""
import asyncio, os, sys, pathlib, itertools, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al, app.ads as ads
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
ads.ADMINS = {999}

from aiogram.types import Chat, Message, User
CHANNEL = -1001112223334
_ids = itertools.count(4000)


class FakeBot:
    id = 42

    def __init__(self):
        self.sent, self.deleted, self.pinned, self.unpinned, self.dm = [], [], [], [], []
        self.fail_send = False

    async def get_me(self):
        return User(id=self.id, is_bot=True, first_name="Bot", username="testbot")

    async def _send(self, chat_id, body, **kw):
        if self.fail_send:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=None, message="chat not found")
        mid = next(_ids)
        (self.dm if chat_id > 0 else self.sent).append((chat_id, body, mid))
        return Message(message_id=mid, date=0, chat=Chat(id=chat_id, type="channel"))

    async def send_message(self, chat_id, text, **kw): return await self._send(chat_id, text, **kw)
    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        return await self._send(chat_id, caption or "", **kw)
    async def send_video(self, chat_id, file_id, caption=None, **kw):
        return await self._send(chat_id, caption or "", **kw)
    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.pinned.append(message_id); return True
    async def unpin_chat_message(self, chat_id, message_id=None):
        self.unpinned.append(message_id); return True
    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id)); return True
    async def get_chat_member(self, chat_id, user_id):
        class M: status = "administrator" if user_id == 555 else "member"
        return M()


class U:
    def __init__(self, uid, username, full_name):
        self.id, self.username, self.full_name = uid, username, full_name


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.set_setting("ad_channel_title", "Канал объявлений")
    bot = FakeBot()
    user = U(1001, "buyer", "Иван Петров")
    await db.upsert_user(user.id, user.username, user.full_name)

    # --- цена ------------------------------------------------------------
    assert (await ads.price_quote())["total"] == 10
    assert (await ads.price_quote(has_image=True))["total"] == 15
    assert (await ads.price_quote(has_image=True, pin_hours=4))["total"] == 30
    assert (await ads.price_quote(pin_hours=8))["total"] == 35
    line = await ads.price_line(has_image=True, pin_hours=8)
    assert "объявление — 10" in line and "картинка — 5" in line and "закреп 8 ч — 25" in line
    print("цены OK:", line.replace("<b>", "").replace("</b>", ""))

    # --- формат поста ----------------------------------------------------
    body = ads.format_ad("Ищу байеров", "прямой_рекламодатель", "гемблинг", "buyer")
    assert body.startswith("#прямой_рекламодатель #гемблинг"), body
    assert "Ищу байеров" in body and "@buyer" in body
    print("формат поста OK:", body.split(chr(10))[0])

    # --- нехватка коинов -------------------------------------------------
    t = (await db.ad_types())[0]
    v = (await db.verticals())[1]
    try:
        await ads.publish_ad(bot, user, text="тест", ad_type_row=t, vertical_row=v)
        raise AssertionError("должно было упасть: нет коинов")
    except ads.AdError as exc:
        err = str(exc)
    assert "Не хватает коинов" in err, err
    assert await db.scalar("SELECT COUNT(*) FROM ads") == 0, "черновик не остаётся в базе"
    print("нехватка коинов OK:", err)

    # --- публикация с картинкой и закрепом --------------------------------
    await tk.add(user.id, 100, "test")
    res = await ads.publish_ad(bot, user, text="Ищу байеров под гемблу", ad_type_row=t,
                               vertical_row=v, media_type="photo", media_file_id="PHOTO1",
                               pin_hours=4)
    assert res["cost"] == 30 and res["balance"] == 70, res
    assert bot.sent and bot.sent[-1][0] == CHANNEL
    assert res["message_id"] in bot.pinned, bot.pinned
    ad = await db.fetchone("SELECT * FROM ads WHERE id = ?", (res["ad_id"],))
    assert ad["cost_base"] == 10 and ad["cost_image"] == 5 and ad["cost_pin"] == 15
    assert ad["ad_type_tag"] == t["tag"] and ad["vertical_tag"] == v["tag"]
    assert ad["pinned_until"], "срок закрепа записан"
    posts, total = await sdb.query_posts(ad_type=t["tag"])
    assert total == 1 and posts[0]["vertical_tag"] == v["tag"], posts
    print(f"публикация OK: списано {res['cost']}, закреп до {ad['pinned_until']}, на сайте {total}")

    # --- снятие закрепа по истечении времени -------------------------------
    assert await ads.unpin_expired(bot) == 0, "рано"
    await db.execute("UPDATE ads SET pinned_until = datetime('now', '-1 minute') WHERE id = ?",
                     (res["ad_id"],))
    assert await ads.unpin_expired(bot) == 1
    assert res["message_id"] in bot.unpinned, bot.unpinned
    assert await ads.unpin_expired(bot) == 0, "повторно не снимаем"
    print("снятие закрепа OK")

    # --- удаление с комментом и возвратом ---------------------------------
    bal = await tk.balance(user.id)
    out = await ads.delete_ad(bot, res["ad_id"], by_admin_id=999, comment="Дубль объявления")
    assert out["refunded"] == 30, out
    assert await tk.balance(user.id) == bal + 30
    assert (CHANNEL, res["message_id"]) in bot.deleted, bot.deleted
    assert any("Дубль объявления" in d[1] for d in bot.dm), "автор получил комментарий"
    assert any("возвращено" in d[1] for d in bot.dm)
    _, total = await sdb.query_posts()
    assert total == 0, "пост снят с сайта"
    again = await ads.delete_ad(bot, res["ad_id"], by_admin_id=999)
    assert again["already"] and again["refunded"] == 0, again
    print("удаление с комментом OK: вернули", out["refunded"], "| повторно 0")

    # --- удаление без возврата (нарушение правил) --------------------------
    res2 = await ads.publish_ad(bot, user, text="Второе", ad_type_row=t, vertical_row=v)
    bal = await tk.balance(user.id)
    out = await ads.delete_ad(bot, res2["ad_id"], by_admin_id=999, comment="Скам", refund=False)
    assert out["refunded"] == 0 and await tk.balance(user.id) == bal
    print("удаление без возврата OK")

    # --- права на кнопки под постом ---------------------------------------
    assert await ads.is_channel_admin(bot, 999) is True, "админ бота"
    assert await ads.is_channel_admin(bot, 555) is True, "админ канала"
    assert await ads.is_channel_admin(bot, 777) is False, "обычный юзер"
    print("права на кнопки OK")

    # --- канал не задан ----------------------------------------------------
    await db.set_setting("ad_channel_id", 0)
    try:
        await ads.publish_ad(bot, user, text="x", ad_type_row=t)
        raise AssertionError("должно было упасть")
    except ads.AdError as exc:
        err = str(exc)
    assert "Канал для объявлений не задан" in err, err
    print("канал не задан OK:", err)

    # --- канал недоступен: черновик не остаётся, коины не списаны ----------
    await db.set_setting("ad_channel_id", CHANNEL)
    bot.fail_send = True
    bal = await tk.balance(user.id)
    before = await db.scalar("SELECT COUNT(*) FROM ads")
    try:
        await ads.publish_ad(bot, user, text="упадёт", ad_type_row=t)
        raise AssertionError("должно было упасть")
    except ads.AdError:
        pass
    assert await db.scalar("SELECT COUNT(*) FROM ads") == before, "черновик удалён"
    assert await tk.balance(user.id) == bal, "коины не списаны"
    print("сбой публикации OK: коины не списаны, мусора в базе нет")

    print("ADS OK")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()

asyncio.run(runner())
