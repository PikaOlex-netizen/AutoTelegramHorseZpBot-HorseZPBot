from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ..database import Database
from ..models import FeedState, UserCooldown, utcnow
from .audit import AuditLog
from .accounts import AccountService
from .chats import ChatManager
from .messages import MessageManager
from .telegram_client import TelegramService, TelegramUnavailable


FEED_COOLDOWN_SECONDS = 4 * 60 * 60 + 22 * 60 + 22  # 4 години + 22 хвилини + 22 секунди


class FeedManager:
    """Менеджер для функції 'Нагодувати гівном' з персональним кулдауном для кожного користувача."""

    def __init__(
        self, db: Database, chats: ChatManager, messages: MessageManager,
        audit: AuditLog, telegram: TelegramService, account_id: int,
    ) -> None:
        self.db = db
        self.chats = chats
        self.messages = messages
        self.audit = audit
        self.telegram = telegram
        self.account_id = account_id
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        with self.db.session() as session:
            if session.get(FeedState, self.account_id) is None:
                session.add(FeedState(account_id=self.account_id))

    def get_state(self) -> dict[str, Any]:
        with self.db.session() as session:
            state = session.get(FeedState, self.account_id)
            assert state is not None
            cooldowns = session.query(UserCooldown).filter_by(account_id=self.account_id).order_by(UserCooldown.last_fed_at.desc()).limit(100).all()
            cooldowns_list = [
                {
                    "user_id": c.user_id,
                    "username": c.username,
                    "last_fed_at": c.last_fed_at.isoformat() if c.last_fed_at else None,
                }
                for c in cooldowns
            ]
            return {
                "account_id": self.account_id,
                "enabled": state.enabled,
                "username_to_feed": state.username_to_feed,
                "message_text": state.message_text or "нагодувати гівном",
                "status": state.status,
                "recent_feds": cooldowns_list,
            }

    async def configure(self, *, username_to_feed: str | None, text: str | None, enabled: bool) -> dict[str, Any]:
        async with self._lock:
            text = (text or "").strip()
            username_to_feed = (username_to_feed or "").strip()
            if enabled:
                if not username_to_feed:
                    raise ValueError("Введіть ім'я користувача для 'Нагодувати гівном'")
                if not text:
                    raise ValueError("Введіть текст")
            with self.db.session() as session:
                state = session.get(FeedState, self.account_id)
                assert state is not None
                state.username_to_feed = username_to_feed or "PikaOlex"
                state.message_text = text or "нагодувати гівном"
                state.enabled = enabled
                state.status = "RUNNING" if enabled else "STOPPED"
            self.audit.write(
                "INFO", "feed", "configured",
                f"Функція 'Нагодувати гівном' увімкнено для @{username_to_feed}" if enabled else "Функція 'Нагодувати гівном' зупинено",
                account_id=self.account_id,
            )
            return self.get_state()

    async def handle_incoming_message(self, chat_id: int, user_id: int, message_id: int, username: str | None = None) -> bool:
        """Обробляє вхідне повідомлення. Повертає True якщо була відправлена відповідь."""
        with self.db.session() as session:
            state = session.get(FeedState, self.account_id)
            if not state or not state.enabled:
                return False

            # Перевіряємо, чи це вказаний користувач
            if state.username_to_feed and username != state.username_to_feed:
                return False

            # Перевіряємо, чи чат у списках ПЧ або ПБ
            if not self.chats.is_currently_eligible(self.account_id, chat_id):
                return False

            cooldown = session.query(UserCooldown).filter_by(
                account_id=self.account_id, user_id=user_id
            ).first()

            now = utcnow()
            if cooldown and cooldown.last_fed_at:
                # Переконатися, що last_fed_at має timezone
                if cooldown.last_fed_at.tzinfo is None:
                    cooldown.last_fed_at = cooldown.last_fed_at.replace(tzinfo=timezone.utc)
                if cooldown.last_fed_at + timedelta(seconds=FEED_COOLDOWN_SECONDS) > now:
                    return False  # Кулдаун ще не пройшов

            if not cooldown:
                cooldown = UserCooldown(
                    account_id=self.account_id,
                    user_id=user_id,
                    username=username,
                    last_fed_at=now
                )
                session.add(cooldown)
            else:
                cooldown.last_fed_at = now
                cooldown.username = username

            session.commit()

        try:
            await self.messages.send(
                self.account_id, chat_id, state.message_text, "FEED",
                reply_to_message_id=message_id, schedule_deletion=False
            )
            self.audit.write(
                "INFO", "feed", "fed",
                f"Користувача {username or user_id} нагодовано в чаті {chat_id}",
                account_id=self.account_id, chat_id=chat_id, user_id=user_id
            )
            return True
        except Exception as exc:
            self.audit.write(
                "ERROR", "feed", "send_failed",
                f"Не вдалося надіслати відповідь: {type(exc).__name__}",
                account_id=self.account_id, chat_id=chat_id
            )
            return False

    async def ensure_listener(self) -> None:
        """Listener is now centralized in main.py, this is a no-op."""
        pass

    async def handle_message(self, account_id: int, event: Any) -> None:
        """Handle incoming message from centralized event handler."""
        if account_id != self.account_id:
            return
        try:
            chat_id = getattr(event, "chat_id", None)
            user_id = getattr(event, "sender_id", None)
            message_id = getattr(getattr(event, "message", None), "id", None)
            username = None
            sender = getattr(event, "sender", None)
            if sender:
                username = getattr(sender, 'username', None)
            if chat_id and user_id and message_id:
                await self.handle_incoming_message(chat_id, user_id, message_id, username)
        except TelegramUnavailable:
            pass
        except Exception as exc:
            self.audit.write(
                "ERROR", "feed", "handler_failed",
                f"Обробка повідомлення не вдалася: {type(exc).__name__}: {str(exc)}",
                account_id=self.account_id
            )


class FeedRegistry:
    def __init__(self, db: Database, chats: ChatManager, messages: MessageManager, audit: AuditLog, accounts: AccountService, telegram: TelegramService) -> None:
        self.db, self.chats, self.messages, self.audit, self.accounts, self.telegram = db, chats, messages, audit, accounts, telegram
        self._managers: dict[int, FeedManager] = {}

    def for_account(self, account_id: int) -> FeedManager:
        if self.accounts.get(account_id) is None:
            raise KeyError(f"Account {account_id} does not exist")
        manager = self._managers.get(account_id)
        if manager is None:
            manager = FeedManager(self.db, self.chats, self.messages, self.audit, self.telegram, account_id)
            manager.initialize()
            self._managers[account_id] = manager
        return manager

    def initialize(self) -> None:
        for account in self.accounts.list():
            self.for_account(account["id"])

    async def ensure_listener(self, account_id: int) -> None:
        await self.for_account(account_id).ensure_listener()

    async def handle_message(self, account_id: int, event: Any) -> None:
        """Handle incoming message from centralized event handler."""
        try:
            await self.for_account(account_id).handle_message(account_id, event)
        except KeyError:
            # Account not found, ignore
            pass
        except Exception as exc:
            self.audit.write(
                "ERROR", "feed", "handler_failed",
                f"Обробка повідомлення не вдалася: {type(exc).__name__}: {str(exc)}",
                account_id=account_id
            )

    async def shutdown(self) -> None:
        pass
