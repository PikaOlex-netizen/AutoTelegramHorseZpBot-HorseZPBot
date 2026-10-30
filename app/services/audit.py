from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

from ..database import Database
from ..models import LogEntry
from .settings import SettingsService


console = logging.getLogger("telegram_scheduler")


def log_to_dict(log: LogEntry) -> dict[str, Any]:
    return {
        "id": log.id,
        "created_at": log.created_at.isoformat(),
        "level": log.level,
        "module": log.module,
        "event": log.event,
        "message": log.message,
        "account_id": log.account_id,
        "mailing_type": log.mailing_type,
        "chat_id": log.chat_id,
        "message_id": log.message_id,
    }


class AuditLog:
    """Стійкий журнал аудиту з невеликим SSE fan-out в процесі."""

    def __init__(self, db: Database, settings: SettingsService | None = None) -> None:
        self.db = db
        self.settings = settings
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def write(
        self,
        level: str,
        module: str,
        event: str,
        message: str,
        *,
        mailing_type: str | None = None,
        account_id: int | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> dict[str, Any]:
        # Секрети навмисно ніколи не приймаються цим публічним API логування.
        configured_level = str(self.settings.get("log_level")) if self.settings else "INFO"
        ranks = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        if ranks.get(level.upper(), 20) < ranks.get(configured_level.upper(), 20):
            return {}
        prefix = f"account={account_id}" if account_id is not None else "system"
        suffix = []
        if mailing_type:
            suffix.append(mailing_type)
        if chat_id is not None:
            suffix.append(f"chat={chat_id}")
        if message_id is not None:
            suffix.append(f"message={message_id}")
        console.log(ranks.get(level.upper(), logging.INFO), "[%s] %s: %s%s", prefix, event, message, f" ({', '.join(suffix)})" if suffix else "")
        with self.db.session() as session:
            entry = LogEntry(
                level=level.upper(), module=module, event=event, message=message,
                mailing_type=mailing_type, account_id=account_id, chat_id=chat_id, message_id=message_id,
            )
            session.add(entry)
            session.flush()
            event_data = log_to_dict(entry)
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event_data)
        return event_data

    def list(
        self, *, limit: int = 200, level: str | None = None, module: str | None = None,
        mailing_type: str | None = None, chat_id: int | None = None, message_id: int | None = None,
        query: str | None = None, account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.db.session() as session:
            stmt = select(LogEntry).order_by(desc(LogEntry.id)).limit(min(limit, 1000))
            if level:
                stmt = stmt.where(LogEntry.level == level.upper())
            if module:
                stmt = stmt.where(LogEntry.module == module)
            if mailing_type:
                stmt = stmt.where(LogEntry.mailing_type == mailing_type.upper())
            if account_id is not None:
                stmt = stmt.where(LogEntry.account_id == account_id)
            if chat_id is not None:
                stmt = stmt.where(LogEntry.chat_id == chat_id)
            if message_id is not None:
                stmt = stmt.where(LogEntry.message_id == message_id)
            if query:
                stmt = stmt.where(LogEntry.message.ilike(f"%{query}%"))
            return [log_to_dict(row) for row in session.scalars(stmt).all()]

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "at": datetime.now().isoformat()}
        finally:
            self._subscribers.discard(queue)
