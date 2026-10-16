# Architecture

```text
Web UI ──HTTP/SSE──> FastAPI API
                          │
     ┌────────────────────┼─────────────────────┐
     │                    │                     │
AccountService       ChatManager          SettingsService
     │                    │                     │
TelegramService ─── Manual chat manager ─── SQLite storage / AuditLog
     │                    │
     │              manually selected chats
     │                    │
     │            ┌───────┴────────┐
     │            │                │
     │       PCH scheduler     PB scheduler
     │        6 groups          12 groups
     │            └───────┬────────┘
     │                    │
     │              MessageManager
     │                    │
     └────────── Delete scheduler ┘
                          │
                    GeminiAssistant
                          │
                KeywordReplyAssistant
```

## Modules

- `app/services/accounts.py` — creates and switches up to three locally stored Telegram-account profiles.
- `app/services/telegram_client.py` — only module making Telegram RPC calls. It owns an isolated Telethon session per account, Phone/QR authentication, reconnection settings, send, read and delete operations.
- `app/services/chats.py` — imports Telegram dialogs, resolves manually entered Chat IDs, preserves history and controls independent ПЧ/ПБ assignments.
- `app/services/farm.py` — durable one-chat sender with a 3-hour-58-minute interval and restart recovery.
- `app/services/scheduler.py` — two durable, separate async runners for each account. `SchedulerState` stores the next group and timestamp; `SchedulerDelivery` is an account-scoped idempotent per-chat ledger within a run.
- `app/services/messages.py` — persists outbound messages and `DeleteTask` rows for temporary PCH/PB/manual sends, checks existence before deletion and applies bounded retry; Farm and Gemini messages are persistent and legacy deletion tasks are cancelled during recovery.
- `app/services/settings.py` — persistent, validated UI settings. Environment values initialise defaults on first run.
- `app/services/audit.py` — persistent audit log and in-process fanout for SSE.
- `app/services/gemini.py` — optional Gemini responder. It receives only incoming messages, then checks both the account-level setting and the manually enabled group before calling Gemini.
- `app/services/keyword_reply.py` — exact-match responder for `ом`, `оч` and `об`. It replies `Вкрасти` to the original message only in manually enabled groups, with a durable one-minute cooldown per account plus a one-hour cooldown per triggering Telegram user.
- `app/main.py` — FastAPI API, lifespan startup/recovery and graceful shutdown.

## Durable state

SQLite tables: `telegram_accounts`, `settings`, `chats`, `scheduler_states`, `scheduler_deliveries`, `outbound_messages`, `delete_tasks`, `gemini_history`, `keyword_reply_states`, and `logs`. Chat rows, delivery rows, messages and logs contain an account scope. Telethon sessions are separate: `data/telegram.session`, `data/telegram-account-2.session` and `data/telegram-account-3.session`.

At Start a group plan is serialised into `scheduler_states.plan_json`; every target gets a delivery row. A restart turns an interrupted `SENDING` state back into runnable work and sends only remaining `PENDING` rows. Existing `SENT` delivery rows never run again. A claimed but unfinished delete task is reset from `PROCESSING` to `PENDING` on startup.

## Manual-list policy and repeat guard

The manual list is authoritative. Adding a chat resolves the supplied Chat ID through Telegram to prevent an incorrect ID from entering the list. A chat remains a target only when it is enabled and selected for the corresponding mailing.

After the last group of a cycle, the next snapshot is built from the then-current manual list. The repeat guard schedules the cycle boundary so a ПЧ target cannot recur sooner than 21,600 seconds (6 hours) and a ПБ target cannot recur sooner than 43,200 seconds (12 hours), even if the number of groups is changed.

## Concurrency model

FastAPI remains responsive because wait periods use `asyncio.Event`/timeouts in independent background tasks. Each mailing type has a separate lock and wake event per account. The delete worker and optional Gemini calls are also separate. SQLite transactions are deliberately short and no database transaction is held during network I/O. Terminal heartbeats use the live scheduler and delete-worker state, so an idle report is printed only while no send/delete action is running.
