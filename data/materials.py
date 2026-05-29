"""Файлы учебных материалов для стажировки."""
from pathlib import Path

_BASE = Path(__file__).parent.parent

# DOCX-файл, который бот отправляет кандидату во 2 день стажировки.
SALES_PRODUCTS_DOC_PATH = str(_BASE / "assets" / "stazhirovka_den2_produkty_mavis_group.docx")
