from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from collections.abc import Awaitable, Callable
from typing import Any

import qrcode
from telethon import TelegramClient, errors
from telethon import utils
from telethon.tl.types import Channel

from ..config import AppConfig
from .audit import AuditLog
from .accounts import AccountService
from .settings import SettingsService


class TelegramUnavailable(RuntimeError):
    pass


class TelegramService:
    """The only component that talks to Telegram. No credentials are logged."""

    def __init__(self, config: AppConfig, audit: AuditLog, settings: SettingsService | None = None, accounts: AccountService | None = None) -> None:
        self.config = config
        self.audit = audit
        self.settings = settings
        self.accounts = accounts
        self.clients: dict[int, TelegramClient] = {}
        self._lock = asyncio.Lock()
        self._phone: dict[int, str] = {}
        self._phone_code_hash: dict[int, str] = {}
        self._qr_login: dict[int, Any] = {}
        self._qr_waiter: dict[int, asyncio.Task[None]] = {}
        self.qr_state: dict[int, str] = {}
        self._authorized_callbacks: list[Callable[[int], Awaitable[None]]] = []

    def add_authorized_callback(self, callback: Callable[[int], Awaitable[None]]) -> None:
        """Attach incoming-message handlers immediately after any login method."""
        self._authorized_callbacks.append(callback)

    async def _notify_authorized(self, account_id: int) -> None:
        for callback in self._authorized_callbacks:
            try:
                await callback(account_id)
            except Exception as exc:
                # A responder failing to attach must never invalidate a valid
                # Telegram session or make the login look unsuccessful.
                self.audit.write(
                    "ERROR", "telegram", "authorized_callback_failed",
                    f"Post-login listener setup failed: {type(exc).__name__}", account_id=account_id,
                )

    def _require_credentials(self) -> None:
        if not self.config.api_id or not self.config.api_hash:
            raise TelegramUnavailable("API_ID and API_HASH must be configured in .env before Telegram can connect")

    def _session_path(self, account_id: int) -> str:
        # Preserve the original single-account session as Account 1 on upgrade.
        if account_id == 1:
            return self.config.telegram_session_path
        return str(self.config.data_dir / f"telegram-account-{account_id}.session")

    def _state(self, account_id: int) -> str:
        return self.qr_state.get(account_id, "IDLE")

    async def _get_client(self, account_id: int) -> TelegramClient:
        self._require_credentials()
        if self.accounts and self.accounts.get(account_id) is None:
            raise TelegramUnavailable("Selected Telegram account does not exist")
        client = self.clients.get(account_id)
        if client is None:
            auto_reconnect = bool(self.settings.get("auto_reconnect")) if self.settings else self.config.auto_reconnect
            client = TelegramClient(
                self._session_path(account_id), self.config.api_id, self.config.api_hash,
                auto_reconnect=auto_reconnect, connection_retries=5, retry_delay=2,
            )
            self.clients[account_id] = client
        if not client.is_connected():
            await client.connect()
            self.audit.write("INFO", "telegram", "connected", "Connected to Telegram", account_id=account_id)
        return client

    async def status(self, account_id: int) -> dict[str, Any]:
        if not self.config.api_id or not self.config.api_hash:
            return {"configured": False, "connected": False, "authorized": False, "auth_stage": "CONFIGURATION_REQUIRED", "qr_state": self._state(account_id)}
        try:
            client = await self._get_client(account_id)
            authorized = await client.is_user_authorized()
            return {
                "configured": True, "connected": client.is_connected(), "authorized": authorized,
                "auth_stage": "AUTHORIZED" if authorized else self._state(account_id) if self._state(account_id) != "IDLE" else "PHONE_OR_QR_REQUIRED",
                "qr_state": self._state(account_id),
            }
        except Exception as exc:
            self.audit.write("WARNING", "telegram", "status_failed", f"Telegram status unavailable: {type(exc).__name__}", account_id=account_id)
            return {"configured": True, "connected": False, "authorized": False, "auth_stage": "CONNECTION_ERROR", "qr_state": self._state(account_id)}

    async def require_authorized(self, account_id: int) -> TelegramClient:
        client = await self._get_client(account_id)
        if not await client.is_user_authorized():
            raise TelegramUnavailable("Telegram authentication is required")
        return client

    async def begin_phone_login(self, account_id: int, phone: str) -> dict[str, str]:
        async with self._lock:
            client = await self._get_client(account_id)
            sent = await client.send_code_request(phone)
            self._phone[account_id] = phone
            self._phone_code_hash[account_id] = sent.phone_code_hash
            self.qr_state[account_id] = "IDLE"
            if self.accounts:
                self.accounts.set_phone(account_id, phone)
            self.audit.write("INFO", "auth", "phone_code_requested", "Telegram login code requested", account_id=account_id)
            return {"status": "CODE_SENT"}

    async def submit_code(self, account_id: int, code: str) -> dict[str, str]:
        if account_id not in self._phone or account_id not in self._phone_code_hash:
            raise TelegramUnavailable("Request a phone code before submitting it")
        client = await self._get_client(account_id)
        try:
            await client.sign_in(self._phone[account_id], code, phone_code_hash=self._phone_code_hash[account_id])
        except errors.SessionPasswordNeededError:
            self.audit.write("INFO", "auth", "password_required", "Telegram account requires 2FA password", account_id=account_id)
            return {"status": "PASSWORD_REQUIRED"}
        except errors.PhoneCodeInvalidError as exc:
            self.audit.write("WARNING", "auth", "phone_code_invalid", "Telegram login code was rejected", account_id=account_id)
            raise TelegramUnavailable("The Telegram code is invalid") from exc
        except errors.PhoneCodeExpiredError as exc:
            self.audit.write("WARNING", "auth", "phone_code_expired", "Telegram login code expired", account_id=account_id)
            raise TelegramUnavailable("The Telegram code expired; request a new code") from exc
        self._clear_phone_state(account_id)
        self.audit.write("INFO", "auth", "authenticated", "Telegram phone authentication completed", account_id=account_id)
        await self._notify_authorized(account_id)
        return {"status": "AUTHORIZED"}

    async def submit_password(self, account_id: int, password: str) -> dict[str, str]:
        client = await self._get_client(account_id)
        try:
            await client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            self.audit.write("WARNING", "auth", "password_invalid", "Telegram 2FA password was rejected", account_id=account_id)
            raise TelegramUnavailable("The 2FA password is invalid") from exc
        self._clear_phone_state(account_id)
        self.qr_state[account_id] = "AUTHORIZED"
        self.audit.write("INFO", "auth", "authenticated", "Telegram 2FA authentication completed", account_id=account_id)
        await self._notify_authorized(account_id)
        return {"status": "AUTHORIZED"}

    def _clear_phone_state(self, account_id: int) -> None:
        self._phone.pop(account_id, None)
        self._phone_code_hash.pop(account_id, None)

    async def begin_qr_login(self, account_id: int) -> dict[str, str]:
        async with self._lock:
            client = await self._get_client(account_id)
            qr_login = await client.qr_login()
            self._qr_login[account_id] = qr_login
            self.qr_state[account_id] = "WAITING_FOR_SCAN"
            old_waiter = self._qr_waiter.get(account_id)
            if old_waiter and not old_waiter.done():
                old_waiter.cancel()
            self._qr_waiter[account_id] = asyncio.create_task(self._wait_for_qr(account_id, qr_login))
            image = qrcode.make(qr_login.url)
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            image_data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            self.audit.write("INFO", "auth", "qr_created", "A live Telegram QR login token was created", account_id=account_id)
            return {"status": self._state(account_id), "qr_url": qr_login.url, "qr_image": image_data}

    async def _wait_for_qr(self, account_id: int, qr_login: Any) -> None:
        try:
            await qr_login.wait(timeout=180)
            self.qr_state[account_id] = "AUTHORIZED"
            self.audit.write("INFO", "auth", "qr_authenticated", "Telegram QR authentication completed", account_id=account_id)
            await self._notify_authorized(account_id)
        except errors.SessionPasswordNeededError:
            self.qr_state[account_id] = "PASSWORD_REQUIRED"
            self.audit.write("INFO", "auth", "qr_password_required", "Telegram QR login requires 2FA password", account_id=account_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.qr_state[account_id] = "EXPIRED"
            self.audit.write("WARNING", "auth", "qr_failed", f"QR login ended: {type(exc).__name__}", account_id=account_id)

    async def logout(self, account_id: int) -> None:
        waiter = self._qr_waiter.get(account_id)
        if waiter and not waiter.done():
            waiter.cancel()
        client = self.clients.get(account_id)
        if client is not None:
            try:
                if await client.is_user_authorized():
                    await client.log_out()
            finally:
                await client.disconnect()
                self.clients.pop(account_id, None)
        self.qr_state[account_id] = "IDLE"
        self._clear_phone_state(account_id)
        self.audit.write("INFO", "auth", "logout", "Telegram session logged out", account_id=account_id)

    async def list_dialogs(self, account_id: int) -> list[dict[str, Any]]:
        client = await self.require_authorized(account_id)
        dialogs: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, Channel) or not entity.megagroup:
                continue
            title = dialog.name or utils.get_display_name(entity) or str(getattr(entity, "id", ""))
            dialogs.append({
                # Use Telegram's signed peer id, not the raw per-type id.
                "chat_id": int(utils.get_peer_id(entity)), "title": title, "username": getattr(entity, "username", None),
                "chat_type": "supergroup",
            })
        self.audit.write("INFO", "chats", "discovered", f"Discovered {len(dialogs)} Telegram supergroups", account_id=account_id)
        return dialogs

    async def send_message(self, account_id: int, chat_id: int, text: str, reply_to_message_id: int | None = None) -> int:
        client = await self.require_authorized(account_id)
        entity = await client.get_input_entity(chat_id)
        message = await client.send_message(entity, text, reply_to=reply_to_message_id)
        return int(message.id)

    async def message_exists(self, account_id: int, chat_id: int, message_id: int) -> bool:
        client = await self.require_authorized(account_id)
        entity = await client.get_input_entity(chat_id)
        message = await client.get_messages(entity, ids=message_id)
        # MessageEmpty and None both mean Telegram no longer exposes the message.
        return bool(message and getattr(message, "id", None))

    async def delete_message(self, account_id: int, chat_id: int, message_id: int) -> None:
        client = await self.require_authorized(account_id)
        entity = await client.get_input_entity(chat_id)
        await client.delete_messages(entity, [message_id])

    async def disconnect(self) -> None:
        for account_id, client in list(self.clients.items()):
            if client.is_connected():
                await client.disconnect()
                self.audit.write("INFO", "telegram", "disconnected", "Telegram connection closed", account_id=account_id)
        self.clients.clear()
