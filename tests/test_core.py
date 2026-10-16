from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.database import Database
from app.models import (
    Chat, DeleteTask, FarmState, KeywordReplyState, KeywordReplyUserState, OutboundMessage,
    SchedulerDelivery, SchedulerState, utcnow,
)
from app.services.audit import AuditLog
from app.services.accounts import AccountService
from app.services.chats import ChatManager
from app.services.gemini import GeminiAssistant
from app.services.keyword_reply import KeywordReplyAssistant, REPLY_TEXT
from app.services.farm import FARM_INTERVAL_SECONDS, FarmScheduler
from app.services.messages import MessageManager
from app.services.scheduler import MailingScheduler, distribute_evenly
from app.services.settings import SettingsService
from app.services.telegram_client import TelegramService, TelegramUnavailable


class FakeDeleteTelegram:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.deleted: list[tuple[int, int]] = []

    async def send_message(self, account_id: int, chat_id: int, text: str, reply_to_message_id: int | None = None) -> int:
        return 700 + len(self.deleted)

    async def message_exists(self, account_id: int, chat_id: int, message_id: int) -> bool:
        return self.exists

    async def delete_message(self, account_id: int, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


class FlakyDeleteTelegram(FakeDeleteTelegram):
    def __init__(self) -> None:
        super().__init__(exists=True)
        self.fail_once = True

    async def delete_message(self, account_id: int, chat_id: int, message_id: int) -> None:
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("temporary")
        await super().delete_message(account_id, chat_id, message_id)


class FakeChats:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids

    def eligible_chat_ids(self, account_id: int, kind: str) -> list[int]:
        return self.ids.copy()

    def is_currently_eligible(self, account_id: int, chat_id: int, mailing_type: str | None = None) -> bool:
        return chat_id in self.ids


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.schedule_deletions: list[bool] = []

    async def send(
        self, account_id: int, chat_id: int, text: str, mailing_type: str, *, schedule_deletion: bool = True,
    ) -> int:
        self.calls.append((chat_id, text, mailing_type))
        self.schedule_deletions.append(schedule_deletion)
        return 1000 + len(self.calls)


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(
            api_id=None, api_hash=None, delete_after_seconds=1, max_delete_retries=2,
            pch_groups=6, pb_groups=12, group_delay_seconds=3600, log_level="INFO",
            auto_reconnect=True, host="127.0.0.1", port=8000, data_dir=root,
            database_url=f"sqlite:///{root / 'test.sqlite3'}", telegram_session_path=str(root / "telegram.session"),
            gemini_api_key=None, gemini_model="gemini-flash-lite-latest", terminal_idle_report_seconds=60,
        )
        self.db = Database(self.config.database_url)
        self.db.initialize()
        self.settings = SettingsService(self.db, self.config)
        self.settings.initialize()
        self.audit = AuditLog(self.db, self.settings)

    async def asyncTearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    async def test_even_group_distribution_for_pch_and_pb(self) -> None:
        for count, groups in ((37, 6), (37, 12), (0, 6), (1, 12)):
            result = distribute_evenly(list(range(count)), groups)
            self.assertEqual(len(result), groups)
            self.assertEqual(sum(map(len, result)), count)
            self.assertLessEqual(max(map(len, result)) - min(map(len, result)), 1)

    async def test_manual_chat_can_be_assigned_without_bot_check(self) -> None:
        with self.db.session() as session:
            session.add(Chat(account_id=1, chat_id=42, title="Manual group", bot_status="CHECK_PENDING"))
        manager = ChatManager(self.db, telegram=None, audit=self.audit)  # network is not required in manual mode
        manager.update_assignment(1, 42, pch_enabled=True)
        with self.db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.account_id == 1, Chat.chat_id == 42))
            assert chat is not None
            self.assertTrue(chat.pch_enabled)
            self.assertEqual(chat.bot_status, "MANUAL")

    async def test_farm_sends_immediately_and_persists_3h58_interval(self) -> None:
        fake_chats = FakeChats([42])
        fake_messages = FakeMessages()
        farm = FarmScheduler(self.db, fake_chats, fake_messages, self.audit, account_id=1)
        farm.initialize()
        state = await farm.configure(chat_id=42, text="farm message", enabled=True)
        self.assertEqual(state["interval_seconds"], FARM_INTERVAL_SECONDS)
        await asyncio.sleep(0.08)
        self.assertEqual(fake_messages.calls, [(42, "farm message", "FARM")])
        self.assertEqual(fake_messages.schedule_deletions, [False])
        with self.db.session() as session:
            saved = session.get(FarmState, 1)
            assert saved is not None
            self.assertTrue(saved.enabled)
            self.assertEqual(saved.status, "WAITING")
            next_send_at = saved.next_send_at.replace(tzinfo=timezone.utc) if saved.next_send_at.tzinfo is None else saved.next_send_at
            self.assertAlmostEqual((next_send_at - utcnow()).total_seconds(), FARM_INTERVAL_SECONDS, delta=2)
        await farm.shutdown()

    async def test_delete_checks_existence_before_deleting(self) -> None:
        telegram = FakeDeleteTelegram(exists=True)
        messages = MessageManager(self.db, telegram, self.settings, self.audit)
        await messages.send(1, 55, "real test content", "PCH")
        with self.db.session() as session:
            task = session.scalar(select(DeleteTask))
            assert task is not None
            task.due_at = utcnow()
        await messages.process_due()
        with self.db.session() as session:
            outbound = session.scalar(select(OutboundMessage))
            task = session.scalar(select(DeleteTask))
            self.assertEqual(outbound.status, "DELETED")
            self.assertEqual(task.status, "DELETED")
        self.assertEqual(telegram.deleted, [(55, 700)])

    async def test_missing_message_is_not_delete_failure(self) -> None:
        telegram = FakeDeleteTelegram(exists=False)
        messages = MessageManager(self.db, telegram, self.settings, self.audit)
        await messages.send(1, 56, "will be absent", "PB")
        with self.db.session() as session:
            task = session.scalar(select(DeleteTask))
            assert task is not None
            task.due_at = utcnow()
        await messages.process_due()
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(OutboundMessage)).status, "ALREADY_DELETED")
            self.assertEqual(session.scalar(select(DeleteTask)).status, "ALREADY_DELETED")
        self.assertEqual(telegram.deleted, [])

    async def test_delete_retry_is_bounded_and_can_recover(self) -> None:
        telegram = FlakyDeleteTelegram()
        messages = MessageManager(self.db, telegram, self.settings, self.audit)
        await messages.send(1, 57, "retry", "PCH")
        with self.db.session() as session:
            task = session.scalar(select(DeleteTask))
            assert task is not None
            task.due_at = utcnow()
        await messages.process_due()
        with self.db.session() as session:
            task = session.scalar(select(DeleteTask))
            message = session.scalar(select(OutboundMessage))
            self.assertEqual(task.attempts, 1)
            self.assertEqual(task.status, "PENDING")
            self.assertEqual(message.status, "DELETE_PENDING")
            task.due_at = utcnow()
        await messages.process_due()
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(DeleteTask)).status, "DELETED")
            self.assertEqual(session.scalar(select(OutboundMessage)).status, "DELETED")

    async def test_messages_are_isolated_by_account(self) -> None:
        messages = MessageManager(self.db, FakeDeleteTelegram(), self.settings, self.audit)
        await messages.send(1, 77, "for account one", "PCH")
        await messages.send(2, 77, "for account two", "PB")
        self.assertEqual(len(messages.list(1)), 1)
        self.assertEqual(len(messages.list(2)), 1)
        self.assertEqual(messages.list(1)[0]["body"], "for account one")
        self.assertEqual(messages.list(2)[0]["body"], "for account two")

    async def test_gemini_messages_are_persistent_and_legacy_tasks_are_cancelled(self) -> None:
        messages = MessageManager(self.db, FakeDeleteTelegram(), self.settings, self.audit)
        # New Gemini messages never receive a DeleteTask.
        await messages.send(1, 78, "keep this answer", "GEMINI", schedule_deletion=False)
        record = messages.list(1)[0]
        self.assertFalse(record["delete_scheduled"])
        self.assertIsNone(record["delete_at"])
        self.assertEqual(record["status"], "SENT")

        # A task created by an older release is cancelled during recovery.
        await messages.send(1, 79, "old Gemini answer", "GEMINI")
        await messages.start()
        with self.db.session() as session:
            task = session.scalar(select(DeleteTask).join(OutboundMessage).where(OutboundMessage.chat_id == 79))
            message = session.scalar(select(OutboundMessage).where(OutboundMessage.chat_id == 79))
            assert task is not None and message is not None
            self.assertEqual(task.status, "CANCELLED")
            self.assertEqual(message.status, "SENT")
            self.assertFalse(messages.list(1)[0]["delete_scheduled"])
        await messages.stop()

    async def test_farm_messages_are_persistent_and_legacy_tasks_are_cancelled(self) -> None:
        messages = MessageManager(self.db, FakeDeleteTelegram(), self.settings, self.audit)
        # New Farm messages deliberately have no DeleteTask.
        await messages.send(1, 80, "keep farm message", "FARM", schedule_deletion=False)
        record = messages.list(1)[0]
        self.assertFalse(record["delete_scheduled"])
        self.assertIsNone(record["delete_at"])
        self.assertEqual(record["status"], "SENT")

        # A task from an older release is cancelled during recovery as well.
        await messages.send(1, 81, "old farm message", "FARM")
        await messages.start()
        with self.db.session() as session:
            task = session.scalar(select(DeleteTask).join(OutboundMessage).where(OutboundMessage.chat_id == 81))
            message = session.scalar(select(OutboundMessage).where(OutboundMessage.chat_id == 81))
            assert task is not None and message is not None
            self.assertEqual(task.status, "CANCELLED")
            self.assertEqual(message.status, "SENT")
            self.assertFalse(messages.list(1)[0]["delete_scheduled"])
        await messages.stop()

    async def test_accounts_stop_at_three_and_keep_separate_chat_rows(self) -> None:
        accounts = AccountService(self.db, self.settings, self.audit)
        accounts.initialize()
        second = accounts.create("Second")
        third = accounts.create("Third")
        with self.assertRaises(ValueError):
            accounts.create("Fourth")
        with self.db.session() as session:
            session.add(Chat(account_id=1, chat_id=-100777, title="Same Telegram group"))
            session.add(Chat(account_id=second["id"], chat_id=-100777, title="Same Telegram group"))
        self.assertEqual(third["id"], 3)
        with self.db.session() as session:
            rows = session.scalars(select(Chat).where(Chat.chat_id == -100777)).all()
            self.assertEqual({row.account_id for row in rows}, {1, 2})

    async def test_scheduler_delivery_cleanup_cannot_delete_another_account_ledger(self) -> None:
        first = MailingScheduler(self.db, FakeChats([1]), FakeMessages(), self.settings, self.audit, account_id=1)
        second = MailingScheduler(self.db, FakeChats([2]), FakeMessages(), self.settings, self.audit, account_id=2)
        first.initialize()
        second.initialize()
        await first.start("PCH", "one")
        await asyncio.sleep(0.06)
        await second.start("PCH", "two")
        await asyncio.sleep(0.06)
        with self.db.session() as session:
            account_one = session.scalars(select(SchedulerDelivery).where(SchedulerDelivery.account_id == 1)).all()
            account_two = session.scalars(select(SchedulerDelivery).where(SchedulerDelivery.account_id == 2)).all()
            self.assertTrue(account_one)
            self.assertTrue(account_two)
        await first.shutdown()
        await second.shutdown()

    async def test_gemini_cannot_enable_without_a_server_key(self) -> None:
        assistant = GeminiAssistant(self.config, self.db, self.settings, None, None, None, None, self.audit)  # type: ignore[arg-type]
        self.assertFalse(assistant.settings_for(1)["configured"])
        with self.assertRaises(ValueError):
            assistant.update_settings(1, enabled=True, model="gemini-flash-lite-latest", system_prompt="Reply briefly")
        saved = assistant.update_settings(1, enabled=False, model="gemini-flash-lite-latest", system_prompt="Reply briefly")
        self.assertFalse(saved["enabled"])

    async def test_keyword_reply_is_exact_reply_and_rate_limited_per_account(self) -> None:
        class KeywordMessages:
            def __init__(self) -> None:
                self.calls: list[tuple[int, int, str, str, int | None]] = []

            async def send(
                self, account_id: int, chat_id: int, text: str, message_type: str,
                reply_to_message_id: int | None = None,
            ) -> int:
                self.calls.append((account_id, chat_id, text, message_type, reply_to_message_id))
                return 999 + len(self.calls)

        with self.db.session() as session:
            session.add(Chat(account_id=1, chat_id=-100901, title="Account one", keyword_reply_enabled=True))
            session.add(Chat(account_id=2, chat_id=-100901, title="Account two", keyword_reply_enabled=True))
        chats = ChatManager(self.db, telegram=None, audit=self.audit)  # type: ignore[arg-type]
        messages = KeywordMessages()
        responder = KeywordReplyAssistant(self.db, None, None, chats, messages, self.audit)  # type: ignore[arg-type]
        first = SimpleNamespace(raw_text=" Ом ", chat_id=-100901, sender_id=700, message=SimpleNamespace(id=123))
        second = SimpleNamespace(raw_text="оч", chat_id=-100901, sender_id=701, message=SimpleNamespace(id=124))
        repeat_sender = SimpleNamespace(raw_text="об", chat_id=-100901, sender_id=700, message=SimpleNamespace(id=126))
        other_sender = SimpleNamespace(raw_text="об", chat_id=-100901, sender_id=702, message=SimpleNamespace(id=127))
        self.assertTrue(await responder.handle_incoming(1, first))
        # A different sender is still blocked by the account-wide one-minute limit.
        self.assertFalse(await responder.handle_incoming(1, second))
        with self.db.session() as session:
            state = session.get(KeywordReplyState, 1)
            assert state is not None
            state.last_sent_at = utcnow() - timedelta(seconds=61)
        # After the account minute, the original sender remains blocked for an hour.
        self.assertFalse(await responder.handle_incoming(1, repeat_sender))
        # A different sender can receive the next allowed reply.
        self.assertTrue(await responder.handle_incoming(1, other_sender))
        self.assertFalse(await responder.handle_incoming(1, SimpleNamespace(raw_text="об!", chat_id=-100901, sender_id=703, message=SimpleNamespace(id=128))))
        # The same person is independent in a separately authorized account.
        self.assertTrue(await responder.handle_incoming(2, second))
        with self.db.session() as session:
            user_state = session.get(KeywordReplyUserState, (1, 700))
            assert user_state is not None
            self.assertIsNotNone(user_state.last_sent_at)
        self.assertEqual(messages.calls, [
            (1, -100901, REPLY_TEXT, "REPLY", 123),
            (1, -100901, REPLY_TEXT, "REPLY", 127),
            (2, -100901, REPLY_TEXT, "REPLY", 124),
        ])

    async def test_legacy_single_account_chat_rows_migrate_without_loss(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        engine = create_engine(f"sqlite:///{legacy_path}")
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE chats (
                    chat_id INTEGER PRIMARY KEY, title VARCHAR(512) NOT NULL,
                    username VARCHAR(255), chat_type VARCHAR(40) NOT NULL,
                    enabled BOOLEAN NOT NULL, pch_enabled BOOLEAN NOT NULL,
                    pb_enabled BOOLEAN NOT NULL, bot_username VARCHAR(255) NOT NULL,
                    bot_status VARCHAR(32) NOT NULL, bot_checked_at DATETIME,
                    added_at DATETIME, updated_at DATETIME
                )
            """)
            connection.exec_driver_sql("""
                INSERT INTO chats (chat_id, title, username, chat_type, enabled, pch_enabled, pb_enabled, bot_username, bot_status)
                VALUES (-100123, 'Existing group', NULL, 'supergroup', 1, 1, 0, 'LEGACY_SERVICE', 'LEGACY_PRESENT')
            """)
        legacy_db = Database(f"sqlite:///{legacy_path}")
        legacy_db.initialize()
        with legacy_db.session() as session:
            chat = session.scalar(select(Chat).where(Chat.chat_id == -100123))
            assert chat is not None
            self.assertEqual(chat.account_id, 1)
            self.assertTrue(chat.pch_enabled)
            self.assertEqual(chat.bot_status, "MANUAL")
        legacy_db.close()
        engine.dispose()

    async def test_http_api_starts_with_isolated_storage_and_exposes_accounts(self) -> None:
        isolated_data = Path(self.temp.name) / "http-data"
        environment = {
            "SCHEDULER_DATA_DIR": str(isolated_data),
            "API_ID": "", "API_HASH": "", "GEMINI_API_KEY": "", "TERMINAL_IDLE_REPORT_SECONDS": "10",
        }
        with patch.dict(os.environ, environment, clear=False):
            from app.main import app
            with TestClient(app) as client:
                accounts = client.get("/api/accounts")
                self.assertEqual(accounts.status_code, 200)
                self.assertEqual(len(accounts.json()["accounts"]), 1)
                with app.state.services["db"].session() as session:
                    session.add(Chat(account_id=1, chat_id=101, title="PCH only", chat_type="supergroup", pch_enabled=True))
                    session.add(Chat(account_id=1, chat_id=102, title="Both", chat_type="supergroup", pch_enabled=True, pb_enabled=True))
                    session.add(Chat(account_id=1, chat_id=103, title="PB only", chat_type="supergroup", pb_enabled=True))
                dashboard = client.get("/api/status")
                self.assertEqual(dashboard.status_code, 200)
                dashboard_data = dashboard.json()["dashboard"]
                self.assertEqual(len(dashboard_data["activity"]["days"]), 7)
                self.assertEqual(dashboard_data["activity"]["total_7d"], 0)
                self.assertEqual(dashboard_data["workload"]["pch"]["percent"], 67)
                self.assertEqual(dashboard.json()["chats"], {"total": 3, "manual": 3, "pch": 2, "pb": 2, "selected": 3})
                self.assertEqual(client.post("/api/accounts", json={"label": "Second"}).status_code, 200)
                self.assertEqual(client.post("/api/accounts", json={"label": "Third"}).status_code, 200)
                fourth = client.post("/api/accounts", json={"label": "Fourth"})
                self.assertEqual(fourth.status_code, 400)

    async def test_scheduler_snapshots_first_group_and_persists_delivery_ledger(self) -> None:
        fake_chats = FakeChats(list(range(1, 15)))
        fake_messages = FakeMessages()
        scheduler = MailingScheduler(self.db, fake_chats, fake_messages, self.settings, self.audit)
        scheduler.initialize()
        await scheduler.start("PCH", "Hello")
        await asyncio.sleep(0.08)
        state = scheduler.get_state("PCH")
        self.assertEqual(state["total_groups"], 6)
        self.assertEqual(state["current_group"], 2)
        self.assertEqual(len(fake_messages.calls), 3)
        with self.db.session() as session:
            sent = session.scalars(select(SchedulerDelivery).where(SchedulerDelivery.status == "SENT")).all()
            self.assertEqual(len(sent), 3)
            self.assertEqual(session.get(SchedulerState, "1:PCH").status, "WAITING")
        await scheduler.shutdown()

    async def test_scheduler_recovery_only_sends_pending_delivery(self) -> None:
        fake_chats = FakeChats([1, 2])
        fake_messages = FakeMessages()
        scheduler = MailingScheduler(self.db, fake_chats, fake_messages, self.settings, self.audit)
        scheduler.initialize()
        with self.db.session() as session:
            state = session.get(SchedulerState, "1:PCH")
            assert state is not None
            state.enabled = True
            state.current_group = 1
            state.total_groups = 1
            state.status = "SENDING"
            state.run_id = "recoverable-run"
            state.plan_json = "[[1, 2]]"
            state.message_text = "resume safely"
            session.add(SchedulerDelivery(account_id=1, run_id="recoverable-run", mailing_type="PCH", group_number=1, chat_id=1, status="SENT"))
            session.add(SchedulerDelivery(account_id=1, run_id="recoverable-run", mailing_type="PCH", group_number=1, chat_id=2, status="PENDING"))
        await scheduler.restore()
        await asyncio.sleep(0.08)
        self.assertEqual(fake_messages.calls, [(2, "resume safely", "PCH")])
        with self.db.session() as session:
            self.assertEqual(session.get(SchedulerState, "1:PCH").status, "CYCLE_WAITING")
            self.assertEqual(len(session.scalars(select(SchedulerDelivery).where(SchedulerDelivery.status == "SENT")).all()), 2)
        await scheduler.shutdown()

    async def test_completed_cycle_waits_then_creates_fresh_manual_snapshot(self) -> None:
        fake_chats = FakeChats([1, 2])
        fake_messages = FakeMessages()
        scheduler = MailingScheduler(self.db, fake_chats, fake_messages, self.settings, self.audit)
        scheduler.initialize()
        with self.db.session() as session:
            state = session.get(SchedulerState, "1:PCH")
            assert state is not None
            state.enabled = True
            state.current_group = 6
            state.total_groups = 6
            state.status = "RUNNING"
            state.run_id = "cycle-one"
            state.plan_json = "[[], [], [], [], [], [1]]"
            state.message_text = "cycle"
            session.add(SchedulerDelivery(account_id=1, run_id="cycle-one", mailing_type="PCH", group_number=6, chat_id=1, status="PENDING"))
        await scheduler._send_current_group("PCH")
        with self.db.session() as session:
            state = session.get(SchedulerState, "1:PCH")
            assert state is not None
            self.assertEqual(state.status, "CYCLE_WAITING")
            self.assertEqual(state.current_group, 1)
            old_run = state.run_id
        await scheduler._begin_next_cycle("PCH")
        with self.db.session() as session:
            state = session.get(SchedulerState, "1:PCH")
            assert state is not None
            self.assertEqual(state.status, "RUNNING")
            self.assertNotEqual(state.run_id, old_run)
            pending = session.scalars(select(SchedulerDelivery).where(SchedulerDelivery.status == "PENDING")).all()
            self.assertEqual({item.chat_id for item in pending}, {1, 2})

    async def test_repeat_guard_is_six_hours_for_pch_and_twelve_for_pb(self) -> None:
        self.assertEqual(MailingScheduler._minimum_repeat_seconds("PCH"), 6 * 3600)
        self.assertEqual(MailingScheduler._minimum_repeat_seconds("PB"), 12 * 3600)

    async def test_pausing_cycle_wait_preserves_next_cycle_transition(self) -> None:
        scheduler = MailingScheduler(self.db, FakeChats([1]), FakeMessages(), self.settings, self.audit)
        scheduler.initialize()
        with self.db.session() as session:
            state = session.get(SchedulerState, "1:PCH")
            assert state is not None
            state.enabled = True
            state.status = "CYCLE_WAITING"
            state.current_group = 1
            state.total_groups = 6
            state.next_group_at = utcnow()
        await scheduler.pause("PCH")
        self.assertEqual(scheduler.get_state("PCH")["status"], "PAUSED_CYCLE")
        await scheduler.resume("PCH")
        self.assertEqual(scheduler.get_state("PCH")["status"], "CYCLE_WAITING")
        await scheduler.shutdown()

    async def test_authentication_refuses_to_fake_missing_credentials(self) -> None:
        telegram = TelegramService(self.config, self.audit, self.settings)
        status = await telegram.status(1)
        self.assertFalse(status["configured"])
        with self.assertRaises(TelegramUnavailable):
            await telegram.begin_phone_login(1, "+380000000000")
