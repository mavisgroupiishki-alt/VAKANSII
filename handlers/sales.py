"""Воронка кандидата на позицию «Менеджер по продажам»."""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select

from config import HR_TELEGRAM_IDS
from db.database import get_session
from db.models import Candidate, TestSession, TestResult
from data.sales_flow import (
    SALES_WELCOME, ANKETA_QUESTIONS, ANKETA_ORDER, ANKETA_DONE,
    SALES_VIDEO_CAPTION, SALES_TEST_INTRO, TEST_SALES_QUESTIONS,
    SALES_TEST_PASSED, SALES_TEST_FAILED, HR_SALES_TEST_DONE,
    INTERNSHIP_WELCOME, INTERNSHIP_DAYS, INTERNSHIP_DAY_REMINDER,
    POSITION_LABELS,
)
from data.videos import VIDEO_1_PATH

log = logging.getLogger(__name__)
router = Router()

# Проходной балл для теста продажника
SALES_PASS_THRESHOLD = 60


# ── Вспомогательные ──────────────────────────────────────────────────────────
async def get_sales_candidate(tg_id: int) -> Candidate | None:
    async with get_session() as s:
        r = await s.execute(
            select(Candidate).where(
                Candidate.telegram_id == tg_id,
                Candidate.position == "sales",
            )
        )
        return r.scalar_one_or_none()


async def notify_hr(bot: Bot, text: str):
    for hr_id in HR_TELEGRAM_IDS:
        try:
            await bot.send_message(hr_id, text)
        except Exception as e:
            log.error(f"Не удалось уведомить HR {hr_id}: {e}")


def get_sales_data(candidate: Candidate) -> dict:
    """Парсим JSON поля sales_data."""
    if candidate.sales_data:
        try:
            return json.loads(candidate.sales_data)
        except Exception:
            pass
    return {}


async def save_sales_data(candidate_id: int, data: dict):
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        c = r.scalar_one()
        c.sales_data = json.dumps(data, ensure_ascii=False)


def kb_start_sales() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Заполнить анкету", callback_data="sales_start_anketa")
    ]])


def kb_choice(options: list[str], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=opt, callback_data=f"{prefix}_{i}")]
            for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_start_test() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Начать тест", callback_data="sales_test_start")
    ]])


def kb_video_watched() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Посмотрел, готов к тесту", callback_data="sales_video_watched")
    ]])


def kb_internship_start(day: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"▶️ Начать День {day}", callback_data=f"sales_day_start_{day}")
    ]])


def kb_answer_option(options: list[str], q_idx: int, is_multi: bool) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options):
        rows.append([InlineKeyboardButton(
            text=opt,
            callback_data=f"sales_ans_{q_idx}_{i}_{'m' if is_multi else 's'}"
        )])
    if is_multi:
        rows.append([InlineKeyboardButton(text="✅ Подтвердить ответ", callback_data=f"sales_ans_confirm_{q_idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Старт воронки (вызывается из candidate.py) ───────────────────────────────
async def start_sales_flow(candidate: Candidate, message: Message, bot: Bot):
    """Запускает воронку продажника — отправляет приветствие."""
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.stage = 10
        c.awaiting = None
        c.last_activity_at = datetime.utcnow()

    await message.answer(SALES_WELCOME, reply_markup=kb_start_sales())


# ── Анкета ───────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "sales_start_anketa")
async def cb_start_anketa(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer()
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Начинаем с первого вопроса
    await send_anketa_question(callback.message, c, "q1")


async def send_anketa_question(message: Message, candidate: Candidate, q_key: str):
    """Отправляет следующий вопрос анкеты."""
    q = ANKETA_QUESTIONS[q_key]

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.awaiting = q["awaiting"]
        c.last_activity_at = datetime.utcnow()

    if q["type"] == "choice":
        await message.answer(
            q["text"],
            reply_markup=kb_choice(q["options"], f"anketa_{q_key}"),
        )
    else:
        await message.answer(q["text"])


@router.callback_query(F.data.startswith("anketa_q"))
async def cb_anketa_choice(callback: CallbackQuery, bot: Bot):
    """Обработка кнопочного ответа анкеты."""
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer()
        return

    # Парсим callback: anketa_q4_1 → q_key=q4, idx=1
    parts = callback.data.split("_")
    q_key = parts[1]  # q4, q6, q7
    opt_idx = int(parts[2])

    q = ANKETA_QUESTIONS[q_key]
    answer_text = q["options"][opt_idx]

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await save_anketa_answer(c, q_key, answer_text, callback.message, bot)


async def save_anketa_answer(candidate: Candidate, q_key: str, answer: str,
                              message: Message, bot: Bot):
    """Сохраняет ответ и переходит к следующему вопросу."""
    data = get_sales_data(candidate)
    if "anketa" not in data:
        data["anketa"] = {}
    data["anketa"][q_key] = answer
    await save_sales_data(candidate.id, data)

    # Следующий вопрос
    order = ANKETA_ORDER
    idx = order.index(q_key)
    if idx + 1 < len(order):
        next_q = order[idx + 1]
        async with get_session() as s:
            r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
            c = r.scalar_one()
        await send_anketa_question(message, c, next_q)
    else:
        # Анкета завершена — переходим к видео
        await finish_anketa(candidate, message, bot)


async def finish_anketa(candidate: Candidate, message: Message, bot: Bot):
    """Анкета заполнена — отправляем видео о компании."""
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.stage = 11
        c.awaiting = "sales_video"
        c.last_activity_at = datetime.utcnow()

    await message.answer(ANKETA_DONE)

    # Отправляем видео
    from aiogram.types import FSInputFile
    video_path = Path(VIDEO_1_PATH)
    if video_path.exists():
        await message.answer_video(
            video=FSInputFile(video_path),
            caption=SALES_VIDEO_CAPTION,
            reply_markup=kb_video_watched(),
        )
    else:
        await message.answer(
            "⚠️ Видео временно недоступно. Нажми кнопку ниже чтобы перейти к тесту.",
            reply_markup=kb_video_watched(),
        )


# Обработка текстовых ответов анкеты
@router.message(F.text & ~F.text.startswith("/"))
async def handle_sales_text(message: Message, bot: Bot):
    c = await get_sales_candidate(message.from_user.id)
    if not c:
        return

    awaiting = c.awaiting

    # Ответы на текстовые вопросы анкеты
    for q_key, q in ANKETA_QUESTIONS.items():
        if q["type"] == "text" and q["awaiting"] == awaiting:
            await save_anketa_answer(c, q_key, message.text.strip(), message, bot)
            return

    # Задание стажировки
    if awaiting and awaiting.startswith("internship_d"):
        await handle_internship_task(c, message, bot)
        return


# ── Видео просмотрено ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "sales_video_watched")
async def cb_video_watched(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer()
        return

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

    await callback.message.answer(SALES_TEST_INTRO, reply_markup=kb_start_test())


# ── Тест по видео ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "sales_test_start")
async def cb_sales_test_start(callback: CallbackQuery):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer()
        return

    # Создаём сессию теста
    deadline = datetime.utcnow() + timedelta(minutes=30)
    async with get_session() as s:
        # Удаляем старые сессии
        from sqlalchemy import delete
        await s.execute(
            delete(TestSession).where(
                TestSession.candidate_id == c.id,
                TestSession.test_number == 10,
            )
        )
        session = TestSession(
            candidate_id=c.id,
            test_number=10,
            deadline=deadline,
            current_question=0,
            answers_json="[]",
            selected_options="[]",
        )
        s.add(session)

        r = await s.execute(select(Candidate).where(Candidate.id == c.id))
        cand = r.scalar_one()
        cand.awaiting = "sales_test_q0"
        cand.last_activity_at = datetime.utcnow()

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_sales_test_question(callback.message, 0, [])


async def send_sales_test_question(message: Message, q_idx: int, selected: list[int]):
    q = TEST_SALES_QUESTIONS[q_idx]
    is_multi = q["type"] == "multi"

    # Формируем клавиатуру с отметками выбранного
    rows = []
    for i, opt in enumerate(q["options"]):
        mark = "✅ " if i in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{opt}",
            callback_data=f"stq_{q_idx}_{i}"
        )])
    if is_multi and selected:
        rows.append([InlineKeyboardButton(
            text="✔️ Подтвердить",
            callback_data=f"stq_confirm_{q_idx}"
        )])

    prefix = f"<b>Вопрос {q_idx + 1}/{len(TEST_SALES_QUESTIONS)}</b>\n"
    if is_multi:
        prefix += "<i>(выберите все верные варианты)</i>\n"
    prefix += f"\n{q['text']}"

    await message.answer(prefix, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("stq_"))
async def cb_sales_test_answer(callback: CallbackQuery, bot: Bot):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer()
        return

    parts = callback.data.split("_")
    action = parts[1]

    async with get_session() as s:
        ts_r = await s.execute(
            select(TestSession).where(
                TestSession.candidate_id == c.id,
                TestSession.test_number == 10,
                TestSession.is_active == True,
            )
        )
        ts = ts_r.scalar_one_or_none()
        if not ts:
            await callback.answer("Сессия не найдена.")
            return

        # Проверяем дедлайн
        if datetime.utcnow() > ts.deadline:
            ts.is_active = False
            await callback.answer("⏰ Время на тест истекло!")
            await callback.message.answer("⏰ К сожалению, время вышло. Свяжитесь с рекрутером.")
            return

        answers = json.loads(ts.answers_json)
        selected = json.loads(ts.selected_options)
        q_idx = ts.current_question
        q = TEST_SALES_QUESTIONS[q_idx]
        is_multi = q["type"] == "multi"

        if action == "confirm":
            # Подтверждение multi-select
            answers.append(sorted(selected))
            ts.answers_json = json.dumps(answers)
            ts.selected_options = "[]"
            ts.current_question = q_idx + 1

            await callback.answer()
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            if q_idx + 1 >= len(TEST_SALES_QUESTIONS):
                await finish_sales_test(c.id, answers, callback.message, bot)
            else:
                await send_sales_test_question(callback.message, q_idx + 1, [])

        else:
            # Выбор варианта
            opt_idx = int(parts[2])

            if is_multi:
                if opt_idx in selected:
                    selected.remove(opt_idx)
                else:
                    selected.append(opt_idx)
                ts.selected_options = json.dumps(selected)

                # Обновляем клавиатуру
                await callback.answer()
                rows = []
                for i, opt in enumerate(q["options"]):
                    mark = "✅ " if i in selected else ""
                    rows.append([InlineKeyboardButton(
                        text=f"{mark}{opt}",
                        callback_data=f"stq_{q_idx}_{i}"
                    )])
                if selected:
                    rows.append([InlineKeyboardButton(
                        text="✔️ Подтвердить",
                        callback_data=f"stq_confirm_{q_idx}"
                    )])
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
                    )
                except Exception:
                    pass
            else:
                # Single — сразу фиксируем
                answers.append([opt_idx])
                ts.answers_json = json.dumps(answers)
                ts.current_question = q_idx + 1

                await callback.answer()
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass

                if q_idx + 1 >= len(TEST_SALES_QUESTIONS):
                    await finish_sales_test(c.id, answers, callback.message, bot)
                else:
                    await send_sales_test_question(callback.message, q_idx + 1, [])


async def finish_sales_test(candidate_id: int, answers: list, message: Message, bot: Bot):
    """Подсчёт результата и переход к следующему этапу."""
    # Считаем результат
    correct = 0
    for i, q in enumerate(TEST_SALES_QUESTIONS):
        if i < len(answers):
            if sorted(answers[i]) == sorted(q["correct"]):
                correct += 1
    score = round(correct / len(TEST_SALES_QUESTIONS) * 100)
    passed = score >= SALES_PASS_THRESHOLD

    async with get_session() as s:
        # Закрываем сессию
        ts_r = await s.execute(
            select(TestSession).where(
                TestSession.candidate_id == candidate_id,
                TestSession.test_number == 10,
            )
        )
        ts = ts_r.scalar_one_or_none()
        if ts:
            ts.is_active = False

        # Сохраняем результат
        tr = TestResult(
            candidate_id=candidate_id,
            test_number=10,
            score_percent=score,
            passed=passed,
        )
        s.add(tr)

        # Обновляем кандидата
        r = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        c = r.scalar_one()
        c.last_activity_at = datetime.utcnow()
        c.awaiting = None

        if passed:
            c.stage = 13  # ждёт звонка РОП
            c.status = "active"
        else:
            c.stage = 12
            c.status = "failed"

        full_name = c.full_name
        cid = c.id
        sales_data = get_sales_data(c)
        anketa = sales_data.get("anketa", {})

    # Сообщение кандидату
    if passed:
        await message.answer(SALES_TEST_PASSED.format(score=score))
    else:
        await message.answer(SALES_TEST_FAILED.format(score=score))

    # Уведомление HR
    if passed:
        anketa_text = "\n".join([
            f"• {k}: {v}" for k, v in anketa.items()
        ]) or "—"
        await notify_hr(bot, HR_SALES_TEST_DONE.format(
            name=full_name, cid=cid, score=score, anketa=anketa_text
        ))


# ── Стажировка ───────────────────────────────────────────────────────────────
async def start_internship(candidate_id: int, bot: Bot):
    """HR запускает стажировку — вызывается командой /start_internship ID из hr.py."""
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        c = r.scalar_one_or_none()
        if not c or c.position != "sales":
            return False

        c.stage = 15
        c.internship_day = 0
        c.awaiting = None
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id
        name = c.full_name

    if tg_id:
        await bot.send_message(tg_id, INTERNSHIP_WELCOME)

    return True


async def send_internship_day(candidate: Candidate, day: int, bot: Bot):
    """Отправляет задание конкретного дня стажировки."""
    if day not in INTERNSHIP_DAYS:
        return

    day_data = INTERNSHIP_DAYS[day]

    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        c.internship_day = day
        c.internship_step = "intro"
        c.awaiting = day_data["awaiting"]
        c.last_activity_at = datetime.utcnow()
        tg_id = c.telegram_id

    if tg_id:
        # Вступление дня
        await bot.send_message(tg_id, day_data["intro"])
        # Задание
        await bot.send_message(tg_id, day_data["task"])


@router.callback_query(F.data.startswith("sales_day_start_"))
async def cb_day_start(callback: CallbackQuery, bot: Bot):
    c = await get_sales_candidate(callback.from_user.id)
    if not c:
        await callback.answer()
        return

    day = int(callback.data.split("_")[-1])
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_internship_day(c, day, bot)


async def handle_internship_task(candidate: Candidate, message: Message, bot: Bot):
    """Обрабатывает текстовый ответ на задание стажировки."""
    day = candidate.internship_day
    if day not in INTERNSHIP_DAYS:
        return

    day_data = INTERNSHIP_DAYS[day]
    answer = message.text.strip()

    if len(answer) < 20:
        await message.answer(
            "Ответ слишком короткий. Постарайся написать развёрнуто — минимум несколько предложений."
        )
        return

    # Сохраняем ответ
    async with get_session() as s:
        r = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = r.scalar_one()
        data = get_sales_data(c)
        if "internship" not in data:
            data["internship"] = {}
        data["internship"][f"day{day}"] = answer
        c.sales_data = json.dumps(data, ensure_ascii=False)
        c.awaiting = None
        c.internship_step = "done"
        c.last_activity_at = datetime.utcnow()
        full_name = c.full_name
        cid = c.id

    # Сообщение кандидату
    await message.answer(day_data["done_msg"])

    # Уведомление HR
    await notify_hr(bot, day_data["hr_notify"].format(
        name=full_name, cid=cid, answer=answer[:800]
    ))


# ── Напоминание о следующем дне (вызывается из планировщика) ─────────────────
async def remind_internship_next_day(bot: Bot):
    """Каждое утро в 9:00 шлёт задание следующего дня стажерам."""
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
            day_data = INTERNSHIP_DAYS[next_day]
            if c.telegram_id:
                try:
                    await bot.send_message(
                        c.telegram_id,
                        INTERNSHIP_DAY_REMINDER.format(title=day_data["title"]),
                        reply_markup=kb_internship_start(next_day),
                    )
                except Exception as e:
                    log.error(f"Ошибка напоминания стажеру {c.id}: {e}")
        elif next_day > 5:
            # Стажировка завершена — переводим в финал
            async with get_session() as s:
                r = await s.execute(select(Candidate).where(Candidate.id == c.id))
                cand = r.scalar_one()
                cand.stage = 16
                cand.status = "passed"
