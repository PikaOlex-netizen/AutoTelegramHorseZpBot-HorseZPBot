from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import select

from ..database import Database
from ..models import DeleteTask, OutboundMessage, utcnow
from .audit import AuditLog
from .settings import SettingsService
from .telegram_client import TelegramService


def message_to_dict(message: OutboundMessage, task: DeleteTask | None = None) -> dict[str, Any]:
    # Скасована застаріла задача зберігається для історії аудиту/відновлення, але більше
    # не є розкладом видалення з точки зору користувача.
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
        """Використовується тільки для правдивого звіту про простій в терміналі."""
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
    ) -> int:  # Returns 0 on failure, actual message_id on success
        max_send_retries = 3
        last_error = None
        message_id = None
        
        for attempt in range(max_send_retries):
            try:
                message_id = await self.telegram.send_message(account_id, chat_id, text, reply_to_message_id)
                break
            except Exception as exc:
                last_error = exc
                if attempt < max_send_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff: 2s, 4s
                    continue
                # All retries failed - record as FAILED without DeleteTask
                with self.db.session() as session:
                    outbound = OutboundMessage(
                        account_id=account_id, chat_id=chat_id, telegram_message_id=0, message_type=message_type,
                        body=text, delete_at=utcnow(), status="FAILED",
                    )
                    outbound.error = type(last_error).__name__
                    session.add(outbound)
                    session.flush()
                self.audit.write(
                    "ERROR", "messages", "send_failed",
                    f"Повідомлення не вдалося надіслати після {max_send_retries} спроб: {type(last_error).__name__}",
                    account_id=account_id, mailing_type=message_type, chat_id=chat_id,
                )
                return 0  # Return 0 to indicate failure
        
        # Зберігаємо існуючу не-null колонку бази даних сумісно з оновленнями.
        # Timestamp не експонується, коли DeleteTask не існує.
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
            "Повідомлення надіслано та видалення заплановано" if schedule_deletion else "Повідомлення надіслано; видалення вимкнено",
            account_id=account_id, mailing_type=message_type, chat_id=chat_id, message_id=message_id,
        )
        return message_id

    async def start(self) -> None:
        self._stop_event.clear()
        # Відповіді Gemini, повідомлення Ферми, ПС та Вкрасти повинні залишатися видимими.
        # Скасуйте застарілі задачі видалення, створені до існування цих політик.
        persistent_message_types = {"GEMINI", "FARM", "PS", "REPLY"}
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
            # Процес може зупинитися після отримання іншої задачі, але перед
            # викликом Telegram. Повертаємо лише відновлюваний стан у чергу.
            for task in session.scalars(select(DeleteTask).where(DeleteTask.status == "PROCESSING")).all():
                task.status = "PENDING"
        if cancelled_persistent_tasks:
            self.audit.write(
                "INFO", "messages", "persistent_delete_cancelled",
                f"Скасовано видалення для {cancelled_persistent_tasks} існуючих повідомлень Gemini або Ферми",
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
        self.audit.write("INFO", "delete_scheduler", "started", "Планувальник видалення запущено")
        while not self._stop_event.is_set():
            try:
                await self.process_due()
            except Exception as exc:
                self.audit.write("ERROR", "delete_scheduler", "loop_error", f"Планувальник видалення відновлено від {type(exc).__name__}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        self.audit.write("INFO", "delete_scheduler", "stopped", "Планувальник видалення зупинено")

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
                task.last_error = "Запис вихідного повідомлення відсутній"
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
                self.audit.write("INFO", "messages", "already_deleted", "Повідомлення вже було відсутнє перед видаленням", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
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
            self.audit.write("INFO", "messages", "deleted", "Повідомлення видалено", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
        except Exception as exc:
            # Retry deletion indefinitely for common errors (database locked, network issues)
            with self.db.session() as session:
                task = session.get(DeleteTask, task_id)
                message = session.get(OutboundMessage, message_db_id)
                if task and message:
                    task.attempts += 1
                    task.last_error = type(exc).__name__
                    message.error = type(exc).__name__
                    # Ліміт retry - 100 спроб, потім FAILED
                    if task.attempts >= 100:
                        task.status = "FAILED"
                        message.status = "FAILED"
                        self.audit.write("WARNING", "messages", "delete_failed", f"Видалення не вдалося після {task.attempts} спроб: {type(exc).__name__}", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
                    else:
                        # Retry deletion - set to PENDING with short delay
                        task.status = "PENDING"
                        task.due_at = utcnow() + timedelta(seconds=2)  # Retry after 2 seconds
                        message.status = "DELETE_PENDING"
                        self.audit.write("WARNING", "messages", "delete_retry", f"Спроба видалення повідомлення не вдалася: {type(exc).__name__}, retry #{task.attempts} in 2 seconds", account_id=account_id, chat_id=chat_id, message_id=telegram_id)
