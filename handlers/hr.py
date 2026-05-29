"""Хендлеры для рекрутера — админ-команды."""
import csv
import io
import secrets
import os
import json
import html
from datetime import datetime

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, BufferedInputFile, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove,
)
from sqlalchemy import select, func, delete

from config import is_hr
from db.database import get_session
from db.models import Candidate, TestResult, TestSession
from data.texts import CASES_TEMPLATE, HR_NEW_CANDIDATE_ADDED
from data.sources import SOURCES, source_label
from data.questions import get_questions
from data.keyboards import (
    kb_main_menu, kb_more_menu,
    kb_candidates_filters, kb_candidate_list, kb_candidate_actions,
    kb_status_choice, kb_confirm_retry, kb_candidate_test_results, kb_back_to_candidate,
)
from data.keyboards import kb_confirm_delete as kb_confirm_delete_new

router = Router()
# Router-level filter — этот роутер работает только для HR.
# Не-HR сообщения пройдут дальше к candidate.router.
router.message.filter(lambda m: is_hr(m.from_user.id))
router.callback_query.filter(lambda c: is_hr(c.from_user.id))


# ============================================================
# FSM ДЛЯ ДОБАВЛЕНИЯ КАНДИДАТА
# ============================================================
class AddCandidate(StatesGroup):
    waiting_full_name = State()
    waiting_phone = State()
    waiting_username = State()
    waiting_source = State()
    waiting_position = State()


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


# Используем kb_confirm_delete_new из data.keyboards для удаления
kb_confirm_delete = kb_confirm_delete_new


def _get_questions_for_test(test_number: int) -> list[dict]:
    """Возвращает вопросы для обычных тестов и тестов воронки продажника."""
    if test_number in (1, 2):
        return get_questions(test_number)

    # Импорт внутри функции, чтобы не тянуть sales_flow при старте обычной воронки.
    from data.sales_flow import TEST_SALES_VIDEO, INTERNSHIP_DAYS

    if test_number == 10:
        return TEST_SALES_VIDEO

    if 101 <= test_number <= 105:
        day = test_number - 100
        day_data = INTERNSHIP_DAYS.get(day)
        if day_data and day_data.get("has_test"):
            return day_data.get("test_questions", [])

    return []


def _format_options(question: dict, indexes: list[int]) -> str:
    """Форматирует выбранные/правильные варианты ответа."""
    if not indexes:
        return "—"

    options = question.get("options", [])
    lines = []
    for idx in indexes:
        try:
            idx_int = int(idx)
        except Exception:
            lines.append(f"• {html.escape(str(idx))}")
            continue

        if 0 <= idx_int < len(options):
            lines.append(f"• {html.escape(str(options[idx_int]))}")
        else:
            lines.append(f"• вариант #{idx_int + 1}")

    return "\n".join(lines) if lines else "—"


def _split_long_text(text: str, limit: int = 3800) -> list[str]:
    """Делит длинный HTML-текст на части, чтобы Telegram не отрезал сообщение."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for block in text.split("\n\n"):
        block_len = len(block) + 2
        if current and current_len + block_len > limit:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = block_len
        else:
            current.append(block)
            current_len += block_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def _build_test_details_text(candidate: Candidate, result: TestResult, answers: list, questions: list[dict]) -> str:
    """Собирает расшифровку ответов кандидата по тесту."""
    mark = "✅" if result.passed else "❌"
    text = (
        f"<b>🧪 Ответы на тест кандидата</b>\n\n"
        f"👤 <b>{html.escape(candidate.full_name)}</b> (#{candidate.id})\n"
        f"{mark} Тест <b>{result.test_number}</b>: <b>{result.score_percent}%</b>\n"
        f"Дата прохождения: {result.completed_at.strftime('%d.%m.%Y %H:%M')}\n\n"
    )

    if not questions:
        return text + "⚠️ Для этого номера теста не найдены вопросы в коде."

    if not answers:
        text += (
            "⚠️ Подробные ответы не найдены. Такое может быть по старым прохождениям, "
            "если сессия теста была удалена или тест открывали на пересдачу."
        )
        return text

    for i, question in enumerate(questions):
        candidate_answer = answers[i] if i < len(answers) else []
        correct_answer = question.get("correct", [])
        is_correct = sorted(candidate_answer) == sorted(correct_answer)
        q_mark = "✅" if is_correct else "❌"

        text += (
            f"{q_mark} <b>{i + 1}. {html.escape(str(question.get('text', 'Вопрос')))}</b>\n"
            f"<b>Ответ кандидата:</b>\n{_format_options(question, candidate_answer)}\n"
            f"<b>Правильный ответ:</b>\n{_format_options(question, correct_answer)}\n\n"
        )

    return text.strip()


# ============================================================
# /start — для HR показываем главное меню
# (для обычных пользователей /start обрабатывается в candidate.py — этот хендлер срабатывает только для HR)
# ============================================================
@router.message(Command("start"))
async def hr_start(message: Message):
    if not is_hr(message.from_user.id):
        return  # пропускаем дальше — обработает candidate.py
    await message.answer(
        "👋 <b>Добро пожаловать в Mavis HR Bot!</b>\n\n"
        "Используйте кнопки внизу для управления воронкой найма.\n\n"
        "💡 <i>Также работают текстовые команды — введите /help для списка.</i>",
        reply_markup=kb_main_menu(),
    )


@router.message(Command("menu"))
async def hr_menu(message: Message):
    if not is_hr(message.from_user.id):
        return
    await message.answer(
        "📋 <b>Главное меню</b>",
        reply_markup=kb_main_menu(),
    )


# ============================================================
# /help — список команд для HR
# ============================================================
@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_hr(message.from_user.id):
        return
    text = (
        "<b>🛠 Команды рекрутера</b>\n\n"
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
        "/myid — мой Telegram ID\n"
        "/cancel — прервать текущую операцию"
    )
    await message.answer(text)


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
async def add_source(callback: CallbackQuery, state: FSMContext):
    source = callback.data[len("src_"):]
    await state.update_data(source=source)
    await state.set_state(AddCandidate.waiting_position)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        "🎯 На какую позицию кандидат?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Специалист по сопровождению", callback_data="pos_support")],
            [InlineKeyboardButton(text="💼 Менеджер по продажам", callback_data="pos_sales")],
        ])
    )
    await callback.answer()


@router.callback_query(AddCandidate.waiting_position, F.data.startswith("pos_"))
async def add_position(callback: CallbackQuery, state: FSMContext, bot: Bot):
    position = callback.data[len("pos_"):]
    data = await state.get_data()

    invite_code = _gen_invite_code()

    async with get_session() as s:
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
            source=data.get("source"),
            position=position,
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

    from data.sales_flow import POSITION_LABELS
    pos_label = POSITION_LABELS.get(position, position)
    bot_info = await bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={invite_code}"

    await callback.message.answer(
        f"✅ Кандидат <b>{data['full_name']}</b> добавлен (#{cid}).\n"
        f"Источник: <b>{source_label(data.get('source', ''))}</b>\n"
        f"Позиция: <b>{pos_label}</b>\n\n"
        f"<b>📨 Ссылка-приглашение:</b>\n"
        f"<code>{invite_link}</code>\n\n"
        f"<b>Отправьте эту ссылку кандидату</b> любым удобным способом:\n"
        f"• Telegram (если знаете username)\n"
        f"• WhatsApp / Viber\n"
        f"• SMS на номер {data.get('phone') or '—'}\n"
        f"• E-mail\n\n"
        f"Когда кандидат кликнет ссылку — бот сразу начнёт воронку и пришлёт вам уведомление."
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

    has_test_1 = any(r.test_number == 1 for r in results)
    has_test_2 = any(r.test_number == 2 for r in results)
    has_telegram = c.telegram_id is not None

    await message.answer(
        text,
        reply_markup=kb_candidate_actions(
            cid, has_telegram, c.stage, c.status, has_test_1, has_test_2,
            test_numbers=[r.test_number for r in results],
        ),
    )


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
# ОБРАБОТЧИКИ КНОПОК ГЛАВНОГО МЕНЮ (Reply Keyboard)
# ============================================================
@router.message(F.text == "➕ Добавить кандидата")
async def menu_add_candidate(message: Message, state: FSMContext):
    """Запускает FSM добавления — тот же что и /add."""
    await add_start(message, state)


@router.message(F.text == "📋 Кандидаты")
async def menu_candidates_list(message: Message):
    """Показывает фильтры списка кандидатов."""
    await message.answer(
        "📋 <b>Кандидаты</b>\n\nВыберите фильтр:",
        reply_markup=kb_candidates_filters(),
    )


@router.message(F.text == "🔍 Поиск")
async def menu_search(message: Message, state: FSMContext):
    """Запускает поиск кандидата по ФИО."""
    await state.set_state(SearchCandidate.waiting_query)
    await message.answer(
        "🔍 <b>Поиск кандидата</b>\n\n"
        "Введите часть ФИО (минимум 2 символа):\n\n"
        "<i>Например: «Иван», «Петров», «Сидорова Анна»</i>"
    )


@router.message(F.text == "📅 Расписание")
async def menu_slots(message: Message):
    """Показывает расписание собеседований."""
    await cmd_slots(message)


@router.message(F.text == "📊 Аналитика")
async def menu_analytics(message: Message):
    """Аналитика с графиками."""
    await message.answer("📊 Готовлю графики...")
    await cmd_analytics(message)


@router.message(F.text == "📈 Статистика")
async def menu_stats(message: Message):
    """Простая статистика."""
    await cmd_stats(message)


@router.message(F.text == "📥 Экспорт CSV")
async def menu_export(message: Message):
    """Экспорт в CSV."""
    await cmd_export(message)


@router.message(F.text == "⚙️ Ещё")
async def menu_more(message: Message):
    """Доп. меню."""
    await message.answer(
        "⚙️ <b>Дополнительно</b>",
        reply_markup=kb_more_menu(),
    )


@router.callback_query(F.data == "menu_bulkadd")
async def inline_bulk_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Имитируем команду
    callback.message.from_user = callback.from_user
    await bulk_add_start(callback.message, state)


@router.callback_query(F.data == "menu_help")
async def inline_help(callback: CallbackQuery):
    await callback.answer()
    await cmd_help(callback.message)


@router.callback_query(F.data == "menu_myid")
async def inline_myid(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f"Ваш Telegram ID: <code>{callback.from_user.id}</code>")


@router.callback_query(F.data == "menu_candidates")
async def back_to_filters(callback: CallbackQuery):
    """Возврат к фильтрам списка кандидатов."""
    await callback.answer()
    try:
        await callback.message.edit_text(
            "📋 <b>Кандидаты</b>\n\nВыберите фильтр:",
            reply_markup=kb_candidates_filters(),
        )
    except Exception:
        await callback.message.answer(
            "📋 <b>Кандидаты</b>\n\nВыберите фильтр:",
            reply_markup=kb_candidates_filters(),
        )


# ============================================================
# ФИЛЬТРЫ СПИСКА КАНДИДАТОВ
# ============================================================
async def _get_filtered_candidates(filter_key: str) -> list[Candidate]:
    """Возвращает отфильтрованный список кандидатов."""
    async with get_session() as s:
        if filter_key == "all":
            q = select(Candidate).order_by(Candidate.added_at.desc())
        elif filter_key == "active":
            q = select(Candidate).where(Candidate.status == "active").order_by(Candidate.added_at.desc())
        elif filter_key == "passed":
            q = select(Candidate).where(Candidate.status == "passed").order_by(Candidate.added_at.desc())
        elif filter_key == "failed":
            q = select(Candidate).where(Candidate.status == "failed").order_by(Candidate.added_at.desc())
        elif filter_key == "offer":
            q = select(Candidate).where(Candidate.status == "offer_sent").order_by(Candidate.added_at.desc())
        elif filter_key == "noresp":
            q = select(Candidate).where(Candidate.status == "no_response").order_by(Candidate.added_at.desc())
        else:
            q = select(Candidate).order_by(Candidate.added_at.desc())
        result = await s.execute(q)
        return list(result.scalars().all())


@router.callback_query(F.data.startswith("cf_"))
async def filter_candidates(callback: CallbackQuery):
    """Применить фильтр и показать список."""
    parts = callback.data.split("_")
    filter_key = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    candidates = await _get_filtered_candidates(filter_key)

    if not candidates:
        await callback.answer()
        try:
            await callback.message.edit_text(
                f"Нет кандидатов в этой категории.",
                reply_markup=kb_candidates_filters(),
            )
        except Exception:
            await callback.message.answer(
                f"Нет кандидатов в этой категории.",
                reply_markup=kb_candidates_filters(),
            )
        return

    filter_titles = {
        "all": "Все кандидаты",
        "active": "Активные",
        "passed": "Прошли тесты",
        "failed": "Провалили",
        "offer": "Оффер отправлен",
        "noresp": "Не отвечают",
    }
    title = filter_titles.get(filter_key, "Кандидаты")
    text = f"📋 <b>{title}</b> (всего: {len(candidates)})\n\nНажмите на кандидата для просмотра карточки:"

    await callback.answer()
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb_candidate_list(candidates, filter_key, page),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb_candidate_list(candidates, filter_key, page),
        )


# ============================================================
# КАРТОЧКА КАНДИДАТА — клик по кнопке
# ============================================================
@router.callback_query(F.data.startswith("cd_"))
async def show_candidate_card(callback: CallbackQuery):
    """Показывает карточку кандидата с кнопками действий."""
    cid = int(callback.data[len("cd_"):])

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
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
        f"Telegram ID: <code>{c.telegram_id or '—'}</code>\n"
        f"Username: @{c.username or '—'}\n"
        f"Телефон: {c.phone or '—'}\n"
        f"Источник: {source_label(c.source)}\n\n"
        f"Этап: {stage_names.get(c.stage, '?')}\n"
        f"Статус: <b>{c.status}</b>\n"
        f"Добавлен: {c.added_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Последняя активность: {c.last_activity_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Напоминаний отправлено: {c.reminder_count}\n\n"
    )

    has_test_1 = any(r.test_number == 1 for r in results)
    has_test_2 = any(r.test_number == 2 for r in results)

    if results:
        text += "<b>Результаты тестов:</b>\n"
        for r in results:
            mark = "✅" if r.passed else "❌"
            text += f"  {mark} Тест {r.test_number}: <b>{r.score_percent}%</b>\n"
    else:
        text += "<i>Тесты ещё не пройдены</i>\n"

    if c.motivation_answer:
        text += f"\n<b>💬 Мотивация:</b>\n<i>{c.motivation_answer[:500]}</i>"

    if c.interview_slot:
        from data.slots import slot_label
        text += f"\n\n<b>📅 Слот собеседования:</b> {slot_label(c.interview_slot)}"

    has_telegram = c.telegram_id is not None

    await callback.answer()
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb_candidate_actions(
                cid, has_telegram, c.stage, c.status, has_test_1, has_test_2,
                test_numbers=[r.test_number for r in results],
            ),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb_candidate_actions(
                cid, has_telegram, c.stage, c.status, has_test_1, has_test_2,
                test_numbers=[r.test_number for r in results],
            ),
        )


# ============================================================
# ДЕЙСТВИЯ В КАРТОЧКЕ КАНДИДАТА
# ============================================================
@router.callback_query(F.data.startswith("ca_tests_"))
async def card_show_tests_menu(callback: CallbackQuery):
    """Показать список тестов кандидата для просмотра ответов."""
    cid = int(callback.data[len("ca_tests_"):])

    async with get_session() as s:
        c_result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = c_result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return

        tr_result = await s.execute(
            select(TestResult)
            .where(TestResult.candidate_id == cid)
            .order_by(TestResult.test_number)
        )
        results = tr_result.scalars().all()

    if not results:
        await callback.answer("У кандидата пока нет завершённых тестов.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_text(
            f"🧪 <b>Ответы на тесты кандидата</b>\n\n"
            f"👤 <b>{html.escape(c.full_name)}</b> (#{cid})\n\n"
            f"Выберите тест, который нужно проверить:",
            reply_markup=kb_candidate_test_results(cid, results),
        )
    except Exception:
        await callback.message.answer(
            f"🧪 <b>Ответы на тесты кандидата</b>\n\n"
            f"👤 <b>{html.escape(c.full_name)}</b> (#{cid})\n\n"
            f"Выберите тест, который нужно проверить:",
            reply_markup=kb_candidate_test_results(cid, results),
        )


@router.callback_query(F.data.startswith("ca_test_"))
async def card_show_test_details(callback: CallbackQuery):
    """Показать подробные ответы кандидата по выбранному тесту."""
    parts = callback.data[len("ca_test_"):].split("_")
    if len(parts) != 2:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    cid = int(parts[0])
    test_num = int(parts[1])

    async with get_session() as s:
        c_result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = c_result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return

        tr_result = await s.execute(
            select(TestResult).where(
                TestResult.candidate_id == cid,
                TestResult.test_number == test_num,
            )
        )
        result = tr_result.scalar_one_or_none()
        if not result:
            await callback.answer("Результат теста не найден.", show_alert=True)
            return

        ts_result = await s.execute(
            select(TestSession)
            .where(
                TestSession.candidate_id == cid,
                TestSession.test_number == test_num,
            )
            .order_by(TestSession.started_at.desc())
        )
        session = ts_result.scalars().first()

        answers = []
        if session and session.answers_json:
            try:
                answers = json.loads(session.answers_json)
            except Exception:
                answers = []

    questions = _get_questions_for_test(test_num)
    text = _build_test_details_text(c, result, answers, questions)
    chunks = _split_long_text(text)

    await callback.answer()

    # Если текст помещается в одно сообщение, заменяем текущее сообщение.
    # Если длинный — первое сообщение редактируем, остальные отправляем следом.
    try:
        await callback.message.edit_text(
            chunks[0],
            reply_markup=kb_back_to_candidate(cid) if len(chunks) == 1 else None,
        )
    except Exception:
        await callback.message.answer(
            chunks[0],
            reply_markup=kb_back_to_candidate(cid) if len(chunks) == 1 else None,
        )

    for chunk in chunks[1:-1]:
        await callback.message.answer(chunk)

    if len(chunks) > 1:
        await callback.message.answer(chunks[-1], reply_markup=kb_back_to_candidate(cid))


@router.callback_query(F.data.startswith("ca_invite_"))
async def card_show_invite(callback: CallbackQuery, bot: Bot):
    """Показать ссылку-приглашение."""
    cid = int(callback.data[len("ca_invite_"):])
    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c or not c.invite_code:
            await callback.answer("Ссылка не найдена.", show_alert=True)
            return
        full_name = c.full_name
        invite_code = c.invite_code
        already_activated = c.telegram_id is not None

    bot_info = await bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={invite_code}"

    status_text = (
        "ℹ️ Кандидат уже активировал приглашение."
        if already_activated
        else "⏳ Кандидат ещё не активировал приглашение."
    )

    await callback.answer()
    await callback.message.answer(
        f"<b>📨 Ссылка для {full_name} (#{cid})</b>\n\n"
        f"<code>{invite_link}</code>\n\n"
        f"{status_text}"
    )


@router.callback_query(F.data.startswith("ca_cases_"))
async def card_show_cases(callback: CallbackQuery):
    """Показать кейсы для кандидата."""
    cid = int(callback.data[len("ca_cases_"):])

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return
        c.stage = 5
        full_name, username, phone = c.full_name, c.username, c.phone

    await callback.answer()
    await callback.message.answer(
        CASES_TEMPLATE.format(
            name=full_name,
            username=username or "—",
            phone=phone or "—",
        ),
    )


@router.callback_query(F.data.startswith("ca_status_"))
async def card_show_status_menu(callback: CallbackQuery):
    """Показать выбор статуса."""
    cid = int(callback.data[len("ca_status_"):])
    await callback.answer()
    try:
        await callback.message.edit_text(
            "🎯 <b>Выберите новый статус кандидата:</b>",
            reply_markup=kb_status_choice(cid),
        )
    except Exception:
        await callback.message.answer(
            "🎯 <b>Выберите новый статус кандидата:</b>",
            reply_markup=kb_status_choice(cid),
        )


@router.callback_query(F.data.startswith("st_set_"))
async def card_set_status(callback: CallbackQuery, bot: Bot):
    """Установить новый статус кандидата."""
    parts = callback.data[len("st_set_"):].split("_", 1)
    cid = int(parts[0])
    new_status = parts[1]

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return
        old_status = c.status
        c.status = new_status
        if new_status == "offer_sent":
            c.stage = 6
        tg_id = c.telegram_id
        full_name = c.full_name

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

    notify_result = ""
    if notify_text and tg_id:
        try:
            await bot.send_message(tg_id, notify_text)
            notify_result = "\n✉️ Кандидату отправлено уведомление."
        except Exception as e:
            notify_result = f"\n⚠️ Не удалось уведомить кандидата: {e}"

    await callback.answer()
    await callback.message.answer(
        f"✅ Статус <b>{full_name}</b> (#{cid}) изменён:\n"
        f"<b>{old_status}</b> → <b>{new_status}</b>{notify_result}"
    )
    # Возвращаемся к карточке
    callback.data = f"cd_{cid}"
    await show_candidate_card(callback)


@router.callback_query(F.data.startswith("ca_retry_"))
async def card_confirm_retry(callback: CallbackQuery):
    """Подтверждение пересдачи."""
    parts = callback.data[len("ca_retry_"):].split("_")
    cid = int(parts[0])
    test_num = int(parts[1])
    await callback.answer()
    try:
        await callback.message.edit_text(
            f"⚠️ Разрешить кандидату #{cid} пересдать <b>Тест {test_num}</b>?\n\n"
            f"Текущий результат теста будет удалён, кандидат сможет пройти заново.",
            reply_markup=kb_confirm_retry(cid, test_num),
        )
    except Exception:
        await callback.message.answer(
            f"⚠️ Разрешить кандидату #{cid} пересдать <b>Тест {test_num}</b>?",
            reply_markup=kb_confirm_retry(cid, test_num),
        )


@router.callback_query(F.data.startswith("retry_yes_"))
async def confirm_retry(callback: CallbackQuery, bot: Bot):
    """Подтверждение и выполнение пересдачи."""
    parts = callback.data[len("retry_yes_"):].split("_")
    cid = int(parts[0])
    test_num = int(parts[1])

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return

        await s.execute(delete(TestResult).where(
            TestResult.candidate_id == cid, TestResult.test_number == test_num
        ))
        await s.execute(delete(TestSession).where(
            TestSession.candidate_id == cid, TestSession.test_number == test_num
        ))

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

    await callback.answer()
    await callback.message.answer(
        f"✅ Кандидат <b>{name}</b> (#{cid}) может пересдать <b>Тест {test_num}</b>."
    )

    if tg_id:
        try:
            await bot.send_message(
                tg_id,
                f"📝 <b>Возможность пересдачи</b>\n\n"
                f"Рекрутер открыл вам возможность пересдать <b>Тест {test_num}</b>.\n\n"
                f"Нажмите /start, чтобы продолжить."
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("ca_delete_"))
async def card_confirm_delete(callback: CallbackQuery):
    """Подтверждение удаления."""
    cid = int(callback.data[len("ca_delete_"):])
    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c:
            await callback.answer("Кандидат не найден.", show_alert=True)
            return
        name = c.full_name

    await callback.answer()
    try:
        await callback.message.edit_text(
            f"⚠️ Удалить кандидата <b>{name}</b> (#{cid})?\n\n"
            f"Это действие <b>необратимо</b>. Будут удалены:\n"
            f"• Карточка кандидата\n"
            f"• Все результаты тестов\n"
            f"• Все активные сессии тестов\n"
            f"• Ссылка-приглашение перестанет работать",
            reply_markup=kb_confirm_delete(cid),
        )
    except Exception:
        await callback.message.answer(
            f"⚠️ Удалить кандидата <b>{name}</b> (#{cid})?",
            reply_markup=kb_confirm_delete(cid),
        )


@router.callback_query(F.data.startswith("ca_notify_"))
async def card_notify_start(callback: CallbackQuery, state: FSMContext):
    """Начало отправки сообщения кандидату через бота."""
    cid = int(callback.data[len("ca_notify_"):])

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c or not c.telegram_id:
            await callback.answer("Невозможно: у кандидата нет привязанного Telegram.", show_alert=True)
            return
        name = c.full_name

    await state.set_state(NotifyCandidate.waiting_text)
    await state.update_data(candidate_id=cid)
    await callback.answer()
    await callback.message.answer(
        f"✉️ <b>Сообщение для {name} (#{cid})</b>\n\n"
        f"Введите текст сообщения, которое бот отправит кандидату:\n\n"
        f"<i>Или /cancel для отмены</i>"
    )


# ============================================================
# ПОИСК КАНДИДАТОВ ПО ФИО
# ============================================================
class SearchCandidate(StatesGroup):
    waiting_query = State()


class NotifyCandidate(StatesGroup):
    waiting_text = State()


@router.message(SearchCandidate.waiting_query)
async def do_search(message: Message, state: FSMContext):
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Минимум 2 символа. Попробуйте снова или /cancel.")
        return

    await state.clear()
    q_lower = query.lower()

    async with get_session() as s:
        result = await s.execute(select(Candidate))
        all_candidates = list(result.scalars().all())

    matches = [c for c in all_candidates if q_lower in c.full_name.lower()]

    if not matches:
        await message.answer(
            f"🔍 По запросу «{query}» ничего не найдено.\n\n"
            f"Попробуйте другое слово или часть имени."
        )
        return

    text = f"🔍 По запросу «{query}» найдено: <b>{len(matches)}</b>\n\nНажмите для просмотра карточки:"

    await message.answer(
        text,
        reply_markup=kb_candidate_list(matches, "all", page=0, per_page=10),
    )


@router.message(NotifyCandidate.waiting_text)
async def do_notify(message: Message, state: FSMContext, bot: Bot):
    """Отправка сообщения кандидату."""
    data = await state.get_data()
    cid = data.get("candidate_id")
    await state.clear()

    if not cid:
        await message.answer("Ошибка: кандидат не выбран.")
        return

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = result.scalar_one_or_none()
        if not c or not c.telegram_id:
            await message.answer("Кандидат не найден или нет Telegram.")
            return
        tg_id = c.telegram_id
        name = c.full_name

    try:
        await bot.send_message(tg_id, message.text)
        await message.answer(f"✉️ Сообщение отправлено <b>{name}</b>.")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить: {e}")


# ============================================================
# /search ФИО — для тех, кто привык к командам
# ============================================================
@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await menu_search(message, state)
        return
    # Прямой поиск с переданным запросом
    query = parts[1].strip()
    if len(query) < 2:
        await message.answer("Минимум 2 символа в запросе.")
        return
    q_lower = query.lower()
    async with get_session() as s:
        result = await s.execute(select(Candidate))
        all_candidates = list(result.scalars().all())
    matches = [c for c in all_candidates if q_lower in c.full_name.lower()]
    if not matches:
        await message.answer(f"🔍 По запросу «{query}» ничего не найдено.")
        return
    await message.answer(
        f"🔍 По запросу «{query}» найдено: <b>{len(matches)}</b>",
        reply_markup=kb_candidate_list(matches, "all", page=0, per_page=10),
    )


# ============================================================
# УПРАВЛЕНИЕ ВОРОНКОЙ ПРОДАЖНИКА
# ============================================================

@router.message(Command("call_done"))
async def cmd_call_done(message: Message, bot: Bot):
    """Звонок РОП проведён — отметить результат."""
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование:\n"
            "/call_done ID pass — кандидат подходит, пригласить\n"
            "/call_done ID fail — отказать после звонка"
        )
        return
    try:
        cid = int(parts[1])
        result = parts[2].lower()
    except ValueError:
        await message.answer("Неверный формат.")
        return

    if result not in ("pass", "fail"):
        await message.answer("Результат: pass или fail")
        return

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c or c.position != "sales":
            await message.answer("Кандидат не найден или не является менеджером по продажам.")
            return
        tg_id = c.telegram_id
        full_name = c.full_name

        if result == "pass":
            c.stage = 14
            c.status = "active"
        else:
            c.stage = 13
            c.status = "rejected"
        c.last_activity_at = datetime.utcnow()

    from data.sales_flow import SALES_AFTER_CALL_PASS, SALES_AFTER_CALL_FAIL
    if tg_id:
        try:
            msg = SALES_AFTER_CALL_PASS if result == "pass" else SALES_AFTER_CALL_FAIL
            await bot.send_message(tg_id, msg)
        except Exception as e:
            await message.answer(f"⚠️ Не удалось уведомить кандидата: {e}")

    verdict = "✅ Приглашён на собеседование" if result == "pass" else "❌ Отказ после звонка"
    await message.answer(f"{verdict}: <b>{full_name}</b> (#{cid})")


@router.message(Command("invite_sales"))
async def cmd_invite_sales(message: Message, bot: Bot):
    """Пригласить продажника на собеседование с деталями."""
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование:\n"
            "/invite_sales ID детали\n\n"
            "Например: /invite_sales 5 Пятница 30 мая, 14:00, ул. Немига 5, офис 203"
        )
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID.")
        return

    details = parts[2]

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c or c.position != "sales":
            await message.answer("Кандидат не найден.")
            return
        tg_id = c.telegram_id
        full_name = c.full_name
        c.stage = 14
        c.last_activity_at = datetime.utcnow()

    from data.sales_flow import SALES_INTERVIEW_INVITE
    if tg_id:
        try:
            await bot.send_message(tg_id, SALES_INTERVIEW_INVITE.format(details=details))
        except Exception as e:
            await message.answer(f"⚠️ Не удалось уведомить: {e}")

    await message.answer(f"📅 Приглашение отправлено <b>{full_name}</b> (#{cid}):\n<i>{details}</i>")


@router.message(Command("start_internship"))
async def cmd_start_internship(message: Message, bot: Bot):
    """Запустить стажировку для продажника."""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /start_internship ID")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID.")
        return

    from handlers.sales import start_internship, send_internship_day
    ok = await start_internship(cid, bot)
    if not ok:
        await message.answer("Кандидат не найден или не является продажником.")
        return

    # Сразу отправляем День 1
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        full_name = c.full_name

    await message.answer(f"🚀 Стажировка запущена для <b>{full_name}</b> (#{cid}). День 1 отправлен.")
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
    await send_internship_day(c, 1, bot)


@router.message(Command("next_day"))
async def cmd_next_day(message: Message, bot: Bot):
    """Вручную перевести стажёра на следующий день."""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /next_day ID")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID.")
        return

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one_or_none()
        if not c or c.position != "sales":
            await message.answer("Кандидат не найден.")
            return
        next_day = c.internship_day + 1
        full_name = c.full_name

    if next_day > 5:
        await message.answer(f"<b>{full_name}</b> уже прошёл все 5 дней стажировки.")
        return

    from handlers.sales import send_internship_day
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
    await send_internship_day(c, next_day, bot)
    await message.answer(f"📅 День {next_day} отправлен кандидату <b>{full_name}</b> (#{cid}).")
