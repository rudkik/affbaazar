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
        home = (await c.get("/")).text
        assert 'data-admin-feed=""' in home, "на сайте кнопок модерации нет"
        assert "G-SZFT16PCD2" in home and "location.pathname !== '/admin/feed'" in home, "Google tag на сайте"
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
        bad = "bad" + "y" * 30
        video = "vid" + "v" * 30
        for f, mt in ((fid, "photo"), (bad, "photo"), (video, "video")):
            await sdb.mirror_post(source_chat_id=CHANNEL, source_message_id=hash(f) % 10**6,
                                  channel_id=CHANNEL, channel_message_id=1, author_id=UID,
                                  text="с фото", media_type=mt, media_file_id=f)
        r = await c.get(f"/media/{fid}")
        assert r.status_code == 200 and r.content == JPEG, (r.status_code, r.headers)
        assert r.headers["content-type"] == "image/jpeg" and "immutable" in r.headers["cache-control"]
        r = await c.get(f"/media/{fid}")
        assert r.status_code == 200 and bot.downloads.count(fid) == 1, "второй раз — с диска"
        assert not list((tmp / "media").glob("*.part")), "временных файлов не осталось"
        assert (await c.get(f"/media/{bad}")).status_code == 404, "Telegram отказал → 404"
        assert (await c.get("/media/../../etc/passwd")).status_code == 404
        assert (await c.get("/media/short")).status_code == 404
        assert (await c.get("/media/" + "z" * 40)).status_code == 404, "id не из ленты"
        assert (await c.get(f"/media/{video}")).status_code == 404, "только фото, видео не качаем"
        # параллельные запросы одного файла — одна закачка
        fid2 = "AgACAgIAAxkBAAIBb2c" + "q" * 30
        await sdb.mirror_post(source_chat_id=CHANNEL, source_message_id=424242, channel_id=CHANNEL,
                              channel_message_id=1, author_id=UID, text="x", media_type="photo",
                              media_file_id=fid2)
        rs = await asyncio.gather(*[c.get(f"/media/{fid2}") for _ in range(5)])
        assert all(r.status_code == 200 for r in rs) and bot.downloads.count(fid2) == 1, bot.downloads
        assert [d for d in bot.downloads if d != fid2] == [fid, bad], \
            "чужие id в Telegram не ходят: %r" % bot.downloads
        print("фото объявления OK: скачано один раз, кэш на диске, чужие/плохие id → 404 без Telegram")

        # --- админская кука: со сроком, чужая/просроченная не работает; неверный пароль — медленно ---
        import time as _time
        good = c.cookies.get("session")
        assert good and good.split(".")[0].isdigit() and int(good.split(".")[0]) > _time.time()
        expired = web._token(int(_time.time()) - 1)
        c.cookies.clear(); c.cookies.set("session", expired)
        assert (await c.get("/admin/api/ads")).status_code == 401, "просроченная кука"
        forged = ("1" if good[0] != "1" else "2") + good[1:]
        c.cookies.clear(); c.cookies.set("session", forged)
        assert (await c.get("/admin/api/ads")).status_code == 401, "подделанный срок"
        c.cookies.clear(); c.cookies.set("session", good)
        t0 = _time.monotonic()
        r = await c.post("/admin/login", data={"password": "wrong"})
        assert r.status_code == 303 and "e=1" in r.headers["location"]
        assert _time.monotonic() - t0 >= 0.9, "неверный пароль должен стоить ~1 с"
        assert (await c.get("/admin/api/ads")).status_code == 200, "кука не затёрта"
        assert (await c.get("/openapi.json")).status_code == 404, "схема API закрыта"
        # отрицательный limit в SQLite = «без ограничения» — зажимаем
        assert len((await c.get("/admin/api/ads", params={"limit": -1})).json()["items"]) <= 1
        assert len((await c.get("/api/posts", params={"limit": -1, "include_deleted": "true"})).json()["items"]) <= 1
        # кука Telegram с флагом adm, но id не в ADMINS — не админ
        import app.auth as auth_mod
        c.cookies.clear(); c.cookies.set(auth_mod.SESSION_COOKIE, auth_mod.issue_session(123, "x", True))
        assert (await c.get("/admin/api/ads")).status_code == 401, "adm в куке не важен, важен ADMINS"
        c.cookies.clear(); c.cookies.set(auth_mod.SESSION_COOKIE, auth_mod.issue_session(999, "a", False))
        assert (await c.get("/admin/api/ads")).status_code == 200, "id из ADMINS — админ"
        c.cookies.clear(); c.cookies.set("session", good)
        print("админская кука OK: срок, подделка, задержка при неверном пароле")

        # --- админка: пользователи без горизонтального скролла ---
        page = (await c.get("/admin")).text
        assert "Приглашено<br>" in page and "#tab-users th{white-space:normal" in page
        print("таблица пользователей OK: заголовки в две строки")

        # --- лимит запросов: с одного IP не больше RATE_LIMIT за RATE_WINDOW, вебхуки не считаем ---
        web._hits.clear()
        assert not any(web.rate_limited("10.0.0.1", now=100 + i * 0.01) for i in range(web.RATE_LIMIT))
        assert web.rate_limited("10.0.0.1", now=101), "сверх лимита — 429"
        assert not web.rate_limited("10.0.0.2", now=101), "другой IP не затронут"
        assert not web.rate_limited("10.0.0.1", now=101 + web.RATE_WINDOW + 1), "окно прошло"
        web._hits.clear(); web.RATE_LIMIT, saved = 3, web.RATE_LIMIT
        hdr = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}          # Caddy дописывает свой IP последним
        codes = [(await c.get("/api/rubrics", headers=hdr)).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429], codes
        assert (await c.get("/api/rubrics", headers={"X-Forwarded-For": "9.9.9.9"})).status_code == 200
        assert (await c.post("/webhooks/loto", headers=hdr, content=b"{}")).status_code == 401, \
            "вебхук не под лимитом: дошёл до проверки подписи"
        web.RATE_LIMIT = saved; web._hits.clear()
        web.LOGIN_LIMIT, saved_login = 2, web.LOGIN_LIMIT
        hdr = {"X-Forwarded-For": "7.7.7.7"}
        codes = [(await c.post("/admin/login", data={"password": "x"}, headers=hdr)).status_code
                 for _ in range(3)]
        assert codes == [303, 303, 429], "подбор пароля: %r" % codes
        web.LOGIN_LIMIT = saved_login; web._hits.clear()
        print("лимит запросов OK: 429 сверх лимита, по IP из X-Forwarded-For, вебхуки без лимита")

        # --- админка только с разрешённых IP (ADMIN_IPS) ---
        from app.config import _networks
        nets = _networks("203.0.113.7, 10.0.0.0/8 ; 2001:db8::/32")
        assert [str(n) for n in nets] == ["203.0.113.7/32", "10.0.0.0/8", "2001:db8::/32"], nets
        assert _networks("") == [] and web.admin_ip_allowed("8.8.8.8", []), "пусто = без ограничений"
        assert web.admin_ip_allowed("203.0.113.7", nets) and web.admin_ip_allowed("10.20.30.40", nets)
        assert web.admin_ip_allowed("2001:db8::1", nets)
        assert not web.admin_ip_allowed("203.0.113.8", nets) and not web.admin_ip_allowed("?", nets)
        try:
            _networks("not-an-ip"); raise AssertionError("кривой адрес должен быть ошибкой")
        except ValueError:
            pass
        web.ADMIN_IPS = nets
        ok_ip, bad_ip = {"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-For": "198.51.100.1"}
        for path in ("/admin", "/admin/feed", "/admin/api/ads", "/admin/api/stats"):
            assert (await c.get(path, headers=bad_ip)).status_code == 403, path
            assert (await c.get(path, headers=ok_ip)).status_code == 200, path
        assert (await c.post("/admin/login", data={"password": "pass"}, headers=bad_ip)).status_code == 403
        assert (await c.post("/admin/api/ads/1/delete", json={}, headers=bad_ip)).status_code == 403
        # за Caddy наш IP — последний в цепочке; подставленный клиентом первым — не считается
        assert (await c.get("/admin", headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.1"})).status_code == 403
        assert (await c.get("/admin", headers={"X-Forwarded-For": "198.51.100.1, 10.1.2.3"})).status_code == 200
        # за Cloudflare: до Caddy доходит IP Cloudflare, клиент — в CF-Connecting-IP
        cf = {"X-Forwarded-For": "104.16.1.1", "CF-Connecting-IP": "203.0.113.7"}
        assert (await c.get("/admin", headers=cf)).status_code == 200, "клиент из CF-Connecting-IP"
        cf_bad = {"X-Forwarded-For": "104.16.1.1", "CF-Connecting-IP": "198.51.100.1"}
        assert (await c.get("/admin", headers=cf_bad)).status_code == 403
        spoof = {"X-Forwarded-For": "198.51.100.1", "CF-Connecting-IP": "203.0.113.7"}
        assert (await c.get("/admin", headers=spoof)).status_code == 403, "CF-заголовок не от Cloudflare — не верим"
        # сайт и API ленты с чужого IP работают как раньше
        assert (await c.get("/", headers=bad_ip)).status_code == 200
        assert (await c.get("/api/posts", headers=bad_ip)).status_code == 200
        web.ADMIN_IPS = []
        assert (await c.get("/admin/api/ads", headers=bad_ip)).status_code == 200, "ограничение снято"
        print("админка по IP OK: список/подсети/IPv6, 403 чужим на /admin*, сайт открыт")

        # --- выход ---
        await c.get("/admin/logout"); c.cookies.clear()
        assert (await c.get("/admin/api/ads")).status_code == 401
        print("выход OK")

    await db.close(); await sdb.close()
    print("WEB ADMIN OK")


asyncio.run(main())
