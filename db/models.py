"""Модели базы данных."""
from datetime import datetime
from sqlalchemy import String, Integer, BigInteger, Float, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # telegram_id заполняется когда кандидат кликает по ссылке-приглашению
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Уникальный код для ссылки-приглашения (t.me/bot?start=inv_XXX)
    invite_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)

    # Воронка:
    # 0 = добавлен, не стартовал
    # 1 = смотрит видео 1 / проходит тест 1
    # 2 = отвечает на мотивационный вопрос
    # 3 = смотрит видео 2 / проходит тест 2
    # 4 = прошёл оба теста, ждёт кейсов
    # 5 = на этапе кейсов
    # 6 = финал (оффер/отказ)
    stage: Mapped[int] = mapped_column(Integer, default=0)

    # active / passed / failed / on_pause / no_response / offer_sent / rejected
    status: Mapped[str] = mapped_column(String(32), default="active")

    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Для напоминаний
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    # Какое действие сейчас ожидается от кандидата (для текста напоминания)
    awaiting: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Мотивационный ответ
    motivation_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    test_results: Mapped[list["TestResult"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    test_sessions: Mapped[list["TestSession"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class TestResult(Base):
    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    test_number: Mapped[int] = mapped_column(Integer)
    score_percent: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidate: Mapped["Candidate"] = relationship(back_populates="test_results")


class TestSession(Base):
    """Активная сессия прохождения теста — для отслеживания таймера и текущего вопроса."""
    __tablename__ = "test_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    test_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deadline: Mapped[datetime] = mapped_column(DateTime)
    current_question: Mapped[int] = mapped_column(Integer, default=0)
    # JSON-строка со списком ответов кандидата по индексам вопросов
    answers_json: Mapped[str] = mapped_column(Text, default="[]")
    # Промежуточный набор выбранных опций (для multi-select)
    selected_options: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    candidate: Mapped["Candidate"] = relationship(back_populates="test_sessions")
