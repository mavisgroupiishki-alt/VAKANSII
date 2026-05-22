"""Работа с базой данных — async движок, фабрика сессий."""
import logging
from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from config import DATABASE_URL
from db.models import Base

log = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _run_migrations(conn) -> None:
    """Безопасные миграции для существующих БД."""
    # Получаем список колонок таблицы candidates
    result = await conn.execute(text("PRAGMA table_info(candidates)"))
    rows = result.fetchall()
    columns = {row[1] for row in rows}  # row[1] — имя колонки

    # Добавляем invite_code, если ещё нет
    if "invite_code" not in columns:
        log.info("Миграция: добавляю поле invite_code в таблицу candidates")
        await conn.execute(text("ALTER TABLE candidates ADD COLUMN invite_code VARCHAR(32)"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_candidates_invite_code ON candidates(invite_code)"))

    # Добавляем interview_slot, если ещё нет
    if "interview_slot" not in columns:
        log.info("Миграция: добавляю поле interview_slot в таблицу candidates")
        await conn.execute(text("ALTER TABLE candidates ADD COLUMN interview_slot VARCHAR(64)"))

    # Добавляем source, если ещё нет
    if "source" not in columns:
        log.info("Миграция: добавляю поле source в таблицу candidates")
        await conn.execute(text("ALTER TABLE candidates ADD COLUMN source VARCHAR(32)"))

    # Добавляем interview_reminded_at, если ещё нет
    if "interview_reminded_at" not in columns:
        log.info("Миграция: добавляю поле interview_reminded_at в таблицу candidates")
        await conn.execute(text("ALTER TABLE candidates ADD COLUMN interview_reminded_at DATETIME"))


async def init_db() -> None:
    """Создать таблицы при первом запуске + миграции."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Если таблица уже существовала — пробуем мигрировать
        await _run_migrations(conn)


@asynccontextmanager
async def get_session() -> AsyncSession:
    """Контекстный менеджер для получения сессии БД."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
