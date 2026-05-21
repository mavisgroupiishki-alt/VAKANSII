"""Конфигурация бота — загружается из .env"""
import os
from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> list[int]:
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
HR_TELEGRAM_IDS: list[int] = _parse_ids(os.getenv("HR_TELEGRAM_IDS", ""))

PASS_THRESHOLD: int = int(os.getenv("PASS_THRESHOLD", "85"))  # для совместимости со старым кодом
PASS_THRESHOLD_TEST_1: int = int(os.getenv("PASS_THRESHOLD_TEST_1", "85"))
PASS_THRESHOLD_TEST_2: int = int(os.getenv("PASS_THRESHOLD_TEST_2", "75"))
REMINDER_INTERVAL_HOURS: int = int(os.getenv("REMINDER_INTERVAL_HOURS", "1"))
MAX_REMINDERS: int = int(os.getenv("MAX_REMINDERS", "3"))

TEST1_TIME_LIMIT_HOURS: int = int(os.getenv("TEST1_TIME_LIMIT_HOURS", "2"))
TEST2_TIME_LIMIT_HOURS: int = int(os.getenv("TEST2_TIME_LIMIT_HOURS", "24"))

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mavis_bot.db")


def is_hr(user_id: int) -> bool:
    """Проверка, является ли пользователь рекрутером."""
    return user_id in HR_TELEGRAM_IDS


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")
