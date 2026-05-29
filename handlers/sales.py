"""Воронка кандидата на позицию «Менеджер по продажам»."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
from pathlib import Path

from aiogram import Router, Bot, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile,
)
from sqlalchemy import select, delete

from config import HR_TELEGRAM_IDS
from db.database import get_session
from db.models import Candidate, TestSession, TestResult
from data.sales_flow import (
    SALES_WELCOME, ANKETA_QUESTIONS, ANKETA_ORDER, ANKETA_DONE,
    SALES_VIDEO_1_CAPTION, SALES_VIDEO_2_CAPTION, SALES_VIDEO_WATCHED_BTN,
    SALES_TEST_INTRO, TEST_SALES_VIDEO,
    SALES_TEST_PASSED, SALES_TEST_FAILED, HR_SALES_TEST_DONE,
    INTERNSHIP_WELCOME, INTERNSHIP_DAYS, INTERNSHIP_DAY_REMINDER,
)
from data.videos import VIDEO_1_PATH, VIDEO_2_PATH, SALES_VIDEO_1_URL, SALES_VIDEO_2_URL
from data.calls import GOOD_CALLS_URL, BAD_CALLS_URL
from data.materials import SALES_PRODUCTS_DOC_PATH

log = logging.getLogger(__name__)
router = Router()

MINSK_TZ = ZoneInfo("Europe/Minsk")
INTERNSHIP_SEND_TIME = time(8, 30)

SALES_PASS  = 60   # порог теста по видео
INTERN_PASS = 85   # запасной порог тестов стажировки


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

async def get_sales_candidate(tg_id: int) -> Candidate | None:
    """Возвращает кандидата-продажника по telegram_id."""
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.telegram_id == tg_id))
        c = r.scalar_one_or_none()
        if c is None:
            return None
        pos = getattr(c, "position", None)
        if pos == "sales" or (pos is None and c.stage >= 10):
            return c
        return None


async def notify_hr(bot: Bot, text: str):
    for hr_id in HR_TELEGRAM_IDS:
        try:
            await bot.send_message(hr_id, text, parse_mode="HTML")
        except Exception as e:
            log.error(f"HR notify error {hr_id}: {e}")


async def notify_hr_with_card_button(bot: Bot, text: str, candidate_id: int):
    """Уведомление HR с кнопкой перехода в карточку кандидата."""
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👤 Открыть карточку кандидата", callback_data=f"cd_{candidate_id}")
    ]])
    for hr_id in HR_TELEGRAM_IDS:
        try:
            await bot.send_message(hr_id, text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            log.error(f"HR notify error {hr_id}: {e}")


def get_sales_data(c: Candidate) -> dict:
    if c.sales_data:
        try:
            return json.loads(c.sales_data)
        except Exception:
            pass
    return {}


def _dump_sales_data(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _internship_meta(data: dict) -> dict:
    meta = data.get("internship_schedule")
    if not isinstance(meta, dict):
        meta = {}
        data["internship_schedule"] = meta
    return meta


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _format_date_ru(d: date) -> str:
    return d.strftime("%d.%m.%Y")


async def update_candidate(cid: int, **kwargs):
    """Обновляет поля кандидата."""
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        for k, v in kwargs.items():
            setattr(c, k, v)


def _with_video_url_button(video_url: str, reply_markup=None) -> InlineKeyboardMarkup:
    """Добавляет кнопку-ссылку на видео над существующими кнопками."""
    rows = [[InlineKeyboardButton(text="▶️ Смотреть видео", url=video_url)]]
    if reply_markup:
        rows.extend(reply_markup.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_video_safe(bot: Bot, tg_id: int, path: str, caption: str, reply_markup=None, video_url: str = ""):
    """Отправляет ссылку на видео, если она задана. Иначе пробует отправить локальный файл."""
    if video_url:
        await bot.send_message(
            tg_id,
            f"<b>{caption}</b>\n\n"
            "Нажмите «▶️ Смотреть видео». После просмотра вернитесь в бот "
            "и нажмите кнопку продолжения ниже.",
            reply_markup=_with_video_url_button(video_url, reply_markup),
        )
        return

    import os
    search_paths = [
        Path(path),
        Path("/app") / path,
        Path(os.getcwd()) / path,
        Path(__file__).parent.parent / path,
    ]
    video_path = next((p for p in search_paths if p.exists()), None)

    if video_path:
        try:
            await bot.send_video(tg_id, FSInputFile(video_path),
                                 caption=caption, reply_markup=reply_markup)
            return
        except Exception as e:
            log.error(f"Ошибка отправки видео {video_path}: {e}")

    # Видео не найдено — логируем и отправляем текст с кнопкой
    log.warning(f"Видео не найдено: {path}. Искал в: {[str(p) for p in search_paths]}")
    text = f"📹 <b>{caption}</b>\n\n⚠️ Видео временно недоступно."
    if reply_markup:
        await bot.send_message(tg_id, text, reply_markup=reply_markup)
    else:
        await bot.send_message(tg_id, text)


async def send_document_safe(bot: Bot, tg_id: int, path: str, caption: str = ""):
    """Отправляет DOCX/PDF-файл кандидату, если файл есть в проекте."""
    import os
    search_paths = [
        Path(path),
        Path("/app") / path,
        Path(os.getcwd()) / path,
        Path(__file__).parent.parent / path,
    ]
    doc_path = next((p for p in search_paths if p.exists()), None)

    if doc_path:
        try:
            await bot.send_document(tg_id, FSInputFile(doc_path), caption=caption)
            return
        except Exception as e:
            log.error(f"Ошибка отправки документа {doc_path}: {e}")

    log.warning(f"Документ не найден: {path}. Искал в: {[str(p) for p in search_paths]}")
    await bot.send_message(
        tg_id,
        "⚠️ Учебный DOCX-файл временно недоступен. Напишите рекрутеру."
    )


# ══════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════

def kb_start_anketa():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Заполнить анкету", callback_data="sales_start_anketa")
    ]])

def kb_choice(options: list, prefix: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opt, callback_data=f"{prefix}_{i}")]
        for i, opt in enumerate(options)
    ])

def kb_video_watched():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=SALES_VIDEO_WATCHED_BTN, callback_data="sales_video_watched")
    ]])

def kb_start_test(cb: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Начать тест", callback_data=cb)
    ]])

def kb_consp_done():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Конспект написан", callback_data="sales_consp_done")
    ]])


def kb_calls_library():
    """Кнопки на хорошие и плохие звонки для Дня 2."""
    rows = []
    if GOOD_CALLS_URL:
        rows.append([InlineKeyboardButton(text="✅ Хорошие звонки", url=GOOD_CALLS_URL)])
    if BAD_CALLS_URL:
        rows.append([InlineKeyboardButton(text="❌ Плохие звонки", url=BAD_CALLS_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

def kb_internship_start(day: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"▶️ Начать День {day}", callback_data=f"sales_day_start_{day}")
    ]])

def kb_test_question(questions: list, q_idx: int, selected: list):
    q = questions[q_idx]
    is_multi = q["type"] == "multi"
    rows = []
    for i, opt in enumerate(q["options"]):
        mark = "✅ " if i in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{opt}",
            callback_data=f"stest_{q_idx}_{i}"
        )])
    if is_multi and selected:
        rows.append([InlineKeyboardButton(
            text="✔️ Подтвердить выбор",
            callback_data=f"stest_confirm_{q_idx}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_itest_question(questions: list, q_idx: int, selected: list):
    q = questions[q_idx]
    is_multi = q["type"] == "multi"
    rows = []
    for i, opt in enumerate(q["options"]):
        mark = "✅ " if i in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{opt}",
            callback_data=f"itq_{q_idx}_{i}"
        )])
    if is_multi and selected:
        rows.append([InlineKeyboardButton(
            text="✔️ Подтвердить выбор",
            callback_data=f"itq_confirm_{q_idx}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ══════════════════════════════════════════════════════════════
# СТАРТ ВОРОНКИ (вызывается из candidate.py)
# ══════════════════════════════════════════════════════════════

async def start_sales_flow(candidate: Candidate, message: Message, bot: Bot):
    """Запускает воронку продажника."""
    await update_candidate(candidate.id, stage=10, awaiting=None,
                           last_activity_at=datetime.utcnow())
    await message.answer(SALES_WELCOME, reply_markup=kb_start_anketa())


# ══════════════════════════════════════════════════════════════
# АНКЕТА
# ══════════════════════════════════════════════════════════════

@router.callback_query(F.data == "sales_start_anketa")
async def cb_start_anketa(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_anketa_q(bot, c.telegram_id, c, "q1")


async def _send_anketa_q(bot: Bot, tg_id: int, candidate: Candidate, q_key: str):
    q = ANKETA_QUESTIONS[q_key]
    await update_candidate(candidate.id, awaiting=q["awaiting"],
                           last_activity_at=datetime.utcnow())
    if q["type"] == "choice":
        await bot.send_message(tg_id, q["text"],
                               reply_markup=kb_choice(q["options"], f"anketa_{q_key}"))
    else:
        await bot.send_message(tg_id, q["text"])


@router.callback_query(F.data.startswith("anketa_q"))
async def cb_anketa_choice(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    parts = callback.data.split("_")
    q_key = parts[1]
    opt_idx = int(parts[2])
    answer_text = ANKETA_QUESTIONS[q_key]["options"][opt_idx]
    await _save_anketa_and_next(bot, c, q_key, answer_text)


async def _save_anketa_and_next(bot: Bot, candidate: Candidate, q_key: str, answer: str):
    # Сохраняем ответ
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        data = get_sales_data(c)
        if "anketa" not in data:
            data["anketa"] = {}
        data["anketa"][q_key] = answer
        c.sales_data = json.dumps(data, ensure_ascii=False)
        tg_id = c.telegram_id
        cid = c.id

    idx = ANKETA_ORDER.index(q_key)
    if idx + 1 < len(ANKETA_ORDER):
        # Следующий вопрос
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == cid))
            c_fresh = r.scalar_one()
        await _send_anketa_q(bot, tg_id, c_fresh, ANKETA_ORDER[idx + 1])
    else:
        # Анкета завершена — отправляем видео
        await _finish_anketa(bot, cid, tg_id)


# Текстовые ответы на вопросы анкеты обрабатываются в handle_sales_text

async def _finish_anketa(bot: Bot, cid: int, tg_id: int):
    """Анкета завершена — переходим к видео."""
    await update_candidate(cid, stage=11, awaiting="sales_video",
                           last_activity_at=datetime.utcnow())
    await bot.send_message(tg_id, ANKETA_DONE)
    # Видео 1 — о компании
    await send_video_safe(bot, tg_id, VIDEO_1_PATH, SALES_VIDEO_1_CAPTION, video_url=SALES_VIDEO_1_URL)
    # Видео 2 — о продуктах (с кнопкой)
    await send_video_safe(bot, tg_id, VIDEO_2_PATH, SALES_VIDEO_2_CAPTION,
                          reply_markup=kb_video_watched(), video_url=SALES_VIDEO_2_URL)


# ══════════════════════════════════════════════════════════════
# ВИДЕО ПРОСМОТРЕНО
# ══════════════════════════════════════════════════════════════

@router.callback_query(F.data == "sales_video_watched")
async def cb_video_watched(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    await update_candidate(c.id, stage=12, awaiting="sales_test",
                           last_activity_at=datetime.utcnow())
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(c.telegram_id, SALES_TEST_INTRO,
                           reply_markup=kb_start_test("sales_test_start"))


# ══════════════════════════════════════════════════════════════
# ТЕСТ ПО ВИДЕО
# ══════════════════════════════════════════════════════════════

@router.callback_query(F.data == "sales_test_start")
async def cb_sales_test_start(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return

    deadline = datetime.utcnow() + timedelta(hours=2)
    async with get_session() as s:
        await s.execute(delete(TestSession).where(
            TestSession.candidate_id == c.id,
            TestSession.test_number == 10,
        ))
        s.add(TestSession(
            candidate_id=c.id, test_number=10,
            deadline=deadline, current_question=0,
            answers_json="[]", selected_options="[]",
            is_active=True,
        ))
        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.awaiting = "sales_test_active"
        tg_id = cand.telegram_id

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_vtest_q(bot, tg_id, 0, [])


async def _send_vtest_q(bot: Bot, tg_id: int, q_idx: int, selected: list):
    """Отправляет вопрос теста по видео — текст + кнопки в одном сообщении."""
    q = TEST_SALES_VIDEO[q_idx]
    is_multi = q["type"] == "multi"
    header = f"<b>Вопрос {q_idx + 1}/{len(TEST_SALES_VIDEO)}</b>"
    if is_multi:
        header += "\n<i>(выберите все верные варианты)</i>"
    # Отправляем текст и кнопки ВМЕСТЕ в одном сообщении
    await bot.send_message(
        tg_id,
        f"{header}\n\n{q['text']}",
        reply_markup=kb_test_question(TEST_SALES_VIDEO, q_idx, selected),
    )


@router.callback_query(F.data.startswith("stest_"))
async def cb_vtest_answer(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return

    parts = callback.data.split("_")
    is_confirm = parts[1] == "confirm"
    q_idx = int(parts[2]) if is_confirm else int(parts[1])
    opt_idx = None if is_confirm else int(parts[2])

    # ── Шаг 1: обновляем БД, запоминаем что делать ──────────
    do_finish    = False
    do_next_q    = False
    do_update_kb = False
    next_q_idx   = None
    finish_ans   = None
    new_selected = None
    q_cur_save   = None
    tg_id = c.telegram_id
    cid   = c.id

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == c.id,
            TestSession.test_number == 10,
            TestSession.is_active == True,
        ))
        ts = ts_r.scalar_one_or_none()
        if not ts:
            await bot.send_message(tg_id, "⚠️ Сессия не найдена. Напишите рекрутеру.")
            return

        if datetime.utcnow() > ts.deadline:
            ts.is_active = False
            await bot.send_message(tg_id, "⏰ Время истекло. Свяжитесь с рекрутером.")
            return

        answers  = json.loads(ts.answers_json)
        selected = json.loads(ts.selected_options)
        q_cur    = ts.current_question
        q        = TEST_SALES_VIDEO[q_cur]
        is_multi = q["type"] == "multi"
        q_cur_save = q_cur

        if is_confirm:
            answers.append(sorted(selected))
            ts.answers_json   = json.dumps(answers)
            ts.selected_options = "[]"
            ts.current_question += 1
            if ts.current_question >= len(TEST_SALES_VIDEO):
                r2 = await s.execute(select(Candidate).where(Candidate.id == cid))
                r2.scalar_one().awaiting = None
                ts.is_active = False
                do_finish  = True
                finish_ans = list(answers)
            else:
                do_next_q  = True
                next_q_idx = ts.current_question

        elif is_multi:
            if opt_idx in selected:
                selected.remove(opt_idx)
            else:
                selected.append(opt_idx)
            ts.selected_options = json.dumps(selected)
            do_update_kb = True
            new_selected = list(selected)

        else:  # single
            answers.append([opt_idx])
            ts.answers_json   = json.dumps(answers)
            ts.current_question += 1
            if ts.current_question >= len(TEST_SALES_VIDEO):
                r2 = await s.execute(select(Candidate).where(Candidate.id == cid))
                r2.scalar_one().awaiting = None
                ts.is_active = False
                do_finish  = True
                finish_ans = list(answers)
            else:
                do_next_q  = True
                next_q_idx = ts.current_question

    # ── Шаг 2: всё что связано с Telegram — ПОСЛЕ сессии БД ──
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if do_finish:
        await _finish_vtest(bot, cid, finish_ans)
    elif do_next_q:
        await _send_vtest_q(bot, tg_id, next_q_idx, [])
    elif do_update_kb:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=kb_test_question(TEST_SALES_VIDEO, q_cur_save, new_selected))
        except Exception:
            await _send_vtest_q(bot, tg_id, q_cur_save, new_selected)

async def _finish_vtest(bot: Bot, cid: int, answers: list):
    """Подсчёт результата теста по видео."""
    correct = sum(
        1 for i, q in enumerate(TEST_SALES_VIDEO)
        if i < len(answers) and sorted(answers[i]) == sorted(q["correct"])
    )
    score = round(correct / len(TEST_SALES_VIDEO) * 100)
    passed = score >= SALES_PASS

    async with get_session() as s:
        s.add(TestResult(candidate_id=cid, test_number=10,
                         score_percent=score, passed=passed))
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        c.stage = 13 if passed else 12
        c.status = "active" if passed else "failed"
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id
        full_name = c.full_name
        data = get_sales_data(c)
        anketa = data.get("anketa", {})

    if passed:
        await bot.send_message(tg_id, SALES_TEST_PASSED.format(score=score))
        anketa_text = "\n".join(f"• {k}: {v}" for k, v in anketa.items()) or "—"
        await notify_hr_with_card_button(bot, HR_SALES_TEST_DONE.format(
            name=full_name, cid=cid, score=score, anketa=anketa_text), cid)
    else:
        await bot.send_message(tg_id, SALES_TEST_FAILED.format(score=score))
        await notify_hr(bot,
            f"❌ Кандидат <b>{full_name}</b> (#{cid}) не прошёл тест по видео: {score}%")


# ══════════════════════════════════════════════════════════════
# СТАЖИРОВКА
# ══════════════════════════════════════════════════════════════

async def schedule_internship(candidate_id: int, start_date: date, bot: Bot) -> tuple[bool, str, str]:
    """Планирует 3-дневную стажировку после звонка РОП.

    День 1 будет отправлен в start_date в 08:30 по Минску.
    День 2 и День 3 — в следующие календарные дни в 08:30.
    Возвращает: ok, full_name, note.
    """
    today = datetime.now(MINSK_TZ).date()
    if start_date < today:
        return False, "", "Дата старта не может быть в прошлом."

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        c = r.scalar_one_or_none()
        if not c:
            return False, "", "Кандидат не найден."

        tr_result = await s.execute(select(TestResult).where(TestResult.candidate_id == candidate_id))
        test_numbers = [r.test_number for r in tr_result.scalars().all()]
        is_sales_flow = (
            getattr(c, "position", None) == "sales"
            or c.stage >= 10
            or 10 in test_numbers
            or any(num >= 100 for num in test_numbers)
        )
        if not is_sales_flow:
            return False, "", "Кандидат не является менеджером по продажам."
        if not c.telegram_id:
            return False, c.full_name if c else "", "Кандидат ещё не активировал бот."

        # Для старых кандидатов, добавленных до выбора позиции, фиксируем позицию как sales.
        if getattr(c, "position", None) != "sales":
            c.position = "sales"

        data = get_sales_data(c)
        meta = _internship_meta(data)
        meta["start_date"] = start_date.strftime("%Y-%m-%d")
        meta["send_time"] = "08:30"
        meta["timezone"] = "Europe/Minsk"
        meta["sent_days"] = []
        meta["scheduled_by_hr_at"] = datetime.now(MINSK_TZ).isoformat()

        c.sales_data = _dump_sales_data(data)
        c.stage = 15
        c.status = "active"
        c.internship_day = 0
        c.internship_step = "scheduled"
        c.awaiting = None
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id
        full_name = c.full_name

    try:
        await bot.send_message(
            tg_id,
            INTERNSHIP_WELCOME +
            f"\n\n📅 <b>Дата старта:</b> {_format_date_ru(start_date)}\n"
            "⏰ Материалы будут приходить каждый день в <b>08:30</b>."
        )
    except Exception as e:
        log.error(f"Не удалось уведомить кандидата о старте стажировки {candidate_id}: {e}")

    note = "Кандидату отправлено сообщение о старте стажировки."

    # Если HR выбрал сегодняшнюю дату уже после 08:30, не ждём следующий день — отправляем День 1 сразу.
    now = datetime.now(MINSK_TZ)
    if start_date == today and now.time() >= INTERNSHIP_SEND_TIME:
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
            c = r.scalar_one()
        await send_internship_day(c, 1, bot)
        note = "Так как сегодня уже позже 08:30, День 1 отправлен сразу."

    return True, full_name, note


async def start_internship(candidate_id: int, bot: Bot) -> bool:
    """Совместимость со старой командой /start_internship: старт сегодня."""
    ok, _, _ = await schedule_internship(candidate_id, datetime.now(MINSK_TZ).date(), bot)
    return ok


async def send_internship_day(candidate: Candidate, day: int, bot: Bot):
    if day not in INTERNSHIP_DAYS:
        return
    dd = INTERNSHIP_DAYS[day]
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        data = get_sales_data(c)
        meta = _internship_meta(data)
        sent_days = meta.get("sent_days")
        if not isinstance(sent_days, list):
            sent_days = []
        if day not in sent_days:
            sent_days.append(day)
        meta["sent_days"] = sorted(sent_days)
        meta[f"day_{day}_sent_at"] = datetime.now(MINSK_TZ).isoformat()

        c.sales_data = _dump_sales_data(data)
        c.internship_day = day
        c.internship_step = "materials"
        c.awaiting = dd["awaiting_task"]
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id

    if tg_id:
        # День 1: даем возможность посмотреть видео и почитать материал.
        if day == 1:
            await send_video_safe(
                bot,
                tg_id,
                VIDEO_1_PATH,
                "📹 Видео о MAVIS GROUP",
                video_url=SALES_VIDEO_1_URL,
            )

        # День 2: бот отправляет DOCX-файл прямо в Telegram.
        if day == 2:
            await send_document_safe(
                bot,
                tg_id,
                SALES_PRODUCTS_DOC_PATH,
                caption="📄 Продуктовый материал Дня 2: СПК, аттестация, ISO/СУОТ, лицензии МВД и МЧС",
            )

        materials_markup = kb_calls_library() if day == 2 else None
        await bot.send_message(tg_id, dd["materials"], reply_markup=materials_markup)
        await bot.send_message(tg_id, dd["task"])


@router.callback_query(F.data.startswith("sales_day_start_"))
async def cb_day_start(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    day = int(callback.data.split("_")[-1])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_internship_day(c, day, bot)


# ── Конспект (День 2) ─────────────────────────────────────────
@router.callback_query(F.data == "sales_consp_done")
async def cb_consp_done(callback: CallbackQuery, bot: Bot):
    """Старый callback конспекта Дня 2 оставлен для совместимости."""
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(
        c.telegram_id,
        "Этот шаг больше не используется. В новой версии Дня 2 нужно отправить один развёрнутый текстовый ответ по продуктам и звонкам."
    )


# ── Тесты стажировки (автоматический тест сейчас есть только в Дне 1) ──
@router.callback_query(F.data.startswith("itest_start_"))
async def cb_itest_start(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    day = int(callback.data.split("_")[-1])
    dd = INTERNSHIP_DAYS.get(day)
    if not dd or not dd.get("has_test"):
        return

    test_num = 100 + day
    deadline = datetime.utcnow() + timedelta(hours=2)

    async with get_session() as s:
        await s.execute(delete(TestSession).where(
            TestSession.candidate_id == c.id,
            TestSession.test_number == test_num,
        ))
        s.add(TestSession(
            candidate_id=c.id, test_number=test_num,
            deadline=deadline, current_question=0,
            answers_json="[]", selected_options="[]",
            is_active=True,
        ))
        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.awaiting = dd["awaiting_test"]
        cand.internship_step = "test"
        tg_id = cand.telegram_id

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_itest_q(bot, tg_id, dd["test_questions"], 0, [])


async def _send_itest_q(bot: Bot, tg_id: int, questions: list, q_idx: int, selected: list):
    q = questions[q_idx]
    is_multi = q["type"] == "multi"
    header = f"<b>Вопрос {q_idx + 1}/{len(questions)}</b>"
    if is_multi:
        header += "\n<i>(выберите все верные варианты)</i>"
    await bot.send_message(tg_id, f"{header}\n\n{q['text']}")
    await bot.send_message(
        tg_id, "👇 Выберите ответ:",
        reply_markup=kb_itest_question(questions, q_idx, selected),
    )


@router.callback_query(F.data.startswith("itq_"))
async def cb_itest_answer(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return

    parts = callback.data.split("_")
    is_confirm = parts[1] == "confirm"
    q_idx = int(parts[2]) if is_confirm else int(parts[1])
    opt_idx = None if is_confirm else int(parts[2])

    day = c.internship_day
    dd = INTERNSHIP_DAYS.get(day)
    if not dd:
        return
    questions = dd["test_questions"]
    test_num = 100 + day

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == c.id,
            TestSession.test_number == test_num,
            TestSession.is_active == True,
        ))
        ts = ts_r.scalar_one_or_none()
        if not ts:
            await bot.send_message(c.telegram_id, "⚠️ Сессия не найдена. Напишите рекрутеру.")
            return

        answers = json.loads(ts.answers_json)
        selected = json.loads(ts.selected_options)
        q_cur = ts.current_question
        q = questions[q_cur]
        is_multi = q["type"] == "multi"
        tg_id = c.telegram_id
        cid = c.id

        if is_confirm:
            answers.append(sorted(selected))
            ts.answers_json = json.dumps(answers)
            ts.selected_options = "[]"
            ts.current_question += 1
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if ts.current_question >= len(questions):
                ts.is_active = False
                await _finish_itest(bot, cid, day, answers)
            else:
                await _send_itest_q(bot, tg_id, questions, ts.current_question, [])

        elif is_multi:
            if opt_idx in selected:
                selected.remove(opt_idx)
            else:
                selected.append(opt_idx)
            ts.selected_options = json.dumps(selected)
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=kb_itest_question(questions, q_cur, selected))
            except Exception:
                await bot.send_message(
                    tg_id, "👇 Выберите ответ:",
                    reply_markup=kb_itest_question(questions, q_cur, selected))

        else:
            answers.append([opt_idx])
            ts.answers_json = json.dumps(answers)
            ts.current_question += 1
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if ts.current_question >= len(questions):
                ts.is_active = False
                await _finish_itest(bot, cid, day, answers)
            else:
                await _send_itest_q(bot, tg_id, questions, ts.current_question, [])


async def _finish_itest(bot: Bot, cid: int, day: int, answers: list):
    dd = INTERNSHIP_DAYS[day]
    questions = dd["test_questions"]
    correct = sum(
        1 for i, q in enumerate(questions)
        if i < len(answers) and sorted(answers[i]) == sorted(q["correct"])
    )
    score = round(correct / len(questions) * 100)
    passed = score >= dd.get("test_pass", INTERN_PASS)
    test_num = 100 + day

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == cid,
            TestSession.test_number == test_num,
        ))
        ts = ts_r.scalar_one_or_none()
        if ts:
            ts.is_active = False
        s.add(TestResult(candidate_id=cid, test_number=test_num,
                         score_percent=score, passed=passed))
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        c.awaiting = None
        c.internship_step = "done"
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id
        full_name = c.full_name

    await bot.send_message(tg_id, dd["test_done_msg"].format(score=score))
    if not passed:
        await bot.send_message(
            tg_id,
            f"⚠️ Результат ниже {dd.get('test_pass', INTERN_PASS)}%. "
            "Рекомендуем перечитать материалы. РОП свяжется с тобой."
        )
    await notify_hr(bot,
        f"📊 День {day} тест: <b>{full_name}</b> (#{cid}) — <b>{score}%</b> "
        f"{'✅' if passed else '❌'}\n/candidate {cid}")


# ══════════════════════════════════════════════════════════════
# ТЕКСТОВЫЕ / ГОЛОСОВЫЕ ОТВЕТЫ (вызывается из candidate.py)
# ══════════════════════════════════════════════════════════════

async def handle_sales_text(message: Message, bot: Bot) -> bool:
    """Возвращает True если сообщение обработано."""
    c = await get_sales_candidate(message.from_user.id)
    if not c:
        return False

    awaiting = c.awaiting or ""

    # Текстовые ответы анкеты
    for q_key, q in ANKETA_QUESTIONS.items():
        if q["type"] == "text" and q["awaiting"] == awaiting:
            await _save_anketa_and_next(bot, c, q_key, message.text.strip())
            return True

    # Голосовое — День 2
    if awaiting == "internship_d2_voice":
        if message.voice or message.audio:
            for hr_id in HR_TELEGRAM_IDS:
                try:
                    await message.forward(hr_id)
                except Exception:
                    pass
            await update_candidate(c.id, awaiting="internship_d2_errors",
                                   last_activity_at=datetime.utcnow())
            await message.answer(
                "✅ Голосовое получено! Передано руководителю.\n\n"
                "Теперь напиши текстом: <b>Какие ошибки совершил менеджер в плохих звонках?</b>\n\n"
                "<i>Разбери каждый из 3 примеров по 1–2 ошибки.</i>"
            )
        else:
            await message.answer("🎤 Жду голосовое сообщение. Нажми на 🎤 в Telegram.")
        return True

    # Анализ ошибок плохих звонков — День 2
    if awaiting == "internship_d2_errors":
        answer = message.text.strip() if message.text else ""
        if len(answer) < 15:
            await message.answer("Напиши развёрнуто — хотя бы 1–2 ошибки на каждый звонок.")
            return True
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == c.id))
            cand = r.scalar_one()
            data = get_sales_data(cand)
            if "internship" not in data:
                data["internship"] = {}
            data["internship"]["day2_errors"] = answer
            cand.sales_data = json.dumps(data, ensure_ascii=False)
            cand.awaiting = "internship_d2_consp"
            cand.last_activity_at = datetime.utcnow()
            full_name = cand.full_name
            cid = cand.id
        await notify_hr(bot,
            f"📋 День 2 — анализ ошибок: <b>{full_name}</b> (#{cid})\n\n{answer[:600]}")
        dd = INTERNSHIP_DAYS[2]
        await message.answer(dd["task_done_msg"])
        await message.answer(dd["consp_task"], reply_markup=kb_consp_done())
        return True

    # Текстовые задания стажировки в боте: дни 1, 2, 3
    day_map = {
        "internship_d1_task": 1,
        "internship_d2_task": 2,
        "internship_d3_task": 3,
    }
    if awaiting in day_map:
        day = day_map[awaiting]
        await _handle_day_task(bot, c, day, message)
        return True

    return False


async def _handle_day_task(bot: Bot, candidate: Candidate, day: int, message: Message):
    dd = INTERNSHIP_DAYS[day]
    answer = message.text.strip() if message.text else ""
    if len(answer) < 20:
        await message.answer("Ответ слишком короткий. Напиши развёрнуто — минимум несколько предложений.")
        return

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        data = get_sales_data(c)
        if "internship" not in data:
            data["internship"] = {}
        data["internship"][f"day{day}"] = answer
        c.sales_data = json.dumps(data, ensure_ascii=False)
        c.last_activity_at = datetime.utcnow()
        full_name = c.full_name
        cid = c.id

        if dd.get("has_test"):
            c.awaiting = dd["awaiting_test"]
            c.internship_step = "test"
        else:
            c.awaiting = None
            c.internship_step = "done"

    await message.answer(dd["task_done_msg"])
    await notify_hr(bot, dd["hr_notify"].format(
        name=full_name, cid=cid, answer=answer[:600]))

    if dd.get("has_test"):
        await message.answer(dd["test_intro"],
                             reply_markup=kb_start_test(f"itest_start_{day}"))

    if dd.get("final_day"):
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == cid))
            c = r.scalar_one()
            c.stage = 16
            c.status = "passed"
            c.internship_step = "finished_bot"
            c.awaiting = None
            c.last_activity_at = datetime.utcnow()


# ══════════════════════════════════════════════════════════════
# НАПОМИНАНИЕ СТАЖЁРАМ (планировщик 9:00)
# ══════════════════════════════════════════════════════════════

async def send_scheduled_internship_days(bot: Bot):
    """Планировщик: каждый день в 08:30 по Минску отправляет нужный день стажировки.

    Стажировка запускается HR через карточку кандидата. Дата старта хранится в sales_data,
    поэтому новые колонки в БД не нужны.
    """
    now = datetime.now(MINSK_TZ)
    today = now.date()

    async with get_session() as s:
        r = await s.execute(
            select(Candidate).where(
                Candidate.position == "sales",
                Candidate.stage == 15,
                Candidate.status == "active",
            )
        )
        candidates = r.scalars().all()

    for c in candidates:
        data = get_sales_data(c)
        meta = data.get("internship_schedule") if isinstance(data, dict) else None
        if not isinstance(meta, dict):
            continue

        start_date = _parse_iso_date(meta.get("start_date"))
        if not start_date or today < start_date:
            continue

        day = (today - start_date).days + 1
        if day < 1 or day > 3:
            continue

        sent_days = meta.get("sent_days")
        if not isinstance(sent_days, list):
            sent_days = []
        if day in sent_days:
            continue

        try:
            await send_internship_day(c, day, bot)
            await notify_hr(
                bot,
                f"📤 <b>Стажировочный день {day} отправлен автоматически</b>\n\n"
                f"👤 {c.full_name} (#{c.id})\n"
                f"⏰ {_format_date_ru(today)} 08:30"
            )
        except Exception as e:
            log.error(f"Auto internship day send error for candidate {c.id}: {e}")


async def remind_internship_next_day(bot: Bot):
    """Старое имя оставлено для совместимости. Теперь отправка идёт по расписанию запуска."""
    await send_scheduled_internship_days(bot)
