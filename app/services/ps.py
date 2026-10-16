from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ..database import Database
from ..models import PSState, utcnow
from .audit import AuditLog
from .accounts import AccountService
from .chats import ChatManager
from .messages import MessageManager


PS_INTERVAL_SECONDS = 23 * 60 * 60 + 58 * 60 + 22


def _future(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > utcnow()


class PSScheduler:
    """Durable repeating sender with one selected destination per account (23h 58m interval)."""

    def __init__(
        self, db: Database, chats: ChatManager, messages: MessageManager,
        audit: AuditLog, account_id: int,
    ) -> None:
        self.db = db
        self.chats = chats
        self.messages = messages
        self.audit = audit
        self.account_id = account_id
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._shutting_down = False

    def initialize(self) -> None:
        with self.db.session() as session:
            if session.get(PSState, self.account_id) is None:
                session.add(PSState(account_id=self.account_id))

    def get_state(self) -> dict[str, Any]:
        with self.db.session() as session:
            state = session.get(PSState, self.account_id)
            assert state is not None
            # Convert UTC to local time for display
            def to_local(dt):
                if dt is None:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone().isoformat()
            # Default text for PS
            message_text = state.message_text or "пограбувати світ"
            return {
                "account_id": self.account_id,
                "enabled": state.enabled,
                "chat_id": state.chat_id,
                "message_text": message_text,
                "status": state.status,
                "next_send_at": to_local(state.next_send_at),
                "last_sent_at": to_local(state.last_sent_at),
                "last_error": state.last_error,
                "interval_seconds": PS_INTERVAL_SECONDS,
                "interval_text": "23 години 58 хвилин 22 секунди",
            }

    async def configure(self, *, chat_id: int | None, text: str | None, enabled: bool) -> dict[str, Any]:
        async with self._lock:
            text = (text or "").strip()
            if enabled:
                if chat_id is None:
                    raise ValueError("Оберіть чат для ПС")
                if not text:
                    raise ValueError("Введіть текст для ПС")
                if not self.chats.is_currently_eligible(self.account_id, chat_id):
                    raise ValueError("Обраний чат не входить в активний список чатів")
            with self.db.session() as session:
                state = session.get(PSState, self.account_id)
                assert state is not None
                changed = state.chat_id != chat_id or state.message_text != text
                was_enabled = state.enabled
                state.chat_id = chat_id
                state.message_text = text or None
                state.enabled = enabled
                state.last_error = None
                if enabled:
                    state.status = "WAITING"
                    if not was_enabled or changed or not _future(state.next_send_at):
                        state.next_send_at = utcnow()
                else:
                    state.status = "STOPPED"
                    state.next_send_at = None
            self.audit.write(
                "INFO", "ps", "configured",
                "PS schedule enabled" if enabled else "PS schedule stopped",
                account_id=self.account_id, chat_id=chat_id,
            )
            if enabled:
                self._ensure_runner()
                self._wake.set()
            return self.get_state()

    async def restore(self) -> None:
        self._shutting_down = False
        with self.db.session() as session:
            state = session.get(PSState, self.account_id)
            assert state is not None
            if state.enabled and state.status == "SENDING":
                state.status = "WAITING"
                state.next_send_at = utcnow()
            should_restore = state.enabled and state.status in {"WAITING", "SENDING"}
        if should_restore:
            self.audit.write("INFO", "ps", "restored", "PS schedule restored after restart", account_id=self.account_id)
            self._ensure_runner()

    def _ensure_runner(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"ps-{self.account_id}")

    async def _wait(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, seconds))
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        while not self._shutting_down:
            try:
                state = self.get_state()
                if not state["enabled"] or state["status"] in {"STOPPED", "IDLE"}:
                    return
                next_at = datetime.fromisoformat(state["next_send_at"]) if state["next_send_at"] else None
                if _future(next_at):
                    if next_at and next_at.tzinfo is None:
                        next_at = next_at.replace(tzinfo=timezone.utc)
                    await self._wait((next_at.timestamp() - utcnow().timestamp()))
                    continue
                await self._send_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self.db.session() as session:
                    state = session.get(PSState, self.account_id)
                    if state and state.enabled:
                        state.status = "WAITING"
                        state.next_send_at = utcnow() + timedelta(seconds=60)
                        state.last_error = type(exc).__name__
                self.audit.write("ERROR", "ps", "worker_failed", f"PS worker recovered from {type(exc).__name__}", account_id=self.account_id)
                await self._wait(60)

    async def _send_once(self) -> None:
        async with self._lock:
            selected_chat_unavailable = False
            with self.db.session() as session:
                state = session.get(PSState, self.account_id)
                assert state is not None
                if not state.enabled or state.chat_id is None or not state.message_text:
                    return
                chat_id, text = state.chat_id, state.message_text
                if not self.chats.is_currently_eligible(self.account_id, chat_id):
                    state.enabled = False
                    state.status = "STOPPED"
                    state.next_send_at = None
                    state.last_error = "Selected chat is no longer active"
                    selected_chat_unavailable = True
                else:
                    state.status = "SENDING"
            if selected_chat_unavailable:
                self.audit.write(
                    "WARNING", "ps", "chat_unavailable",
                    "PS stopped because its selected chat was removed from the working list",
                    account_id=self.account_id, chat_id=chat_id,
                )
                return
        try:
            # PS is a repeating presence in the selected chat, so its messages
            # must stay visible.  PCH/PB keep their independent deletion policy.
            message_id = await self.messages.send(
                self.account_id, chat_id, text, "PS", schedule_deletion=False,
            )
        except Exception as exc:
            with self.db.session() as session:
                state = session.get(PSState, self.account_id)
                if state and state.enabled:
                    state.status = "WAITING"
                    state.next_send_at = utcnow() + timedelta(seconds=60)
                    state.last_error = type(exc).__name__
            self.audit.write("ERROR", "ps", "send_failed", f"PS message failed: {type(exc).__name__}", account_id=self.account_id, chat_id=chat_id)
            return
        with self.db.session() as session:
            state = session.get(PSState, self.account_id)
            if state and state.enabled:
                state.status = "WAITING"
                state.last_sent_at = utcnow()
                state.next_send_at = utcnow() + timedelta(seconds=PS_INTERVAL_SECONDS)
                state.last_error = None
        self.audit.write("INFO", "ps", "sent", f"PS message sent (Telegram message {message_id})", account_id=self.account_id, chat_id=chat_id, message_id=message_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        self._wake.set()
        task = self._task
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                task.cancel()


class PSRegistry:
    def __init__(self, db: Database, chats: ChatManager, messages: MessageManager, audit: AuditLog, accounts: AccountService) -> None:
        self.db, self.chats, self.messages, self.audit, self.accounts = db, chats, messages, audit, accounts
        self._schedulers: dict[int, PSScheduler] = {}

    def for_account(self, account_id: int) -> PSScheduler:
        if self.accounts.get(account_id) is None:
            raise KeyError(f"Account {account_id} does not exist")
        scheduler = self._schedulers.get(account_id)
        if scheduler is None:
            scheduler = PSScheduler(self.db, self.chats, self.messages, self.audit, account_id)
            scheduler.initialize()
            self._schedulers[account_id] = scheduler
        return scheduler

    def initialize(self) -> None:
        for account in self.accounts.list():
            self.for_account(account["id"])

    async def restore(self) -> None:
        for account in self.accounts.list():
            await self.for_account(account["id"]).restore()

    async def shutdown(self) -> None:
        await asyncio.gather(*(scheduler.shutdown() for scheduler in self._schedulers.values()), return_exceptions=True)
