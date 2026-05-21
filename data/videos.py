"""
Видео и ссылки на материалы.

VIDEO_1_PATH — путь к локальному файлу видео о компании (отправляется через бота).
VIDEO_2_URL — ссылка на видео о продуктах (можно YouTube/Drive — отправим текстом).
MATERIALS_URL — ссылка на материалы для изучения (NotebookLM).
"""

# Видео о компании (локальный файл из assets/)
VIDEO_1_PATH = "assets/MAVIS_GROUP.mp4"

# Если хотите выдавать видео-1 как ссылку вместо файла — раскомментируйте:
# VIDEO_1_URL = "https://drive.google.com/..."

# Видео о продуктах + материалы
VIDEO_2_URL = None  # подставьте ссылку, когда появится
MATERIALS_URL = "https://notebooklm.google.com/notebook/69f5b448-3709-46ab-ade7-136b143b1f7f"
