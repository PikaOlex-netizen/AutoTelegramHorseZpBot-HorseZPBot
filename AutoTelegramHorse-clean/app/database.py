from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_engine(url, connect_args={"check_same_thread": False})
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        self._migrate_legacy_schema()

    def _migrate_legacy_schema(self) -> None:
        """Small SQLite migrations for installations created before multi-account.

        SQLAlchemy's create_all creates tables but cannot add columns or replace a
        primary key. Existing chats are copied into a per-account table and
        assigned to Account 1, preserving all user data and delivery history.
        """
        with self.engine.begin() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chats)").fetchall()}
            if columns and "id" not in columns:
                connection.exec_driver_sql("""
                    CREATE TABLE chats_multi (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER NOT NULL DEFAULT 1,
                        chat_id INTEGER NOT NULL,
                        title VARCHAR(512) NOT NULL DEFAULT '', username VARCHAR(255),
                        chat_type VARCHAR(40) NOT NULL DEFAULT 'unknown', enabled BOOLEAN NOT NULL DEFAULT 1,
                        pch_enabled BOOLEAN NOT NULL DEFAULT 0, pb_enabled BOOLEAN NOT NULL DEFAULT 0,
                        gemini_enabled BOOLEAN NOT NULL DEFAULT 0,
                        keyword_reply_enabled BOOLEAN NOT NULL DEFAULT 0,
                        bot_username VARCHAR(255) NOT NULL DEFAULT 'legacy',
                        bot_status VARCHAR(32) NOT NULL DEFAULT 'MANUAL', bot_checked_at DATETIME,
                        added_at DATETIME, updated_at DATETIME,
                        CONSTRAINT uq_chat_account_peer UNIQUE (account_id, chat_id)
                    )
                """)
                connection.exec_driver_sql("""
                    INSERT INTO chats_multi (
                        account_id, chat_id, title, username, chat_type, enabled,
                        pch_enabled, pb_enabled, bot_username, bot_status,
                        bot_checked_at, added_at, updated_at
                    ) SELECT 1, chat_id, title, username, chat_type, enabled,
                        pch_enabled, pb_enabled, 'legacy', 'MANUAL',
                        NULL, added_at, updated_at FROM chats
                """)
                connection.exec_driver_sql("DROP TABLE chats")
                connection.exec_driver_sql("ALTER TABLE chats_multi RENAME TO chats")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_chats_account_id ON chats (account_id)")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_chats_chat_id ON chats (chat_id)")
            else:
                chat_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chats)").fetchall()}
                if chat_columns and "gemini_enabled" not in chat_columns:
                    connection.exec_driver_sql("ALTER TABLE chats ADD COLUMN gemini_enabled BOOLEAN NOT NULL DEFAULT 0")
                if chat_columns and "keyword_reply_enabled" not in chat_columns:
                    connection.exec_driver_sql("ALTER TABLE chats ADD COLUMN keyword_reply_enabled BOOLEAN NOT NULL DEFAULT 0")
            if "bot_username" in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chats)").fetchall()}:
                # This legacy metadata is no longer used by the application.
                connection.exec_driver_sql("UPDATE chats SET bot_username = 'legacy'")
            outbound_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(outbound_messages)").fetchall()}
            if outbound_columns and "account_id" not in outbound_columns:
                connection.exec_driver_sql("ALTER TABLE outbound_messages ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_outbound_messages_account_id ON outbound_messages (account_id)")
            log_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(logs)").fetchall()}
            if log_columns and "account_id" not in log_columns:
                connection.exec_driver_sql("ALTER TABLE logs ADD COLUMN account_id INTEGER")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_logs_account_id ON logs (account_id)")
            delivery_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(scheduler_deliveries)").fetchall()}
            if delivery_columns and "account_id" not in delivery_columns:
                connection.exec_driver_sql("ALTER TABLE scheduler_deliveries ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_scheduler_deliveries_account_id ON scheduler_deliveries (account_id)")
            states = [row[0] for row in connection.exec_driver_sql("SELECT mailing_type FROM scheduler_states").fetchall()]
            for mailing_type in states:
                if mailing_type in {"PCH", "PB"}:
                    connection.exec_driver_sql(
                        "UPDATE scheduler_states SET mailing_type = ? WHERE mailing_type = ?",
                        (f"1:{mailing_type}", mailing_type),
                    )
            # Add global keyword reply state table if it doesn't exist
            global_state_tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "keyword_reply_global_state" not in global_state_tables:
                connection.exec_driver_sql("""
                    CREATE TABLE keyword_reply_global_state (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        last_sent_at DATETIME,
                        updated_at DATETIME
                    )
                """)
                connection.exec_driver_sql("INSERT INTO keyword_reply_global_state (id, last_sent_at, updated_at) VALUES (1, NULL, ?)", (datetime.now(timezone.utc).isoformat(),))

    @contextmanager
    def session(self):
        db: Session = self.Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def close(self) -> None:
        self.engine.dispose()
