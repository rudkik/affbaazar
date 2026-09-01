"""Авторизация через Telegram: Mini App, Login Widget, сессии, личный кабинет."""
import asyncio, hashlib, hmac, json, os, pathlib, sys, tempfile, time
from urllib.parse import urlencode
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR
import app.auth as auth
auth.BOT_TOKEN = "123456:TESTTOKEN"; auth.SECRET_KEY = "key"

TOKEN = "123456:TESTTOKEN"
USER, ADMIN, OTHER = 111, 999, 222
CHANNEL = -1002223334445


def make_init_data(user: dict, token: str = TOKEN, auth_date: int = None) -> str:
    data = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH",
            "user": json.dumps(user, ensure_ascii=False, separators=(",", ":"))}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def make_widget(user: dict, token: str = TOKEN, auth_date: int = None) -> dict:
    data = {"id": str(user["id"]), "first_name": user.get("first_name", ""),
            "username": user.get("username", ""), "auth_date": str(auth_date or int(time.time()))}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(token.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


async def seed():
    await db.init(); await sdb.init()
    await db.set_setting("ad_channel_id", CHANNEL)
    for uid, un in ((USER, "seller"), (ADMIN, "boss"), (OTHER, "stranger")):
        await db.upsert_user(uid, un, un.title())
    await tk.add(USER, 40, "test")
    t = (await db.ad_types())[0]
    for i, uid in enumerate((USER, USER, OTHER)):
        await db.execute(
            """INSERT INTO ads(user_id, channel_id, channel_message_id, ad_type_id,
                               ad_type_name, ad_type_tag, text, media_type, cost_total, status)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (uid, CHANNEL, 700 + i, t["id"], t["name"], t["tag"], f"Объявление {i}",
             "text", 10, "published"))


async def main():
    await seed()
    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"; web.ADMINS = {ADMIN}
    auth.SECRET_KEY = "key"

    # --- модуль проверки подписи ---
    good = make_init_data({"id": USER, "username": "seller", "first_name": "Продавец"})
    assert auth.verify_webapp(good, TOKEN)["id"] == USER
    assert auth.verify_webapp(good.replace("seller", "boss"), TOKEN) is None, "подмена данных"
    assert auth.verify_webapp(good, "999:OTHER") is None, "чужой токен"
    old = make_init_data({"id": USER}, auth_date=int(time.time()) - 90000)
    assert auth.verify_webapp(old, TOKEN) is None, "просроченный initData"
    assert auth.verify_webapp("", TOKEN) is None and auth.verify_webapp("hash=x", TOKEN) is None
    print("проверка initData OK: подделка, чужой токен и просрочка отклонены")

    w = make_widget({"id": USER, "username": "seller", "first_name": "Продавец"})
    assert auth.verify_login_widget(w, TOKEN)["id"] == USER
    bad = dict(w); bad["id"] = str(ADMIN)
    assert auth.verify_login_widget(bad, TOKEN) is None, "подмена id"
    assert auth.verify_login_widget(make_widget({"id": USER}, token="9:X"), TOKEN) is None
    print("проверка Login Widget OK")

    s = auth.issue_session(USER, "seller", False)
    assert auth.read_session(s)["uid"] == USER
    assert auth.read_session(s[:-4] + "beef") is None, "подделка подписи"
    assert auth.read_session(auth.issue_session(USER, "s", False, ttl=-1)) is None, "истёкшая"
    print("подписанная сессия OK")

    # --- эндпоинты ---
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://t") as c:
        assert (await c.get("/api/me")).json() == {"authorized": False}
        assert (await c.get("/api/my/ads")).status_code == 401
        print("без входа личные данные закрыты OK")

        r = await c.post("/api/auth/telegram", json={"init_data": "мусор"})
        assert r.status_code == 401, r.status_code
        # подменяем латиницу — она в urlencode остаётся как есть, подпись перестаёт сходиться
        r = await c.post("/api/auth/telegram",
                         json={"init_data": good.replace("seller", "hacker")})
        assert r.status_code == 401, "подделанные данные не пускают"
        assert not c.cookies.get(auth.SESSION_COOKIE)

        r = await c.post("/api/auth/telegram", json={"init_data": good})
        body = r.json()
        assert r.status_code == 200 and body["authorized"] and body["id"] == USER, body
        assert body["balance"] == 40 and body["ads"] == 2 and body["is_admin"] is False, body
        assert c.cookies.get(auth.SESSION_COOKIE), "кука сессии выставлена"
        print("вход из Mini App OK: баланс", body["balance"], "| объявлений", body["ads"])

        me = (await c.get("/api/me")).json()
        assert me["authorized"] and me["username"] == "seller", me

        mine = (await c.get("/api/my/ads")).json()
        assert mine["total"] == 2 and all(i["text"].startswith("Объявление") for i in mine["items"])
        assert all(i["id"] for i in mine["items"])
        texts = {i["text"] for i in mine["items"]}
        assert "Объявление 2" not in texts, "чужие объявления не показываются"
        print("личный кабинет OK: видно только свои объявления")

        # обычный пользователь не админ
        assert (await c.get("/admin/api/stats")).status_code == 401, "не админ в админку не может"

        await c.post("/api/auth/logout"); c.cookies.clear()
        assert (await c.get("/api/me")).json() == {"authorized": False}
        print("выход OK")

        # --- админ входит через Telegram, без пароля ---
        admin_init = make_init_data({"id": ADMIN, "username": "boss", "first_name": "Босс"})
        r = await c.post("/api/auth/telegram", json={"init_data": admin_init})
        assert r.json()["is_admin"] is True, r.json()
        st = await c.get("/admin/api/stats")
        assert st.status_code == 200, "админ бота попадает в админку по Telegram-входу"
        assert "users" in st.json()
        assert "Aff Bazar" in (await c.get("/admin")).text, "открывается панель, а не форма входа"
        print("вход админа через Telegram OK: пароль не нужен")
        await c.post("/api/auth/logout"); c.cookies.clear()

        # --- Login Widget ---
        r = await c.get("/api/auth/widget", params=make_widget(
            {"id": USER, "username": "seller", "first_name": "Продавец"}))
        assert r.status_code == 303 and "auth=ok" in r.headers["location"], r.headers
        assert c.cookies.get(auth.SESSION_COOKIE)
        c.cookies.clear()
        # неASCII в подписи не должен ронять сервер
        r = await c.get("/api/auth/widget", params={"id": "1", "hash": "нет"})
        assert r.status_code == 303 and "auth=error" in r.headers["location"], r.headers
        assert not c.cookies.get(auth.SESSION_COOKIE), "при неверной подписи сессии нет"
        r = await c.get("/api/auth/widget", params={"id": "1", "hash": "0" * 64})
        assert r.status_code == 303 and "auth=error" in r.headers["location"]
        r = await c.post("/api/auth/telegram", json={"init_data": "hash=%D0%BD%D0%B5%D1%82"})
        assert r.status_code == 401, "мусорная подпись — отказ, а не ошибка сервера"
        print("Login Widget OK: верный вход пускает, подделка и мусор — нет")

        cfgr = (await c.get("/api/config")).json()
        assert "bot_username" in cfgr and "public_url" in cfgr, cfgr
        print("конфиг для фронтенда OK")

    print("AUTH OK")


async def runner():
    try:
        await main()
    finally:
        await db.close(); await sdb.close()


asyncio.run(runner())
