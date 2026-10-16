from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..database import Database
from ..models import DeleteTask, OutboundMessage, utcnow
from .audit import AuditLog
from .settings import SettingsService
from .telegram_client import TelegramService


def message_to_dict(message: OutboundMessage, task: DeleteTask | None = None) -> dict[str, Any]:
    # A cancelled legacy task is retained for audit/recovery history but is no
    # longer a deletion schedule from the user's perspective.
    delete_scheduled = task is not None and task.status != "CANCELLED"
    return {
        "id": message.id, "account_id": message.account_id, "chat_id": message.chat_id, "telegram_message_id": message.telegram_message_id,
        "message_type": message.message_type, "body": message.body, "sent_at": message.sent_at.isoformat(),
        "delete_scheduled": delete_scheduled,
        "delete_at": task.due_at.isoformat() if delete_scheduled else None,
        "status": message.status, "error": message.error,
        "delete_task": None if task is None else {"id": task.id, "due_at": task.due_at.isoformat(), "attempts": task.attempts, "status": task.status, "last_error": task.last_error},
    }


class MessageManager:
    def __init__(self, db: Database, telegram: TelegramService, settings: SettingsService, audit: AuditLog) -> None:
        self.db = db
        self.telegram = telegram
        self.settings = settings
        self.audit = audit
        self._worker: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._processing_lock = asyncio.Lock()
        self._active_delete_tasks: set[int] = set()

    @property
    def is_processing_deletions(self) -> bool:
        """Used only for a truthful terminal idle report."""
        return bool(self._active_delete_tasks)

    def list(self, account_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.session() as session:
            stmt = select(OutboundMessage).order_by(OutboundMessage.id.desc()).limit(min(limit, 1000))
            if account_id is not None:
                stmt = stmt.where(OutboundMessage.account_id == account_id)
            messages = session.scalars(stmt).all()
            return [message_to_dict(message, session.scalar(select(DeleteTask).where(DeleteTask.outbound_message_id == message.id))) for message in messages]

    async def send(
        self, account_id: int, chat_id: int, text: str, message_type: str,
        reply_to_message_id: int | None = None, *, schedule_deletion: bool = True,
    ) -> int:
        message_id = await self.telegram.send_message(account_id, chat_id, text, reply_to_message_id)
        # Keep the existing non-null database column compatible with upgrades.
        # The timestamp is not exposed when no DeleteTask exists.
        delete_at = utcnow() + timedelta(seconds=int(self.settings.get("delete_after_seconds"))) if schedule_deletion else utcnow()
        with self.db.session() as session:
            outbound = OutboundMessage(
                account_id=account_id, chat_id=chat_id, telegram_message_id=message_id, message_type=message_type,
                body=text, delete_at=delete_at, status="DELETE_PENDING" if schedule_deletion else "SENT",
            )
            session.add(outbound)
            session.flush()
            if schedule_deletion:
                session.add(DeleteTask(outbound_message_id=outbound.id, due_at=delete_at, status="PENDING"))
        self.audit.write(
            "INFO", "messages", "sent",
            "Message sent and deletion scheduled" if schedule_deletion else "Message sent; deletion disabled",
            account_id=account_id, mailing_type=message_type, chat_id=chat_id, message_id=message_id,
        )
        return message_id

    async def start(self) -> None:
        self._stop_event.clear()
        # Gemini responses, Farm and PS messages are meant to remain visible.
        # Cancel legacy delete tasks created before those policies existed.
        persistent_message_types = {"GEMINI", "FARM", "PS"}
        cancelled_persistent_tasks = 0
        with self.db.session() as session:
            persistent_tasks = session.scalars(
                select(DeleteTask).join(OutboundMessage, DeleteTask.outbound_message_id == OutboundMessage.id).where(
                    OutboundMessage.message_type.in_(persistent_message_types),
                    DeleteTask.status.not_in({"DELETED", "ALREADY_DELETED", "CANCELLED"}),
                )
            ).all()
            for task in persistent_tasks:
                message = session.get(OutboundMessage, task.outbound_message_id)
                task.status = "CANCELLED"
                if message:
                    message.status = "SENT"
                cancelled_persistent_tasks += 1
            # A process may stop after claiming another task but before
            # calling Telegram. Return only that recoverable state to the queue.
            for task in session.scalars(select(DeleteTask).where(DeleteTask.status == "PROCESSING")).all():
                task.status = "PENDING"
        if cancelled_persistent_tasks:
            self.audit.write(
                "INFO", "messages", "persistent_delete_cancelled",
                f"Cancelled deletion for {cancelled_persistent_tasks} existing Gemini or Farm message(s)",
            )
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="delete-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._worker:
            try:
                await asyncio.wait_for(self._worker, timeout=5)
            except asyncio.TimeoutError:
                self._worker.cancel()

    async def _run(self) -> None:
        self.audit.write("INFO", "delete_scheduler", "started", "Delete scheduler started")
        while not self._stop_event.is_set():
            try:
                await self.process_due()
            except Exception as exc:
                self.audit.write("ERROR", "delete_scheduler", "loop_error", f"Delete scheduler recovered from {type(exc).__name__}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        self.audit.write("INFO", "delete_scheduler", "stopped", "Delete scheduler stopped")

    async def process_due(self) -> None:
        async with self._processing_lock:
            now = utcnow()
            with self.db.session() as session:
                task_ids = list(session.scalars(
                    select(DeleteTask.id).where(DeleteTask.status == "PENDING", DeleteTask.due_at <= now).order_by(DeleteTask.due_at).limit(50)
                ).all())
                for task_id in task_ids:
                    task = session.get(DeleteTask, task_id)
                    if task:
                        task.status = "PROCESSING"
            for task_id in task_ids:
                await self._process_one(task_id)

    async def _process_one(self, task_id: int) -> None:
        self._active_delete_tasks.add(task_id)
        try:
            await self._process_one_inner(task_id)
        finally:
            self._active_delete_tasks.discard(task_id)

    async def _process_one_inner(self, task_id: int) -> None:
        with self.db.session() as session:
            task = session.get(DeleteTask, task_id)
            if task is None or task.status != "PROCESSING":
                return
            message = session.get(OutboundMessage, task.outbound_message_id)
            if message is None:
                task.status = "FAILED"
                task.last_error = "Outbound message record missing"
                return
            account_id, chat_id, telegram_id, message_db_id = message.account_id, message.chat_id, message.telegram_message_id, message.id
            message.status = "CHECKING"
        try:
            exists = await self.telegram.message_exists(account_id, chat_id, telegram_id)
            if not exists:
                with self.db.session() as session:
                    task = session.get(DeleteTask, task_id)
                    message = session.get(OutboundMessage, message_db_id)
                    if task and message:
                        task.status = "ALREADY_DELETED"
                        message.status = "ALREADY_DELETED"
                self.audit.write("INFO", "messages", "already_deleted", "Message was already absent before deletion", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
                return
            with self.db.session() as session:
                message = session.get(OutboundMessage, message_db_id)
                if message:
                    message.status = "DELETING"
            await self.telegram.delete_message(account_id, chat_id, telegram_id)
            with self.db.session() as session:
                task = session.get(DeleteTask, task_id)
                message = session.get(OutboundMessage, message_db_id)
                if task and message:
                    task.status = "DELETED"
                    message.status = "DELETED"
            self.audit.write("INFO", "messages", "deleted", "Message deleted", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
        except Exception as exc:
            max_retries = int(self.settings.get("max_delete_retries"))
            with self.db.session() as session:
                task = session.get(DeleteTask, task_id)
                message = session.get(OutboundMessage, message_db_id)
                if task and message:
                    task.attempts += 1
                    task.last_error = type(exc).__name__
                    message.error = type(exc).__name__
                    if task.attempts > max_retries:
                        task.status = "FAILED"
                        message.status = "FAILED"
                    else:
                        task.status = "PENDING"
                        task.due_at = utcnow() + timedelta(seconds=min(60, 5 * task.attempts))
                        message.status = "DELETE_PENDING"
            self.audit.write("WARNING", "messages", "delete_retry", f"Message deletion attempt failed: {type(exc).__name__}", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
