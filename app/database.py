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
        """Невеликі міграції SQLite для інсталяцій, створених до multi-account.

        SQLAlchemy's create_all створює таблиці, але не може додавати стовпці або замінити
        первинний ключ. Існуючі чати копіюються в таблицю для кожного акаунту і
        призначаються до Account 1, зберігаючи всі дані користувача та історію доставки.
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
                # Ці застарілі метадані більше не використовуються застосунком.
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
            # Додати таблицю глобального стану відповіді на ключові слова, якщо вона не існує
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
            # Додати таблиці для функції 'Нагодувати гівном', якщо вони не існують
            all_tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "user_cooldowns" not in all_tables:
                connection.exec_driver_sql("""
                    CREATE TABLE user_cooldowns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        username TEXT,
                        last_fed_at DATETIME,
                        updated_at DATETIME,
                        UNIQUE(account_id, user_id)
                    )
                """)
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_cooldowns_account_id ON user_cooldowns (account_id)")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_cooldowns_user_id ON user_cooldowns (user_id)")
            if "feed_states" not in all_tables:
                connection.exec_driver_sql("""
                    CREATE TABLE feed_states (
                        account_id INTEGER PRIMARY KEY,
                        enabled BOOLEAN NOT NULL DEFAULT 0,
                        username_to_feed TEXT,
                        message_text TEXT,
                        status VARCHAR(32) NOT NULL DEFAULT 'IDLE',
                        updated_at DATETIME
                    )
                """)
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_feed_states_status ON feed_states (status)")

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
