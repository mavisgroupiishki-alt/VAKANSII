"""Хендлеры для рекрутера — админ-команды + кнопочное меню."""
import csv
import io
import secrets
import os
from datetime import datetime

from aiogram import Router, Bot, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, BufferedInputFile, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from sqlalchemy import select, func, delete

from config import is_hr
from db.database import get_session
from db.models import Candidate, TestResult, TestSession
from data.texts import CASES_TEMPLATE, HR_NEW_CANDIDATE_ADDED
from data.sources import SOURCES, source_label

router = Router()


# ============================================================
# REPLY-КЛАВИАТУРА РЕКРУТЕРА (постоянная, под полем ввода)
# ============================================================
# Тексты кнопок — это то, что бот будет получать как обычный текст.
# По этому тексту мы маршрутизируем в нужный обработчик.
BTN_ADD = "➕ Добавить кандидата"
BTN_LIST = "📋 Все кандидаты"
BTN_ACTIVE = "🔥 Активные"
BTN_SLOTS = "📅 Слоты"
BTN_STATS = "📊 Статистика"
BTN_ANALYTICS = "📈 Аналитика"
BTN_EXPORT = "📥 Экспорт CSV"
BTN_DELETE = "🗑 Удалить кандидата"
BTN_HELP = "❓ Помощь"


def kb_hr_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню рекрутера под полем ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_ACTIVE), KeyboardButton(text=BTN_SLOTS)],
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_ANALYTICS)],
            [KeyboardButton(text=BTN_EXPORT), KeyboardButton(text=BTN_DELETE)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или введите команду",
    )


def kb_candidate_actions(cid: int, *, with_cases: bool = True) -> InlineKeyboardMarkup:
    """Inline-кнопки действий над конкретным кандидатом.

    Кладутся под уведомления, чтобы рекрутер не вводил ID руками.
    with_cases=False — для уведомлений на ранних этапах (видео/тест 1),
    когда выдавать кейсы рано.
    """
    rows = []
    if with_cases:
        rows.append([
            InlineKeyboardButton(text="📋 Кейсы", callback_data=f"hr_cases_{cid}"),
            InlineKeyboardButton(text="👤 Карточка", callback_data=f"hr_card_{cid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="💼 Оффер", callback_data=f"hr_offer_{cid}"),
            InlineKeyboardButton(text="❌ Отказ", callback_data=f"hr_reject_{cid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="⏸ Пауза", callback_data=f"hr_pause_{cid}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="👤 Карточка", callback_data=f"hr_card_{cid}"),
            InlineKeyboardButton(text="⏸ Пауза", callback_data=f"hr_pause_{cid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# /start для рекрутера — показывает постоянное меню
# ============================================================
# Фильтр is_hr через magic-filter: для не-HR обработчик не сработает,
# и /start уйдёт в candidate.py (там свой CommandStart).
@router.message(Command("start"), F.from_user.id.func(is_hr))
async def hr_cmd_start(message: Message):
    await message.answer(
        "<b>👋 Mavis HR Bot</b>\n\n"
        "Используйте кнопки ниже для основных действий. "
        "Для конкретного кандидата действия появляются прямо в уведомлениях о нём.\n\n"
        "Если нужны редкие команды — /help.",
        reply_markup=kb_hr_menu(),
    )


@router.message(Command("menu"), F.from_user.id.func(is_hr))
async def hr_cmd_menu(message: Message):
    """Принудительно показать меню (если рекрутер случайно его закрыл)."""
    await message.answer("Меню обновлено.", reply_markup=kb_hr_menu())


# ============================================================
# FSM ДЛЯ ДОБАВЛЕНИЯ КАНДИДАТА
# ============================================================
class AddCandidate(StatesGroup):
    waiting_full_name = State()
    waiting_phone = State()
    waiting_username = State()
    waiting_source = State()


class BulkAdd(StatesGroup):
    waiting_list = State()
    waiting_source = State()


def _gen_invite_code() -> str:
    """Генерирует уникальный короткий код приглашения."""
    return secrets.token_urlsafe(8).replace("-", "a").replace("_", "b")[:10]


def kb_sources() -> InlineKeyboardMarkup:
    """Клавиатура выбора источника кандидата."""
    rows = []
    for key, label in SOURCES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"src_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm_delete(candidate_id: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждения удаления."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"del_yes_{candidate_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="del_no"),
    ]])


# ============================================================
# /help — список команд для HR
# ============================================================
@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_hr(message.from_user.id):
        return
    text = (
        "<b>🛠 Команды рекрутера</b>\n\n"
        "<i>Большинство действий доступно через кнопки внизу экрана. "
        "Команды нужны только для редких операций.</i>\n\n"
        "<b>Добавление:</b>\n"
        "/add — добавить кандидата (выдаст ссылку-приглашение)\n"
        "/bulk_add — массово добавить кандидатов списком\n\n"
        "<b>Просмотр:</b>\n"
        "/list — список всех кандидатов\n"
        "/active — только активные\n"
        "/candidate ID — карточка кандидата\n"
        "/invite ID — показать ссылку-приглашение ещё раз\n"
        "/slots — расписание собеседований\n"
        "/stats — статистика по воронке\n"
        "/analytics — аналитика с графиками 📊\n"
        "/export — выгрузить CSV\n\n"
        "<b>Управление кандидатом:</b>\n"
        "/cases ID — кейсы для собеседования\n"
        "/set_status ID статус — изменить статус\n"
        "  Статусы: active, passed, failed, on_pause, no_response, offer_sent, rejected\n"
        "/retry ID 1 — разрешить пересдачу теста 1\n"
        "/retry ID 2 — разрешить пересдачу теста 2\n"
        "/notify ID текст — отправить сообщение кандидату\n"
        "/delete ID — удалить кандидата 🗑\n\n"
        "<b>Прочее:</b>\n"
        "/menu — показать кнопки меню\n"
        "/myid — мой Telegram ID\n"
        "/cancel — прервать текущую операцию"
    )
    await message.answer(text, reply_markup=kb_hr_menu())


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


# ============================================================
# /add — добавление кандидата (FSM)
# ============================================================
@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    if not is_hr(message.from_user.id):
        return
    await state.set_state(AddCandidate.waiting_full_name)
    await message.answer(
        "<b>➕ Добавление кандидата</b>\n\n"
        "Введите <b>ФИО кандидата</b>:\n\n"
        "(или /cancel для отмены)"
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    if not is_hr(message.from_user.id):
        return
    await state.clear()
    await message.answer("Отменено.")


@router.message(AddCandidate.waiting_full_name)
async def add_full_name(message: Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name) < 3:
        await message.answer("ФИО слишком короткое. Введите снова.")
        return
    await state.update_data(full_name=full_name)
    await state.set_state(AddCandidate.waiting_phone)
    await message.answer("📱 Введите <b>номер телефона</b> кандидата (или '-' если нет):")


@router.message(AddCandidate.waiting_phone)
async def add_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if phone == "-":
        phone = None
    await state.update_data(phone=phone)
    await state.set_state(AddCandidate.waiting_username)
    await message.answer(
        "💬 Введите <b>username в Telegram</b> (@username) или '-' если не знаете.\n\n"
        "<i>Это нужно только для вашего удобства — для связи. На работу бота не влияет.</i>"
    )


@router.message(AddCandidate.waiting_username)
async def add_username(message: Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    if username == "-":
        username = None
    await state.update_data(username=username)
    await state.set_state(AddCandidate.waiting_source)
    await message.answer(
        "📌 Откуда пришёл кандидат? Выберите источник:",
        reply_markup=kb_sources(),
    )


@router.callback_query(AddCandidate.waiting_source, F.data.startswith("src_"))
async def add_source(callback: CallbackQuery, state: FSMContext, bot: Bot):
    source = callback.data[len("src_"):]
    data = await state.get_data()

    # Генерируем уникальный код приглашения
    invite_code = _gen_invite_code()

    async with get_session() as s:
        # Защита от коллизий
        for _ in range(5):
            check = await s.execute(select(Candidate).where(Candidate.invite_code == invite_code))
            if check.scalar_one_or_none() is None:
                break
            invite_code = _gen_invite_code()

        candidate = Candidate(
            telegram_id=None,
            full_name=data["full_name"],
            username=data.get("username"),
            phone=data.get("phone"),
            invite_code=invite_code,
            source=source,
            stage=0,
            status="active",
            added_by=callback.from_user.id,
            awaiting="invite_pending",
        )
        s.add(candidate)
        await s.flush()
        cid = candidate.id

    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    bot_info = await bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={invite_code}"

    await callback.message.answer(
        f"✅ Кандидат <b>{data['full_name']}</b> добавлен (#{cid}).\n"
        f"Источник: <b>{source_label(source)}</b>\n\n"
        f"<b>📨 Ссылка-приглашение:</b>\n"
        f"<code>{invite_link}</code>\n\n"
        f"<b>Отправьте эту ссылку кандидату</b> любым удобным способом:\n"
        f"• Telegram (если знаете username)\n"
        f"• WhatsApp / Viber\n"
        f"• SMS на номер {data.get('phone') or '—'}\n"
        f"• E-mail\n\n"
        f"Когда кандидат кликнет ссылку — бот сразу начнёт собеседование и пришлёт вам уведомление."
    )
    await callback.answer()


# ============================================================
# /invite ID — повторно показать ссылку
# ============================================================
@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /invite &lt;ID&gt;")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return
        if not c.invite_code:
            await message.answer("У этого кандидата нет ссылки-приглашения (старая запись).")
            return
        full_name = c.full_name
        invite_code = c.invite_code
        already_activated = c.telegram_id is not None

    bot_info = await bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={invite_code}"

    status_text = (
        "ℹ️ Кандидат уже активировал приглашение и проходит воронку."
        if already_activated
        else "⏳ Кандидат ещё не активировал приглашение."
    )

    await message.answer(
        f"<b>📨 Ссылка для {full_name} (#{cid})</b>\n\n"
        f"<code>{invite_link}</code>\n\n"
        f"{status_text}"
    )


# ============================================================
# /list, /active — список кандидатов
# ============================================================
def _format_candidate_short(c: Candidate) -> str:
    stage_names = {
        0: "не начал", 1: "видео 1 / тест 1", 2: "мотивация",
        3: "видео 2 / тест 2", 4: "тесты пройдены", 5: "на кейсах", 6: "финал"
    }
    return (
        f"#{c.id} <b>{c.full_name}</b>\n"
        f"  Этап: {stage_names.get(c.stage, '?')} | Статус: {c.status}\n"
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    if not is_hr(message.from_user.id):
        return
    async with get_session() as s:
        result = await s.execute(select(Candidate).order_by(Candidate.added_at.desc()))
        candidates = result.scalars().all()

    if not candidates:
        await message.answer("Нет кандидатов.")
        return

    text = "<b>📋 Все кандидаты:</b>\n\n" + "\n".join(_format_candidate_short(c) for c in candidates)
    # Telegram лимит сообщения 4096
    for chunk in [text[i:i+3800] for i in range(0, len(text), 3800)]:
        await message.answer(chunk)


@router.message(Command("active"))
async def cmd_active(message: Message):
    if not is_hr(message.from_user.id):
        return
    async with get_session() as s:
        result = await s.execute(
            select(Candidate).where(Candidate.status == "active").order_by(Candidate.added_at.desc())
        )
        candidates = result.scalars().all()

    if not candidates:
        await message.answer("Активных кандидатов нет.")
        return

    text = "<b>📋 Активные кандидаты:</b>\n\n" + "\n".join(_format_candidate_short(c) for c in candidates)
    await message.answer(text)


# ============================================================
# /candidate ID — карточка кандидата
# ============================================================
@router.message(Command("candidate"))
async def cmd_candidate(message: Message):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /candidate &lt;ID&gt;")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return

        tr_result = await s.execute(
            select(TestResult).where(TestResult.candidate_id == cid).order_by(TestResult.test_number)
        )
        results = tr_result.scalars().all()

    stage_names = {
        0: "не начал", 1: "видео 1 / тест 1", 2: "мотивация",
        3: "видео 2 / тест 2", 4: "тесты пройдены", 5: "на кейсах", 6: "финал"
    }

    text = (
        f"<b>👤 Кандидат #{c.id}</b>\n\n"
        f"ФИО: <b>{c.full_name}</b>\n"
        f"Telegram ID: <code>{c.telegram_id}</code>\n"
        f"Username: @{c.username or '—'}\n"
        f"Телефон: {c.phone or '—'}\n"
        f"Источник: {source_label(c.source)}\n\n"
        f"Этап: {stage_names.get(c.stage, '?')}\n"
        f"Статус: <b>{c.status}</b>\n"
        f"Добавлен: {c.added_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Последняя активность: {c.last_activity_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Напоминаний отправлено: {c.reminder_count}\n\n"
    )

    if results:
        text += "<b>Результаты тестов:</b>\n"
        for r in results:
            mark = "✅" if r.passed else "❌"
            text += f"  {mark} Тест {r.test_number}: <b>{r.score_percent}%</b>\n"
    else:
        text += "<i>Тесты ещё не пройдены</i>\n"

    if c.motivation_answer:
        text += f"\n<b>💬 Мотивационный ответ:</b>\n<i>{c.motivation_answer[:500]}</i>"

    if c.interview_slot:
        from data.slots import slot_label
        text += f"\n\n<b>📅 Слот собеседования:</b> {slot_label(c.interview_slot)}"

    await message.answer(text)


# ============================================================
# /cases ID — выдать кейсы для собеседования
# ============================================================
@router.message(Command("cases"))
async def cmd_cases(message: Message):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /cases &lt;ID&gt;")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return
        # Переводим в этап кейсов
        c.stage = 5
        full_name, username, phone = c.full_name, c.username, c.phone

    await message.answer(
        CASES_TEMPLATE.format(
            name=full_name,
            username=username or "—",
            phone=phone or "—",
        ),
    )


# ============================================================
# /set_status ID status — изменить статус
# ============================================================
ALLOWED_STATUSES = {"active", "passed", "failed", "on_pause", "no_response", "offer_sent", "rejected"}


@router.message(Command("set_status"))
async def cmd_set_status(message: Message, bot: Bot):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование: /set_status &lt;ID&gt; &lt;статус&gt;\n\n"
            f"Допустимые статусы: {', '.join(sorted(ALLOWED_STATUSES))}"
        )
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    new_status = parts[2].strip().lower()
    if new_status not in ALLOWED_STATUSES:
        await message.answer(f"Недопустимый статус. Используйте: {', '.join(sorted(ALLOWED_STATUSES))}")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return
        old_status = c.status
        c.status = new_status
        if new_status == "offer_sent":
            c.stage = 6
        tg_id = c.telegram_id

    await message.answer(f"✅ Статус кандидата #{cid} изменён: {old_status} → <b>{new_status}</b>")

    # Авто-уведомления кандидату по некоторым статусам
    notify_text = None
    if new_status == "offer_sent":
        notify_text = (
            "🎉 <b>Поздравляем!</b>\n\n"
            "По итогам собеседования мы готовы сделать вам оффер. "
            "С вами свяжется рекрутер в ближайшее время для обсуждения деталей."
        )
    elif new_status == "rejected":
        notify_text = (
            "Благодарим вас за участие в собеседовании. "
            "К сожалению, мы приняли решение не продолжать с вами процесс на этом этапе. "
            "Желаем удачи в дальнейших поисках!"
        )

    if notify_text:
        try:
            await bot.send_message(tg_id, notify_text)
            await message.answer("✉️ Кандидату отправлено уведомление.")
        except Exception as e:
            await message.answer(f"⚠️ Не удалось уведомить кандидата: {e}")


# ============================================================
# /notify ID текст — отправить кастомное сообщение кандидату
# ============================================================
@router.message(Command("notify"))
async def cmd_notify(message: Message, bot: Bot):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /notify &lt;ID&gt; &lt;текст&gt;")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return
        tg_id = c.telegram_id

    try:
        await bot.send_message(tg_id, parts[2])
        await message.answer("✉️ Отправлено.")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить: {e}")


# ============================================================
# /stats — статистика
# ============================================================
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_hr(message.from_user.id):
        return
    async with get_session() as s:
        total_q = await s.execute(select(func.count(Candidate.id)))
        total = total_q.scalar() or 0

        # По статусам
        status_q = await s.execute(
            select(Candidate.status, func.count(Candidate.id)).group_by(Candidate.status)
        )
        by_status = dict(status_q.all())

        # По этапам
        stage_q = await s.execute(
            select(Candidate.stage, func.count(Candidate.id)).group_by(Candidate.stage)
        )
        by_stage = dict(stage_q.all())

    stage_names = {
        0: "не начали", 1: "видео 1 / тест 1", 2: "мотивация",
        3: "видео 2 / тест 2", 4: "тесты пройдены", 5: "на кейсах", 6: "финал"
    }

    text = f"<b>📊 Статистика</b>\n\nВсего кандидатов: <b>{total}</b>\n\n<b>По этапам:</b>\n"
    for stage, count in sorted(by_stage.items()):
        text += f"  {stage_names.get(stage, '?')}: <b>{count}</b>\n"

    text += "\n<b>По статусам:</b>\n"
    for status, count in by_status.items():
        text += f"  {status}: <b>{count}</b>\n"

    await message.answer(text)


# ============================================================
# /export — выгрузка CSV
# ============================================================
@router.message(Command("export"))
async def cmd_export(message: Message):
    if not is_hr(message.from_user.id):
        return
    async with get_session() as s:
        result = await s.execute(select(Candidate))
        candidates = result.scalars().all()
        tr_result = await s.execute(select(TestResult))
        all_tr = tr_result.scalars().all()

    tr_by_cid: dict[int, dict[int, float]] = {}
    for r in all_tr:
        tr_by_cid.setdefault(r.candidate_id, {})[r.test_number] = r.score_percent

    from data.slots import slot_label as _slot_label

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ID", "ФИО", "Telegram ID", "Username", "Телефон", "Источник",
        "Этап", "Статус",
        "Тест 1 %", "Тест 2 %",
        "Мотивация",
        "Слот собеседования",
        "Добавлен", "Последняя активность"
    ])
    for c in candidates:
        scores = tr_by_cid.get(c.id, {})
        writer.writerow([
            c.id, c.full_name, c.telegram_id, c.username or "", c.phone or "",
            source_label(c.source) if c.source else "",
            c.stage, c.status,
            scores.get(1, ""), scores.get(2, ""),
            (c.motivation_answer or "").replace("\n", " ")[:300],
            _slot_label(c.interview_slot) if c.interview_slot else "",
            c.added_at.strftime("%Y-%m-%d %H:%M"),
            c.last_activity_at.strftime("%Y-%m-%d %H:%M"),
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM для Excel
    filename = f"candidates_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename=filename),
        caption=f"📊 Выгрузка кандидатов: {len(candidates)} шт."
    )


# ============================================================
# /slots — расписание группового собеседования
# ============================================================
@router.message(Command("slots"))
async def cmd_slots(message: Message):
    """Показывает, кто на какой слот записался + список «не подходит»."""
    if not is_hr(message.from_user.id):
        return

    from data.slots import INTERVIEW_SLOTS

    async with get_session() as s:
        result = await s.execute(
            select(Candidate).where(Candidate.interview_slot.is_not(None))
        )
        candidates = result.scalars().all()

    # Группируем по слотам
    by_slot: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_slot.setdefault(c.interview_slot, []).append(c)

    text = "<b>📅 Расписание собеседований</b>\n\n"

    # Сначала фиксированные слоты по порядку
    for key, slot in INTERVIEW_SLOTS.items():
        people = by_slot.get(key, [])
        text += f"<b>{slot['label']}</b> — {len(people)} чел.\n"
        for c in people:
            text += f"  • #{c.id} {c.full_name}"
            if c.username:
                text += f" (@{c.username})"
            if c.phone:
                text += f" — {c.phone}"
            text += "\n"
        text += "\n"

    # Потом — те, кому не подошло время
    no_match = by_slot.get("no_match", [])
    if no_match:
        text += f"<b>⚠️ Требуют индивидуального подбора времени</b> — {len(no_match)} чел.\n"
        for c in no_match:
            text += f"  • #{c.id} {c.full_name}"
            if c.username:
                text += f" (@{c.username})"
            if c.phone:
                text += f" — <b>{c.phone}</b>"
            text += "\n"

    if not candidates:
        text += "<i>Никто ещё не выбрал время.</i>"

    await message.answer(text)


# ============================================================
# /delete ID — удалить кандидата
# ============================================================
@router.message(Command("delete"))
async def cmd_delete(message: Message):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /delete &lt;ID&gt;")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return
        name = c.full_name

    await message.answer(
        f"⚠️ Удалить кандидата <b>{name}</b> (#{cid})?\n\n"
        f"Это действие <b>необратимо</b>. Будут удалены:\n"
        f"• Карточка кандидата\n"
        f"• Все результаты тестов\n"
        f"• Все активные сессии тестов\n"
        f"• Ссылка-приглашение перестанет работать",
        reply_markup=kb_confirm_delete(cid),
    )


@router.callback_query(F.data.startswith("del_yes_"))
async def confirm_delete(callback: CallbackQuery):
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("del_yes_"):])

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат уже удалён.", show_alert=True)
            return
        name = c.full_name
        # Удаляем — cascade сам удалит test_results и test_sessions
        await s.delete(c)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(f"🗑 Кандидат <b>{name}</b> (#{cid}) удалён.")
    await callback.answer()


@router.callback_query(F.data == "del_no")
async def cancel_delete(callback: CallbackQuery):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Удаление отменено.")
    await callback.answer()


# ============================================================
# /retry ID NUM — разрешить пересдачу теста
# ============================================================
@router.message(Command("retry"))
async def cmd_retry(message: Message, bot: Bot):
    if not is_hr(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование: /retry &lt;ID&gt; &lt;номер_теста&gt;\n\n"
            "Например: /retry 5 1 — разрешить кандидату #5 пересдать тест 1"
        )
        return
    try:
        cid = int(parts[1])
        test_num = int(parts[2])
    except ValueError:
        await message.answer("ID и номер теста должны быть числами.")
        return
    if test_num not in (1, 2):
        await message.answer("Номер теста — только 1 или 2.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await message.answer("Кандидат не найден.")
            return

        # Удаляем результат и активные сессии этого теста
        await s.execute(
            delete(TestResult).where(
                TestResult.candidate_id == cid,
                TestResult.test_number == test_num,
            )
        )
        await s.execute(
            delete(TestSession).where(
                TestSession.candidate_id == cid,
                TestSession.test_number == test_num,
            )
        )

        # Возвращаем кандидата к нужному этапу
        if test_num == 1:
            c.stage = 1
            c.awaiting = "video_1_watched"
        else:
            c.stage = 3
            c.awaiting = "video_2_watched"
        c.status = "active"
        c.last_activity_at = datetime.utcnow()
        c.reminder_count = 0
        tg_id = c.telegram_id
        name = c.full_name

    await message.answer(
        f"✅ Кандидат <b>{name}</b> (#{cid}) может пересдать <b>Тест {test_num}</b>.\n\n"
        f"Я только что отправил ему сообщение со ссылкой на пересдачу."
    )

    # Уведомляем кандидата
    if tg_id:
        try:
            await bot.send_message(
                tg_id,
                f"📝 <b>Возможность пересдачи</b>\n\n"
                f"Рекрутер открыл вам возможность пересдать <b>Тест {test_num}</b>.\n\n"
                f"Нажмите /start, чтобы продолжить."
            )
        except Exception as e:
            await message.answer(f"⚠️ Не удалось уведомить кандидата: {e}")


# ============================================================
# /bulk_add — массовое добавление кандидатов
# ============================================================
@router.message(Command("bulk_add"))
async def bulk_add_start(message: Message, state: FSMContext):
    if not is_hr(message.from_user.id):
        return
    await state.set_state(BulkAdd.waiting_list)
    await message.answer(
        "<b>📋 Массовое добавление кандидатов</b>\n\n"
        "Пришлите список кандидатов одним сообщением. Каждый кандидат — одна строка в формате:\n\n"
        "<code>ФИО | телефон | username</code>\n\n"
        "Где username и телефон необязательны (можно поставить -).\n\n"
        "<b>Пример:</b>\n"
        "<code>Иванов Иван | +375291234567 | ivanov\n"
        "Петрова Анна | +375291112233 | -\n"
        "Сидоров Пётр | - | sidorov</code>\n\n"
        "Или /cancel для отмены."
    )


@router.message(BulkAdd.waiting_list)
async def bulk_add_list(message: Message, state: FSMContext):
    lines = [line.strip() for line in message.text.strip().split("\n") if line.strip()]
    if not lines:
        await message.answer("Пустой список. Пришлите хотя бы одного кандидата.")
        return

    parsed = []
    errors = []
    for i, line in enumerate(lines, 1):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 1 or not parts[0]:
            errors.append(f"Строка {i}: нет ФИО")
            continue
        full_name = parts[0]
        if len(full_name) < 3:
            errors.append(f"Строка {i}: ФИО слишком короткое")
            continue
        phone = parts[1] if len(parts) > 1 and parts[1] not in ("-", "") else None
        username = parts[2].lstrip("@") if len(parts) > 2 and parts[2] not in ("-", "") else None
        parsed.append({"full_name": full_name, "phone": phone, "username": username})

    if errors:
        await message.answer(
            "⚠️ Ошибки в списке:\n" + "\n".join(errors) +
            f"\n\nИсправьте и пришлите заново, или /cancel."
        )
        return

    await state.update_data(candidates=parsed)
    await state.set_state(BulkAdd.waiting_source)
    await message.answer(
        f"📦 Распознано: <b>{len(parsed)} кандидатов</b>\n\n"
        f"Все они придут из одного источника. Выберите его:",
        reply_markup=kb_sources(),
    )


@router.callback_query(BulkAdd.waiting_source, F.data.startswith("src_"))
async def bulk_add_source(callback: CallbackQuery, state: FSMContext, bot: Bot):
    source = callback.data[len("src_"):]
    data = await state.get_data()
    candidates_data = data.get("candidates", [])

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    bot_info = await bot.get_me()
    bot_username = bot_info.username

    added = []
    async with get_session() as s:
        for cdata in candidates_data:
            # Уникальный код для каждого
            invite_code = _gen_invite_code()
            for _ in range(5):
                check = await s.execute(select(Candidate).where(Candidate.invite_code == invite_code))
                if check.scalar_one_or_none() is None:
                    break
                invite_code = _gen_invite_code()

            candidate = Candidate(
                telegram_id=None,
                full_name=cdata["full_name"],
                username=cdata.get("username"),
                phone=cdata.get("phone"),
                invite_code=invite_code,
                source=source,
                stage=0,
                status="active",
                added_by=callback.from_user.id,
                awaiting="invite_pending",
            )
            s.add(candidate)
            await s.flush()
            added.append({
                "id": candidate.id,
                "name": cdata["full_name"],
                "phone": cdata.get("phone"),
                "link": f"https://t.me/{bot_username}?start={invite_code}",
            })

    await state.clear()

    # Шлём результат частями (по 10 кандидатов на сообщение)
    await callback.message.answer(
        f"✅ Добавлено <b>{len(added)}</b> кандидатов "
        f"(источник: <b>{source_label(source)}</b>).\n\n"
        f"Ссылки-приглашения 👇"
    )

    chunk_size = 10
    for i in range(0, len(added), chunk_size):
        chunk = added[i:i + chunk_size]
        text_parts = []
        for a in chunk:
            phone_str = f"📱 {a['phone']}" if a['phone'] else ""
            text_parts.append(
                f"<b>#{a['id']} {a['name']}</b> {phone_str}\n"
                f"<code>{a['link']}</code>"
            )
        await callback.message.answer("\n\n".join(text_parts))

    await callback.answer()


# ============================================================
# /analytics — графики воронки
# ============================================================
@router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    if not is_hr(message.from_user.id):
        return

    # Подгружаем тяжёлые библиотеки только при вызове команды
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO

    async with get_session() as s:
        # Все кандидаты
        r_all = await s.execute(select(Candidate))
        all_candidates = r_all.scalars().all()
        # Результаты тестов
        r_tr = await s.execute(select(TestResult))
        results = r_tr.scalars().all()

    if not all_candidates:
        await message.answer("Пока нет кандидатов для аналитики.")
        return

    total = len(all_candidates)

    # ---------- Воронка ----------
    # Считаем сколько кандидатов прошли каждый этап
    activated = sum(1 for c in all_candidates if c.telegram_id is not None)
    started = sum(1 for c in all_candidates if c.stage >= 1)
    test1_passed = sum(1 for r in results if r.test_number == 1 and r.passed)
    motivation = sum(1 for c in all_candidates if c.motivation_answer)
    test2_passed = sum(1 for r in results if r.test_number == 2 and r.passed)
    slot_picked = sum(1 for c in all_candidates if c.interview_slot and c.interview_slot != "no_match")
    offer = sum(1 for c in all_candidates if c.status == "offer_sent")

    stages = [
        ("Добавлены", total),
        ("Активировали", activated),
        ("Прошли тест 1", test1_passed),
        ("Мотивация", motivation),
        ("Прошли тест 2", test2_passed),
        ("Выбрали слот", slot_picked),
        ("Оффер", offer),
    ]

    # ---------- График 1: воронка ----------
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    plt.rcParams['font.family'] = ['DejaVu Sans']

    labels = [s[0] for s in stages]
    values = [s[1] for s in stages]
    colors_funnel = ['#4A90E2', '#5BA0F2', '#6BB0FF', '#7AC0FF', '#8AD0FF', '#99D9F5', '#A8E5C0']

    bars = axes[0].barh(labels, values, color=colors_funnel[:len(stages)], edgecolor='white')
    axes[0].invert_yaxis()
    axes[0].set_title("Воронка найма", fontsize=14, fontweight='bold', pad=15)
    axes[0].set_xlabel("Кандидатов")
    axes[0].set_xlim(0, max(max(values), 1) * 1.15)
    # Подписи с количеством и %
    for i, (bar, val) in enumerate(zip(bars, values)):
        pct = (val / total * 100) if total else 0
        axes[0].text(bar.get_width() + max(values) * 0.01, bar.get_y() + bar.get_height() / 2,
                     f"{val} ({pct:.0f}%)", va='center', fontsize=10)
    axes[0].spines['top'].set_visible(False)
    axes[0].spines['right'].set_visible(False)
    axes[0].grid(axis='x', alpha=0.3)

    # ---------- График 2: распределение по статусам ----------
    statuses = {}
    for c in all_candidates:
        statuses[c.status] = statuses.get(c.status, 0) + 1

    status_labels_ru = {
        "active": "В процессе",
        "passed": "Прошли тесты",
        "failed": "Провалили",
        "on_pause": "На паузе",
        "no_response": "Не отвечают",
        "offer_sent": "Оффер",
        "rejected": "Отказано",
    }
    status_colors = {
        "active": "#4A90E2",
        "passed": "#7ED321",
        "failed": "#D0021B",
        "on_pause": "#F5A623",
        "no_response": "#9B9B9B",
        "offer_sent": "#50C878",
        "rejected": "#8B0000",
    }

    labels2 = [status_labels_ru.get(s, s) for s in statuses.keys()]
    sizes2 = list(statuses.values())
    colors2 = [status_colors.get(s, "#CCCCCC") for s in statuses.keys()]

    axes[1].pie(sizes2, labels=labels2, colors=colors2, autopct='%1.0f%%',
                startangle=90, textprops={'fontsize': 10},
                wedgeprops={'edgecolor': 'white', 'linewidth': 2})
    axes[1].set_title("Распределение по статусам", fontsize=14, fontweight='bold', pad=15)

    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=110, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)

    # ---------- График 3: источники + средние баллы ----------
    fig2, axes2 = plt.subplots(1, 2, figsize=(14, 5))

    # Источники
    src_counts = {}
    for c in all_candidates:
        key = c.source or "unknown"
        src_counts[key] = src_counts.get(key, 0) + 1

    if src_counts:
        src_items = sorted(src_counts.items(), key=lambda x: x[1], reverse=True)
        src_labels = [source_label(k) if k != "unknown" else "Не указан" for k, _ in src_items]
        src_values = [v for _, v in src_items]
        axes2[0].bar(src_labels, src_values, color='#4A90E2', edgecolor='white')
        axes2[0].set_title("Источники кандидатов", fontsize=14, fontweight='bold', pad=15)
        axes2[0].set_ylabel("Кандидатов")
        plt.setp(axes2[0].get_xticklabels(), rotation=30, ha='right')
        axes2[0].spines['top'].set_visible(False)
        axes2[0].spines['right'].set_visible(False)
        axes2[0].grid(axis='y', alpha=0.3)
        for i, v in enumerate(src_values):
            axes2[0].text(i, v + 0.1, str(v), ha='center', fontsize=10, fontweight='bold')

    # Средние баллы
    test1_scores = [r.score_percent for r in results if r.test_number == 1]
    test2_scores = [r.score_percent for r in results if r.test_number == 2]
    avg_t1 = sum(test1_scores) / len(test1_scores) if test1_scores else 0
    avg_t2 = sum(test2_scores) / len(test2_scores) if test2_scores else 0

    axes2[1].bar(["Тест 1", "Тест 2"], [avg_t1, avg_t2],
                 color=['#4A90E2', '#7ED321'], edgecolor='white')
    axes2[1].set_title("Средние результаты тестов", fontsize=14, fontweight='bold', pad=15)
    axes2[1].set_ylabel("%")
    axes2[1].set_ylim(0, 100)
    axes2[1].axhline(y=85, color='#D0021B', linestyle='--', alpha=0.5, label='Порог Т1 (85%)')
    axes2[1].axhline(y=75, color='#F5A623', linestyle='--', alpha=0.5, label='Порог Т2 (75%)')
    axes2[1].legend(loc='lower right', fontsize=9)
    axes2[1].spines['top'].set_visible(False)
    axes2[1].spines['right'].set_visible(False)
    axes2[1].grid(axis='y', alpha=0.3)
    for i, v in enumerate([avg_t1, avg_t2]):
        axes2[1].text(i, v + 2, f"{v:.1f}%", ha='center', fontsize=11, fontweight='bold')

    plt.tight_layout()
    buf2 = BytesIO()
    plt.savefig(buf2, format='png', dpi=110, bbox_inches='tight', facecolor='white')
    plt.close(fig2)
    buf2.seek(0)

    # Конверсии
    conv_text = "<b>📊 Сводная аналитика</b>\n\n"
    conv_text += f"Всего кандидатов: <b>{total}</b>\n"
    if total:
        conv_text += f"Конверсия активации: <b>{activated / total * 100:.0f}%</b> ({activated}/{total})\n"
    if activated:
        conv_text += f"Дошли до теста 1 → прошли: <b>{test1_passed / activated * 100:.0f}%</b>\n"
    if test1_passed:
        conv_text += f"Тест 1 → Тест 2 (прошли): <b>{test2_passed / test1_passed * 100:.0f}%</b>\n"
    if test2_passed:
        conv_text += f"Прошли тесты → выбрали слот: <b>{slot_picked / test2_passed * 100:.0f}%</b>\n"
    if total:
        conv_text += f"\n<b>Итоговая конверсия в оффер: {offer / total * 100:.1f}%</b>"

    # Отправляем картинки
    await message.answer_photo(
        BufferedInputFile(buf.read(), filename="funnel.png"),
        caption=conv_text,
    )
    await message.answer_photo(
        BufferedInputFile(buf2.read(), filename="sources.png"),
        caption="📈 Источники и средние результаты тестов",
    )


# ============================================================
# ОБРАБОТЧИКИ REPLY-КНОПОК (постоянное меню)
# ============================================================
# Кнопки шлют боту обычный текст — ловим его и вызываем нужный handler.
# StateFilter(None) важен: внутри FSM (например, AddCandidate.waiting_full_name)
# мы НЕ хотим, чтобы текст "📋 Все кандидаты" интерпретировался как ФИО.
# Когда FSM не пустой — пользователь сам должен выйти через /cancel.


@router.message(StateFilter(None), F.text == BTN_ADD, F.from_user.id.func(is_hr))
async def btn_add(message: Message, state: FSMContext):
    await add_start(message, state)


@router.message(StateFilter(None), F.text == BTN_LIST, F.from_user.id.func(is_hr))
async def btn_list(message: Message):
    await cmd_list(message)


@router.message(StateFilter(None), F.text == BTN_ACTIVE, F.from_user.id.func(is_hr))
async def btn_active(message: Message):
    await cmd_active(message)


@router.message(StateFilter(None), F.text == BTN_SLOTS, F.from_user.id.func(is_hr))
async def btn_slots(message: Message):
    await cmd_slots(message)


@router.message(StateFilter(None), F.text == BTN_STATS, F.from_user.id.func(is_hr))
async def btn_stats(message: Message):
    await cmd_stats(message)


@router.message(StateFilter(None), F.text == BTN_ANALYTICS, F.from_user.id.func(is_hr))
async def btn_analytics(message: Message):
    await cmd_analytics(message)


@router.message(StateFilter(None), F.text == BTN_EXPORT, F.from_user.id.func(is_hr))
async def btn_export(message: Message):
    await cmd_export(message)


@router.message(StateFilter(None), F.text == BTN_HELP, F.from_user.id.func(is_hr))
async def btn_help(message: Message):
    await cmd_help(message)


# ============================================================
# INLINE-КНОПКИ ДЕЙСТВИЙ НАД КАНДИДАТОМ
# ============================================================
# Под уведомлениями о кандидате — чтобы рекрутер мог тапнуть
# «Кейсы / Оффер / Отказ / Карточка / Пауза» без ввода ID руками.


@router.callback_query(F.data.startswith("hr_cases_"))
async def cb_hr_cases(callback: CallbackQuery):
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("hr_cases_"):])

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return
        c.stage = 5
        full_name, username, phone = c.full_name, c.username, c.phone

    await callback.message.answer(
        CASES_TEMPLATE.format(
            name=full_name,
            username=username or "—",
            phone=phone or "—",
        ),
    )
    await callback.answer("Кейсы выданы")


@router.callback_query(F.data.startswith("hr_card_"))
async def cb_hr_card(callback: CallbackQuery):
    """Показать карточку кандидата + inline-действия под ней."""
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("hr_card_"):])

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return
        tr_result = await s.execute(
            select(TestResult).where(TestResult.candidate_id == cid).order_by(TestResult.test_number)
        )
        results = tr_result.scalars().all()

    stage_names = {
        0: "не начал", 1: "видео 1 / тест 1", 2: "мотивация",
        3: "видео 2 / тест 2", 4: "тесты пройдены", 5: "на кейсах", 6: "финал"
    }

    text = (
        f"<b>👤 Кандидат #{c.id}</b>\n\n"
        f"ФИО: <b>{c.full_name}</b>\n"
        f"Telegram ID: <code>{c.telegram_id}</code>\n"
        f"Username: @{c.username or '—'}\n"
        f"Телефон: {c.phone or '—'}\n"
        f"Источник: {source_label(c.source)}\n\n"
        f"Этап: {stage_names.get(c.stage, '?')}\n"
        f"Статус: <b>{c.status}</b>\n"
        f"Добавлен: {c.added_at.strftime('%d.%m.%Y %H:%M')}\n"
    )
    if results:
        text += "\n<b>Результаты тестов:</b>\n"
        for tr in results:
            mark = "✅" if tr.passed else "❌"
            text += f"  {mark} Тест {tr.test_number}: <b>{tr.score_percent}%</b>\n"
    if c.motivation_answer:
        text += f"\n<b>💬 Мотивация:</b>\n<i>{c.motivation_answer[:500]}</i>"
    if c.interview_slot:
        from data.slots import slot_label
        text += f"\n\n<b>📅 Слот:</b> {slot_label(c.interview_slot)}"

    await callback.message.answer(text, reply_markup=kb_candidate_actions(cid))
    await callback.answer()


async def _change_status_with_notify(
    bot: Bot,
    cid: int,
    new_status: str,
    notify_kandidat_text: str | None,
) -> tuple[bool, str]:
    """Меняет статус кандидата + при необходимости пишет ему. Возвращает (ok, msg)."""
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c:
            return False, "Кандидат не найден."
        old_status = c.status
        c.status = new_status
        if new_status == "offer_sent":
            c.stage = 6
        tg_id = c.telegram_id
        full_name = c.full_name

    msg = f"✅ {full_name} (#{cid}): {old_status} → <b>{new_status}</b>"

    if notify_kandidat_text and tg_id:
        try:
            await bot.send_message(tg_id, notify_kandidat_text)
            msg += "\n✉️ Кандидату отправлено уведомление."
        except Exception as e:
            msg += f"\n⚠️ Не удалось уведомить кандидата: {e}"
    return True, msg


@router.callback_query(F.data.startswith("hr_offer_"))
async def cb_hr_offer(callback: CallbackQuery, bot: Bot):
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("hr_offer_"):])
    ok, msg = await _change_status_with_notify(
        bot, cid, "offer_sent",
        notify_kandidat_text=(
            "🎉 <b>Поздравляем!</b>\n\n"
            "По итогам собеседования мы готовы сделать вам оффер. "
            "С вами свяжется рекрутер в ближайшее время для обсуждения деталей."
        ),
    )
    await callback.message.answer(msg)
    await callback.answer("Оффер" if ok else "Ошибка", show_alert=not ok)


@router.callback_query(F.data.startswith("hr_reject_"))
async def cb_hr_reject(callback: CallbackQuery, bot: Bot):
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("hr_reject_"):])
    ok, msg = await _change_status_with_notify(
        bot, cid, "rejected",
        notify_kandidat_text=(
            "Благодарим вас за участие в собеседовании. "
            "К сожалению, мы приняли решение не продолжать с вами процесс на этом этапе. "
            "Желаем удачи в дальнейших поисках!"
        ),
    )
    await callback.message.answer(msg)
    await callback.answer("Отказ отправлен" if ok else "Ошибка", show_alert=not ok)


@router.callback_query(F.data.startswith("hr_pause_"))
async def cb_hr_pause(callback: CallbackQuery, bot: Bot):
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("hr_pause_"):])
    ok, msg = await _change_status_with_notify(
        bot, cid, "on_pause", notify_kandidat_text=None,
    )
    await callback.message.answer(msg)
    await callback.answer("На паузе" if ok else "Ошибка", show_alert=not ok)

# ============================================================
# КНОПКА «🗑 Удалить кандидата» С ВЫБОРОМ ИЗ СПИСКА
# ============================================================
# Поток: рекрутер тапает «🗑 Удалить кандидата» → бот показывает список
# активных кандидатов с inline-кнопками (по одной на каждого, с ФИО и ID).
# Тап по кандидату → стандартное подтверждение «Точно удалить?» (используем
# kb_confirm_delete и существующие confirm_delete / cancel_delete).

DELETE_LIST_LIMIT = 30  # максимум кандидатов на одном экране выбора


def kb_delete_pick(candidates: list[Candidate]) -> InlineKeyboardMarkup:
    """Inline-клавиатура: по одной строке на кандидата для выбора удаления."""
    rows = []
    for c in candidates:
        # Маркер статуса перед именем — чтобы рекрутер видел, кого опасно удалять
        status_mark = {
            "active": "🔥",
            "passed": "✅",
            "failed": "❌",
            "rejected": "🚫",
            "offer_sent": "💼",
            "on_pause": "⏸",
            "no_response": "📵",
        }.get(c.status, "•")
        label = f"{status_mark} #{c.id} {c.full_name[:35]}"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"del_pick_{c.id}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="del_pick_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(StateFilter(None), F.text == BTN_DELETE, F.from_user.id.func(is_hr))
async def btn_delete(message: Message):
    """Показать список кандидатов для выбора удаления."""
    async with get_session() as s:
        # Сначала активные/недавние, потом архивные. Сортируем по убыванию ID
        # (самые новые сверху — обычно их и хотят удалить, если ошиблись при /add).
        r = await s.execute(
            select(Candidate)
            .order_by(Candidate.id.desc())
            .limit(DELETE_LIST_LIMIT)
        )
        candidates = r.scalars().all()

    if not candidates:
        await message.answer("В базе нет кандидатов — удалять нечего.")
        return

    # Считаем сколько всего в базе — чтобы предупредить о лимите экрана
    async with get_session() as s:
        r = await s.execute(select(func.count(Candidate.id)))
        total = r.scalar() or 0

    text = "<b>🗑 Кого удалить?</b>\n\nТапните по кандидату, чтобы выбрать его для удаления."
    if total > DELETE_LIST_LIMIT:
        text += (
            f"\n\n<i>Показаны последние {DELETE_LIST_LIMIT} из {total} кандидатов. "
            f"Если нужного нет в списке — используйте команду /delete ID.</i>"
        )

    await message.answer(text, reply_markup=kb_delete_pick(candidates))


@router.callback_query(F.data == "del_pick_cancel")
async def cb_delete_pick_cancel(callback: CallbackQuery):
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Удаление отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("del_pick_"))
async def cb_delete_pick(callback: CallbackQuery):
    """Клик по кандидату в списке — показать подтверждение."""
    if not is_hr(callback.from_user.id):
        await callback.answer()
        return
    cid = int(callback.data[len("del_pick_"):])

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат уже удалён.", show_alert=True)
            return
        name = c.full_name
        status = c.status
        stage = c.stage

    # Убираем кнопки списка из старого сообщения, чтобы не нажали повторно
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    stage_names = {
        0: "не начал", 1: "видео 1 / тест 1", 2: "мотивация",
        3: "видео 2 / тест 2", 4: "тесты пройдены", 5: "на кейсах", 6: "финал"
    }
    await callback.message.answer(
        f"⚠️ Удалить кандидата <b>{name}</b> (#{cid})?\n\n"
        f"Этап: {stage_names.get(stage, '?')}\n"
        f"Статус: <b>{status}</b>\n\n"
        f"Это действие <b>необратимо</b>. Будут удалены:\n"
        f"• Карточка кандидата\n"
        f"• Все результаты тестов\n"
        f"• Все активные сессии тестов\n"
        f"• Ссылка-приглашение перестанет работать",
        reply_markup=kb_confirm_delete(cid),
    )
    await callback.answer()
