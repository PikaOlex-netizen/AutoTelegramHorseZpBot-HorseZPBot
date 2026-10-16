from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from ..config import AppConfig, DEFAULT_SETTINGS
from ..database import Database
from ..models import Setting


class SettingsService:
    def __init__(self, db: Database, config: AppConfig) -> None:
        self.db = db
        self.config = config

    def initialize(self) -> None:
        configured = {
            **DEFAULT_SETTINGS,
            "delete_after_seconds": self.config.delete_after_seconds,
            "max_delete_retries": self.config.max_delete_retries,
            "pch_groups": self.config.pch_groups,
            "pb_groups": self.config.pb_groups,
            "group_delay_seconds": self.config.group_delay_seconds,
            "log_level": self.config.log_level,
            "auto_reconnect": self.config.auto_reconnect,
        }
        with self.db.session() as session:
            existing = {s.key for s in session.scalars(select(Setting)).all()}
            for key, value in configured.items():
                if key not in existing:
                    session.add(Setting(key=key, value=json.dumps(value)))

    def get_all(self) -> dict[str, Any]:
        with self.db.session() as session:
            # Scheduler templates deliberately use the same table but are plain
            # text, whereas the UI settings are JSON-encoded scalars.  Restrict
            # this response to configurable settings so a message body can never
            # make the Settings endpoint fail to decode.
            rows = {s.key: s.value for s in session.scalars(select(Setting)).all()}
            values: dict[str, Any] = {}
            for key in DEFAULT_SETTINGS:
                raw = rows.get(key)
                if raw is None:
                    continue
                try:
                    values[key] = json.loads(raw)
                except json.JSONDecodeError:
                    # A legacy/manual value is ignored in favour of the safe
                    # default rather than breaking the whole Web UI.
                    continue
        return {**DEFAULT_SETTINGS, **values}

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.db.session() as session:
            for key, value in values.items():
                setting = session.get(Setting, key)
                if setting is None:
                    session.add(Setting(key=key, value=json.dumps(value)))
                else:
                    setting.value = json.dumps(value)
        return self.get_all()

    def get(self, key: str) -> Any:
        return self.get_all()[key]
