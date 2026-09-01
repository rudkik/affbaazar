"""Авторизация через Telegram: Mini App (initData) и Login Widget.

Оба способа подтверждают личность подписью на секрете бота, поэтому доверять
можно только проверенным здесь данным — никогда не user_id из тела запроса.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import parse_qsl

from app.config import BOT_TOKEN, SECRET_KEY

log = logging.getLogger(__name__)

SESSION_COOKIE = "tg_session"
SESSION_TTL = 30 * 24 * 3600        # сколько живёт сессия сайта
INIT_DATA_TTL = 24 * 3600           # сколько считаем свежим initData из Mini App
WIDGET_TTL = 86400                  # то же для Login Widget


def _same_hash(expected: str, received: Any) -> bool:
    """Безопасное сравнение: неASCII и не-строки — просто не совпадение, а не ошибка."""
    if not isinstance(received, str):
        return False
    try:
        return hmac.compare_digest(expected, received)
    except TypeError:            # в подписи оказались неASCII-символы
        return False


# ------------------------------------------------------------------ Telegram
def verify_webapp(init_data: str, bot_token: str = "", max_age: int = INIT_DATA_TTL
                  ) -> Optional[dict[str, Any]]:
    """Проверяет initData из Telegram Mini App. -> данные пользователя или None."""
    token = bot_token or BOT_TOKEN
    if not token or not init_data:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None

    received = data.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not _same_hash(expected, received):
        return None

    if max_age:
        try:
            if time.time() - int(data.get("auth_date", 0)) > max_age:
                return None
        except (TypeError, ValueError):
            return None

    try:
        user = json.loads(data.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    return user if user.get("id") else None


def verify_login_widget(params: dict[str, Any], bot_token: str = "",
                        max_age: int = WIDGET_TTL) -> Optional[dict[str, Any]]:
    """Проверяет данные Telegram Login Widget (вход с обычного сайта)."""
    token = bot_token or BOT_TOKEN
    if not token or not params:
        return None
    data = {k: str(v) for k, v in params.items() if v is not None}
    received = data.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not _same_hash(expected, received):
        return None
    if max_age:
        try:
            if time.time() - int(data.get("auth_date", 0)) > max_age:
                return None
        except (TypeError, ValueError):
            return None
    if not data.get("id"):
        return None
    return {
        "id": int(data["id"]),
        "username": data.get("username"),
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "photo_url": data.get("photo_url"),
    }


# ------------------------------------------------------------------ сессия сайта
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session(user_id: int, username: Optional[str], is_admin: bool,
                  ttl: int = SESSION_TTL) -> str:
    """Подписанная кука сессии: без секрета её не подделать."""
    payload = {"uid": int(user_id), "un": username or "", "adm": int(bool(is_admin)),
               "exp": int(time.time()) + ttl}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(cookie: Optional[str]) -> Optional[dict[str, Any]]:
    """Разбирает и проверяет куку сессии. -> {'uid','un','adm'} или None."""
    if not cookie or "." not in cookie:
        return None
    body, _, sig = cookie.rpartition(".")
    expected = hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not _same_hash(expected, sig):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload
