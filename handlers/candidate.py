"""Хендлеры для кандидата — прохождение воронки."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
)
from sqlalchemy import select

from config import (
    HR_TELEGRAM_IDS, PASS_THRESHOLD_TEST_1, PASS_THRESHOLD_TEST_2,
    TEST1_TIME_LIMIT_HOURS, TEST2_TIME_LIMIT_HOURS,
)
from db.database import get_session
from db.models import Candidate, TestResult, TestSession
from data.questions import get_questions
from data.slots import INTERVIEW_SLOTS, slot_label
from data.texts import (
    WELCOME, STAGE_1_INTRO, STAGE_2_INTRO,
    TEST_1_START, TEST_2_START,
    TEST_PASSED, TEST_FAILED, INTERVIEW_PASSED,
    MOTIVATION_QUESTION, MOTIVATION_RECEIVED,
    SLOT_CHOSEN, SLOT_NO_MATCH,
    HR_STARTED, HR_TEST_PASSED, HR_TEST_FAILED, HR_INTERVIEW_PASSED,
    HR_MOTIVATION_ANSWER, HR_SLOT_CHOSEN, HR_SLOT_NO_MATCH,
)
from data.videos import VIDEO_1_PATH, VIDEO_2_PATH, MATERIALS_URL

router = Router()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def kb_start_journey() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Начать собеседование", callback_data="start_stage_1")
    ]])


def kb_watched_video_1() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я посмотрел, готов к тесту", callback_data="ready_test_1")
    ]])


def kb_start_test(test_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"📝 Начать тест {test_num}", callback_data=f"begin_test_{test_num}")
    ]])


def kb_watched_video_2() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я изучил, готов к тесту", callback_data="ready_test_2")
    ]])


def kb_interview_slots() -> InlineKeyboardMarkup:
    """Кнопки с доступными слотами + кнопка «Время не подходит»."""
    rows = []
    for key, slot in INTERVIEW_SLOTS.items():
        rows.append([InlineKeyboardButton(
            text=f"📅 {slot['label']}",
            callback_data=f"slot_{key}",
        )])
    rows.append([InlineKeyboardButton(
        text="❌ Время не подходит",
        callback_data="slot_no_match",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_question(test_num: int, q_idx: int, question: dict, selected: list[int]) -> InlineKeyboardMarkup:
    """Клавиатура для вопроса теста.

    Если вариант ответа длинный — на кнопке показываем номер + начало,
    а сам полный текст идёт в сообщении с вопросом (см. send_question).
    """
    rows = []
    options = question["options"]
    # Считаем максимальную длину опции — если все короткие, выводим как есть
    use_numbers = any(len(opt) > 50 for opt in options)

    for i, opt in enumerate(options):
        # Для multi-select показываем галочки на выбранных
        if question["type"] == "multi":
            prefix = "✅ " if i in selected else "⬜ "
        else:
            prefix = ""

        if use_numbers:
            # Длинные варианты — на кнопке только номер
            btn_text = f"{prefix}{i + 1}"
        else:
            btn_text = f"{prefix}{opt}"

        rows.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"ans_{test_num}_{q_idx}_{i}"
        )])
    # Для multi нужна кнопка "Подтвердить"
    if question["type"] == "multi":
        rows.append([InlineKeyboardButton(
            text="➡️ Подтвердить выбор", callback_data=f"confirm_{test_num}_{q_idx}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# ХЕЛПЕРЫ
# ============================================================
async def get_candidate(tg_id: int) -> Candidate | None:
    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.telegram_id == tg_id))
        return result.scalar_one_or_none()


async def update_activity(candidate_id: int, awaiting: str | None = None) -> None:
    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        c = result.scalar_one()
        c.last_activity_at = datetime.utcnow()
        c.reminder_count = 0
        if awaiting is not None:
            c.awaiting = awaiting


async def notify_hr(bot: Bot, text: str) -> None:
    """Отправить уведомление всем рекрутерам."""
    for hr_id in HR_TELEGRAM_IDS:
        try:
            await bot.send_message(hr_id, text)
        except Exception as e:
            print(f"Не удалось уведомить HR {hr_id}: {e}")


# ============================================================
# /start — кандидат заходит в бот
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    # Извлекаем параметр после /start (deep link, например /start inv_abc123)
    parts = message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    # Сначала проверяем, нет ли активной записи у этого пользователя
    candidate = await get_candidate(message.from_user.id)

    # Если есть deep-link payload и пользователь ещё не кандидат —
    # пробуем привязать его к приглашению
    if payload and not candidate:
        async with get_session() as s:
            result = await s.execute(
                select(Candidate).where(Candidate.invite_code == payload)
            )
            invited = result.scalar_one_or_none()

            if invited is None:
                await message.answer(
                    "👋 Здравствуйте! Этот бот доступен только для приглашённых кандидатов.\n\n"
                    "Ссылка-приглашение неверна или истекла. "
                    "Если вы ожидаете собеседования в Mavis Group — свяжитесь с рекрутером."
                )
                return

            if invited.telegram_id is not None:
                # Кто-то уже активировал это приглашение
                await message.answer(
                    "⚠️ Эта ссылка-приглашение уже была использована другим пользователем.\n\n"
                    "Если это ошибка — свяжитесь с рекрутером."
                )
                return

            # Привязываем Telegram-аккаунт к записи кандидата
            invited.telegram_id = message.from_user.id
            # Если HR не указал username, подтянем из Telegram
            if not invited.username and message.from_user.username:
                invited.username = message.from_user.username
            invited.last_activity_at = datetime.utcnow()
            invited.awaiting = "start_button"
            candidate_id_local = invited.id
            full_name_local = invited.full_name

        # Перечитаем кандидата свежим запросом
        candidate = await get_candidate(message.from_user.id)
        # Уведомляем HR об активации
        await notify_hr(
            bot,
            f"✅ Кандидат <b>{full_name_local}</b> (#{candidate_id_local}) активировал приглашение и начал собеседование."
        )

    if not candidate:
        # Нет ни payload, ни существующей записи — это не приглашённый
        await message.answer(
            "👋 Здравствуйте! Этот бот доступен только для приглашённых кандидатов.\n\n"
            "Если вы ожидаете собеседования в Mavis Group — свяжитесь с рекрутером, "
            "чтобы получить ссылку-приглашение."
        )
        return

    if candidate.stage == 0:
        # Первый запуск — приветствие
        async with get_session() as s:
            result = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
            c = result.scalar_one()
            c.last_activity_at = datetime.utcnow()
            c.awaiting = "start_button"

        await message.answer(
            WELCOME.format(name=candidate.full_name.split()[0]),
            reply_markup=kb_start_journey(),
        )
        await notify_hr(bot, HR_STARTED.format(name=candidate.full_name))
    elif candidate.stage == 6 or candidate.status in ("failed", "rejected", "no_response"):
        await message.answer("Процесс собеседования уже завершён. Спасибо!")
    else:
        # Возобновление
        await message.answer(
            f"С возвращением, {candidate.full_name.split()[0]}!\n\n"
            "Вы можете продолжить с того места, где остановились. "
            "Если кнопок не видно — напишите рекрутеру."
        )


# ============================================================
# ЭТАП 1: ВИДЕО О КОМПАНИИ
# ============================================================
@router.callback_query(F.data == "start_stage_1")
async def start_stage_1(callback: CallbackQuery):
    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        await callback.answer("Кандидат не найден", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(STAGE_1_INTRO)

    # Отправляем видео
    video_path = Path(VIDEO_1_PATH)
    if video_path.exists():
        await callback.message.answer_video(
            video=FSInputFile(video_path),
            caption="📹 Видео о Mavis Group",
            reply_markup=kb_watched_video_1()
        )
    else:
        await callback.message.answer(
            "⚠️ Видео временно недоступно. Уведомите рекрутера.",
            reply_markup=kb_watched_video_1()
        )

    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = result.scalar_one()
        c.stage = 1
        c.awaiting = "video_1_watched"
        c.last_activity_at = datetime.utcnow()
        c.reminder_count = 0

    await callback.answer()


@router.callback_query(F.data == "ready_test_1")
async def ready_test_1(callback: CallbackQuery):
    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    questions = get_questions(1)
    await callback.message.answer(
        TEST_1_START.format(total=len(questions)),
        reply_markup=kb_start_test(1),
    )
    await update_activity(candidate.id, awaiting="test_1_start")
    await callback.answer()


# ============================================================
# ЛОГИКА ТЕСТА (универсальная для теста 1 и 2)
# ============================================================
async def start_test(callback: CallbackQuery, test_num: int):
    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        return

    hours = TEST1_TIME_LIMIT_HOURS if test_num == 1 else TEST2_TIME_LIMIT_HOURS
    deadline = datetime.utcnow() + timedelta(hours=hours)

    async with get_session() as s:
        # Закрываем предыдущие сессии этого теста
        prev = await s.execute(
            select(TestSession).where(
                TestSession.candidate_id == candidate.id,
                TestSession.test_number == test_num,
                TestSession.is_active.is_(True)
            )
        )
        for old in prev.scalars():
            old.is_active = False

        session = TestSession(
            candidate_id=candidate.id,
            test_number=test_num,
            deadline=deadline,
            current_question=0,
            answers_json="[]",
            selected_options="[]",
            is_active=True,
        )
        s.add(session)

        result = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = result.scalar_one()
        c.awaiting = f"test_{test_num}_in_progress"
        c.last_activity_at = datetime.utcnow()
        c.reminder_count = 0

    await callback.message.edit_reply_markup(reply_markup=None)
    await send_question(callback.message, candidate.id, test_num, 0, [])
    await callback.answer()


@router.callback_query(F.data == "begin_test_1")
async def begin_test_1(callback: CallbackQuery):
    await start_test(callback, 1)


@router.callback_query(F.data == "begin_test_2")
async def begin_test_2(callback: CallbackQuery):
    await start_test(callback, 2)


async def send_question(message: Message, candidate_id: int, test_num: int, q_idx: int, selected: list[int]):
    """Отправляет очередной вопрос теста."""
    questions = get_questions(test_num)
    if q_idx >= len(questions):
        await finish_test(message, candidate_id, test_num)
        return

    question = questions[q_idx]
    options = question["options"]
    use_numbers = any(len(opt) > 50 for opt in options)

    text = (
        f"<b>Вопрос {q_idx + 1} из {len(questions)}</b>\n\n"
        f"{question['text']}"
    )

    # Если опции длинные — выводим их нумерованным списком в тексте
    if use_numbers:
        text += "\n\n<b>Варианты ответа:</b>"
        for i, opt in enumerate(options):
            text += f"\n<b>{i + 1}.</b> {opt}"
        text += "\n\n<i>Нажмите кнопку с номером ответа ниже 👇</i>"

    if question["type"] == "multi":
        if use_numbers:
            text += "\n<i>Можно выбрать несколько. После выбора нажмите «Подтвердить выбор».</i>"
        else:
            text += "\n\n<i>Выберите все подходящие варианты и нажмите «Подтвердить выбор».</i>"

    await message.answer(
        text,
        reply_markup=kb_question(test_num, q_idx, question, selected),
    )


@router.callback_query(F.data.startswith("ans_"))
async def handle_answer(callback: CallbackQuery):
    """Обработка нажатия на вариант ответа."""
    _, test_num_s, q_idx_s, opt_s = callback.data.split("_")
    test_num, q_idx, opt = int(test_num_s), int(q_idx_s), int(opt_s)

    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        return

    questions = get_questions(test_num)
    question = questions[q_idx]

    async with get_session() as s:
        result = await s.execute(
            select(TestSession).where(
                TestSession.candidate_id == candidate.id,
                TestSession.test_number == test_num,
                TestSession.is_active.is_(True)
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            await callback.answer("Сессия теста не найдена", show_alert=True)
            return

        # Проверка времени
        if datetime.utcnow() > session.deadline:
            session.is_active = False
            await callback.message.answer("⏰ Время на прохождение теста истекло.")
            await callback.answer()
            return

        answers = json.loads(session.answers_json)
        selected = json.loads(session.selected_options)

        if question["type"] == "single":
            # Сразу записываем ответ и переходим к следующему вопросу
            answers.append([opt])
            session.answers_json = json.dumps(answers)
            session.selected_options = "[]"
            session.current_question = q_idx + 1
            next_idx = q_idx + 1
            next_selected = []
            move_on = True
        else:
            # Multi: переключаем выбор
            if opt in selected:
                selected.remove(opt)
            else:
                selected.append(opt)
            session.selected_options = json.dumps(selected)
            next_idx = q_idx
            next_selected = selected
            move_on = False

        candidate_db = (await s.execute(select(Candidate).where(Candidate.id == candidate.id))).scalar_one()
        candidate_db.last_activity_at = datetime.utcnow()
        candidate_db.reminder_count = 0

    # Удаляем клавиатуру с предыдущего вопроса (только для single)
    if move_on:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_question(callback.message, candidate.id, test_num, next_idx, next_selected)
    else:
        # Обновляем клавиатуру с галочками
        try:
            await callback.message.edit_reply_markup(
                reply_markup=kb_question(test_num, q_idx, question, next_selected)
            )
        except Exception:
            pass

    await callback.answer()


@router.callback_query(F.data.startswith("confirm_"))
async def confirm_multi(callback: CallbackQuery):
    """Подтверждение multi-select ответа."""
    _, test_num_s, q_idx_s = callback.data.split("_")
    test_num, q_idx = int(test_num_s), int(q_idx_s)

    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        return

    async with get_session() as s:
        result = await s.execute(
            select(TestSession).where(
                TestSession.candidate_id == candidate.id,
                TestSession.test_number == test_num,
                TestSession.is_active.is_(True)
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            return

        selected = json.loads(session.selected_options)
        if not selected:
            await callback.answer("Выберите хотя бы один вариант", show_alert=True)
            return

        answers = json.loads(session.answers_json)
        answers.append(sorted(selected))
        session.answers_json = json.dumps(answers)
        session.selected_options = "[]"
        session.current_question = q_idx + 1

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_question(callback.message, candidate.id, test_num, q_idx + 1, [])
    await callback.answer()


async def finish_test(message: Message, candidate_id: int, test_num: int):
    """Завершение теста: подсчёт результата, сохранение, переход дальше."""
    bot = message.bot
    questions = get_questions(test_num)

    async with get_session() as s:
        result = await s.execute(
            select(TestSession).where(
                TestSession.candidate_id == candidate_id,
                TestSession.test_number == test_num,
                TestSession.is_active.is_(True)
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            return

        answers = json.loads(session.answers_json)

        # Подсчёт
        correct_count = 0
        for i, q in enumerate(questions):
            if i >= len(answers):
                break
            given = sorted(answers[i])
            correct = sorted(q["correct"])
            if given == correct:
                correct_count += 1

        score = round(correct_count / len(questions) * 100, 1)
        threshold = PASS_THRESHOLD_TEST_1 if test_num == 1 else PASS_THRESHOLD_TEST_2
        passed = score >= threshold

        session.is_active = False

        # Сохраняем результат
        tr = TestResult(
            candidate_id=candidate_id,
            test_number=test_num,
            score_percent=score,
            passed=passed,
        )
        s.add(tr)

        candidate = (await s.execute(select(Candidate).where(Candidate.id == candidate_id))).scalar_one()

        if passed:
            if test_num == 1:
                candidate.stage = 2  # Переход к мотивационному вопросу
                candidate.awaiting = "motivation_answer"
            else:
                candidate.stage = 4  # Прошёл всё
                candidate.status = "passed"
                candidate.awaiting = None
        else:
            candidate.status = "failed"
            candidate.awaiting = None

        candidate.last_activity_at = datetime.utcnow()
        candidate.reminder_count = 0
        full_name = candidate.full_name

    # Сообщение кандидату
    if passed:
        await message.answer(TEST_PASSED.format(score=score))
    else:
        await message.answer(TEST_FAILED.format(score=score))

    # Уведомление HR
    if passed:
        await notify_hr(bot, HR_TEST_PASSED.format(
            name=full_name, test_num=test_num, score=score,
            stage=("Тест 1 пройден, ждём мотивацию" if test_num == 1 else "Оба теста пройдены")
        ))
    else:
        await notify_hr(bot, HR_TEST_FAILED.format(
            name=full_name, test_num=test_num, score=score
        ))

    # Дальнейшие шаги
    if passed and test_num == 1:
        # Спрашиваем мотивационный вопрос
        await message.answer(MOTIVATION_QUESTION)
    elif passed and test_num == 2:
        # Финал тестовой части — предлагаем выбрать время собеседования
        await message.answer(
            INTERVIEW_PASSED,
            reply_markup=kb_interview_slots(),
        )
        await notify_hr(bot, HR_INTERVIEW_PASSED.format(
            name=full_name, candidate_id=candidate_id
        ))
        # Обновляем awaiting, чтобы напоминания при необходимости работали
        async with get_session() as s:
            cand = (await s.execute(select(Candidate).where(Candidate.id == candidate_id))).scalar_one()
            cand.awaiting = "slot_pick"


# ============================================================
# МОТИВАЦИОННЫЙ ВОПРОС (между тестами)
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_messages(message: Message, bot: Bot):
    """Универсальный обработчик для свободного текста кандидата.

    Срабатывает только для тех, кто в базе и от кого ожидается текстовый ввод:
    - motivation_answer — мотивационный ответ после теста 1
    - phone_correction — корректный телефон / комментарий после «Время не подходит»
    """
    candidate = await get_candidate(message.from_user.id)
    if not candidate:
        return

    # --- Мотивационный ответ ---
    if candidate.awaiting == "motivation_answer":
        answer = message.text.strip()
        if len(answer) < 10:
            await message.answer("Пожалуйста, напишите более развёрнутый ответ (хотя бы пару предложений).")
            return

        async with get_session() as s:
            result = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
            c = result.scalar_one()
            c.motivation_answer = answer
            c.stage = 3  # Готов к видео 2
            c.awaiting = "video_2_watched"
            c.last_activity_at = datetime.utcnow()
            c.reminder_count = 0
            full_name = c.full_name

        await message.answer(MOTIVATION_RECEIVED)
        await notify_hr(bot, HR_MOTIVATION_ANSWER.format(name=full_name, answer=answer))

        # Запускаем этап 2
        await message.answer(
            STAGE_2_INTRO.format(materials_url=MATERIALS_URL),
            disable_web_page_preview=False,
        )
        video_2 = Path(VIDEO_2_PATH)
        if video_2.exists():
            await message.answer_video(
                video=FSInputFile(video_2),
                caption="📹 Видео о продуктах Mavis Group",
                reply_markup=kb_watched_video_2(),
            )
        else:
            await message.answer(
                "⚠️ Видео временно недоступно. Уведомите рекрутера.",
                reply_markup=kb_watched_video_2(),
            )
        return

    # --- Коррекция контактов после «Время не подходит» ---
    if candidate.awaiting == "phone_correction":
        text = message.text.strip()

        # Если в сообщении похоже на телефон — обновляем поле phone
        digits = "".join(ch for ch in text if ch.isdigit())
        is_phone_like = len(digits) >= 9  # минимум 9 цифр = похож на телефон

        async with get_session() as s:
            result = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
            c = result.scalar_one()
            old_phone = c.phone
            if is_phone_like:
                # Сохраняем как новый телефон (с исходным форматированием)
                c.phone = text[:32]
            c.last_activity_at = datetime.utcnow()
            c.awaiting = None  # больше ничего не ждём — рекрутер сам свяжется
            full_name = c.full_name
            username = c.username or "—"
            new_phone = c.phone or "—"

        # Подтверждение кандидату
        if is_phone_like:
            await message.answer(
                f"✅ Спасибо! Записали ваш контакт: <b>{text}</b>\n\n"
                f"Рекрутер свяжется с вами в ближайшее время для подбора удобного времени."
            )
        else:
            await message.answer(
                "✅ Спасибо! Передали ваше сообщение рекрутеру. "
                "Он свяжется с вами в ближайшее время."
            )

        # Уведомляем HR
        hr_msg = (
            f"📝 Кандидат <b>{full_name}</b> прислал сообщение после «Время не подходит»:\n\n"
            f"<i>«{text}»</i>\n\n"
        )
        if is_phone_like and text != old_phone:
            hr_msg += f"📱 <b>Телефон обновлён:</b> {old_phone or '—'} → <b>{new_phone}</b>\n"
        hr_msg += f"Telegram: @{username}"
        await notify_hr(bot, hr_msg)
        return

    # В остальных случаях молчим (например, если кандидат пишет произвольный текст
    # без ожидания — это нормально, бот не должен спамить)


# ============================================================
# ЭТАП 2: ВИДЕО О ПРОДУКТАХ → ТЕСТ 2
# ============================================================
@router.callback_query(F.data == "ready_test_2")
async def ready_test_2(callback: CallbackQuery):
    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    questions = get_questions(2)
    await callback.message.answer(
        TEST_2_START.format(total=len(questions)),
        reply_markup=kb_start_test(2),
    )
    await update_activity(candidate.id, awaiting="test_2_start")
    await callback.answer()


# ============================================================
# ВЫБОР СЛОТА СОБЕСЕДОВАНИЯ
# ============================================================
@router.callback_query(F.data.startswith("slot_"))
async def handle_slot_choice(callback: CallbackQuery, bot: Bot):
    """Обработка выбора слота или нажатия «Время не подходит»."""
    candidate = await get_candidate(callback.from_user.id)
    if not candidate:
        await callback.answer("Кандидат не найден", show_alert=True)
        return

    # Если кандидат уже выбрал — не даём выбрать второй раз
    if candidate.interview_slot:
        await callback.answer("Вы уже сделали выбор. Если нужно изменить — напишите рекрутеру.", show_alert=True)
        return

    payload = callback.data[len("slot_"):]  # "mon_12", "wed_09", "fri_15" или "no_match"

    is_no_match = payload == "no_match"

    # Валидация — слот должен существовать в списке (или это no_match)
    if not is_no_match and payload not in INTERVIEW_SLOTS:
        await callback.answer("Этот слот недоступен.", show_alert=True)
        return

    # Сохраняем выбор
    async with get_session() as s:
        result = await s.execute(select(Candidate).where(Candidate.id == candidate.id))
        c = result.scalar_one()
        c.interview_slot = payload
        c.stage = 5  # на этапе кейсов
        # Если "не подходит" — ждём ответ кандидата (с корректным телефоном или комментарием)
        c.awaiting = "phone_correction" if is_no_match else None
        c.last_activity_at = datetime.utcnow()
        c.reminder_count = 0
        full_name = c.full_name
        phone = c.phone or "—"
        username = c.username or "—"
        candidate_id = c.id

    # Убираем клавиатуру
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if is_no_match:
        # Кандидат не может в предложенное время
        await callback.message.answer(SLOT_NO_MATCH.format(phone=phone))
        await notify_hr(bot, HR_SLOT_NO_MATCH.format(
            name=full_name,
            candidate_id=candidate_id,
            username=username,
            phone=phone,
        ))
    else:
        # Кандидат выбрал слот
        label = slot_label(payload)
        await callback.message.answer(SLOT_CHOSEN.format(slot_label=label))
        await notify_hr(bot, HR_SLOT_CHOSEN.format(
            name=full_name,
            candidate_id=candidate_id,
            slot_label=label,
            username=username,
            phone=phone,
        ))

    await callback.answer()
