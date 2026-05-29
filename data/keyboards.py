"""Клавиатуры для рекрутера — все в одном месте."""
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


# ============================================================
# ОСНОВНОЕ МЕНЮ ВНИЗУ ЭКРАНА (Reply Keyboard)
# ============================================================
def kb_main_menu() -> ReplyKeyboardMarkup:
    """Главное меню рекрутера — всегда видно внизу."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Добавить кандидата"),
                KeyboardButton(text="📋 Кандидаты"),
            ],
            [
                KeyboardButton(text="🔍 Поиск"),
                KeyboardButton(text="📅 Расписание"),
            ],
            [
                KeyboardButton(text="📊 Аналитика"),
                KeyboardButton(text="📈 Статистика"),
            ],
            [
                KeyboardButton(text="📥 Экспорт CSV"),
                KeyboardButton(text="⚙️ Ещё"),
            ],
        ],
        resize_keyboard=True,
        persistent=True,
        input_field_placeholder="Выберите действие или введите команду…",
    )


def kb_more_menu() -> InlineKeyboardMarkup:
    """Дополнительные функции."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Массовое добавление", callback_data="menu_bulkadd")],
        [InlineKeyboardButton(text="📚 Помощь / все команды", callback_data="menu_help")],
        [InlineKeyboardButton(text="🆔 Мой Telegram ID", callback_data="menu_myid")],
    ])


# ============================================================
# СПИСОК КАНДИДАТОВ — ФИЛЬТРЫ
# ============================================================
def kb_candidates_filters() -> InlineKeyboardMarkup:
    """Фильтры списка кандидатов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Все", callback_data="cf_all_0"),
            InlineKeyboardButton(text="Активные", callback_data="cf_active_0"),
        ],
        [
            InlineKeyboardButton(text="Прошли тесты", callback_data="cf_passed_0"),
            InlineKeyboardButton(text="Провалили", callback_data="cf_failed_0"),
        ],
        [
            InlineKeyboardButton(text="Оффер отправлен", callback_data="cf_offer_0"),
            InlineKeyboardButton(text="Не отвечают", callback_data="cf_noresp_0"),
        ],
    ])


def kb_candidate_list(candidates: list, filter_key: str, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    """Список кандидатов — каждая строка кликабельна, ведёт на карточку."""
    rows = []
    start = page * per_page
    end = start + per_page
    visible = candidates[start:end]

    for c in visible:
        # Эмодзи статуса для визуального скана
        emoji = {
            "active": "🔵",
            "passed": "🟢",
            "failed": "🔴",
            "on_pause": "🟡",
            "no_response": "⚫",
            "offer_sent": "🎉",
            "rejected": "❌",
        }.get(c.status, "⚪")

        label = f"{emoji} #{c.id} {c.full_name}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"cd_{c.id}")])

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cf_{filter_key}_{page - 1}"))
    if end < len(candidates):
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"cf_{filter_key}_{page + 1}"))
    if nav:
        rows.append(nav)

    # Кнопка возврата к фильтрам
    rows.append([InlineKeyboardButton(text="🔄 Сменить фильтр", callback_data="menu_candidates")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# КАРТОЧКА КАНДИДАТА — ДЕЙСТВИЯ
# ============================================================
def kb_candidate_actions(
    candidate_id: int,
    has_telegram: bool,
    stage: int,
    status: str,
    has_test_1: bool,
    has_test_2: bool,
    test_numbers: list[int] | None = None,
    position: str | None = None,
    internship_day: int = 0,
) -> InlineKeyboardMarkup:
    """Кнопки действий в карточке кандидата."""
    rows = []
    test_numbers = test_numbers or []

    # Связь — только если кандидат активировал приглашение
    if has_telegram:
        rows.append([
            InlineKeyboardButton(text="✉️ Написать в бот", callback_data=f"ca_notify_{candidate_id}"),
        ])

    # Ссылка-приглашение (если ещё не активирован)
    if not has_telegram:
        rows.append([
            InlineKeyboardButton(text="🔗 Показать ссылку-приглашение", callback_data=f"ca_invite_{candidate_id}"),
        ])

    # Отдельная кнопка для рекрутера: просмотр ответов по тестам.
    # Кандидат её не видит, потому что она показывается только в HR-карточке.
    if test_numbers:
        rows.append([
            InlineKeyboardButton(text="🧪 Посмотреть ответы на тесты", callback_data=f"ca_tests_{candidate_id}"),
        ])

    # Запуск стажировки после звонка РОП.
    # Показываем кнопку шире, чем только position == "sales", потому что старые кандидаты
    # могли быть добавлены до появления поля "position" или с дефолтной позицией support.
    # Кандидату кнопка не видна — это клавиатура только HR-карточки.
    is_sales_flow = (
        position == "sales"
        or stage >= 10
        or 10 in test_numbers
        or any(num >= 100 for num in test_numbers)
    )
    if is_sales_flow and has_telegram and status not in ("rejected",):
        if internship_day == 0:
            button_text = "📅 Изменить дату старта стажировки" if stage == 15 else "🚀 Запустить стажировку"
            rows.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"ca_start_internship_{candidate_id}",
                ),
            ])

        # Ручная отправка стажировочного дня прямо сейчас.
        # Нужна, если РОП хочет дать кандидату материал вне расписания 08:30.
        rows.append([
            InlineKeyboardButton(text="📤 День 1 сейчас", callback_data=f"ca_send_internship_day_{candidate_id}_1"),
        ])
        rows.append([
            InlineKeyboardButton(text="📤 День 2 сейчас", callback_data=f"ca_send_internship_day_{candidate_id}_2"),
        ])
        rows.append([
            InlineKeyboardButton(text="📤 День 3 сейчас", callback_data=f"ca_send_internship_day_{candidate_id}_3"),
        ])

    # Кейсы — только если прошёл оба теста
    if has_test_1 and has_test_2 and status == "passed":
        rows.append([
            InlineKeyboardButton(text="📋 Получить кейсы", callback_data=f"ca_cases_{candidate_id}"),
        ])

    # Изменение статуса
    rows.append([
        InlineKeyboardButton(text="🎯 Изменить статус", callback_data=f"ca_status_{candidate_id}"),
    ])

    # Пересдача
    retry_buttons = []
    if has_test_1:
        retry_buttons.append(InlineKeyboardButton(text="🔄 Пересдать тест 1", callback_data=f"ca_retry_{candidate_id}_1"))
    if has_test_2:
        retry_buttons.append(InlineKeyboardButton(text="🔄 Пересдать тест 2", callback_data=f"ca_retry_{candidate_id}_2"))
    if retry_buttons:
        # По одной кнопке в строке для удобства
        for btn in retry_buttons:
            rows.append([btn])

    # Удаление
    rows.append([
        InlineKeyboardButton(text="🗑 Удалить кандидата", callback_data=f"ca_delete_{candidate_id}"),
    ])

    # Назад к списку
    rows.append([
        InlineKeyboardButton(text="↩️ К списку", callback_data="menu_candidates"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_candidate_test_results(candidate_id: int, results: list) -> InlineKeyboardMarkup:
    """Список тестов кандидата для просмотра подробных ответов."""
    rows = []
    for r in results:
        mark = "✅" if r.passed else "❌"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} Тест {r.test_number} — {r.score_percent}%",
                callback_data=f"ca_test_{candidate_id}_{r.test_number}",
            )
        ])

    rows.append([InlineKeyboardButton(text="↩️ Назад в карточку", callback_data=f"cd_{candidate_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back_to_candidate(candidate_id: int) -> InlineKeyboardMarkup:
    """Кнопка возврата в карточку кандидата."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад в карточку", callback_data=f"cd_{candidate_id}")]
    ])


def kb_internship_start_dates(candidate_id: int, dates: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Выбор даты старта стажировки для HR.

    dates: список пар (label, yyyymmdd).
    """
    rows = []
    for label, date_value in dates:
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"ca_set_internship_date_{candidate_id}_{date_value}",
        )])
    rows.append([InlineKeyboardButton(text="↩️ Назад в карточку", callback_data=f"cd_{candidate_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_status_choice(candidate_id: int) -> InlineKeyboardMarkup:
    """Кнопки для выбора нового статуса."""
    statuses = [
        ("🔵 Активный", "active"),
        ("🟢 Прошёл тесты", "passed"),
        ("🔴 Провалил", "failed"),
        ("🟡 На паузе", "on_pause"),
        ("⚫ Не отвечает", "no_response"),
        ("🎉 Оффер отправлен", "offer_sent"),
        ("❌ Отказано", "rejected"),
    ]
    rows = []
    for label, status_key in statuses:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"st_set_{candidate_id}_{status_key}")])
    rows.append([InlineKeyboardButton(text="↩️ Отмена", callback_data=f"cd_{candidate_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm_delete(candidate_id: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждения удаления."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"del_yes_{candidate_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cd_{candidate_id}"),
    ]])


def kb_confirm_retry(candidate_id: int, test_num: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждения пересдачи."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Да, разрешить пересдачу теста {test_num}",
                             callback_data=f"retry_yes_{candidate_id}_{test_num}"),
    ], [
        InlineKeyboardButton(text="↩️ Отмена", callback_data=f"cd_{candidate_id}"),
    ]])
