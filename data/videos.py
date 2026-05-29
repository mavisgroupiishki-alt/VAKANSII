"""Пути к локальным видео и ссылки на видео на Google Drive/Google Vids."""
import os
from pathlib import Path

_BASE = Path(__file__).parent.parent

# Локальные файлы оставляем как запасной вариант, если ссылка не задана.
VIDEO_1_PATH = str(_BASE / "assets" / "MAVIS_GROUP.mp4")
VIDEO_2_PATH = str(_BASE / "assets" / "MAVIS_PRODUCT.mp4")

# Ссылки на видео. Их можно менять в Render → Environment без изменения кода.
VIDEO_1_URL = os.getenv(
    "VIDEO_1_URL",
    "https://docs.google.com/videos/d/1i3c78SieIX4SOQVvViOQ1t5WSzKgY4UuCoqecyZ0fus/edit?usp=sharing",
).strip()
VIDEO_2_URL = os.getenv("VIDEO_2_URL", "").strip()

# Для воронки менеджера по продажам можно задать отдельные ссылки.
SALES_VIDEO_1_URL = os.getenv("SALES_VIDEO_1_URL", VIDEO_1_URL).strip()
SALES_VIDEO_2_URL = os.getenv("SALES_VIDEO_2_URL", VIDEO_2_URL).strip()
