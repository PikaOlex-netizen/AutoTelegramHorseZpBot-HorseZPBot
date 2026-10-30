from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import events

from ..database import Database
from ..models import KeywordReplyGlobalState, KeywordReplyState, KeywordReplyUserState, utcnow
from .accounts import AccountService
from .audit import AuditLog
from .chats import ChatManager
from .messages import MessageManager
from .telegram_client import TelegramService, TelegramUnavailable


TRIGGER_WORDS = {"ом", "оч", "об"}
REPLY_TEXT = "Вкрасти"
ACCOUNT_COOLDOWN_SECONDS = 60
USER_COOLDOWN_SECONDS = 60 * 60
GLOBAL_COOLDOWN_SECONDS = 60


class KeywordReplyAssistant:
    """Точні короткі відповіді в явно увімкнених ручних групах.

    Хвилинне обмеження для всього акаунту захищає акаунт відправника від сплесків
    по групах. Друге обмеження запобігає тому, що той самий відправник Telegram отримає
    відповідь знову протягом години, навіть після закінчення хвилини на рівні акаунту.
    """

    def __init__(
        self, db: Database, accounts: AccountService, telegram: TelegramService,
        chats: ChatManager, messages: MessageManager, audit: AuditLog,
    ) -> None:
        self.db = db
        self.accounts = accounts
        self.telegram = telegram
        self.chats = chats
        self.messages = messages
        self.audit = audit
        self._handlers: dict[int, Any] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_lock = asyncio.Lock()
        self._initialize_global_state()

    def _initialize_global_state(self) -> None:
        """Забезпечити існування запису глобального стану."""
        with self.db.session() as session:
            if session.get(KeywordReplyGlobalState, 1) is None:
                session.add(KeywordReplyGlobalState(id=1))

    def _accounts_with_keyword_reply_enabled(self) -> int:
        """Порахувати, скільки акаунтів мають хоча б один чат з увімкненою відповіддю на ключові слова."""
        count = 0
        for account in self.accounts.list():
            if self.chats.has_keyword_reply_enabled(account["id"]):
                count += 1
        return count

    def _should_apply_global_cooldown(self) -> bool:
        """Глобальний cooldown застосовується, коли 2 або більше акаунтів мають увімкнену відповідь на ключові слова."""
        return self._accounts_with_keyword_reply_enabled() >= 2

    async def ensure_listener(self, account_id: int) -> None:
        # This responder is opt-in at group level. Avoid opening Telegram at
        # startup when no manual group uses the feature.
        if not self.chats.has_keyword_reply_enabled(account_id):
            return
        if account_id in self._handlers:
            # A logout removes the Telethon client. Do not leave a stale marker
            # that would prevent the next authenticated session from listening.
            if self.telegram.clients.get(account_id) is not None:
                return
            self._handlers.pop(account_id, None)
        try:
            client = await self.telegram.require_authorized(account_id)
        except TelegramUnavailable:
            return
        except Exception as exc:
            self.audit.write(
                "WARNING", "keyword_reply", "listener_unavailable",
                f"Keyword reply listener will retry later: {type(exc).__name__}", account_id=account_id,
            )
            return

        async def handler(event: Any) -> None:
            await self.handle_incoming(account_id, event)

        client.add_event_handler(handler, events.NewMessage(incoming=True))
        self._handlers[account_id] = handler
        self.audit.write("INFO", "keyword_reply", "listener_attached", "Keyword reply listener attached to account", account_id=account_id)

    @staticmethod
    def _remaining_seconds(last_sent_at: datetime | None, now: datetime, cooldown_seconds: int) -> int:
        if last_sent_at is None:
            return 0
        if last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
        remaining = (last_sent_at + timedelta(seconds=cooldown_seconds) - now).total_seconds()
        return max(0, int(remaining))

    async def handle_incoming(self, account_id: int, event: Any) -> bool:
        """Повернути, чи була відправлена реальна відповідь; корисно для детермінованих тестів."""
        text = (getattr(event, "raw_text", "") or "").strip().casefold()
        if text not in TRIGGER_WORDS:
            return False
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        source_message = getattr(event, "message", None)
        source_message_id = getattr(source_message, "id", None)
        sender_id = getattr(event, "sender_id", None) or getattr(source_message, "sender_id", None)
        if not chat_id or not source_message_id or not self.chats.is_keyword_reply_enabled(account_id, chat_id):
            return False
        if not sender_id:
            # Анонімні відправники навмисно пропускаються: ми не можемо правдиво
            # забезпечити запитуваний погодинний захист для кожного користувача без ID.
            self.audit.write(
                "INFO", "keyword_reply", "sender_unavailable",
                "Відповідь на ключові слова пропущено, оскільки Telegram не надав ID відправника",
                account_id=account_id, chat_id=chat_id,
            )
            return False
        sender_id = int(sender_id)

        # Перевірити глобальний cooldown, якщо 2+ акаунти мають увімкнену відповідь на ключові слова
        apply_global = self._should_apply_global_cooldown()
        if apply_global:
            async with self._global_lock:
                now = utcnow()
                with self.db.session() as session:
                    global_state = session.get(KeywordReplyGlobalState, 1)
                    global_remaining = self._remaining_seconds(
                        global_state.last_sent_at, now, GLOBAL_COOLDOWN_SECONDS,
                    )
                if global_remaining:
                    self.audit.write(
                        "INFO", "keyword_reply", "global_rate_limited",
                        f"Відповідь на ключові слова пропущено; глобальний cooldown має {global_remaining} секунд залишилося (2+ акаунти увімкнено)",
                        account_id=account_id, chat_id=chat_id,
                    )
                    return False

        async with self._locks[account_id]:
            now = utcnow()
            with self.db.session() as session:
                account_state = session.get(KeywordReplyState, account_id)
                if account_state is None:
                    account_state = KeywordReplyState(account_id=account_id)
                    session.add(account_state)
                user_state = session.get(KeywordReplyUserState, (account_id, sender_id))
                if user_state is None:
                    user_state = KeywordReplyUserState(account_id=account_id, sender_id=sender_id)
                    session.add(user_state)

                account_remaining = self._remaining_seconds(
                    account_state.last_sent_at, now, ACCOUNT_COOLDOWN_SECONDS,
                )
                user_remaining = self._remaining_seconds(
                    user_state.last_sent_at, now, USER_COOLDOWN_SECONDS,
                )
                if account_remaining or user_remaining:
                    previous_account = previous_user = previous_global = None
                else:
                    # Заняти обидва cooldown перед Telegram I/O. Це запобігає
                    # сплеску оновлень від створення дублікатів відповідей.
                    previous_account = account_state.last_sent_at
                    previous_user = user_state.last_sent_at
                    previous_global = None
                    if apply_global:
                        global_state = session.get(KeywordReplyGlobalState, 1)
                        previous_global = global_state.last_sent_at
                        global_state.last_sent_at = now
                    account_state.last_sent_at = now
                    user_state.last_sent_at = now

            if account_remaining:
                self.audit.write(
                    "INFO", "keyword_reply", "account_rate_limited",
                    f"Відповідь на ключові слова пропущено; cooldown акаунту має {account_remaining} секунд залишилося",
                    account_id=account_id, chat_id=chat_id,
                )
                return False
            if user_remaining:
                self.audit.write(
                    "INFO", "keyword_reply", "user_rate_limited",
                    f"Відповідь на ключові слова пропущено; cooldown відправника має {user_remaining} секунд залишилося",
                    account_id=account_id, chat_id=chat_id,
                )
                return False

            try:
                reply_id = await self.messages.send(
                    account_id, chat_id, REPLY_TEXT, "REPLY", reply_to_message_id=int(source_message_id),
                )
            except Exception as exc:
                # Звільнити лише претензії, зроблені для цього конкретного невдалого відправлення.
                with self.db.session() as session:
                    account_state = session.get(KeywordReplyState, account_id)
                    user_state = session.get(KeywordReplyUserState, (account_id, sender_id))
                    if account_state and account_state.last_sent_at == now:
                        account_state.last_sent_at = previous_account
                    if user_state and user_state.last_sent_at == now:
                        user_state.last_sent_at = previous_user
                    if apply_global and previous_global is not None:
                        global_state = session.get(KeywordReplyGlobalState, 1)
                        if global_state and global_state.last_sent_at == now:
                            global_state.last_sent_at = previous_global
                self.audit.write(
                    "ERROR", "keyword_reply", "send_failed", f"Відповідь на ключові слова не вдалася: {type(exc).__name__}",
                    account_id=account_id, chat_id=chat_id,
                )
                return False
            self.audit.write(
                "INFO", "keyword_reply", "replied", "Replied ‘Вкрасти’ to a keyword message",
                account_id=account_id, chat_id=chat_id, message_id=reply_id,
            )
            return True

    async def start(self) -> None:
        for account in self.accounts.list():
            await self.ensure_listener(account["id"])

    async def handle_message(self, account_id: int, event: Any) -> None:
        # Handlers are local now, no-op for centralized dispatch
        pass

    async def shutdown(self) -> None:
        for account_id, handler in list(self._handlers.items()):
            client = self.telegram.clients.get(account_id)
            if client:
                client.remove_event_handler(handler)
        self._handlers.clear()
