"""
Пути к видеофайлам — абсолютные относительно корня проекта.
"""
from pathlib import Path

# Корень проекта = папка где лежит этот файл + один уровень вверх
_BASE = Path(__file__).parent.parent

VIDEO_1_PATH = str(_BASE / "assets" / "MAVIS_GROUP.mp4")
VIDEO_2_PATH = str(_BASE / "assets" / "MAVIS_PRODUCT.mp4")
