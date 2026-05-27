"""Воронка кандидата на позицию «Менеджер по продажам»."""
import json, logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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
    POSITION_LABELS,
)
from data.videos import VIDEO_1_PATH, VIDEO_2_PATH

log = logging.getLogger(__name__)
router = Router()
SALES_PASS = 60      # порог теста по видео
INTERN_PASS = 70     # порог тестов стажировки


# ── Helpers ──────────────────────────────────────────────────
async def get_sales_candidate(tg_id: int) -> Candidate | None:
    async with get_session() as s:
        r = await s.execute(
            select(Candidate).where(Candidate.telegram_id == tg_id, Candidate.position == "sales")
        )
        return r.scalar_one_or_none()


async def notify_hr(bot: Bot, text: str):
    for hr_id in HR_TELEGRAM_IDS:
        try:
            await bot.send_message(hr_id, text)
        except Exception as e:
            log.error(f"HR notify error {hr_id}: {e}")


def get_sales_data(c: Candidate) -> dict:
    if c.sales_data:
        try:
            return json.loads(c.sales_data)
        except Exception:
            pass
    return {}


async def set_sales_data(cid: int, data: dict):
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        c.sales_data = json.dumps(data, ensure_ascii=False)


# ── Keyboards ─────────────────────────────────────────────────
def kb_start_anketa():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Заполнить анкету", callback_data="sales_start_anketa")
    ]])

def kb_choice(options, prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opt, callback_data=f"{prefix}_{i}")]
        for i, opt in enumerate(options)
    ])

def kb_video_watched():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=SALES_VIDEO_WATCHED_BTN, callback_data="sales_video_watched")
    ]])

def kb_start_test(cb):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Начать тест", callback_data=cb)
    ]])

def kb_internship_start(day):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"▶️ Начать День {day}", callback_data=f"sales_day_start_{day}")
    ]])

def kb_consp_done():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Конспект написан", callback_data="sales_consp_done")
    ]])

def kb_test_question(questions, q_idx, selected):
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
        rows.append([InlineKeyboardButton(text="✔️ Подтвердить", callback_data=f"stest_confirm_{q_idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Старт воронки ─────────────────────────────────────────────
async def start_sales_flow(candidate: Candidate, message: Message, bot: Bot):
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.stage = 10
        c.awaiting = None
        c.last_activity_at = datetime.utcnow()
    await message.answer(SALES_WELCOME, reply_markup=kb_start_anketa())


# ── Анкета ────────────────────────────────────────────────────
@router.callback_query(F.data == "sales_start_anketa")
async def cb_start_anketa(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_anketa_question(callback.message, c, "q1")


async def send_anketa_question(message: Message, candidate: Candidate, q_key: str):
    q = ANKETA_QUESTIONS[q_key]
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.awaiting = q["awaiting"]
        c.last_activity_at = datetime.utcnow()
    if q["type"] == "choice":
        await message.answer(q["text"], reply_markup=kb_choice(q["options"], f"anketa_{q_key}"))
    else:
        await message.answer(q["text"])


@router.callback_query(F.data.startswith("anketa_q"))
async def cb_anketa_choice(callback: CallbackQuery, bot: Bot):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    parts = callback.data.split("_")
    q_key = parts[1]
    opt_idx = int(parts[2])
    answer_text = ANKETA_QUESTIONS[q_key]["options"][opt_idx]
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _save_anketa_and_next(c, q_key, answer_text, callback.message, bot)


async def _save_anketa_and_next(candidate, q_key, answer, message, bot):
    data = get_sales_data(candidate)
    if "anketa" not in data:
        data["anketa"] = {}
    data["anketa"][q_key] = answer
    await set_sales_data(candidate.id, data)

    idx = ANKETA_ORDER.index(q_key)
    if idx + 1 < len(ANKETA_ORDER):
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
            c = r.scalar_one()
        await send_anketa_question(message, c, ANKETA_ORDER[idx + 1])
    else:
        await _finish_anketa(candidate, message, bot)


async def _finish_anketa(candidate, message, bot):
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.stage = 11
        c.awaiting = "sales_video"
        c.last_activity_at = datetime.utcnow()

    await message.answer(ANKETA_DONE)

    from aiogram.types import FSInputFile
    # Видео 1 — о компании
    v1 = Path(VIDEO_1_PATH)
    if v1.exists():
        await message.answer_video(FSInputFile(v1), caption=SALES_VIDEO_1_CAPTION)
    # Видео 2 — о продуктах
    v2 = Path(VIDEO_2_PATH)
    if v2.exists():
        await message.answer_video(FSInputFile(v2), caption=SALES_VIDEO_2_CAPTION,
                                   reply_markup=kb_video_watched())
    else:
        await message.answer("⚠️ Видео временно недоступно.", reply_markup=kb_video_watched())


# ── Видео просмотрено ─────────────────────────────────────────
@router.callback_query(F.data == "sales_video_watched")
async def cb_video_watched(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.stage = 12
        cand.awaiting = "sales_test"
        cand.last_activity_at = datetime.utcnow()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(SALES_TEST_INTRO, reply_markup=kb_start_test("sales_test_start"))


# ── Тест по видео ─────────────────────────────────────────────
@router.callback_query(F.data == "sales_test_start")
async def cb_sales_test_start(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    deadline = datetime.utcnow() + timedelta(minutes=30)
    async with get_session() as s:
        await s.execute(delete(TestSession).where(
            TestSession.candidate_id == c.id, TestSession.test_number == 10))
        s.add(TestSession(candidate_id=c.id, test_number=10, deadline=deadline,
                          current_question=0, answers_json="[]", selected_options="[]"))
        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.awaiting = "sales_test_active"
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_video_test_q(callback.message, 0, [])


async def _send_video_test_q(message, q_idx, selected):
    q = TEST_SALES_VIDEO[q_idx]
    is_multi = q["type"] == "multi"
    prefix = f"<b>Вопрос {q_idx+1}/{len(TEST_SALES_VIDEO)}</b>"
    if is_multi:
        prefix += "\n<i>(выберите все верные)</i>"
    await message.answer(f"{prefix}\n\n{q['text']}",
                         reply_markup=kb_test_question(TEST_SALES_VIDEO, q_idx, selected))


@router.callback_query(F.data.startswith("stest_"))
async def cb_video_test_answer(callback: CallbackQuery, bot: Bot):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return

    parts = callback.data.split("_")
    # stest_confirm_N или stest_N_I
    if parts[1] == "confirm":
        q_idx = int(parts[2])
        action = "confirm"
        opt_idx = None
    else:
        q_idx = int(parts[1])
        opt_idx = int(parts[2])
        action = "pick"

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == c.id, TestSession.test_number == 10,
            TestSession.is_active == True))
        ts = ts_r.scalar_one_or_none()
        if not ts:
            await callback.answer("Сессия не найдена."); return

        if datetime.utcnow() > ts.deadline:
            ts.is_active = False
            await callback.answer("⏰ Время вышло!")
            await callback.message.answer("⏰ Время на тест истекло. Свяжитесь с рекрутером.")
            return

        answers = json.loads(ts.answers_json)
        selected = json.loads(ts.selected_options)
        q = TEST_SALES_VIDEO[ts.current_question]
        is_multi = q["type"] == "multi"

        if action == "confirm":
            answers.append(sorted(selected))
            ts.answers_json = json.dumps(answers)
            ts.selected_options = "[]"
            ts.current_question += 1
            await callback.answer()
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if ts.current_question >= len(TEST_SALES_VIDEO):
                cid = c.id
                cand_r = await s.execute(select(Candidate).where(Candidate.id == cid))
                cand = cand_r.scalar_one()
                cand.awaiting = None
                await _finish_video_test(cid, answers, callback.message, bot)
            else:
                await _send_video_test_q(callback.message, ts.current_question, [])
        else:
            if is_multi:
                if opt_idx in selected:
                    selected.remove(opt_idx)
                else:
                    selected.append(opt_idx)
                ts.selected_options = json.dumps(selected)
                await callback.answer()
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=kb_test_question(TEST_SALES_VIDEO, ts.current_question, selected))
                except Exception:
                    pass
            else:
                answers.append([opt_idx])
                ts.answers_json = json.dumps(answers)
                ts.current_question += 1
                await callback.answer()
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                if ts.current_question >= len(TEST_SALES_VIDEO):
                    cid = c.id
                    cand_r = await s.execute(select(Candidate).where(Candidate.id == cid))
                    cand = cand_r.scalar_one()
                    cand.awaiting = None
                    await _finish_video_test(cid, answers, callback.message, bot)
                else:
                    await _send_video_test_q(callback.message, ts.current_question, [])


async def _finish_video_test(cid, answers, message, bot):
    correct = sum(1 for i, q in enumerate(TEST_SALES_VIDEO)
                  if i < len(answers) and sorted(answers[i]) == sorted(q["correct"]))
    score = round(correct / len(TEST_SALES_VIDEO) * 100)
    passed = score >= SALES_PASS

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == cid, TestSession.test_number == 10))
        ts = ts_r.scalar_one_or_none()
        if ts:
            ts.is_active = False
        s.add(TestResult(candidate_id=cid, test_number=10, score_percent=score, passed=passed))
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        c.stage = 13 if passed else 12
        c.status = "active" if passed else "failed"
        c.last_activity_at = datetime.utcnow()
        full_name = c.full_name
        data = get_sales_data(c)
        anketa = data.get("anketa", {})

    if passed:
        await message.answer(SALES_TEST_PASSED.format(score=score))
        anketa_text = "\n".join(f"• {k}: {v}" for k, v in anketa.items()) or "—"
        await notify_hr(bot, HR_SALES_TEST_DONE.format(
            name=full_name, cid=cid, score=score, anketa=anketa_text))
    else:
        await message.answer(SALES_TEST_FAILED.format(score=score))


# ── После звонка РОП / собеседование ─────────────────────────
# (вызывается из hr.py командами /call_done, /invite_sales)

# ── Стажировка ────────────────────────────────────────────────
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
        await bot.send_message(tg_id, dd["materials"])
        await bot.send_message(tg_id, dd["task"])


@router.callback_query(F.data.startswith("sales_day_start_"))
async def cb_day_start(callback: CallbackQuery, bot: Bot):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    day = int(callback.data.split("_")[-1])
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_internship_day(c, day, bot)


# ── Конспект (День 2) ─────────────────────────────────────────
@router.callback_query(F.data == "sales_consp_done")
async def cb_consp_done(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.awaiting = "internship_d2_test"
        cand.internship_step = "test"
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    dd = INTERNSHIP_DAYS[2]
    await callback.message.answer("✅ Отлично! Конспект записан.\n\nТеперь пройди тест по материалам дня 👇")
    await callback.message.answer(dd["test_intro"],
                                  reply_markup=kb_start_test("itest_start_2"))


# ── Тесты стажировки (дни 1, 2, 3) ───────────────────────────
@router.callback_query(F.data.startswith("itest_start_"))
async def cb_itest_start(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return
    day = int(callback.data.split("_")[-1])
    dd = INTERNSHIP_DAYS.get(day)
    if not dd or not dd.get("has_test"):
        await callback.answer(); return

    deadline = datetime.utcnow() + timedelta(hours=2)
    test_num = 100 + day  # 101, 102, 103

    async with get_session() as s:
        await s.execute(delete(TestSession).where(
            TestSession.candidate_id == c.id, TestSession.test_number == test_num))
        s.add(TestSession(candidate_id=c.id, test_number=test_num, deadline=deadline,
                          current_question=0, answers_json="[]", selected_options="[]"))
        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.awaiting = dd["awaiting_test"]
        cand.internship_step = "test"

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    questions = dd["test_questions"]
    await _send_itest_q(callback.message, questions, 0, [])


async def _send_itest_q(message, questions, q_idx, selected):
    q = questions[q_idx]
    is_multi = q["type"] == "multi"
    prefix = f"<b>Вопрос {q_idx+1}/{len(questions)}</b>"
    if is_multi:
        prefix += "\n<i>(выберите все верные)</i>"
    # Используем отдельный префикс callback для тестов стажировки
    rows = []
    for i, opt in enumerate(q["options"]):
        mark = "✅ " if i in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{opt}",
            callback_data=f"itq_{q_idx}_{i}"
        )])
    if is_multi and selected:
        rows.append([InlineKeyboardButton(
            text="✔️ Подтвердить", callback_data=f"itq_confirm_{q_idx}")])
    await message.answer(f"{prefix}\n\n{q['text']}",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("itq_"))
async def cb_itest_answer(callback: CallbackQuery, bot: Bot):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer(); return

    parts = callback.data.split("_")
    is_confirm = parts[1] == "confirm"
    q_idx = int(parts[2]) if is_confirm else int(parts[1])
    opt_idx = None if is_confirm else int(parts[2])

    # Определяем день по awaiting
    day = c.internship_day
    dd = INTERNSHIP_DAYS.get(day)
    if not dd:
        await callback.answer(); return
    questions = dd["test_questions"]
    test_num = 100 + day

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == c.id, TestSession.test_number == test_num,
            TestSession.is_active == True))
        ts = ts_r.scalar_one_or_none()
        if not ts:
            await callback.answer("Сессия не найдена."); return

        answers = json.loads(ts.answers_json)
        selected = json.loads(ts.selected_options)
        q = questions[ts.current_question]
        is_multi = q["type"] == "multi"

        if is_confirm:
            answers.append(sorted(selected))
            ts.answers_json = json.dumps(answers)
            ts.selected_options = "[]"
            ts.current_question += 1
            await callback.answer()
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if ts.current_question >= len(questions):
                cid = c.id
                await _finish_itest(cid, day, answers, callback.message, bot)
            else:
                await _send_itest_q(callback.message, questions, ts.current_question, [])
        else:
            if is_multi:
                if opt_idx in selected:
                    selected.remove(opt_idx)
                else:
                    selected.append(opt_idx)
                ts.selected_options = json.dumps(selected)
                await callback.answer()
                rows = []
                for i, opt in enumerate(q["options"]):
                    mark = "✅ " if i in selected else ""
                    rows.append([InlineKeyboardButton(
                        text=f"{mark}{opt}", callback_data=f"itq_{ts.current_question}_{i}")])
                if selected:
                    rows.append([InlineKeyboardButton(
                        text="✔️ Подтвердить",
                        callback_data=f"itq_confirm_{ts.current_question}")])
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                except Exception:
                    pass
            else:
                answers.append([opt_idx])
                ts.answers_json = json.dumps(answers)
                ts.current_question += 1
                await callback.answer()
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                if ts.current_question >= len(questions):
                    cid = c.id
                    await _finish_itest(cid, day, answers, callback.message, bot)
                else:
                    await _send_itest_q(callback.message, questions, ts.current_question, [])


async def _finish_itest(cid, day, answers, message, bot):
    dd = INTERNSHIP_DAYS[day]
    questions = dd["test_questions"]
    correct = sum(1 for i, q in enumerate(questions)
                  if i < len(answers) and sorted(answers[i]) == sorted(q["correct"]))
    score = round(correct / len(questions) * 100)
    passed = score >= dd.get("test_pass", 70)
    test_num = 100 + day

    async with get_session() as s:
        ts_r = await s.execute(select(TestSession).where(
            TestSession.candidate_id == cid, TestSession.test_number == test_num))
        ts = ts_r.scalar_one_or_none()
        if ts:
            ts.is_active = False
        s.add(TestResult(candidate_id=cid, test_number=test_num, score_percent=score, passed=passed))
        r = await s.execute(select(Candidate).where(Candidate.id == cid))
        c = r.scalar_one()
        c.awaiting = None
        c.internship_step = "done"
        c.last_activity_at = datetime.utcnow()
        full_name = c.full_name

    await message.answer(dd["test_done_msg"].format(score=score))

    if not passed:
        await message.answer(
            f"⚠️ Результат ниже {dd['test_pass']}%. "
            f"Рекомендуем перечитать материалы дня. РОП свяжется с тобой."
        )

    await notify_hr(bot,
        f"📊 День {day} тест: <b>{full_name}</b> (#{cid}) — <b>{score}%</b> "
        f"{'✅' if passed else '❌'}\n/candidate {cid}"
    )


# ── Текстовые задания стажировки ──────────────────────────────
async def handle_sales_text(message: Message, bot: Bot):
    """Вызывается из candidate.py для кандидатов-продажников."""
    c = await get_sales_candidate(message.from_user.id)
    if not c:
        return False

    awaiting = c.awaiting or ""

    # Текстовые ответы анкеты
    for q_key, q in ANKETA_QUESTIONS.items():
        if q["type"] == "text" and q["awaiting"] == awaiting:
            await _save_anketa_and_next(c, q_key, message.text.strip(), message, bot)
            return True

    # Голосовое — День 2
    if awaiting == "internship_d2_voice":
        if message.voice or message.audio:
            # Пересылаем голосовое рекрутеру
            for hr_id in HR_TELEGRAM_IDS:
                try:
                    await message.forward(hr_id)
                except Exception:
                    pass
            async with get_session() as s:
                r = await s.execute(select(Candidate).where(Candidate.id == c.id))
                cand = r.scalar_one()
                cand.awaiting = "internship_d2_consp"
                cand.last_activity_at = datetime.utcnow()
            await message.answer(INTERNSHIP_DAYS[2]["task_done_msg"])
            await message.answer(INTERNSHIP_DAYS[2]["consp_task"], reply_markup=kb_consp_done())
            return True
        else:
            await message.answer(
                "🎤 Жду голосовое сообщение. "
                "Нажми на 🎤 микрофон в Telegram и запиши презентацию компании."
            )
            return True

    # Текстовые задания дней 1, 3, 4, 5
    day_awaiting_map = {
        "internship_d1_task": 1,
        "internship_d3_task": 3,
        "internship_d4_task": 4,
        "internship_d5_task": 5,
    }
    if awaiting in day_awaiting_map:
        day = day_awaiting_map[awaiting]
        await _handle_day_task(c, day, message, bot)
        return True

    return False


async def _handle_day_task(candidate, day, message, bot):
    dd = INTERNSHIP_DAYS[day]
    answer = message.text.strip()
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
    await notify_hr(bot, dd["hr_notify"].format(name=full_name, cid=cid, answer=answer[:600]))

    # Если есть тест — запускаем
    if dd.get("has_test"):
        await message.answer(dd["test_intro"], reply_markup=kb_start_test(f"itest_start_{day}"))
    # День 4 и 5 — просто завершаем
    elif day in (4, 5):
        if day == 5:
            async with get_session() as s:
                r = await s.execute(select(Candidate).where(Candidate.id == cid))
                c = r.scalar_one()
                c.stage = 16
                c.status = "passed"


# ── Напоминание стажёрам (планировщик) ───────────────────────
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
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(
                                text=f"▶️ Начать День {next_day}",
                                callback_data=f"sales_day_start_{next_day}"
                            )
                        ]])
                    )
                except Exception as e:
                    log.error(f"Reminder error {c.id}: {e}")
        elif next_day > 5:
            async with get_session() as s2:
                r2 = await s2.execute(select(Candidate).where(Candidate.id == c.id))
                cand = r2.scalar_one()
                cand.stage = 16
                cand.status = "passed"
