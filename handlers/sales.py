"""Воронка кандидата на позицию «Менеджер по продажам»."""
import asyncio
import json
import logging
from datetime import datetime, timedelta
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

log = logging.getLogger(__name__)
router = Router()

SALES_PASS  = 60   # порог теста по видео
INTERN_PASS = 70   # порог тестов стажировки


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


def get_sales_data(c: Candidate) -> dict:
    if c.sales_data:
        try:
            return json.loads(c.sales_data)
        except Exception:
            pass
    return {}


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
        await notify_hr(bot, HR_SALES_TEST_DONE.format(
            name=full_name, cid=cid, score=score, anketa=anketa_text))
    else:
        await bot.send_message(tg_id, SALES_TEST_FAILED.format(score=score))
        await notify_hr(bot,
            f"❌ Кандидат <b>{full_name}</b> (#{cid}) не прошёл тест по видео: {score}%")


# ══════════════════════════════════════════════════════════════
# СТАЖИРОВКА
# ══════════════════════════════════════════════════════════════

async def start_internship(candidate_id: int, bot: Bot) -> bool:
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        c = r.scalar_one_or_none()
        if not c or c.position != "sales":
            return False
        c.stage = 15
        c.internship_day = 0
        c.internship_step = None
        c.awaiting = None
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id

    if tg_id:
        await bot.send_message(tg_id, INTERNSHIP_WELCOME)
    return True


async def send_internship_day(candidate: Candidate, day: int, bot: Bot):
    if day not in INTERNSHIP_DAYS:
        return
    dd = INTERNSHIP_DAYS[day]
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.internship_day = day
        c.internship_step = "materials"
        c.awaiting = dd["awaiting_task"]
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id

    if tg_id:
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
    await callback.answer()
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        return
    await update_candidate(c.id, awaiting="internship_d2_test",
                           internship_step="test")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    dd = INTERNSHIP_DAYS[2]
    await bot.send_message(c.telegram_id, "✅ Отлично! Конспект записан.\n\nТеперь пройди тест 👇")
    await bot.send_message(c.telegram_id, dd["test_intro"],
                           reply_markup=kb_start_test("itest_start_2"))


# ── Тесты стажировки (Дни 1, 2, 3) ───────────────────────────
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

    # Текстовые задания дней 1, 3, 4, 5
    day_map = {
        "internship_d1_task": 1,
        "internship_d3_task": 3,
        "internship_d4_task": 4,
        "internship_d5_task": 5,
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
    elif day == 5:
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == cid))
            c = r.scalar_one()
            c.stage = 16
            c.status = "passed"


# ══════════════════════════════════════════════════════════════
# НАПОМИНАНИЕ СТАЖЁРАМ (планировщик 9:00)
# ══════════════════════════════════════════════════════════════

async def remind_internship_next_day(bot: Bot):
    async with get_session() as s:
        r = await s.execute(
            select(Candidate).where(
                Candidate.position == "sales",
                Candidate.stage == 15,
                Candidate.status == "active",
                Candidate.internship_step == "done",
            )
        )
        candidates = r.scalars().all()

    for c in candidates:
        next_day = c.internship_day + 1
        if 1 <= next_day <= 5:
            dd = INTERNSHIP_DAYS[next_day]
            if c.telegram_id:
                try:
                    await bot.send_message(
                        c.telegram_id,
                        INTERNSHIP_DAY_REMINDER.format(title=dd["title"]),
                        reply_markup=kb_internship_start(next_day),
                    )
                except Exception as e:
                    log.error(f"Reminder error {c.id}: {e}")
        elif next_day > 5:
            async with get_session() as s2:
                r2 = await s2.execute(select(Candidate).where(Candidate.id == c.id))
                cand = r2.scalar_one()
                cand.stage = 16
                cand.status = "passed"
