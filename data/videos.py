"""Пути к видео и ссылки на Google Drive/Google Vids.

Видео в Telegram больше не загружаем файлом: бот отправляет только кнопку-ссылку.
Ссылки можно менять в Render → Environment.
"""
import os
from pathlib import Path

_BASE = Path(__file__).parent.parent

# Пути оставлены только для совместимости с импортами. send_video_safe локальные mp4 не отправляет.
VIDEO_1_PATH = str(_BASE / "assets" / "MAVIS_GROUP.mp4")
VIDEO_2_PATH = str(_BASE / "assets" / "MAVIS_PRODUCT.mp4")

DEFAULT_COMPANY_VIDEO_URL = "https://docs.google.com/videos/d/1i3c78SieIX4SOQVvViOQ1t5WSzKgY4UuCoqecyZ0fus/edit?usp=sharing"


def _env_or_default(name: str, default: str = "") -> str:
    """Берём env, но если переменная задана пустой строкой — используем default."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default.strip()
    return value.strip()


VIDEO_1_URL = _env_or_default("VIDEO_1_URL", DEFAULT_COMPANY_VIDEO_URL)
VIDEO_2_URL = _env_or_default("VIDEO_2_URL", "")

# Для воронки менеджера по продажам можно задать отдельную ссылку.
# Если SALES_VIDEO_1_URL пустая/не задана, используется основная ссылка на видео о компании.
SALES_VIDEO_1_URL = _env_or_default("SALES_VIDEO_1_URL", VIDEO_1_URL)

# SALES_VIDEO_2_URL оставлена для совместимости, но второй ролик в начале воронки больше не отправляется.
SALES_VIDEO_2_URL = _env_or_default("SALES_VIDEO_2_URL", VIDEO_2_URL)
