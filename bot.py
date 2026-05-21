"""Mavis HR Bot — точка входа."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BOT_TOKEN
from db.database import init_db
from handlers import hr, candidate
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
    dp.include_router(candidate.router)

    # Запускаем планировщик напоминаний
    setup_scheduler(bot)

    log.info("Бот запущен и готов к работе")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
