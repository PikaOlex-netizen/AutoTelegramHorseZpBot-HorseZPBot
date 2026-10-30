from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from ..database import Database
from ..models import SchedulerDelivery, SchedulerState, utcnow
from .audit import AuditLog
from .chats import ChatManager
from .messages import MessageManager
from .settings import SettingsService
from .accounts import AccountService
from .telegram_client import TelegramService


VALID_TYPES = {"PCH", "PB"}


def distribute_evenly(chat_ids: list[int], group_count: int) -> list[list[int]]:
    """Детерміновано розділити знімок, розміри груп відрізняються не більше ніж на одиницю."""
    if group_count < 1:
        raise ValueError("group_count must be at least one")
    quotient, remainder = divmod(len(chat_ids), group_count)
    groups: list[list[int]] = []
    index = 0
    for group_index in range(group_count):
        size = quotient + (1 if group_index < remainder else 0)
        groups.append(chat_ids[index:index + size])
        index += size
    return groups


def _future(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > utcnow()


def state_to_dict(state: SchedulerState, kind: str, account_id: int, eligible_count: int | None = None) -> dict[str, Any]:
    # Перетворити UTC на місцевий час для відображення
    def to_local(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat()
    return {
        "mailing_type": kind, "account_id": account_id, "enabled": state.enabled, "current_group": state.current_group,
        "total_groups": state.total_groups, "status": state.status,
        "last_group_started_at": to_local(state.last_group_started_at),
        "last_group_completed_at": to_local(state.last_group_completed_at),
        "next_group_at": to_local(state.next_group_at),
        "run_id": state.run_id, "eligible_chats": eligible_count,
        "plan": json.loads(state.plan_json) if state.plan_json else [],
    }


class MailingScheduler:
    """Два незалежних стійких планувальника; їх план знімається при Start."""

    def __init__(
        self, db: Database, chats: ChatManager, messages: MessageManager, settings: SettingsService, audit: AuditLog,
        account_id: int = 1, telegram: TelegramService | None = None,
    ) -> None:
        self.db = db
        self.chats = chats
        self.messages = messages
        self.settings = settings
        self.audit = audit
        self.account_id = account_id
        self.telegram = telegram
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake: dict[str, asyncio.Event] = {kind: asyncio.Event() for kind in VALID_TYPES}
        self._locks: dict[str, asyncio.Lock] = {kind: asyncio.Lock() for kind in VALID_TYPES}
        self._shutting_down = False

    def _state_key(self, kind: str) -> str:
        return f"{self.account_id}:{kind}"

    def initialize(self) -> None:
        with self.db.session() as session:
            for kind in VALID_TYPES:
                if session.get(SchedulerState, self._state_key(kind)) is None:
                    session.add(SchedulerState(mailing_type=self._state_key(kind)))

    def _group_count(self, kind: str) -> int:
        return int(self.settings.get("pch_groups" if kind == "PCH" else "pb_groups"))

    def get_state(self, kind: str) -> dict[str, Any]:
        self._check_kind(kind)
        with self.db.session() as session:
            state = session.get(SchedulerState, self._state_key(kind))
            assert state is not None
            result = state_to_dict(state, kind, self.account_id, len(self.chats.eligible_chat_ids(self.account_id, kind)))
        return result

    def all_states(self) -> dict[str, dict[str, Any]]:
        return {kind: self.get_state(kind) for kind in ("PCH", "PB")}

    def get_template(self, kind: str) -> str:
        self._check_kind(kind)
        key = f"account_{self.account_id}_{kind.lower()}_template"
        with self.db.session() as session:
            from ..models import Setting
            setting = session.get(Setting, key)
            if setting:
                return setting.value
            # Акаунт 1 зберігає попередній шаблон одиночного акаунту після оновлення.
            legacy = session.get(Setting, f"{kind.lower()}_template") if self.account_id == 1 else None
            if legacy:
                return legacy.value
            # Текст за замовчуванням для ПБ та ПЧ
            if kind == "PB":
                return "пограбувати банк"
            if kind == "PCH":
                return "пограбувати чат"
            return ""

    def set_template(self, kind: str, text: str) -> None:
        self._check_kind(kind)
        from ..models import Setting
        key = f"account_{self.account_id}_{kind.lower()}_template"
        with self.db.session() as session:
            setting = session.get(Setting, key)
            if setting is None:
                session.add(Setting(key=key, value=text))
            else:
                setting.value = text
        self.audit.write("INFO", "scheduler", "template_updated", f"{kind} message template updated", account_id=self.account_id, mailing_type=kind)

    async def start(self, kind: str, text: str | None = None) -> dict[str, Any]:
        self._check_kind(kind)
        async with self._locks[kind]:
            current = self.get_state(kind)
            if current["status"] in {"RUNNING", "WAITING", "CYCLE_WAITING", "SENDING", "PAUSED", "PAUSED_CYCLE"}:
                raise ValueError(f"{kind} scheduler is already active; resume, pause, or stop it instead")
            content = text if text is not None else self.get_template(kind)
            if not content or not content.strip():
                raise ValueError("Save a message template or provide text before starting")
            chat_ids = self.chats.eligible_chat_ids(self.account_id, kind)
            if not chat_ids:
                raise ValueError("No manually selected chats are assigned to this mailing")
            group_count = self._group_count(kind)
            plan = distribute_evenly(chat_ids, group_count)
            run_id = uuid.uuid4().hex
            now = utcnow()
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                session.execute(delete(SchedulerDelivery).where(
                    SchedulerDelivery.account_id == self.account_id,
                    SchedulerDelivery.mailing_type == kind,
                ))
                state.enabled = True
                state.current_group = 1
                state.total_groups = group_count
                state.status = "RUNNING"
                state.last_group_started_at = None
                state.last_group_completed_at = None
                state.next_group_at = now
                state.run_id = run_id
                state.plan_json = json.dumps(plan)
                state.message_text = content
                for number, group in enumerate(plan, start=1):
                    for chat_id in group:
                        session.add(SchedulerDelivery(
                            account_id=self.account_id, run_id=run_id, mailing_type=kind,
                            group_number=number, chat_id=chat_id,
                        ))
            self.audit.write("INFO", "scheduler", "started", f"{kind} scheduler started with {len(chat_ids)} chats in {group_count} groups", account_id=self.account_id, mailing_type=kind)
            self._ensure_runner(kind)
            self._wake[kind].set()
            return self.get_state(kind)

    async def pause(self, kind: str) -> dict[str, Any]:
        self._check_kind(kind)
        async with self._locks[kind]:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                if state.status not in {"RUNNING", "WAITING", "CYCLE_WAITING", "SENDING"}:
                    raise ValueError(f"Cannot pause {kind} while it is {state.status}")
                state.status = "PAUSED_CYCLE" if state.status == "CYCLE_WAITING" else "PAUSED"
            self.audit.write("INFO", "scheduler", "paused", f"{kind} scheduler paused", account_id=self.account_id, mailing_type=kind)
            self._wake[kind].set()
            return self.get_state(kind)

    async def resume(self, kind: str) -> dict[str, Any]:
        self._check_kind(kind)
        async with self._locks[kind]:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                if state.status not in {"PAUSED", "PAUSED_CYCLE"}:
                    raise ValueError(f"Cannot resume {kind} while it is {state.status}")
                state.enabled = True
                if state.status == "PAUSED_CYCLE":
                    state.status = "CYCLE_WAITING"
                else:
                    state.status = "WAITING" if _future(state.next_group_at) else "RUNNING"
                if state.next_group_at is None:
                    state.next_group_at = utcnow()
            self.audit.write("INFO", "scheduler", "resumed", f"{kind} scheduler resumed", account_id=self.account_id, mailing_type=kind)
            self._ensure_runner(kind)
            self._wake[kind].set()
            return self.get_state(kind)

    async def stop(self, kind: str) -> dict[str, Any]:
        self._check_kind(kind)
        async with self._locks[kind]:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                state.enabled = False
                state.status = "STOPPED"
                state.next_group_at = None
            self.audit.write("INFO", "scheduler", "stopped", f"{kind} scheduler stopped", account_id=self.account_id, mailing_type=kind)
            self._wake[kind].set()
            return self.get_state(kind)

    async def restore(self) -> None:
        """Продовжити збережену роботу без recreation плану або повторної відправки завершених доставок."""
        self._shutting_down = False
        # Stagger restores to avoid all accounts starting at once
        await asyncio.sleep(self.account_id * 0.5)  # 0.5s delay per account
        for kind in VALID_TYPES:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                if state.status == "SENDING":
                    # Зупинка посеред групи відновлює лише рядки доставки, що ще PENDING.
                    state.status = "RUNNING"
                    # Keep existing next_group_at to continue where it left off
                should_restore = state.enabled and state.status in {"RUNNING", "WAITING", "CYCLE_WAITING"}
            if should_restore:
                self.audit.write("INFO", "scheduler", "restored", f"{kind} scheduler state restored", account_id=self.account_id, mailing_type=kind)
                self._ensure_runner(kind)

    def _ensure_runner(self, kind: str) -> None:
        task = self._tasks.get(kind)
        if task is None or task.done():
            # Clear done task to avoid creating duplicate
            if task and task.done():
                self._tasks.pop(kind, None)
            # Double-check after clearing to prevent race condition
            if kind not in self._tasks:
                self._tasks[kind] = asyncio.create_task(self._run(kind), name=f"{kind.lower()}-scheduler-{self.account_id}")

    async def _run(self, kind: str) -> None:
        while not self._shutting_down:
            try:
                state = self.get_state(kind)
                status = state["status"]
                if status in {"IDLE", "STOPPED", "COMPLETED"} or not state["enabled"]:
                    return
                if status in {"PAUSED", "PAUSED_CYCLE"}:
                    await self._wait(kind, 30)
                    continue

                # Затримка між ПЧ і ПБ
                if kind == "PB":
                    try:
                        all_settings = self.settings.get_all()
                        pch_delay = int(all_settings.get("pch_pb_delay_seconds", 0))
                        if pch_delay > 0:
                            pch_state = self.get_state("PCH")
                            if pch_state and isinstance(pch_state, dict):
                                pch_last_at = pch_state.get("last_group_completed_at")
                                if pch_last_at and isinstance(pch_last_at, str):
                                    pch_last_dt = datetime.fromisoformat(pch_last_at)
                                    if pch_last_dt.tzinfo is None:
                                        pch_last_dt = pch_last_dt.replace(tzinfo=timezone.utc)
                                    elapsed = (utcnow() - pch_last_dt).total_seconds()
                                    if elapsed < pch_delay:
                                        wait_time = pch_delay - elapsed
                                        self.audit.write("INFO", "scheduler", "pch_pb_delay", f"PB waiting {wait_time}s after PCH", account_id=self.account_id, mailing_type=kind)
                                        await self._wait(kind, wait_time)
                                        continue
                    except Exception as e:
                        # Ігнорувати помилки при перевірці PCH state
                        pass

                next_value = state["next_group_at"]
                if next_value:
                    next_at = datetime.fromisoformat(next_value)
                    if _future(next_at):
                        # Перетворити обидва в UTC для порівняння
                        if next_at.tzinfo is None:
                            next_at = next_at.replace(tzinfo=timezone.utc)
                        utc_now = utcnow()
                        delay = max(0.0, (next_at.timestamp() - utc_now.timestamp()))
                        await self._wait(kind, min(delay, 60))
                        continue
                if status == "CYCLE_WAITING":
                    await self._begin_next_cycle(kind)
                    continue
                await self._send_current_group(kind)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Спотворений застарілий timestamp, тимчасова помилка бази даних або
                # виключення на стороні Telegram не повинні назавжди вбивати ПЧ/ПБ.
                with self.db.session() as session:
                    state = session.get(SchedulerState, self._state_key(kind))
                    if state and state.enabled:
                        state.status = "RUNNING"
                        state.next_group_at = utcnow() + timedelta(seconds=60)
                self.audit.write(
                    "ERROR", "scheduler", "worker_failed",
                    f"{kind} worker recovered from {type(exc).__name__}",
                    account_id=self.account_id, mailing_type=kind,
                )
                await self._wait(kind, 60)

    async def _wait(self, kind: str, seconds: float) -> None:
        event = self._wake[kind]
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.05, seconds))
        except asyncio.TimeoutError:
            pass

    async def _send_current_group(self, kind: str) -> None:
        repeat_wait_seconds: int | None = None
        async with self._locks[kind]:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                if not state.enabled or state.status in {"PAUSED", "STOPPED", "COMPLETED"}:
                    return
                group = state.current_group
                if group < 1 or group > state.total_groups or not state.run_id:
                    state.enabled = False
                    state.status = "COMPLETED"
                    return
                run_id = state.run_id
                text = state.message_text or ""
                state.status = "SENDING"
                state.last_group_started_at = utcnow()
            self.audit.write("INFO", "scheduler", "group_started", f"{kind} group {group} started", account_id=self.account_id, mailing_type=kind)

        # Отримання кожної доставки знову після перезапуску означає, що рядки SENT ніколи не дублюються.
        with self.db.session() as session:
            delivery_ids = list(session.scalars(
                select(SchedulerDelivery.id).where(
                    SchedulerDelivery.account_id == self.account_id,
                    SchedulerDelivery.run_id == run_id, SchedulerDelivery.mailing_type == kind,
                    SchedulerDelivery.group_number == group, SchedulerDelivery.status == "PENDING",
                ).order_by(SchedulerDelivery.id)
            ).all())
        for delivery_id in delivery_ids:
            if self._shutting_down:
                return
            await self._send_delivery(kind, delivery_id, text)

        async with self._locks[kind]:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                if state.status in {"PAUSED", "STOPPED"} or not state.enabled:
                    return
                # Не переходити до наступної групи, якщо перерва залишила запис PENDING.
                pending = session.scalar(select(SchedulerDelivery.id).where(
                    SchedulerDelivery.account_id == self.account_id,
                    SchedulerDelivery.run_id == state.run_id, SchedulerDelivery.mailing_type == kind,
                    SchedulerDelivery.group_number == state.current_group, SchedulerDelivery.status == "PENDING",
                ))
                if pending:
                    state.status = "RUNNING"
                    state.next_group_at = utcnow()
                    return
                completed_group = state.current_group
                state.last_group_completed_at = utcnow()
                if completed_group >= state.total_groups:
                    # Зберігати стійкий годинний ритм між останньою групою
                    # цього циклу та першою групою наступного. Це робить
                    # ціль ПЧ повторюваною не частіше ніж кожні 6 інтервалів груп, а ПБ —
                    # не частіше ніж кожні 12 (за замовчуванням), ніколи підряд.
                    group_delay = int(self.settings.get("group_delay_seconds"))
                    minimum_repeat = self._minimum_repeat_seconds(kind)
                    # Перша ціль у новому циклі вже чекала через
                    # total_groups - 1 звичайних інтервалів. Додайте достатньо часу після
                    # останньої групи, щоб забезпечити 6 год для ПЧ / 12 год для ПБ, навіть якщо
                    # кількість груп пізніше змінена в Settings.
                    cycle_wait = max(group_delay, minimum_repeat - ((state.total_groups - 1) * group_delay))
                    repeat_wait_seconds = cycle_wait
                    state.current_group = 1
                    state.status = "CYCLE_WAITING"
                    state.next_group_at = utcnow() + timedelta(seconds=cycle_wait)
                    completed = True
                else:
                    state.current_group += 1
                    state.status = "WAITING"
                    state.next_group_at = utcnow() + timedelta(seconds=int(self.settings.get("group_delay_seconds")))
                    completed = False
            self.audit.write("INFO", "scheduler", "group_completed", f"{kind} group {completed_group} completed", account_id=self.account_id, mailing_type=kind)
            if completed:
                self.audit.write("INFO", "scheduler", "cycle_completed", f"{kind} completed its cycle; repeat protection is waiting before the next cycle", account_id=self.account_id, mailing_type=kind)
                self.audit.write(
                    "INFO", "scheduler", "repeat_guard_wait",
                    f"{kind} next cycle waits {repeat_wait_seconds} seconds; no chat repeats sooner than {self._minimum_repeat_seconds(kind)} seconds",
                    account_id=self.account_id, mailing_type=kind,
                )
            else:
                self.audit.write("INFO", "scheduler", "waiting", f"{kind} waiting before group {completed_group + 1}", account_id=self.account_id, mailing_type=kind)

    async def _begin_next_cycle(self, kind: str) -> None:
        """Побудувати наступний стійкий знімок після затримки завершеного циклу.

        Ручні операції додавання/видалення, зроблені під час циклу, потрапляють сюди, ніколи в
        частково завершений. Кожен цикл отримує свіжий run id та книгу доставки.
        """
        template = self.get_template(kind).strip()
        async with self._locks[kind]:
            with self.db.session() as session:
                state = session.get(SchedulerState, self._state_key(kind))
                assert state is not None
                if not state.enabled or state.status != "CYCLE_WAITING":
                    return
                chat_ids = self.chats.eligible_chat_ids(self.account_id, kind)
                if not chat_ids:
                    state.enabled = False
                    state.status = "COMPLETED"
                    state.next_group_at = None
                    self.audit.write("WARNING", "scheduler", "no_eligible_chats", f"{kind} stopped because its next cycle has no eligible chats", account_id=self.account_id, mailing_type=kind)
                    return
                group_count = self._group_count(kind)
                plan = distribute_evenly(chat_ids, group_count)
                run_id = uuid.uuid4().hex
                session.execute(delete(SchedulerDelivery).where(
                    SchedulerDelivery.account_id == self.account_id,
                    SchedulerDelivery.mailing_type == kind,
                ))
                state.current_group = 1
                state.total_groups = group_count
                state.status = "RUNNING"
                state.next_group_at = utcnow()
                state.run_id = run_id
                state.plan_json = json.dumps(plan)
                if template:
                    state.message_text = template
                for number, group in enumerate(plan, start=1):
                    for chat_id in group:
                        session.add(SchedulerDelivery(
                            account_id=self.account_id, run_id=run_id, mailing_type=kind,
                            group_number=number, chat_id=chat_id,
                        ))
            self.audit.write("INFO", "scheduler", "cycle_started", f"{kind} started a new cycle with {len(chat_ids)} manually selected chats", account_id=self.account_id, mailing_type=kind)

    async def _send_delivery(self, kind: str, delivery_id: int, text: str) -> None:
        with self.db.session() as session:
            delivery = session.get(SchedulerDelivery, delivery_id)
            if delivery is None or delivery.status != "PENDING":
                return
            chat_id = delivery.chat_id
        # Чат, видалений з цієї розсилки перед його чергою, пропускається.
        if not self.chats.is_currently_eligible(self.account_id, chat_id, kind):
            with self.db.session() as session:
                delivery = session.get(SchedulerDelivery, delivery_id)
                if delivery and delivery.status == "PENDING":
                    delivery.status = "SKIPPED"
                    delivery.error = "Chat is no longer selected in the manual mailing list"
                    delivery.completed_at = utcnow()
            self.audit.write("WARNING", "scheduler", "chat_skipped", "Skipped chat removed from manual mailing list", account_id=self.account_id, mailing_type=kind, chat_id=chat_id)
            return
        try:
            message_id = await self.messages.send(self.account_id, chat_id, text, kind)
            with self.db.session() as session:
                delivery = session.get(SchedulerDelivery, delivery_id)
                if delivery:
                    delivery.status = "SENT"
                    delivery.message_id = message_id
                    delivery.completed_at = utcnow()
        except Exception as exc:
            with self.db.session() as session:
                delivery = session.get(SchedulerDelivery, delivery_id)
                if delivery:
                    delivery.status = "FAILED"
                    delivery.error = type(exc).__name__
                    delivery.completed_at = utcnow()
            self.audit.write("ERROR", "scheduler", "message_failed", f"Message send failed: {type(exc).__name__}", account_id=self.account_id, mailing_type=kind, chat_id=chat_id)

    async def shutdown(self) -> None:
        """Зупинити виконавців без перезапису стійкого стану запуску; запуск може відновити його."""
        self._shutting_down = True
        for event in self._wake.values():
            event.set()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in VALID_TYPES:
            raise ValueError("mailing type must be PCH or PB")

    @staticmethod
    def _minimum_repeat_seconds(kind: str) -> int:
        return 6 * 3600 + 22 if kind == "PCH" else 12 * 3600 + 22


class SchedulerRegistry:
    """Володіє незалежними виконавцями ПЧ/ПБ для кожного активного Telegram-акаунту."""

    def __init__(
        self, db: Database, chats: ChatManager, messages: MessageManager, settings: SettingsService,
        audit: AuditLog, accounts: AccountService, telegram: TelegramService | None = None,
    ) -> None:
        self.db = db
        self.chats = chats
        self.messages = messages
        self.settings = settings
        self.audit = audit
        self.accounts = accounts
        self.telegram = telegram
        self._schedulers: dict[int, MailingScheduler] = {}

    def for_account(self, account_id: int) -> MailingScheduler:
        if self.accounts.get(account_id) is None:
            raise KeyError(f"Account {account_id} does not exist")
        scheduler = self._schedulers.get(account_id)
        if scheduler is None:
            scheduler = MailingScheduler(
                self.db, self.chats, self.messages, self.settings, self.audit,
                account_id, telegram=self.telegram,
            )
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
