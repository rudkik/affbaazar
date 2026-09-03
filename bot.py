"""Точка входа: Telegram-бот + веб-сервер (лента и админка) в одном процессе."""
import asyncio
import logging
import logging.handlers
import sys

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonDefault

from app import config, db, site_db
from app.handlers import admin, chat_guard, members, moderation, payments, post, user
from app.web.server import app as web_app
from app.web.server import set_bot

log = logging.getLogger("bot")

USER_COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="balance", description="Баланс токенов"),
    BotCommand(command="buy", description="Купить токены"),
    BotCommand(command="ref", description="Пригласить друга"),
    BotCommand(command="site", description="Лента сообщений на сайте"),
]


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def run_web(bot: Bot) -> None:
    set_bot(bot)
    # proxy_headers: за nginx берём схему/IP из X-Forwarded-*. Порт наружу не публикуется
    # (docker-compose вешает его на 127.0.0.1), поэтому доверять всем источникам безопасно.
    server = uvicorn.Server(uvicorn.Config(
        web_app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning",
        access_log=False, proxy_headers=True, forwarded_allow_ips="*"))
    await server.serve()


async def pin_watcher(bot: Bot) -> None:
    """Снимает закрепы объявлений, у которых вышло оплаченное время."""
    from app import ads
    while True:
        try:
            done = await ads.unpin_expired(bot)
            if done:
                log.info("Снято закрепов: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в задаче снятия закрепов")
        await asyncio.sleep(60)


async def main() -> None:
    setup_logging()
    if not config.BOT_TOKEN:
        log.error("BOT_TOKEN не задан — заполните .env")
        return
    if not config.ADMINS:
        log.warning("ADMINS пуст — админ-команды бота будут недоступны")

    await db.init()
    await site_db.init()

    bot = Bot(token=config.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)
    dp.include_router(moderation.router)
    dp.include_router(post.router)
    dp.include_router(payments.router)
    dp.include_router(user.router)
    dp.include_router(chat_guard.router)
    dp.include_router(members.router)

    me = await bot.get_me()
    log.info("Бот запущен: @%s (id=%s)", me.username, me.id)
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось установить меню команд")

    # Mini App пока отключена: сбрасываем кнопку «Приложение» слева от строки ввода на стандартное
    # меню команд (она хранится на стороне Telegram и иначе останется от прошлых запусков).
    # Вместо неё в главном меню две кнопки-ссылки: канал и сайт (keyboards.main_menu).
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        log.info("Mini App отключена, кнопка меню сброшена; сайт: %s", config.PUBLIC_URL)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сбросить кнопку меню")

    web_task = asyncio.create_task(run_web(bot))
    pin_task = asyncio.create_task(pin_watcher(bot))
    log.info("Веб-сервер: http://%s:%s (лента) и /admin", config.WEB_HOST, config.WEB_PORT)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        # chat_member добавляем явно: без него Telegram не пришлёт вход/выход
        # в канале (учёт подписчиков в app/handlers/members.py).
        allowed = dp.resolve_used_update_types()
        if "chat_member" not in allowed:
            allowed.append("chat_member")
        log.info("Слушаем апдейты: %s", ", ".join(allowed))
        await dp.start_polling(bot, allowed_updates=allowed)
    finally:
        web_task.cancel()
        pin_task.cancel()
        await bot.session.close()
        await db.close()
        await site_db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
