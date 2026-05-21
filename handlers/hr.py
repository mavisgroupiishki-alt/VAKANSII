"""Хендлеры для рекрутера — админ-команды."""
import csv
import io
import secrets
import os
from datetime import datetime

from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, BufferedInputFile
from sqlalchemy import select, func

from config import is_hr
from db.database import get_session
from db.models import Candidate, TestResult
from data.texts import CASES_TEMPLATE, HR_NEW_CANDIDATE_ADDED

router = Router()


# ============================================================
# FSM ДЛЯ ДОБАВЛЕНИЯ КАНДИДАТА
# ============================================================
class AddCandidate(StatesGroup):
    waiting_full_name = State()
    waiting_phone = State()
    waiting_username = State()


def _gen_invite_code() -> str:
    """Генерирует уникальный короткий код приглашения."""
    return secrets.token_urlsafe(8).replace("-", "a").replace("_", "b")[:10]


# ============================================================
# /help — список команд для HR
# ============================================================
@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_hr(message.from_user.id):
        return
    text = (
        "<b>🛠 Команды рекрутера</b>\n\n"
        "/add — добавить кандидата (выдаст ссылку-приглашение)\n"
        "/list — список всех кандидатов\n"
        "/active — только активные\n"
        "/candidate ID — карточка кандидата\n"
        "/invite ID — показать ссылку-приглашение ещё раз\n"
        "/cases ID — кейсы для собеседования\n"
        "/slots — расписание собеседований (кто на какое время записался)\n"
        "/set_status ID статус — изменить статус\n"
        "  Статусы: active, passed, failed, on_pause, no_response, offer_sent, rejected\n"
        "/notify ID текст — отправить сообщение кандидату\n"
        "/stats — статистика по воронке\n"
        "/export — выгрузить CSV\n"
        "/myid — мой Telegram ID"
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
async def add_username(message: Message, state: FSMContext, bot: Bot):
    username = message.text.strip().lstrip("@")
    if username == "-":
        username = None
    data = await state.get_data()

    # Генерируем уникальный код приглашения
    invite_code = _gen_invite_code()

    async with get_session() as s:
        # Защита от коллизий — крайне маловероятно, но мало ли
        for _ in range(5):
            check = await s.execute(select(Candidate).where(Candidate.invite_code == invite_code))
            if check.scalar_one_or_none() is None:
                break
            invite_code = _gen_invite_code()

        candidate = Candidate(
            telegram_id=None,  # Заполнится когда кандидат кликнет ссылку
            full_name=data["full_name"],
            username=username,
            phone=data.get("phone"),
            invite_code=invite_code,
            stage=0,
            status="active",
            added_by=message.from_user.id,
            awaiting="invite_pending",
        )
        s.add(candidate)
        await s.flush()
        cid = candidate.id

    await state.clear()

    # Получаем username бота для формирования ссылки
    bot_info = await bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={invite_code}"

    await message.answer(
        f"✅ Кандидат <b>{data['full_name']}</b> добавлен (#{cid}).\n\n"
        f"<b>📨 Ссылка-приглашение:</b>\n"
        f"<code>{invite_link}</code>\n\n"
        f"<b>Отправьте эту ссылку кандидату</b> любым удобным способом:\n"
        f"• Telegram (если знаете username)\n"
        f"• WhatsApp / Viber\n"
        f"• SMS на номер {data.get('phone') or '—'}\n"
        f"• E-mail\n\n"
        f"Когда кандидат кликнет ссылку — бот сразу начнёт собеседование и пришлёт вам уведомление."
    )


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
        f"Телефон: {c.phone or '—'}\n\n"
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
        "ID", "ФИО", "Telegram ID", "Username", "Телефон",
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
