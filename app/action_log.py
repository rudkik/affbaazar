"""Файловые логи действий бота.

  ./logs/<chat_id>/<DD_MM_YYYY>.log
  время | user_id | никнейм | текст сообщения (в одну строку)

  ./logs-restricted/<chat_id>/restricted_<DD_MM_YYYY>.log
  — те, кто превысил лимит последовательных проверок подписки.
"""
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import LOG_DIR, RESTRICTED_LOG_DIR

log = logging.getLogger(__name__)
_lock = asyncio.Lock()
_WS = re.compile(r"\s+")


def flatten(text: Optional[str]) -> str:
    """Приводит сообщение к одной строке."""
    if not text:
        return ""
    return _WS.sub(" ", text.replace("|", "¦")).strip()


def _stamp() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%d_%m_%Y")


async def _write(path: Path, line: str) -> None:
    async with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            log.exception("Не удалось записать лог %s", path)


async def action(chat_id: int, user_id: int, username: Optional[str],
                 text: Optional[str], event: str = "") -> None:
    ts, day = _stamp()
    nick = f"@{username}" if username else "-"
    body = flatten(text)
    if event:
        body = f"[{event}] {body}".strip()
    path = LOG_DIR / str(chat_id) / f"{day}.log"
    await _write(path, f"{ts} | {user_id} | {nick} | {body}")


async def restricted(chat_id: int, user_id: int, username: Optional[str],
                     until: str, attempts: int) -> None:
    ts, day = _stamp()
    nick = f"@{username}" if username else "-"
    path = RESTRICTED_LOG_DIR / str(chat_id) / f"restricted_{day}.log"
    await _write(path, f"{ts} | {user_id} | {nick} | превышен лимит проверок "
                       f"({attempts}) | ограничен до {until}")
