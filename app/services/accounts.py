from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..database import Database
from ..models import TelegramAccount
from .audit import AuditLog
from .settings import SettingsService


MAX_ACCOUNTS = 3
SELECTED_ACCOUNT_KEY = "selected_account_id"


def account_to_dict(account: TelegramAccount) -> dict[str, Any]:
    return {"id": account.id, "label": account.label, "phone": account.phone, "active": account.active}


class AccountService:
    def __init__(self, db: Database, settings: SettingsService, audit: AuditLog) -> None:
        self.db = db
        self.settings = settings
        self.audit = audit

    def initialize(self) -> None:
        with self.db.session() as session:
            if session.scalar(select(func.count(TelegramAccount.id))) == 0:
                session.add(TelegramAccount(label="Аккаунт 1"))
        selected = self._raw_selected()
        if selected is None or self.get(selected) is None:
            first = self.list()[0]
            self.settings.update({SELECTED_ACCOUNT_KEY: first["id"]})

    def _raw_selected(self) -> int | None:
        with self.db.session() as session:
            from ..models import Setting
            setting = session.get(Setting, SELECTED_ACCOUNT_KEY)
            if setting is None:
                return None
            try:
                return int(setting.value)
            except ValueError:
                return None

    def list(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return [account_to_dict(a) for a in session.scalars(select(TelegramAccount).where(TelegramAccount.active.is_(True)).order_by(TelegramAccount.id)).all()]

    def get(self, account_id: int) -> dict[str, Any] | None:
        with self.db.session() as session:
            account = session.get(TelegramAccount, account_id)
            return account_to_dict(account) if account and account.active else None

    def selected_id(self) -> int:
        selected = self._raw_selected()
        if selected is not None and self.get(selected):
            return selected
        accounts = self.list()
        if not accounts:
            raise RuntimeError("Немає активних Telegram акаунтів")
        self.settings.update({SELECTED_ACCOUNT_KEY: accounts[0]["id"]})
        return accounts[0]["id"]

    def select(self, account_id: int) -> dict[str, Any]:
        account = self.get(account_id)
        if account is None:
            raise KeyError(f"Акаунт {account_id} не існує")
        self.settings.update({SELECTED_ACCOUNT_KEY: account_id})
        self.audit.write("INFO", "accounts", "selected", f"Вибрано {account['label']}", account_id=account_id)
        return account

    def create(self, label: str) -> dict[str, Any]:
        clean = label.strip() or f"Акаунт {len(self.list()) + 1}"
        if len(clean) > 80:
            raise ValueError("Назва акаунту має бути 80 символів або менше")
        with self.db.session() as session:
            active_count = session.scalar(select(func.count(TelegramAccount.id)).where(TelegramAccount.active.is_(True)))
            if active_count >= MAX_ACCOUNTS:
                raise ValueError("Підтримується максимум три Telegram акаунти")
            account = TelegramAccount(label=clean)
            session.add(account)
            session.flush()
            result = account_to_dict(account)
        self.audit.write("INFO", "accounts", "created", f"Створено {clean}", account_id=result["id"])
        return result

    def set_phone(self, account_id: int, phone: str) -> None:
        with self.db.session() as session:
            account = session.get(TelegramAccount, account_id)
            if account:
                account.phone = phone

    def rename(self, account_id: int, new_label: str) -> dict[str, Any]:
        clean = new_label.strip() or f"Акаунт {account_id}"
        if len(clean) > 80:
            raise ValueError("Назва акаунту має бути 80 символів або менше")
        with self.db.session() as session:
            account = session.get(TelegramAccount, account_id)
            if account is None or not account.active:
                raise KeyError(f"Акаунт {account_id} не існує")
            old_label = account.label
            account.label = clean
            result = account_to_dict(account)
        self.audit.write("INFO", "accounts", "renamed", f"Перейменовано з {old_label} на {clean}", account_id=account_id)
        return result

