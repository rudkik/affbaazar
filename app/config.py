"""Конфигурация из .env + константы приложения."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Бот поддержки (@aff_bazzar_support_bot): отдельный токен и форум-группа с топиками,
# куда попадают обращения. Пусто = бот поддержки не запускается (app/support.py).
SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN", "").strip()


def _chat_id(raw: str) -> int:
    """ID супергруппы: принимаем и «-100…», и голый номер из адресной строки Telegram."""
    raw = (raw or "").strip()
    if not raw.lstrip("-").isdigit():
        return 0
    value = int(raw)
    return value if value < 0 else int(f"-100{value}")


SUPPORT_CHAT_ID = _chat_id(os.getenv("SUPPORT_CHAT_ID", ""))

def _ids(raw: str) -> set[int]:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out

ADMINS: set[int] = _ids(os.getenv("ADMINS", ""))

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "insecure-dev-secret")
PUBLIC_URL = os.getenv("PUBLIC_URL", f"http://localhost:{WEB_PORT}").rstrip("/")

# CryptoPay (USDT/USDC): ключ и секрет вебхука выдаёт админка процессинга, см. INTEGRATION.md.
CRYPTOPAY_API_KEY = os.getenv("CRYPTOPAY_API_KEY", "").strip()
CRYPTOPAY_WEBHOOK_SECRET = os.getenv("CRYPTOPAY_WEBHOOK_SECRET", "").strip()
CRYPTOPAY_BASE_URL = (os.getenv("CRYPTOPAY_BASE_URL") or "https://ubaduba.top").strip().rstrip("/")


def cryptopay_enabled() -> bool:
    """Кнопка «оплатить криптой» показывается, только когда задан API-ключ."""
    return bool(CRYPTOPAY_API_KEY)

# Адрес сайта для Telegram Mini App. Telegram принимает только HTTPS,
# поэтому на localhost кнопка-приложение не появится — это нормально.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or PUBLIC_URL).rstrip("/")


def webapp_available() -> bool:
    return WEBAPP_URL.startswith("https://")

# Каталог с данными: базы и логи. В Docker сюда монтируется том.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAIN_DB = DATA_DIR / "bot.db"
SITE_DB = DATA_DIR / "site.db"          # независимая база-дублёр для сайта
LOG_DIR = Path(os.getenv("LOG_DIR") or DATA_DIR / "logs")
RESTRICTED_LOG_DIR = Path(os.getenv("RESTRICTED_LOG_DIR") or DATA_DIR / "logs-restricted")
