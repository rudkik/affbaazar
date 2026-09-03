"""Вкладка «Удалённые» в админке: список удалений, фильтр по нику, счётчик нарушений."""
import asyncio, os, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al, app.ads as ads
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR

CHANNEL = -1003334445557
UID_A, UID_B = 700001, 700002


class FakeBot:
    """Бот-заглушка: публикация и удаление постов в канале никуда не уходят."""
    def __init__(self): self.deleted, self.dm = [], []

    async def send_message(self, chat_id, text, **kw):
        self.dm.append((chat_id, text))
        return type("M", (), {"message_id": 5000 + len(self.dm)})()

    async def send_photo(self, chat_id, photo, **kw):
        return await self.send_message(chat_id, kw.get("caption", ""))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id)); return True

    async def pin_chat_message(self, chat_id, message_id, **kw): return True
    async def unpin_chat_message(self, chat_id, message_id=None): return True
    async def edit_message_reply_markup(self, **kw): return True


class U:
    """Минимальный «пользователь» для прямых вызовов ads.publish_ad."""
    def __init__(self, uid, username, full_name):
        self.id, self.username, self.full_name = uid, username, full_name


async def main():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    bot = FakeBot()

    a = U(UID_A, "firstseller", "Первый Продавец")
    b = U(UID_B, "secondguy", "Второй Автор")
    for u in (a, b):
        await db.upsert_user(u.id, u.username, u.full_name)
        await tk.add(u.id, 500, "test")

    types = {r["name"]: r for r in await db.ad_types()}
    resume = types["Резюме"]

    # три объявления: два у первого юзера, одно у второго
    ad1 = await ads.publish_ad(bot, a, text="Первое объявление", ad_type_row=resume)
    ad2 = await ads.publish_ad(bot, a, text="Второе объявление", ad_type_row=resume)
    ad3 = await ads.publish_ad(bot, b, text="Третье объявление", ad_type_row=resume)

    # одно удалил модератор с комментарием, одно — сам автор
    out = await ads.delete_ad(bot, ad1["ad_id"], by_admin_id=None,
                              comment="Не по правилам", refund=True)
    assert out["delete_kind"] == "moderator", out
    out = await ads.delete_ad(bot, ad3["ad_id"], by_admin_id=UID_B, delete_kind="author")
    assert out["delete_kind"] == "author", out
    print("подготовка OK: 3 объявления, 2 удалены (модератор + автор)")

    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"
    web.set_bot(bot)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://t") as c:
        # --- без куки закрыто ---
        r = await c.get("/admin/api/ads/deleted")
        assert r.status_code == 401, r.status_code
        print("без авторизации /admin/api/ads/deleted закрыт OK")

        await c.post("/admin/login", data={"password": "pass"})

        # --- полный список ---
        d = (await c.get("/admin/api/ads/deleted")).json()
        assert d["total"] == 2 and len(d["items"]) == 2, d["total"]
        by_id = {i["id"]: i for i in d["items"]}
        first = by_id[ad1["ad_id"]]
        assert first["delete_kind"] == "moderator", first["delete_kind"]
        assert first["delete_comment"] == "Не по правилам", first["delete_comment"]
        assert first["username"] == "firstseller" and first["full_name"] == "Первый Продавец"
        assert first["ad_type_name"] == resume["name"] and first["deleted_at"], first
        assert first["refunded"] == 1 and first["cost_total"] > 0, first
        assert by_id[ad3["ad_id"]]["delete_kind"] == "author"
        assert ad2["ad_id"] not in by_id, "опубликованное не попало в список"
        print("список удалённых OK: всего", d["total"], "| кто удалил, коммент, возврат на месте")

        # --- фильтр по юзернейму (с @ и без, регистр не важен) ---
        for q in ("@firstseller", "firstseller", "FirstSeller"):
            d = (await c.get("/admin/api/ads/deleted", params={"username": q})).json()
            assert d["total"] == 1 and d["items"][0]["id"] == ad1["ad_id"], (q, d)
        d = (await c.get("/admin/api/ads/deleted", params={"username": str(UID_B)})).json()
        assert d["total"] == 1 and d["items"][0]["id"] == ad3["ad_id"], d
        print("фильтр по нику и по ID OK")

        # --- несуществующий ник ---
        d = (await c.get("/admin/api/ads/deleted", params={"username": "@nobody"})).json()
        assert d["total"] == 0 and d["items"] == [], d
        print("фильтр по несуществующему нику OK: 0")

        # --- пагинация ---
        d = (await c.get("/admin/api/ads/deleted", params={"limit": 1, "offset": 0})).json()
        assert d["total"] == 2 and len(d["items"]) == 1, d
        page2 = (await c.get("/admin/api/ads/deleted", params={"limit": 1, "offset": 1})).json()
        assert len(page2["items"]) == 1 and page2["items"][0]["id"] != d["items"][0]["id"]
        print("пагинация OK")

        # --- счётчик нарушений в списке пользователей ---
        u = (await c.get("/admin/api/users", params={"q": "firstseller"})).json()
        assert u["total"] == 1, u
        row = u["items"][0]
        assert row["deleted_total"] == 1 and row["deleted_by_moderator"] == 1, row
        assert row == {**row, **(await db.user_violations(UID_A))}, "совпадает с db.user_violations"
        row2 = next(i for i in (await c.get("/admin/api/users")).json()["items"]
                    if i["user_id"] == UID_B)
        assert row2["deleted_total"] == 1 and row2["deleted_by_moderator"] == 0, row2
        print("deleted_total/deleted_by_moderator в /admin/api/users OK")

        # --- вкладка есть в разметке админки ---
        page = (await c.get("/admin")).text
        assert 'data-tab="deleted"' in page and 'id="tab-deleted"' in page
        assert "/admin/api/ads/deleted" in page, "вкладка ходит в новый эндпоинт"
        print("вкладка «Удалённые» в admin.html OK")

    await db.close(); await sdb.close()
    print("ADMIN DELETED OK")


asyncio.run(main())
