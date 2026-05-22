"""Источники кандидатов — откуда пришёл."""

SOURCES = {
    "hh": "hh.ru / Rabota.by",
    "telegram": "Telegram-канал",
    "recommendation": "Рекомендация",
    "ad": "Реклама",
    "linkedin": "LinkedIn",
    "instagram": "Instagram",
    "other": "Другое",
}


def source_label(key: str | None) -> str:
    if not key:
        return "—"
    return SOURCES.get(key, key)
