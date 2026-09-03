"""Веб-часть: публичная лайв-лента репостов + админ-панель."""
import hashlib
import hmac
import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth, db, site_db
from app.config import ADMIN_PASSWORD, ADMINS, DATA_DIR, PUBLIC_URL, SECRET_KEY

# На боевом домене (https) куки отдаём только по защищённому соединению.
COOKIE_SECURE = PUBLIC_URL.startswith("https://")

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
# Загруженный через админку логотип живёт в томе с данными — переживает пересборку образа.
BRANDING_DIR = DATA_DIR / "branding"
_page_cache: dict[str, str] = {}


def page(name: str) -> str:
    """Читает шаблон с диска один раз (шаблонизатор не нужен — данные тянутся через API)."""
    if name not in _page_cache:
        _page_cache[name] = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    return _page_cache[name]


def logo_path() -> Path:
    """Загруженный логотип, а если его нет — дефолтный из статики."""
    uploaded = BRANDING_DIR / "logo.png"
    return uploaded if uploaded.exists() else STATIC_DIR / "logo.png"


def logo_url() -> str:
    """Адрес логотипа с cache-buster'ом по времени изменения файла."""
    try:
        version = int(logo_path().stat().st_mtime)
    except OSError:
        version = 0
    return f"/branding/logo.png?v={version}"


async def render(name: str) -> HTMLResponse:
    """Отдаёт страницу, подставляя брендинг в плейсхолдеры {{...}} закэшированного шаблона."""
    values = {
        "site_title": await db.get_setting("site_title"),
        "site_tagline": await db.get_setting("site_tagline"),
        "logo_url": logo_url(),
    }
    text = page(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", html.escape(str(value or ""), quote=True))
    return HTMLResponse(text)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Позволяет поднимать веб отдельно от бота — базы инициализируются при старте."""
    if db._conn is None:
        await db.init()
    if site_db._conn is None:
        await site_db.init()
    yield


app = FastAPI(title="Chat Gate Bot", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Экземпляр бота проставляется из bot.py — нужен для удаления/репоста из админки.
BOT = None


def set_bot(bot) -> None:
    global BOT
    BOT = bot


# ------------------------------------------------------------------ авторизация
def _token() -> str:
    return hmac.new(SECRET_KEY.encode(), b"admin-session", hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    """Админ по паролю ИЛИ вошедший через Telegram админ бота."""
    cookie = request.cookies.get("session", "")
    if cookie and hmac.compare_digest(cookie, _token()):
        return True
    session = current_user(request)
    return bool(session and session.get("adm"))


def current_user(request: Request) -> Optional[dict]:
    """Пользователь, вошедший через Telegram (Mini App или Login Widget)."""
    return auth.read_session(request.cookies.get(auth.SESSION_COOKIE))


def _set_session(response, user: dict) -> None:
    is_admin = int(user["id"]) in ADMINS
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(int(user["id"]), user.get("username"), is_admin),
        httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=auth.SESSION_TTL)


async def _remember(user: dict) -> None:
    full_name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    await db.upsert_user(int(user["id"]), user.get("username"), full_name or None,
                         user.get("first_name"), user.get("last_name"))


def require_admin(request: Request) -> bool:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Требуется вход")
    return True


# ------------------------------------------------------------------ брендинг: иконки и логотип
# Разрешённые для логотипа форматы проверяем по сигнатуре файла, а не по заголовку от браузера.
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _logo_media_type(blob: bytes) -> Optional[str]:
    for signature, media_type in LOGO_TYPES:
        if blob.startswith(signature):
            return media_type
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/branding/logo.png", include_in_schema=False)
async def branding_logo():
    """Логотип сайта: загруженный из админки, иначе дефолтный из статики."""
    path = logo_path()
    if not path.exists():
        raise HTTPException(404, "Логотип не найден")
    media_type = _logo_media_type(path.read_bytes()[:16]) or "image/png"
    return FileResponse(path, media_type=media_type)


@app.post("/admin/api/branding/logo")
async def admin_upload_logo(file: UploadFile = File(...), _: bool = Depends(require_admin)):
    """Загрузка логотипа: png/jpg/webp до 2 МБ, кладём в том с данными."""
    if (file.size or 0) > LOGO_MAX_BYTES:
        raise HTTPException(400, "Файл больше 2 МБ")
    blob = await file.read(LOGO_MAX_BYTES + 1)          # больше лимита в память не тянем
    if not blob:
        raise HTTPException(400, "Пустой файл")
    if len(blob) > LOGO_MAX_BYTES:
        raise HTTPException(400, "Файл больше 2 МБ")
    if _logo_media_type(blob) is None:
        raise HTTPException(400, "Поддерживаются только PNG, JPEG и WebP")
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    (BRANDING_DIR / "logo.png").write_bytes(blob)
    return {"ok": True, "logo_url": logo_url(), "size": len(blob)}


@app.post("/admin/api/branding/logo/reset")
async def admin_reset_logo(_: bool = Depends(require_admin)):
    """Возврат к дефолтному логотипу — просто удаляем загруженный файл."""
    (BRANDING_DIR / "logo.png").unlink(missing_ok=True)
    return {"ok": True, "logo_url": logo_url()}


# ------------------------------------------------------------------ публичная лента
@app.get("/", response_class=HTMLResponse)
async def index():
    return await render("index.html")


@app.get("/api/posts")
async def api_posts(q: str = "", chat_id: Optional[int] = None, author: str = "",
                    media: str = "", ad_type: str = "", vertical: str = "",
                    only_pinned: bool = False, only_reposted: bool = False,
                    period: str = "all", sort: str = "created_at", order: str = "desc",
                    limit: int = 50, offset: int = 0, after_id: Optional[int] = None):
    rows, total = await site_db.query_posts(
        q=q, chat_id=chat_id, author=author, media=media, ad_type=ad_type, vertical=vertical,
        only_pinned=only_pinned, only_reposted=only_reposted, period=period,
        sort=sort, order=order, limit=limit, offset=offset, after_id=after_id)
    return {"items": rows, "total": total}


@app.get("/api/rubrics")
async def api_rubrics():
    """Справочники для фильтров на сайте."""
    types = await db.ad_types()
    verts = await db.verticals()
    return {
        "types": [{"id": r["id"], "name": r["name"], "tag": r["tag"],
                   "has_vertical": bool(r["has_vertical"])} for r in types],
        "verticals": [{"id": r["id"], "name": r["name"], "tag": r["tag"]} for r in verts],
    }


@app.get("/api/chats")
async def api_chats():
    rows = await db.active_chats()
    return [{"chat_id": r["chat_id"], "title": r["title"]} for r in rows]


# ------------------------------------------------------------------ вход через Telegram
@app.post("/api/auth/telegram")
async def auth_telegram(payload: dict):
    """Вход из Mini App: браузер присылает initData, подпись проверяем секретом бота."""
    user = auth.verify_webapp(payload.get("init_data", ""))
    if not user:
        raise HTTPException(401, "Не удалось проверить данные Telegram")
    await _remember(user)
    response = JSONResponse(await _me_payload(int(user["id"])))
    _set_session(response, user)
    return response


@app.get("/api/auth/widget")
async def auth_widget(request: Request):
    """Возврат Telegram Login Widget при входе с обычного сайта."""
    user = auth.verify_login_widget(dict(request.query_params))
    if not user:
        return RedirectResponse("/?auth=error", status_code=303)
    await _remember(user)
    response = RedirectResponse("/?auth=ok", status_code=303)
    _set_session(response, user)
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


async def _me_payload(user_id: int) -> dict:
    row = await db.get_user(user_id)
    ads_total = await db.scalar(
        "SELECT COUNT(*) FROM ads WHERE user_id = ? AND status = 'published'", (user_id,))
    invited = await db.scalar(
        "SELECT COUNT(*) FROM users WHERE referrer_id = ? AND activated = 1", (user_id,))
    return {
        "authorized": True,
        "id": user_id,
        "username": row["username"] if row else None,
        "name": row["full_name"] if row else None,
        "balance": row["tokens"] if row else 0,
        "activated": bool(row["activated"]) if row else False,
        "ads": ads_total,
        "invited": invited,
        "is_admin": user_id in ADMINS,
    }


@app.get("/api/me")
async def api_me(request: Request):
    session = current_user(request)
    if not session:
        return {"authorized": False}
    return await _me_payload(int(session["uid"]))


@app.get("/api/my/ads")
async def api_my_ads(request: Request, limit: int = 50, offset: int = 0):
    session = current_user(request)
    if not session:
        raise HTTPException(401, "Нужен вход через Telegram")
    rows = await db.fetchall(
        """SELECT id, channel_message_id, ad_type_name, ad_type_tag, vertical_name,
                  vertical_tag, text, media_type, cost_total, pin_hours, pinned_until,
                  status, delete_comment, created_at
           FROM ads WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?""",
        (int(session["uid"]), min(limit, 200), offset))
    total = await db.scalar("SELECT COUNT(*) FROM ads WHERE user_id = ?", (int(session["uid"]),))
    return {"items": [dict(r) for r in rows], "total": total}


@app.get("/api/config")
async def api_config():
    """То, что нужно фронтенду: имя бота для Login Widget и адрес сайта."""
    me = None
    if BOT:
        try:
            me = (await BOT.get_me()).username
        except Exception:  # noqa: BLE001
            me = None
    return {"bot_username": me, "public_url": PUBLIC_URL}


# ------------------------------------------------------------------ вход в админку
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not is_authed(request):
        return await render("login.html")
    return await render("admin.html")


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        return RedirectResponse("/admin?e=1", status_code=303)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("session", _token(), httponly=True, samesite="lax", secure=COOKIE_SECURE,
                        max_age=7 * 24 * 3600)
    return response


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("session")
    return response


# ------------------------------------------------------------------ админ API
@app.get("/admin/api/stats")
async def admin_stats(_: bool = Depends(require_admin)):
    async def one(sql, args=()):
        return await db.scalar(sql, args)

    daily = await db.fetchall(
        """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt
           FROM chat_messages WHERE status = 'published'
           GROUP BY day ORDER BY day DESC LIMIT 14""")
    top_users = await db.fetchall(
        """SELECT u.user_id, u.username, u.full_name, u.tokens, u.messages_sent,
                  (SELECT COUNT(*) FROM users r WHERE r.referrer_id = u.user_id) AS invited
           FROM users u ORDER BY u.messages_sent DESC LIMIT 10""")
    by_type = await db.fetchall(
        """SELECT COALESCE(ad_type_name, '—') AS name, COUNT(*) AS cnt
           FROM ads WHERE status = 'published'
           GROUP BY ad_type_name ORDER BY cnt DESC LIMIT 10""")
    return {
        "chats": await one("SELECT COUNT(*) FROM chats WHERE is_active = 1"),
        "users": await one("SELECT COUNT(*) FROM users"),
        "activated": await one("SELECT COUNT(*) FROM users WHERE activated = 1"),
        "restricted": await one("SELECT COUNT(*) FROM user_chat_state "
                                "WHERE restricted_until > datetime('now')"),
        "messages": await one("SELECT COUNT(*) FROM chat_messages WHERE status = 'published'"),
        "deleted": await one("SELECT COUNT(*) FROM chat_messages WHERE status = 'deleted'"),
        "tokens_balance": await one("SELECT COALESCE(SUM(tokens),0) FROM users"),
        "tokens_spent": await one("SELECT COALESCE(SUM(-amount),0) FROM token_tx WHERE amount < 0"),
        "tokens_bought": await one("SELECT COALESCE(SUM(tokens),0) FROM payments"),
        "revenue": await one("SELECT COALESCE(SUM(amount),0) FROM payments"),
        "referrals": await one("SELECT COUNT(*) FROM users WHERE referral_rewarded = 1"),
        "ads_published": await one("SELECT COUNT(*) FROM ads WHERE status = 'published'"),
        "ads_deleted": await one("SELECT COUNT(*) FROM ads WHERE status = 'deleted'"),
        "coins_spent_ads": await one(
            "SELECT COALESCE(SUM(cost_total),0) FROM ads WHERE status = 'published'"),
        "coins_refunded_ads": await one(
            "SELECT COALESCE(SUM(cost_total),0) FROM ads WHERE refunded = 1"),
        "ads_with_image": await one(
            "SELECT COUNT(*) FROM ads WHERE status = 'published' AND cost_image > 0"),
        "ads_pinned": await one(
            "SELECT COUNT(*) FROM ads WHERE status = 'published' AND pin_hours > 0"),
        "revenue_extras": await one(
            "SELECT COALESCE(SUM(cost_image + cost_pin),0) FROM ads WHERE status = 'published'"),
        "daily": [dict(r) for r in daily],
        "top_users": [dict(r) for r in top_users],
        "by_type": [dict(r) for r in by_type],
    }


@app.get("/admin/api/users")
async def admin_users(q: str = "", limit: int = 50, offset: int = 0,
                      _: bool = Depends(require_admin)):
    where, args = "", []
    if q:
        # колонки квалифицируем: ниже есть self-join на реферера, иначе «ambiguous column»
        where = ("WHERE u.username LIKE ? OR u.full_name LIKE ? "
                 "OR CAST(u.user_id AS TEXT) LIKE ?")
        like = f"%{q}%"
        args = [like, like, like]
    # u.* отдаёт и новые поля учёта (first_name/last_name/bio/started/subscribed/…),
    # реферер подтягивается JOIN-ом — фронт использует поля по имени, лишние не мешают.
    rows = await db.fetchall(
        f"""SELECT u.*, (SELECT COUNT(*) FROM users r2 WHERE r2.referrer_id = u.user_id) AS invited,
                   r.user_id AS referrer_db_id, r.username AS referrer_username
            FROM users u
            LEFT JOIN users r ON r.user_id = u.referrer_id
            {where} ORDER BY u.updated_at DESC LIMIT ? OFFSET ?""",
        (*args, min(limit, 200), offset))
    total = await db.scalar(f"SELECT COUNT(*) FROM users u {where}", args)
    return {"items": [dict(r) for r in rows], "total": total}


@app.get("/admin/api/users/{user_id}/card")
async def admin_user_card(user_id: int, _: bool = Depends(require_admin)):
    """Полная карточка пользователя по ТЗ: профиль, реферер, подписки на каналы."""
    card = await db.user_card(user_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return card


@app.post("/admin/api/users/{user_id}/tokens")
async def admin_give_tokens(user_id: int, payload: dict, _: bool = Depends(require_admin)):
    from app import tokens as tk
    amount = int(payload.get("amount", 0))
    if not amount:
        raise HTTPException(400, "amount обязателен")
    balance = await tk.add(user_id, amount, "admin_grant_web", {"source": "web"})
    if BOT:
        try:
            await BOT.send_message(user_id, f"💰 Баланс изменён на {amount:+d}. "
                                            f"Текущий баланс: <b>{balance}</b>.")
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "balance": balance}


@app.post("/admin/api/users/{user_id}/unrestrict")
async def admin_unrestrict(user_id: int, payload: dict, _: bool = Depends(require_admin)):
    chat_id = int(payload.get("chat_id"))
    await db.update_state(user_id, chat_id, restricted_until=None, fail_streak=0)
    if BOT:
        try:
            from aiogram.types import ChatPermissions
            await BOT.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True,
                                            can_send_polls=True, can_add_web_page_previews=True))
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@app.get("/admin/api/messages")
async def admin_messages(q: str = "", chat_id: Optional[int] = None, status: str = "",
                         limit: int = 50, offset: int = 0, _: bool = Depends(require_admin)):
    where, args = ["1=1"], []
    if q:
        where.append("m.text LIKE ?")
        args.append(f"%{q}%")
    if chat_id:
        where.append("m.chat_id = ?")
        args.append(chat_id)
    if status:
        where.append("m.status = ?")
        args.append(status)
    clause = " AND ".join(where)
    rows = await db.fetchall(
        f"""SELECT m.*, u.username, u.full_name FROM chat_messages m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE {clause} ORDER BY m.id DESC LIMIT ? OFFSET ?""",
        (*args, min(limit, 200), offset))
    total = await db.scalar(f"SELECT COUNT(*) FROM chat_messages m WHERE {clause}", args)
    return {"items": [dict(r) for r in rows], "total": total}


@app.post("/admin/api/messages/{record_id}/delete")
async def admin_delete_message(record_id: int, _: bool = Depends(require_admin)):
    from app import services
    row = await db.fetchone("SELECT * FROM chat_messages WHERE id = ?", (record_id,))
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    refunded = 0
    if row["message_id"]:
        # удаление в Telegram возможно только при живом боте, возврат токенов — всегда
        if BOT:
            await services.delete_quiet(BOT, row["chat_id"], row["message_id"])
        refunded = await services.refund_message(BOT, row["chat_id"], row["message_id"], "web_admin")
    else:
        # запись без message_id: в чат не публиковалась, токены не списывались
        await db.execute("UPDATE chat_messages SET status = 'deleted' WHERE id = ?", (record_id,))
    return {"ok": True, "refunded": refunded, "in_telegram": bool(BOT and row["message_id"])}


@app.post("/admin/api/messages/{record_id}/repost")
async def admin_repost(record_id: int, _: bool = Depends(require_admin)):
    from app import services
    row = await db.fetchone("SELECT * FROM chat_messages WHERE id = ?", (record_id,))
    if not row or not row["message_id"]:
        raise HTTPException(404, "Сообщение не найдено")
    if not BOT:
        raise HTTPException(503, "Бот недоступен")
    posted = await services.repost_to_channel(BOT, row["chat_id"], row["message_id"])
    return {"ok": bool(posted), "channel_message_id": posted}


@app.get("/admin/api/settings")
async def admin_get_settings(_: bool = Depends(require_admin)):
    return await db.all_settings()


@app.post("/admin/api/settings")
async def admin_set_settings(payload: dict, _: bool = Depends(require_admin)):
    for key, value in payload.items():
        if key in db.DEFAULT_SETTINGS:
            await db.set_setting(key, value)
    return {"ok": True, "settings": await db.all_settings()}


@app.get("/admin/api/chats")
async def admin_chats(_: bool = Depends(require_admin)):
    rows = await db.fetchall("SELECT * FROM chats ORDER BY created_at")
    out = []
    for row in rows:
        item = dict(row)
        item["channels"] = [dict(c) for c in await db.required_channels(row["chat_id"])]
        out.append(item)
    return out


@app.post("/admin/api/chats/{chat_id}")
async def admin_update_chat(chat_id: int, payload: dict, _: bool = Depends(require_admin)):
    allowed = {"welcome_message", "post_mode", "repost_mode",
               "repost_channel_id", "is_active"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    sets = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE chats SET {sets} WHERE chat_id = ?", (*fields.values(), chat_id))
    return {"ok": True, "chat": dict(await db.get_chat(chat_id))}


@app.get("/admin/api/restricted")
async def admin_restricted(_: bool = Depends(require_admin)):
    rows = await db.fetchall(
        """SELECT s.*, u.username, u.full_name FROM user_chat_state s
           LEFT JOIN users u ON u.user_id = s.user_id
           WHERE s.restricted_until > datetime('now') ORDER BY s.restricted_until DESC""")
    return [dict(r) for r in rows]


@app.get("/admin/api/payments")
async def admin_payments(limit: int = 50, _: bool = Depends(require_admin)):
    rows = await db.fetchall(
        """SELECT p.*, u.username FROM payments p LEFT JOIN users u ON u.user_id = p.user_id
           ORDER BY p.id DESC LIMIT ?""", (min(limit, 200),))
    return [dict(r) for r in rows]


@app.get("/admin/api/transactions")
async def admin_transactions(user_id: Optional[int] = None, limit: int = 100,
                             _: bool = Depends(require_admin)):
    if user_id:
        rows = await db.fetchall(
            "SELECT * FROM token_tx WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, min(limit, 500)))
    else:
        rows = await db.fetchall("SELECT * FROM token_tx ORDER BY id DESC LIMIT ?",
                                 (min(limit, 500),))
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ объявления
@app.get("/admin/api/ads")
async def admin_ads(q: str = "", status: str = "", ad_type: str = "", vertical: str = "",
                    user_id: Optional[int] = None, limit: int = 50, offset: int = 0,
                    _: bool = Depends(require_admin)):
    where, args = ["1=1"], []
    if q:
        where.append("(a.text LIKE ? OR u.username LIKE ? OR u.full_name LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if status:
        where.append("a.status = ?")
        args.append(status)
    if ad_type:
        where.append("a.ad_type_tag = ?")
        args.append(ad_type)
    if vertical:
        where.append("a.vertical_tag = ?")
        args.append(vertical)
    if user_id:
        where.append("a.user_id = ?")
        args.append(user_id)
    clause = " AND ".join(where)
    rows = await db.fetchall(
        f"""SELECT a.*, u.username, u.full_name FROM ads a
            LEFT JOIN users u ON u.user_id = a.user_id
            WHERE {clause} ORDER BY a.id DESC LIMIT ? OFFSET ?""",
        (*args, min(limit, 200), offset))
    total = await db.scalar(
        f"SELECT COUNT(*) FROM ads a LEFT JOIN users u ON u.user_id = a.user_id WHERE {clause}",
        args)
    return {"items": [dict(r) for r in rows], "total": total}


@app.post("/admin/api/ads/{ad_id}/delete")
async def admin_delete_ad(ad_id: int, payload: dict, _: bool = Depends(require_admin)):
    from app import ads
    comment = (payload.get("comment") or "").strip() or None
    refund = bool(payload.get("refund", True))
    try:
        result = await ads.delete_ad(BOT, ad_id, by_admin_id=None, comment=comment, refund=refund)
    except ads.AdError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "refunded": result["refunded"], "already": result.get("already", False)}


# ------------------------------------------------------------------ рубрики
@app.get("/admin/api/rubrics")
async def admin_rubrics(_: bool = Depends(require_admin)):
    types = await db.fetchall("SELECT * FROM ad_types ORDER BY position, id")
    verts = await db.fetchall("SELECT * FROM verticals ORDER BY position, id")
    return {"types": [dict(r) for r in types], "verticals": [dict(r) for r in verts]}


@app.post("/admin/api/rubrics/types")
async def admin_create_type(payload: dict, _: bool = Depends(require_admin)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name обязателен")
    tag = (payload.get("tag") or "").strip() or db.make_tag(name)
    has_vertical = 1 if payload.get("has_vertical") else 0
    note = payload.get("note") or None
    position = int(payload.get("position", 0) or 0)
    cur = await db.execute(
        "INSERT INTO ad_types(name, tag, has_vertical, note, position) VALUES (?, ?, ?, ?, ?)",
        (name, tag, has_vertical, note, position))
    row = await db.fetchone("SELECT * FROM ad_types WHERE id = ?", (cur.lastrowid,))
    return {"ok": True, "item": dict(row)}


@app.post("/admin/api/rubrics/types/{type_id}")
async def admin_update_type(type_id: int, payload: dict, _: bool = Depends(require_admin)):
    allowed = {"name", "tag", "has_vertical", "note", "position", "is_active"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    sets = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE ad_types SET {sets} WHERE id = ?", (*fields.values(), type_id))
    row = await db.fetchone("SELECT * FROM ad_types WHERE id = ?", (type_id,))
    if not row:
        raise HTTPException(404, "Рубрика не найдена")
    return {"ok": True, "item": dict(row)}


@app.post("/admin/api/rubrics/verticals")
async def admin_create_vertical(payload: dict, _: bool = Depends(require_admin)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name обязателен")
    tag = (payload.get("tag") or "").strip() or db.make_tag(name)
    position = int(payload.get("position", 0) or 0)
    cur = await db.execute(
        "INSERT INTO verticals(name, tag, position) VALUES (?, ?, ?)",
        (name, tag, position))
    row = await db.fetchone("SELECT * FROM verticals WHERE id = ?", (cur.lastrowid,))
    return {"ok": True, "item": dict(row)}


@app.post("/admin/api/rubrics/verticals/{vertical_id}")
async def admin_update_vertical(vertical_id: int, payload: dict, _: bool = Depends(require_admin)):
    allowed = {"name", "tag", "position", "is_active"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    sets = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE verticals SET {sets} WHERE id = ?", (*fields.values(), vertical_id))
    row = await db.fetchone("SELECT * FROM verticals WHERE id = ?", (vertical_id,))
    if not row:
        raise HTTPException(404, "Вертикаль не найдена")
    return {"ok": True, "item": dict(row)}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/admin"):
        if not request.url.path.startswith("/admin/api"):
            return RedirectResponse("/admin", status_code=303)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
