"""Веб-админка: объявления, рубрики, цены, статистика, права доступа."""
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

CHANNEL, UID = -1003334445556, 600001


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


class FakeBot:
    """Веб-админка удаляет пост в Telegram через этот объект, сайт — качает фото."""
    def __init__(self): self.deleted, self.dm, self.downloads = [], [], []
    async def download(self, file, destination=None, **kw):
        self.downloads.append(file)
        if file.startswith("bad"):
            raise RuntimeError("wrong file_id")
        pathlib.Path(destination).write_bytes(JPEG)
    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id)); return True
    async def unpin_chat_message(self, chat_id, message_id=None): return True
    async def send_message(self, chat_id, text, **kw):
        self.dm.append((chat_id, text)); return None


async def seed():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    await db.upsert_user(UID, "seller", "Продавец")
    await tk.add(UID, 100, "test")
    types = {r["name"]: r for r in await db.ad_types()}
    verts = {r["name"]: r for r in await db.verticals()}
    t, v = types["CPA сеть"], verts["Нутра"]
    for i, (text, cost) in enumerate([("Оффер по нутре, Латам", 15), ("Второй оффер", 10)]):
        await db.execute(
            """INSERT INTO ads(user_id, channel_id, channel_message_id, ad_type_id, ad_type_name,
                               ad_type_tag, vertical_id, vertical_name, vertical_tag, text,
                               media_type, cost_base, cost_image, cost_pin, cost_total, pin_hours)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (UID, CHANNEL, 900 + i, t["id"], t["name"], t["tag"], v["id"], v["name"], v["tag"],
             text, "text", 10, 5 if i == 0 else 0, 0, cost, 0))
        await sdb.mirror_post(source_chat_id=CHANNEL, source_message_id=900 + i,
                              channel_id=CHANNEL, channel_message_id=900 + i, author_id=UID,
                              author_username="seller", text=text, media_type="text",
                              ad_type_name=t["name"], ad_type_tag=t["tag"],
                              vertical_name=v["name"], vertical_tag=v["tag"], is_reposted=1)


async def main():
    await seed()
    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"
    bot = FakeBot(); web.set_bot(bot)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://t") as c:
        # --- публичная часть ---
        r = await c.get("/api/rubrics")
        data = r.json()
        assert len(data["types"]) == 16 and len(data["verticals"]) == 20, (len(data["types"]),)
        assert any(t["tag"] == "прямой_рекламодатель" and t["has_vertical"] for t in data["types"])
        print("справочник рубрик OK: 16 типов, 20 вертикалей")

        r = await c.get("/api/posts", params={"ad_type": "cpa_сеть"})
        assert r.json()["total"] == 2, r.json()["total"]
        r = await c.get("/api/posts", params={"vertical": "нутра", "q": "Латам"})
        assert r.json()["total"] == 1, r.json()
        print("фильтры ленты по рубрике и вертикали OK")

        # --- права ---
        for path in ("/admin/api/ads", "/admin/api/rubrics", "/admin/api/stats"):
            assert (await c.get(path)).status_code == 401, path
        assert (await c.post("/admin/api/ads/1/delete", json={})).status_code == 401
        print("без авторизации админ-API закрыт OK")

        await c.post("/admin/login", data={"password": "pass"})

        # --- объявления ---
        r = await c.get("/admin/api/ads")
        body = r.json()
        assert body["total"] == 2 and body["items"][0]["username"] == "seller", body["total"]
        assert (await c.get("/admin/api/ads", params={"q": "Латам"})).json()["total"] == 1
        assert (await c.get("/admin/api/ads", params={"ad_type": "cpa_сеть"})).json()["total"] == 2
        assert (await c.get("/admin/api/ads", params={"status": "deleted"})).json()["total"] == 0
        print("список объявлений и фильтры OK")

        # --- удаление с комментарием и возвратом ---
        ad_id = body["items"][0]["id"]
        cost = body["items"][0]["cost_total"]
        bal = await tk.balance(UID)
        r = await c.post(f"/admin/api/ads/{ad_id}/delete",
                         json={"comment": "Не по правилам", "refund": True})
        out = r.json()
        assert out["ok"] and out["refunded"] == cost, out
        assert await tk.balance(UID) == bal + cost
        assert (CHANNEL, 901) in bot.deleted or (CHANNEL, 900) in bot.deleted, bot.deleted
        assert any("Не по правилам" in t for _, t in bot.dm), bot.dm
        assert (await c.get("/api/posts")).json()["total"] == 1, "пост ушёл из ленты"
        r = await c.post(f"/admin/api/ads/{ad_id}/delete", json={})
        assert r.json()["already"] and r.json()["refunded"] == 0, r.json()
        print("удаление из админки OK: возврат", out["refunded"], "коинов, повторно 0")

        # --- лента модерации: страница и удаление без возврата по id поста сайта ---
        assert 'data-admin-feed=""' in (await c.get("/")).text, "на сайте кнопок модерации нет"
        assert 'data-admin-feed="1"' in (await c.get("/admin/feed")).text
        post = (await c.get("/api/posts")).json()["items"][0]
        r = await c.get("/admin/api/ads", params={"status": "published"})
        ad2 = r.json()["items"][0]
        bal = await tk.balance(UID)
        r = await c.post(f"/admin/api/posts/{post['id']}/delete",
                         json={"comment": "Скам", "refund": False})
        assert r.status_code == 200 and r.json()["refunded"] == 0, r.text
        assert await tk.balance(UID) == bal
        assert (CHANNEL, post["source_message_id"]) in bot.deleted
        assert (await db.fetchone("SELECT status FROM ads WHERE id = ?", (ad2["id"],)))["status"] == "deleted"
        assert (await c.get("/api/posts")).json()["total"] == 0, "лента пуста"
        adm = (await c.get("/api/posts", params={"include_deleted": "true"})).json()
        assert adm["total"] == 2 and all(p["is_deleted"] for p in adm["items"]), "админ видит удалённые"
        info = {p["delete_comment"]: p["delete_refund"] for p in adm["items"]}
        assert info == {"Не по правилам": cost, "Скам": 0}, "возврат и причина из базы бота: %r" % info
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://t") as anon:
            assert (await anon.get("/api/posts", params={"include_deleted": "true"})).json()["total"] == 0, \
                "публике удалённые не отдаются"
        assert (await c.post("/admin/api/posts/777/delete", json={})).status_code == 404
        print("лента модерации OK: флаг страницы, удаление без возврата по посту")

        # --- рубрики ---
        r = await c.get("/admin/api/rubrics")
        types = r.json()["types"]
        target = next(t for t in types if t["tag"] == "резюме")
        r = await c.post(f"/admin/api/rubrics/types/{target['id']}",
                         json={"has_vertical": 1, "name": "Резюме и CV"})
        assert r.status_code == 200, r.text
        r = await c.get("/api/rubrics")
        upd = next(t for t in r.json()["types"] if t["id"] == target["id"])
        assert upd["has_vertical"] and upd["name"] == "Резюме и CV", upd
        print("правка рубрики OK:", upd["name"], "| вертикаль:", upd["has_vertical"])

        r = await c.post("/admin/api/rubrics/types", json={"name": "Новая рубрика"})
        assert r.status_code == 200, r.text
        created = (await c.get("/api/rubrics")).json()["types"]
        new_one = next((t for t in created if t["name"] == "Новая рубрика"), None)
        assert new_one and new_one["tag"] == "новая_рубрика", new_one
        print("создание рубрики OK, тег сгенерирован:", new_one["tag"])

        await c.post(f"/admin/api/rubrics/types/{new_one['id']}", json={"is_active": 0})
        visible = [t["id"] for t in (await c.get("/api/rubrics")).json()["types"]]
        assert new_one["id"] not in visible, "выключенная рубрика скрыта из публичного списка"
        print("выключение рубрики OK")

        r = await c.post("/admin/api/rubrics/verticals", json={"name": "Тестовая вертикаль"})
        assert r.status_code == 200
        assert any(v["tag"] == "тестовая_вертикаль"
                   for v in (await c.get("/api/rubrics")).json()["verticals"])
        print("создание вертикали OK")

        # --- цены и правила через настройки ---
        r = await c.post("/admin/api/settings", json={"price_post": "20", "price_image": "7",
                                                      "price_pin_4h": "30", "price_pin_8h": "50",
                                                      "rules_text": "Новые правила"})
        s = r.json()["settings"]
        assert s["price_post"] == "20" and s["rules_text"] == "Новые правила", s
        q = await ads.price_quote(has_image=True, pin_hours=8)
        assert q["total"] == 20 + 7 + 50, q
        print("цены из админки применяются OK: итог", q["total"], "коинов")

        # --- статистика ---
        st = (await c.get("/admin/api/stats")).json()
        assert st["ads_published"] == 0 and st["ads_deleted"] == 2, st
        assert st["coins_refunded_ads"] >= cost, st
        assert isinstance(st["by_type"], list)
        assert "users" in st and "tokens_balance" in st, "старые ключи на месте"
        print("статистика OK: опубликовано", st["ads_published"], "удалено", st["ads_deleted"],
              "| возвращено коинов", st["coins_refunded_ads"])

        # --- фото объявления: /media/<file_id> качает из Telegram один раз, дальше с диска ---
        web.MEDIA_DIR = tmp / "media"
        fid = "AgACAgIAAxkBAAIBb2b" + "x" * 30
        r = await c.get(f"/media/{fid}")
        assert r.status_code == 200 and r.content == JPEG, (r.status_code, r.headers)
        assert r.headers["content-type"] == "image/jpeg" and "immutable" in r.headers["cache-control"]
        r = await c.get(f"/media/{fid}")
        assert r.status_code == 200 and bot.downloads.count(fid) == 1, "второй раз — с диска"
        assert not list((tmp / "media").glob("*.part")), "временных файлов не осталось"
        assert (await c.get("/media/bad" + "y" * 30)).status_code == 404, "Telegram отказал → 404"
        assert (await c.get("/media/../../etc/passwd")).status_code == 404
        assert (await c.get("/media/short")).status_code == 404
        assert len(bot.downloads) == 2, bot.downloads
        print("фото объявления OK: скачано один раз, кэш на диске, плохие id → 404")

        # --- админка: пользователи без горизонтального скролла ---
        page = (await c.get("/admin")).text
        assert "Приглашено<br>" in page and "#tab-users th{white-space:normal" in page
        print("таблица пользователей OK: заголовки в две строки")

        # --- выход ---
        await c.get("/admin/logout"); c.cookies.clear()
        assert (await c.get("/admin/api/ads")).status_code == 401
        print("выход OK")

    await db.close(); await sdb.close()
    print("WEB ADMIN OK")


asyncio.run(main())
