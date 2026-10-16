from __future__ import annotations

from typing import Any

from sqlalchemy import select
from telethon import utils
from telethon.tl.types import Channel

from ..database import Database
from ..models import Chat
from .audit import AuditLog
from .telegram_client import TelegramService


def chat_to_dict(chat: Chat) -> dict[str, Any]:
    return {
        "chat_id": chat.chat_id, "title": chat.title, "username": chat.username,
        "chat_type": chat.chat_type, "enabled": chat.enabled, "pch_enabled": chat.pch_enabled,
        "pb_enabled": chat.pb_enabled, "gemini_enabled": chat.gemini_enabled,
        "keyword_reply_enabled": chat.keyword_reply_enabled,
        "added_at": chat.added_at, "updated_at": chat.updated_at,
    }


class ChatManager:
    def __init__(self, db: Database, telegram: TelegramService, audit: AuditLog) -> None:
        self.db = db
        self.telegram = telegram
        self.audit = audit
    def list(self, account_id: int, status: str | None = None, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as session:
            statement = select(Chat).where(
                Chat.account_id == account_id, Chat.chat_type == "supergroup",
            ).order_by(Chat.title.collate("NOCASE"), Chat.chat_id)
            if not include_archived:
                statement = statement.where(Chat.enabled.is_(True))
            if status:
                statement = statement.where(Chat.bot_status == status)
            return [chat_to_dict(chat) for chat in session.scalars(statement).all()]

    def get(self, account_id: int, chat_id: int) -> dict[str, Any] | None:
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            return chat_to_dict(chat) if chat else None

    async def refresh_from_telegram(self, account_id: int) -> dict[str, int]:
        dialogs = await self.telegram.list_dialogs(account_id)
        added = updated = 0
        with self.db.session() as session:
            for item in dialogs:
                chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == item["chat_id"]))
                if chat is None:
                    session.add(Chat(**item, account_id=account_id, bot_status="MANUAL"))
                    added += 1
                else:
                    chat.title = item["title"]
                    chat.username = item["username"]
                    chat.chat_type = item["chat_type"]
                    updated += 1
        self.audit.write("INFO", "chats", "refresh_completed", f"Chat refresh completed: {added} added, {updated} updated", account_id=account_id)
        return {"added": added, "updated": updated, "total": len(dialogs)}

    async def add_from_telegram(self, account_id: int, chat_id: int) -> dict[str, Any]:
        # Resolve through Telegram so a pasted id is normalized to its signed peer id.
        client = await self.telegram.require_authorized(account_id)
        entity = await client.get_entity(chat_id)
        if not isinstance(entity, Channel) or not entity.megagroup:
            raise ValueError("Only Telegram supergroups can be added to Chats")
        title = getattr(entity, "title", None) or utils.get_display_name(entity)
        if not title:
            raise ValueError("Telegram chat could not be resolved")
        # Store Telegram's signed peer id (-123 / -100123), so raw ids from two
        # peer types cannot collide and a pasted peer id cannot duplicate a dialog.
        chat_id = int(utils.get_peer_id(entity))
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            if chat is None:
                chat = Chat(
                    account_id=account_id, chat_id=chat_id, title=title,
                    username=getattr(entity, "username", None), chat_type="supergroup",
                    bot_status="MANUAL",
                )
                session.add(chat)
            else:
                # Re-adding a manually archived group restores only that group;
                # no historical chats are silently re-enabled by a dialog refresh.
                chat.enabled = True
                chat.title = title
                chat.username = getattr(entity, "username", None)
                chat.bot_status = "MANUAL"
        self.audit.write("INFO", "chats", "manually_added", "Chat added to the manual working list", account_id=account_id, chat_id=chat_id)
        result = self.get(account_id, chat_id)
        assert result is not None
        return result

    async def shutdown(self) -> None:
        return None

    def update_assignment(
        self, account_id: int, chat_id: int, *, pch_enabled: bool | None = None, pb_enabled: bool | None = None,
        gemini_enabled: bool | None = None, keyword_reply_enabled: bool | None = None, enabled: bool | None = None,
    ) -> dict[str, Any]:
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            if chat is None:
                raise KeyError(f"Chat {chat_id} does not exist")
            if pch_enabled is not None:
                chat.pch_enabled = pch_enabled
            if pb_enabled is not None:
                chat.pb_enabled = pb_enabled
            if gemini_enabled is not None:
                chat.gemini_enabled = gemini_enabled
            if keyword_reply_enabled is not None:
                chat.keyword_reply_enabled = keyword_reply_enabled
            if pch_enabled is True or pb_enabled is True:
                chat.bot_status = "MANUAL"
            if enabled is not None:
                chat.enabled = enabled
                if not enabled:
                    chat.pch_enabled = False
                    chat.pb_enabled = False
                    chat.gemini_enabled = False
                    chat.keyword_reply_enabled = False
            session.flush()
            result = chat_to_dict(chat)
        self.audit.write("INFO", "chats", "assignment_updated", "Chat assignment updated", account_id=account_id, chat_id=chat_id)
        return result

    def remove_from_mailings(self, account_id: int, chat_id: int) -> None:
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            if chat is None:
                raise KeyError(f"Chat {chat_id} does not exist")
            chat.pch_enabled = False
            chat.pb_enabled = False
            chat.gemini_enabled = False
            chat.keyword_reply_enabled = False
            chat.enabled = False
        self.audit.write("INFO", "chats", "archived", "Chat removed from manual working list", account_id=account_id, chat_id=chat_id)

    def eligible_chat_ids(self, account_id: int, mailing_type: str) -> list[int]:
        """Manual target list; only the user's assignment is considered."""
        field = Chat.pch_enabled if mailing_type == "PCH" else Chat.pb_enabled
        with self.db.session() as session:
            return list(session.scalars(
                select(Chat.chat_id).where(
                    Chat.account_id == account_id, Chat.chat_type == "supergroup",
                    Chat.enabled.is_(True), field.is_(True),
                ).order_by(Chat.chat_id)
            ).all())

    def is_currently_eligible(self, account_id: int, chat_id: int, mailing_type: str | None = None) -> bool:
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            if not chat or chat.chat_type != "supergroup" or not chat.enabled:
                return False
            if mailing_type == "PCH":
                return chat.pch_enabled
            if mailing_type == "PB":
                return chat.pb_enabled
            # PS and Farm (single-chat selections) only require the chat to be enabled
            if mailing_type in {"PS", "FARM"}:
                return True
            return True

    def is_gemini_enabled(self, account_id: int, chat_id: int) -> bool:
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            return bool(chat and chat.enabled and chat.gemini_enabled)

    def is_keyword_reply_enabled(self, account_id: int, chat_id: int) -> bool:
        """Keyword replies are opt-in per manually selected group."""
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == account_id, Chat.chat_id == chat_id))
            return bool(chat and chat.enabled and chat.keyword_reply_enabled)

    def has_keyword_reply_enabled(self, account_id: int) -> bool:
        """Avoid opening a Telegram session when this optional rule is unused."""
        with self.db.session() as session:
            return session.scalar(
                select(Chat.id).where(
                    Chat.account_id == account_id,
                    Chat.enabled.is_(True),
                    Chat.keyword_reply_enabled.is_(True),
                ).limit(1)
            ) is not None
