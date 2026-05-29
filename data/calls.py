"""Ссылки на учебную библиотеку звонков для стажировки продажников."""
import os

# Ссылки можно менять в Render → Environment без изменения кода.
GOOD_CALLS_URL = os.getenv(
    "GOOD_CALLS_URL",
    "https://drive.google.com/drive/folders/1Cklv-UtH-pexL2RGQHDIHbvvzsqXU6ga?usp=sharing",
).strip()

BAD_CALLS_URL = os.getenv(
    "BAD_CALLS_URL",
    "https://drive.google.com/drive/folders/1RBQv-XRiL5mixHFLmKAI3J7SYfg_NuxD?usp=sharing",
).strip()
