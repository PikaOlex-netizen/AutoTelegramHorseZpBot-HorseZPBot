from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HorseRatingState, utcnow
from .audit import AuditLog


HORSE_BOT_USERNAME = "@HorseZPBot"
RATING_THRESHOLD = 1_000_000
RATING_CHECK_INTERVAL_SECONDS = 300  # 5 хвилин
STEAL_COOLDOWN_SECONDS = 3660  # 61 хвилин
RETRY_AFTER_STEAL_IN_PROGRESS_SECONDS = 69  # 69 секунд якщо "зараз відбувається крадіжка"
INTER_ACCOUNT_DELAY_SECONDS = 69  # між акаунтами


class HorseRatingMonitor:
    """Monitor для перевірки рейтингу в @HorseZPBot та автоматичної крадіжки."""

    def __init__(self, db, accounts, telegram, audit: AuditLog):
        self.db = db
        self.accounts = accounts
        self.telegram = telegram
        self.audit = audit
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._current_account_index = 0

    def for_account(self, account_id: int) -> AccountHorseMonitor:
        return AccountHorseMonitor(self.db, account_id, self.telegram, self.audit)

    def initialize(self) -> None:
        self.audit.write("INFO", "horse_rating", "initialize", "Horse rating monitor initialized")

    async def start(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        self._stop_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="horse-rating-monitor")
        self.audit.write("INFO", "horse_rating", "started", "Horse rating monitor started")

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self.audit.write("INFO", "horse_rating", "stopped", "Horse rating monitor stopped")

    async def _monitor_loop(self) -> None:
        # Затримка перед першою перевіркою щоб дати іншим сервісам ініціалізуватися
        await asyncio.sleep(10)
        while not self._stop_event.is_set():
            try:
                account_list = self.accounts.list()
                if not account_list:
                    await asyncio.sleep(RATING_CHECK_INTERVAL_SECONDS)
                    continue

                for i, account in enumerate(account_list):
                    if self._stop_event.is_set():
                        break
                    account_id = account["id"]
                    await self._check_account(account_id)
                    if i < len(account_list) - 1:
                        await asyncio.sleep(INTER_ACCOUNT_DELAY_SECONDS)

                await asyncio.sleep(RATING_CHECK_INTERVAL_SECONDS)
            except Exception as exc:
                self.audit.write("ERROR", "horse_rating", "loop_error", f"Monitor loop error: {type(exc).__name__}: {str(exc)}")
                await asyncio.sleep(RATING_CHECK_INTERVAL_SECONDS)

    async def _check_account(self, account_id: int) -> None:
        try:
            monitor = self.for_account(account_id)
            state = monitor.get_state()
            if not state.get("enabled"):
                return

            if state.get("cooldown_until"):
                cooldown_until = state["cooldown_until"]
                if cooldown_until.tzinfo is None:
                    cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
                if utcnow() < cooldown_until:
                    self.audit.write("INFO", "horse_rating", "cooldown_active", f"Account {account_id} on cooldown until {cooldown_until}", account_id=account_id)
                    return

            # Скинути статус WAITING_FOR_RETRY і перевірити рейтинг знову
            if state.get("status") == "WAITING_FOR_RETRY":
                self.audit.write("INFO", "horse_rating", "retry_after_cooldown", f"Account {account_id} retry after cooldown", account_id=account_id)

            await monitor.check_and_steal()
        except Exception as exc:
            self.audit.write("ERROR", "horse_rating", "check_failed", f"Check failed for account {account_id}: {type(exc).__name__}", account_id=account_id)


class AccountHorseMonitor:
    """Monitor для одного акаунту."""

    def __init__(self, db, account_id: int, telegram, audit: AuditLog):
        self.db = db
        self.account_id = account_id
        self.telegram = telegram
        self.audit = audit

    def get_state(self) -> dict:
        with self.db.session() as session:
            state = session.get(HorseRatingState, self.account_id)
            if state is None:
                return {"enabled": False, "status": "IDLE"}
            return {
                "enabled": state.enabled,
                "status": state.status,
                "last_check_at": state.last_check_at,
                "last_steal_at": state.last_steal_at,
                "cooldown_until": state.cooldown_until,
                "last_rating": state.last_rating,
                "last_error": state.last_error,
            }

    def set_enabled(self, enabled: bool) -> None:
        with self.db.session() as session:
            state = session.get(HorseRatingState, self.account_id)
            if state is None:
                state = HorseRatingState(account_id=self.account_id, enabled=enabled)
                session.add(state)
            else:
                state.enabled = enabled
            session.commit()
        self.audit.write("INFO", "horse_rating", "enabled_set", f"Account {self.account_id} enabled={enabled}", account_id=self.account_id)

    async def check_and_steal(self) -> None:
        try:
            client = await self.telegram.require_authorized(self.account_id)

            # Отримати останнє повідомлення бота
            messages = await client.get_messages(HORSE_BOT_USERNAME, limit=1)
            if not messages:
                self.audit.write("WARNING", "horse_rating", "no_messages", "No messages from bot", account_id=self.account_id)
                return

            last_message = messages[0]

            # Відправити команду "рейтинг" як reply на останнє повідомлення
            await client.send_message(HORSE_BOT_USERNAME, "рейтинг", reply_to=last_message.id)
            self.audit.write("INFO", "horse_rating", "rating_command_sent", "Sent 'рейтинг' command", account_id=self.account_id)

            # Почекати відповідь
            await asyncio.sleep(3)
            messages = await client.get_messages(HORSE_BOT_USERNAME, limit=1)
            if not messages:
                self.audit.write("WARNING", "horse_rating", "no_response", "No response from bot", account_id=self.account_id)
                return

            response = messages[0]
            if not response.text:
                self.audit.write("WARNING", "horse_rating", "no_text", "Response has no text", account_id=self.account_id)
                return

            rating = self._parse_rating(response.text)
            if rating is None:
                self.audit.write("WARNING", "horse_rating", "parse_failed", f"Could not parse rating from: {response.text[:100]}", account_id=self.account_id)
                return

            self._update_state(last_check_at=utcnow(), last_rating=rating, status="CHECKED")
            self.audit.write("INFO", "horse_rating", "rating_checked", f"Rating: {rating}", account_id=self.account_id)

            if rating > RATING_THRESHOLD:
                await self._attempt_steal(client, response.id)
            else:
                self.audit.write("INFO", "horse_rating", "rating_low", f"Rating {rating} below threshold {RATING_THRESHOLD}", account_id=self.account_id)

        except Exception as exc:
            self._update_state(last_error=str(exc), status="ERROR")
            self.audit.write("ERROR", "horse_rating", "check_error", f"Check error: {type(exc).__name__}: {str(exc)}", account_id=self.account_id)

    def _parse_rating(self, text: str) -> int | None:
        # Парсить числа з комами або пробілами як роздільники тисяч: 1,000,000 або 1 000 000
        # Також обробляє суфікси: 31.0M, 125М, 125m, 125 млн, 125 мільйонів
        match = re.search(r'[-+]?\d+(?:[,\s]\d{3})*(?:\.\d+)?\s*[МмMm]?(?:лн|ільйонів)?', text, re.IGNORECASE)
        if match:
            number_str = match.group().replace(',', '').replace(' ', '').replace('М', '').replace('м', '').replace('M', '').replace('m', '').replace('лн', '').replace('ільйонів', '').strip()
            try:
                return int(float(number_str) * 1_000_000)
            except ValueError:
                pass
        # Обробляє суфікси тисяч: 100к, 100K, 100 тис, 100 тисяч
        match = re.search(r'[-+]?\d+(?:[,\s]\d{3})*(?:\.\d+)?\s*[КкKk]?(?:ис|исяч)?', text, re.IGNORECASE)
        if match:
            number_str = match.group().replace(',', '').replace(' ', '').replace('К', '').replace('к', '').replace('K', '').replace('k', '').replace('ис', '').replace('исяч', '').strip()
            try:
                return int(float(number_str) * 1_000)
            except ValueError:
                pass
        # Звичайний формат без суфіксів
        match = re.search(r'[-+]?\d+(?:[,\s]\d{3})*(?:\.\d+)?', text)
        if match:
            number_str = match.group().replace(',', '').replace(' ', '')
            try:
                return int(float(number_str))
            except ValueError:
                pass
        return None

    async def _attempt_steal(self, client, reply_to_message_id: int) -> None:
        try:
            # Отримати останнє повідомлення бота
            messages = await client.get_messages(HORSE_BOT_USERNAME, limit=1)
            if not messages:
                self.audit.write("WARNING", "horse_rating", "no_messages_for_steal", "No messages from bot", account_id=self.account_id)
                return

            last_message = messages[0]

            # Відправити "вкрасти" як reply на останнє повідомлення
            await client.send_message(HORSE_BOT_USERNAME, "вкрасти", reply_to=last_message.id)
            self._update_state(last_steal_at=utcnow(), cooldown_until=utcnow() + timedelta(seconds=STEAL_COOLDOWN_SECONDS), status="STEAL_SENT")
            self.audit.write("INFO", "horse_rating", "steal_sent", "Sent 'вкрасти' command", account_id=self.account_id)

            await asyncio.sleep(3)
            messages = await client.get_messages(HORSE_BOT_USERNAME, limit=1)
            if messages and messages[0].text:
                response_text = messages[0].text.lower()
                if "зараз відбувається крадіжка" in response_text or "зараз вже відбувається пограбування" in response_text:
                    self.audit.write("INFO", "horse_rating", "steal_in_progress", "Theft in progress, will retry in 69 seconds", account_id=self.account_id)
                    self._update_state(cooldown_until=utcnow() + timedelta(seconds=RETRY_AFTER_STEAL_IN_PROGRESS_SECONDS), status="WAITING_FOR_RETRY")
                else:
                    self.audit.write("INFO", "horse_rating", "steal_response", f"Response: {messages[0].text[:100]}", account_id=self.account_id)

        except Exception as exc:
            self._update_state(last_error=str(exc), status="STEAL_ERROR")
            self.audit.write("ERROR", "horse_rating", "steal_error", f"Steal error: {type(exc).__name__}: {str(exc)}", account_id=self.account_id)

    def _update_state(self, **kwargs) -> None:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db.session() as session:
                    state = session.get(HorseRatingState, self.account_id)
                    if state is None:
                        state = HorseRatingState(account_id=self.account_id)
                        session.add(state)
                    for key, value in kwargs.items():
                        setattr(state, key, value)
                    state.updated_at = utcnow()
                    session.commit()
                    return
            except Exception as exc:
                if attempt < max_retries - 1:
                    import time
                    time.sleep(0.5 * (attempt + 1))
                else:
                    self.audit.write("ERROR", "horse_rating", "state_update_failed", f"Failed to update state after {max_retries} retries: {type(exc).__name__}", account_id=self.account_id)
