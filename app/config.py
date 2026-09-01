"""Конфигурация из .env + константы приложения."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

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
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()

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
