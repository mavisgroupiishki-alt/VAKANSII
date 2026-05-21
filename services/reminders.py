"""Сервис напоминаний — проверяет кандидатов каждый час."""
from datetime import datetime, timedelta
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import REMINDER_INTERVAL_HOURS, MAX_REMINDERS, HR_TELEGRAM_IDS
from db.database import get_session
from db.models import Candidate, TestSession
from data.texts import REMINDER_TEXTS, NO_RESPONSE, HR_NO_RESPONSE, HR_TIME_EXPIRED

log = logging.getLogger(__name__)

# Карта awaiting → ключ текста напоминания
AWAITING_TO_REMINDER = {
    "start_button": "welcome_pending",
    "video_1_watched": "video_1_pending",
    "test_1_start": "video_1_pending",
    "test_1_in_progress": "test_1_pending",
    "motivation_answer": "motivation_pending",
    "video_2_watched": "stage_2_pending",
    "test_2_start": "stage_2_pending",
    "test_2_in_progress": "test_2_pending",
}


async def check_and_send_reminders(bot: Bot) -> None:
    """Главная функция планировщика — вызывается каждый час."""
    now = datetime.utcnow()
    threshold = now - timedelta(hours=REMINDER_INTERVAL_HOURS)

    async with get_session() as s:
        # Кандидаты, кому пора напомнить
        result = await s.execute(
            select(Candidate).where(
                Candidate.status == "active",
                Candidate.awaiting.is_not(None),
                Candidate.last_activity_at <= threshold,
                Candidate.reminder_count < MAX_REMINDERS,
            )
        )
        candidates_to_remind = result.scalars().all()

        for c in candidates_to_remind:
            reminder_key = AWAITING_TO_REMINDER.get(c.awaiting)
            if not reminder_key:
                continue
            try:
                await bot.send_message(c.telegram_id, REMINDER_TEXTS[reminder_key])
                c.reminder_count += 1
                c.last_activity_at = now  # сдвигаем, чтобы не спамить
                log.info(f"Напоминание {c.reminder_count}/{MAX_REMINDERS} отправлено: {c.full_name}")
            except Exception as e:
                log.error(f"Не удалось отправить напоминание {c.full_name}: {e}")

        # Кандидаты, исчерпавшие лимит
        result_exhausted = await s.execute(
            select(Candidate).where(
                Candidate.status == "active",
                Candidate.awaiting.is_not(None),
                Candidate.reminder_count >= MAX_REMINDERS,
                Candidate.last_activity_at <= threshold,
            )
        )
        for c in result_exhausted.scalars():
            c.status = "no_response"
            try:
                await bot.send_message(c.telegram_id, NO_RESPONSE)
            except Exception:
                pass
            # Уведомляем HR
            for hr_id in HR_TELEGRAM_IDS:
                try:
                    await bot.send_message(
                        hr_id,
                        HR_NO_RESPONSE.format(
                            name=c.full_name,
                            n=c.reminder_count,
                            stage=c.awaiting or "—"
                        ),
                    )
                except Exception:
                    pass

        # Проверяем истёкшие дедлайны тестов
        result_expired = await s.execute(
            select(TestSession).where(
                TestSession.is_active.is_(True),
                TestSession.deadline <= now,
            )
        )
        for sess in result_expired.scalars():
            sess.is_active = False
            cand_res = await s.execute(select(Candidate).where(Candidate.id == sess.candidate_id))
            cand = cand_res.scalar_one()
            if cand.status == "active":
                cand.status = "failed"
                cand.awaiting = None
                try:
                    await bot.send_message(
                        cand.telegram_id,
                        f"⏰ К сожалению, время на прохождение теста {sess.test_number} истекло. "
                        "Процесс собеседования завершён."
                    )
                except Exception:
                    pass
                for hr_id in HR_TELEGRAM_IDS:
                    try:
                        await bot.send_message(
                            hr_id,
                            HR_TIME_EXPIRED.format(name=cand.full_name, test_num=sess.test_number),
                        )
                    except Exception:
                        pass


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Запустить планировщик с проверкой каждый час."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_send_reminders,
        trigger="interval",
        hours=REMINDER_INTERVAL_HOURS,
        args=[bot],
        next_run_time=datetime.utcnow() + timedelta(minutes=1),
    )
    scheduler.start()
    log.info(f"Планировщик запущен, интервал = {REMINDER_INTERVAL_HOURS}ч")
    return scheduler
