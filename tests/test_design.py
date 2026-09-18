"""Новый стиль сайта (макет «Affbazaar Стиль Ленты»): лента, админка, вход.

Проверяем контракт между JS и разметкой (все id, которые ищет скрипт, есть в HTML), общие токены
бренда на трёх страницах и ключевые элементы макета. Браузер не поднимаем — только отдача шаблонов."""
import asyncio, os, re, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp/"bot.db"; cfg.SITE_DB = tmp/"site.db"
cfg.LOG_DIR = tmp/"logs"; cfg.RESTRICTED_LOG_DIR = tmp/"logs-restricted"
cfg.ADMIN_PASSWORD = "pass"; cfg.SECRET_KEY = "key"; cfg.ADMINS = {999}
import app.db as db, app.site_db as sdb
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent/"app"/"web"/"templates"

INK, YELLOW, RED, CREAM = "#1c2540", "#f5b81c", "#b8262b", "#fbf5e8"
FONTS = "fonts.googleapis.com/css2?family=Rubik"
STRIPE = re.compile(r"linear-gradient\(90deg,(?:#1c2540|var\(--stripe-1\)) 0 33%,#f5b81c 33% 66%,#b8262b 66%\)")


def ids_in_html(html: str) -> set[str]:
    return set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))


def ids_used_by_js(html: str, page: str) -> set[str]:
    """Статические обращения к элементам по id: $('x') на ленте, $('#x') / getElementById('x') в админке.
    Динамические ('give-' + uid, 's-' + key) сюда не попадают — они создаются самим JS."""
    used = set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", html))
    if page == "index":
        used |= set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", html))
    else:
        used |= set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", html))
    return used


def check_brand(html: str, page: str) -> None:
    """Общая система: токены, шрифты, полоса-триколор, theme-color, favicon."""
    for needle in (INK, YELLOW, RED, CREAM, FONTS, 'name="theme-color" content="#1c2540"',
                   'href="/favicon.ico?v=', 'href="/static/site.webmanifest?v='):
        assert needle in html, f"{page}: нет {needle!r}"
    assert STRIPE.search(html), f"{page}: нет полосы-триколора"
    # Старая айдентика не должна просочиться
    for stale in ("#4c46d9", "#8d86ff"):
        assert stale not in html, f"{page}: остался старый акцент {stale}"
    # Дизайн-макет — тёмная тема сохранена и завязана на data-theme / prefers-color-scheme
    assert "prefers-color-scheme: dark" in html, f"{page}: нет тёмной темы"


async def main():
    await db.init(); await sdb.init()
    await sdb.conn().execute(
        """INSERT INTO posts(source_chat_id, source_message_id, author_id, author_username,
                             text, media_type, search_blob, is_reposted, created_at)
           VALUES (?,?,?,?,?,?,?,1, datetime('now'))""",
        (-1002223334445, 1, 777, "seller", "Продам трафик", "text", "продам трафик"))
    await sdb.conn().commit()

    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "pass"; web.SECRET_KEY = "key"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://t") as c:
        index = (await c.get("/")).text
        login = (await c.get("/admin")).text
        await c.post("/admin/login", data={"password": "pass"})
        admin = (await c.get("/admin")).text
    assert "Пароль" in login and "Статистика" in admin, "получили не те страницы"

    # ------------------------------------------------------------ контракт JS ↔ разметка
    for page, html in (("index", index), ("admin", admin)):
        missing = ids_used_by_js(html, page) - ids_in_html(html)
        assert not missing, f"{page}: JS ищет элементы, которых нет в HTML: {sorted(missing)}"
    print("все id из JS есть в разметке OK:",
          len(ids_used_by_js(index, "index")), "на ленте,", len(ids_used_by_js(admin, "admin")), "в админке")

    # ------------------------------------------------------------ общий бренд
    for page, html in (("index", index), ("admin", admin), ("login", login)):
        check_brand(html, page)
    print("токены бренда, шрифты, триколор, тёмная тема на 3 страницах OK")

    # ------------------------------------------------------------ лента
    # Элементы старого JS, без которых ломаются фильтры/авторизация/подгрузка
    for el in ("authbar", "q", "filtersToggle", "filtersPanel", "adType", "vertical", "chatField", "chat",
               "period", "pinned", "resetFilters", "chips", "newbar", "backToFeed", "toolbarTitle",
               "count", "feed", "skeletons", "empty", "more", "toast"):
        assert f'id="{el}"' in index, f"лента: нет #{el}"
    # Новое из макета
    for el in ("rubricPills", "rubricMore", "postAdLink", "rubricColors"):
        assert f'id="{el}"' in index, f"лента: нет #{el} (пилюли рубрик / ссылка на бота)"
    # Пилюли не заменяют фильтр, а переключают select#adType и будят старый обработчик change
    assert "adTypeSel.dispatchEvent(new Event('change'" in index, "пилюли должны переключать select#adType"
    assert "Поиск по тексту, автору или хэштегу" in index
    assert "Только закреплённые" in index or "только закреплённые" in index
    for text in ("Написать", "Закреплено", "Разместить объявление", "Сбросить фильтры", "Пока нет объявлений"):
        assert text in index, f"лента: нет текста {text!r} из макета"
    assert "background-size:22px 22px" in index, "лента: нет точечной сетки фона"
    assert re.search(r"box-shadow:\s*4px 4px 0", index), "лента: нет жёсткой тени карточки"
    assert 'src="{{logo_url}}"'.replace("{{logo_url}}", "/branding/logo.png") in index or "/branding/logo.png?v=" in index
    print("лента: элементы макета и старые id OK")

    # ------------------------------------------------------------ админка
    for tab in ("stats", "ads", "deleted", "rubrics", "messages", "users", "chats", "settings",
                "restricted", "payments"):
        assert f'data-tab="{tab}"' in admin and f'id="tab-{tab}"' in admin, f"админка: вкладка {tab}"
    for el in ("stat-cards", "a-body", "d-body", "u-body", "m-body", "p-body", "r-body",
               "rt-list", "rv-list", "chats-box", "settings-box", "logo-file", "logo-preview"):
        assert f'id="{el}"' in admin, f"админка: нет #{el}"
    assert "Aff Bazar" in admin and "админка" in admin
    assert "/branding/logo.png?v=" in admin, "админка: логотип в шапке"
    assert "table-wrap" in admin and "overflow-x:auto" in admin.replace(" ", ""), "таблицы скроллятся внутри"
    print("админка: вкладки, контейнеры, логотип OK")

    # ------------------------------------------------------------ вход
    assert 'action="/admin/login"' in login and 'name="password"' in login and 'id="password"' in login
    assert 'id="err"' in login and "Войти" in login and "к ленте" in login
    assert "/branding/logo.png?v=" in login
    print("вход: форма, логотип OK")

    await db.close(); await sdb.close()
    print("DESIGN OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:
        import traceback; traceback.print_exc()
        os._exit(1)      # поток aiosqlite не даёт процессу завершиться штатно
