"""Mavis HR Bot — точка входа."""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from config import BOT_TOKEN
from db.database import init_db
from handlers import hr, candidate
from handlers import sales
from services.reminders import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    log.info("Запуск Mavis HR Bot...")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Инициализация БД
    await init_db()
    log.info("База данных готова")

    # Подключаем роутеры — HR раньше кандидата, чтобы команды /add и т.п.
    # перехватывались до универсального обработчика текстов в candidate.py
    dp.include_router(hr.router)
    dp.include_router(sales.router)
    dp.include_router(candidate.router)

    # Запускаем планировщик напоминаний
    setup_scheduler(bot)

    # HTTP-сервер для health-check (нужен бесплатному тарифу Render)
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Mavis HR Bot is running"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"HTTP health-check на порту {port}")

    log.info("Бот запущен и готов к работе")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
