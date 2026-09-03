"""Сайт: брендинг (favicon, заголовок, логотип) и фильтр «Дата публикации»."""
import asyncio, os, sys, pathlib, tempfile
from datetime import date, datetime, timedelta, timezone
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB

CHAT = -1002223334445

TODAY = datetime.now(timezone.utc).date()
MONDAY = TODAY - timedelta(days=TODAY.weekday())          # понедельник текущей недели
MONTH_START = TODAY.replace(day=1)
PREV_MONTH_END = MONTH_START - timedelta(days=1)          # последний день прошлого месяца


def at(day: date, clock: str = "12:00:00") -> str:
    return f"{day} {clock}"


# Метка -> момент публикации. Границы специально «неудобные»: полночь и последняя секунда суток.
POSTS = {
    "today_start":   at(TODAY, "00:00:00"),
    "yest_end":      at(TODAY - timedelta(days=1), "23:59:59"),
    "yest_start":    at(TODAY - timedelta(days=1), "00:00:00"),
    "d3":            at(TODAY - timedelta(days=3)),
    "d10":           at(TODAY - timedelta(days=10)),
    "d20":           at(TODAY - timedelta(days=20)),
    "d100":          at(TODAY - timedelta(days=100)),
    "week_start":    at(MONDAY, "00:00:00"),
    "last_week_mid": at(MONDAY - timedelta(days=4)),
    "month_start":   at(MONTH_START, "00:00:00"),
    "prev_month":    at(PREV_MONTH_END.replace(day=15)),
}


def day_of(label: str) -> date:
    return date.fromisoformat(POSTS[label][:10])


def expected(period: str) -> set[str]:
    """Ожидаемый набор меток считаем независимо от реализации фильтра."""
    out = set()
    for label in POSTS:
        d = day_of(label)
        if period == "all":
            ok = True
        elif period == "today":
            ok = d == TODAY
        elif period == "yesterday":
            ok = d == TODAY - timedelta(days=1)
        elif period == "this_week":
            ok = MONDAY <= d < MONDAY + timedelta(days=7)
        elif period == "last_week":
            ok = MONDAY - timedelta(days=7) <= d < MONDAY
        elif period == "this_month":
            ok = (d.year, d.month) == (TODAY.year, TODAY.month)
        elif period == "last_month":
            ok = (d.year, d.month) == (PREV_MONTH_END.year, PREV_MONTH_END.month)
        else:                                             # last_2d … last_30d
            days = int(period[len("last_"):-1])
            ok = TODAY - timedelta(days=days - 1) <= d <= TODAY
        if ok:
            out.add(label)
    return out


async def seed():
    await db.init(); await sdb.init()
    for i, (label, created) in enumerate(POSTS.items()):
        await sdb.conn().execute(
            """INSERT INTO posts(source_chat_id, source_message_id, author_id, author_username,
                                 text, media_type, search_blob, is_reposted, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (CHAT, 1000 + i, 777, "seller", label, "text", label.lower(), created))
    await sdb.conn().commit()


async def main():
    await seed()
    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"
    web.BRANDING_DIR = tmp/"branding"                     # том с данными подменяем на temp
    default_logo = (web.STATIC_DIR/"logo.png").read_bytes()
    png = (web.STATIC_DIR/"favicon-16x16.png").read_bytes()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://t") as c:
        # ---------------------------------------------------------- статика и favicon
        for path in ("/favicon.ico", "/static/favicon.ico", "/static/favicon-32x32.png",
                     "/static/apple-touch-icon.png", "/static/site.webmanifest",
                     "/static/android-chrome-192x192.png", "/static/logo.png"):
            r = await c.get(path)
            assert r.status_code == 200 and r.content, path
        print("статика и favicon отдаются OK")

        html = (await c.get("/")).text
        for tag in ('href="/favicon.ico"', 'href="/static/favicon-32x32.png"',
                    'href="/static/apple-touch-icon.png"', 'href="/static/site.webmanifest"'):
            assert tag in html, tag
        assert "/branding/logo.png?v=" in html, "логотип с cache-buster'ом"
        print("теги favicon и логотип в шапке OK")

        # ---------------------------------------------------------- заголовок из настроек
        assert "<title>Aff Bazar — Лента объявлений · биржа affiliate-рынка</title>" in html, \
            html[:400]
        r = await c.get("/admin")
        assert "<title>Aff Bazar — вход в админку</title>" in r.text
        print("заголовок по умолчанию OK")

        assert (await c.post("/admin/api/settings",
                             json={"site_title": "Тест Базар"})).status_code == 401
        await c.post("/admin/login", data={"password": "pass"})
        r = await c.post("/admin/api/settings", json={"site_title": "Тест Базар",
                                                      "site_tagline": "Тестовая лента"})
        s = r.json()["settings"]
        assert s["site_title"] == "Тест Базар" and s["site_tagline"] == "Тестовая лента", s
        html = (await c.get("/")).text
        assert "<title>Тест Базар — Тестовая лента</title>" in html, html[:400]
        assert "Тестовая лента" in html and "Тест Базар" in html
        assert "<title>Тест Базар — админка</title>" in (await c.get("/admin")).text
        print("заголовок меняется через админку OK")

        # ---------------------------------------------------------- логотип
        r = await c.get("/branding/logo.png")
        assert r.status_code == 200 and r.content == default_logo, "без загрузки отдаём дефолт"

        r = await c.post("/admin/api/branding/logo",
                         files={"file": ("logo.png", png, "image/png")})
        assert r.status_code == 200 and r.json()["ok"], r.text
        saved = tmp/"branding"/"logo.png"
        assert saved.exists() and saved.read_bytes() == png, "файл лёг в том с данными"
        r = await c.get("/branding/logo.png")
        assert r.content == png and r.headers["content-type"] == "image/png", r.headers
        print("загрузка логотипа OK, размер", len(png), "байт")

        r = await c.post("/admin/api/branding/logo",
                         files={"file": ("evil.png", b"<html>not an image</html>", "image/png")})
        assert r.status_code == 400, r.status_code
        big = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024)
        r = await c.post("/admin/api/branding/logo", files={"file": ("big.png", big, "image/png")})
        assert r.status_code == 400, r.status_code
        assert saved.read_bytes() == png, "неудачные загрузки не портят текущий логотип"
        print("проверки формата и размера логотипа OK")

        r = await c.post("/admin/api/branding/logo/reset")
        assert r.status_code == 200 and not saved.exists()
        assert (await c.get("/branding/logo.png")).content == default_logo
        print("сброс логотипа OK")

        await c.get("/admin/logout"); c.cookies.clear()
        r = await c.post("/admin/api/branding/logo",
                         files={"file": ("logo.png", png, "image/png")})
        assert r.status_code == 401, r.status_code
        print("загрузка логотипа без прав закрыта OK")

        # ---------------------------------------------------------- фильтр по периоду
        async def labels(period=None):
            params = {"limit": 200}
            if period is not None:
                params["period"] = period
            data = (await c.get("/api/posts", params=params)).json()
            assert data["total"] == len(data["items"]), data["total"]
            return {p["text"] for p in data["items"]}

        assert await labels() == set(POSTS), "без параметра — вся лента"
        for period in sdb.PERIODS:
            got = await labels(period)
            assert got == expected(period), (period, sorted(got), sorted(expected(period)))
        print("все периоды считаются верно OK:", len(sdb.PERIODS), "значений")

        assert "today_start" in await labels("today"), "сегодня 00:00 попадает в «Сегодня»"
        assert "yest_end" not in await labels("today"), "вчера 23:59 не попадает в «Сегодня»"
        assert "yest_end" in await labels("yesterday")
        assert await labels("буквы") == set(POSTS), "неизвестный период = за всё время"
        assert await labels("") == set(POSTS)
        print("границы суток и неизвестный период OK")

        # период дружит с остальными фильтрами и с автоподгрузкой (after_id)
        data = (await c.get("/api/posts", params={"period": "today", "q": "today"})).json()
        assert data["total"] == 1 and data["items"][0]["text"] == "today_start", data
        data = (await c.get("/api/posts", params={"period": "all", "after_id": 10**9})).json()
        assert data["total"] == 0, data
        print("период вместе с поиском и after_id OK")

    await db.close(); await sdb.close()
    print("SITE OK")


asyncio.run(main())
