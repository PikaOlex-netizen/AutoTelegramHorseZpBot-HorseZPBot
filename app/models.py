from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (UniqueConstraint("account_id", "chat_id", name="uq_chat_account_peer"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True, default=1)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_type: Mapped[str] = mapped_column(String(40), default="unknown")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pch_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pb_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    gemini_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    keyword_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Збережено для сумісності бази даних зі старими інсталяціями. Більше не
    # використовується або відображається: відповідність чату повністю контролюється користувачем.
    bot_username: Mapped[str] = mapped_column(String(255), default="legacy")
    bot_status: Mapped[str] = mapped_column(String(32), default="CHECK_PENDING", index=True)
    bot_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FarmState(Base):
    """Стійкий розклад для одного чату для Farm розсилки."""

    __tablename__ = "farm_states"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval_hours: Mapped[int] = mapped_column(Integer, default=23)  # 23, 71, 119, 167, 335, 559
    status: Mapped[str] = mapped_column(String(32), default="IDLE", index=True)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PSState(Base):
    """Стійкий розклад для одного чату для PS розсилки (23 години 58 хвилин)."""

    __tablename__ = "ps_states"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="IDLE", index=True)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SchedulerState(Base):
    __tablename__ = "scheduler_states"
    mailing_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    current_group: Mapped[int] = mapped_column(Integer, default=0)
    total_groups: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="IDLE")
    last_group_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_group_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_group_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SchedulerDelivery(Base):
    __tablename__ = "scheduler_deliveries"
    __table_args__ = (UniqueConstraint("account_id", "run_id", "mailing_type", "group_number", "chat_id", name="uq_delivery"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True, default=1)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    mailing_type: Mapped[str] = mapped_column(String(8), index=True)
    group_number: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True, default=1)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer)
    message_type: Mapped[str] = mapped_column(String(8), index=True)
    body: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delete_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="DELETE_PENDING", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeleteTask(Base):
    __tablename__ = "delete_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    outbound_message_id: Mapped[int] = mapped_column(Integer, ForeignKey("outbound_messages.id"), unique=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LogEntry(Base):
    __tablename__ = "logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    module: Mapped[str] = mapped_column(String(80), index=True)
    event: Mapped[str] = mapped_column(String(100), index=True)
    message: Mapped[str] = mapped_column(Text)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mailing_type: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class GeminiHistory(Base):
    __tablename__ = "gemini_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UserCooldown(Base):
    """Персональний cooldown для кожного користувача для функції 'Нагодувати гівном'."""

    __tablename__ = "user_cooldowns"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    last_fed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("account_id", "user_id", name="uq_account_user"),)


class KeywordReplyState(Base):
    """Durable account-wide cooldown for the short keyword auto-reply."""

    __tablename__ = "keyword_reply_states"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KeywordReplyUserState(Base):
    """Durable per-sender cooldown for the short keyword auto-reply."""

    __tablename__ = "keyword_reply_user_states"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KeywordReplyGlobalState(Base):
    """Global cooldown across all accounts when 2+ accounts have keyword reply enabled."""

    __tablename__ = "keyword_reply_global_state"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FeedState(Base):
    """Стан функції 'Нагодувати гівном' для одного акаунту."""

    __tablename__ = "feed_states"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    username_to_feed: Mapped[str | None] = mapped_column(String, nullable=True, default="PikaOlex")
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True, default="нагодувати гівном")
    status: Mapped[str] = mapped_column(String(32), default="IDLE", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class HorseRatingState(Base):
    """Стан Horse rating monitor для кожного акаунту."""

    __tablename__ = "horse_rating_states"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="IDLE", index=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_steal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
