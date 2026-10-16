# HTTP API

All endpoints are local by default at `http://127.0.0.1:8000`. JSON write requests use `Content-Type: application/json`.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/status` | dashboard summary |
| GET | `/api/accounts` | up to three account profiles and their non-secret Telegram states |
| POST | `/api/accounts` | create account profile: `{ "label": "Work" }` |
| POST | `/api/accounts/{id}/select` | choose the account scope used by the UI and following requests |
| GET | `/api/auth/status` | non-secret Telegram state |
| POST | `/api/auth/phone` | request code: `{ "phone": "+…" }` |
| POST | `/api/auth/code` | submit code: `{ "code": "…" }` |
| POST | `/api/auth/2fa` | submit password: `{ "password": "…" }` |
| POST | `/api/auth/qr` | create a live QR token and PNG data URI |
| POST | `/api/auth/logout` | log out and invalidate session |
| GET | `/api/chats` | stored groups |
| POST | `/api/chats/refresh` | import current Telegram dialogs into the manual list |
| POST | `/api/chats` | add a Telegram chat by `{ "chat_id": 123 }` |
| PUT | `/api/chats/{id}` | set `pch_enabled`, `pb_enabled`, `gemini_enabled`, `keyword_reply_enabled`, or `enabled` |
| DELETE | `/api/chats/{id}` | remove from both active mailing lists |
| GET | `/api/messages` | persisted outbound messages and delete task state |
| POST | `/api/messages/send` | manual verified send `{chat_id,text,message_type}` |
| GET | `/api/scheduler/status` | both persisted scheduler states |
| GET/PUT | `/api/farm` | get or configure one-chat Farm schedule (3 hours 58 minutes) |
| POST | `/api/scheduler/{pch\|pb}/start` | start snapshot, optional `{text}` |
| POST | `/api/scheduler/{pch\|pb}/pause` | pause after current safe operation |
| POST | `/api/scheduler/{pch\|pb}/resume` | resume existing plan |
| POST | `/api/scheduler/{pch\|pb}/stop` | stop existing plan |
| GET/PUT | `/api/scheduler/{pch\|pb}/template` | get/save real send text |
| GET/PUT | `/api/settings` | get/save validated settings |
| GET/PUT | `/api/gemini/settings` | get/save Gemini settings for the selected account; enabling requires `GEMINI_API_KEY` |
| GET | `/api/logs` | filterable persistent logs (`level`, `module`, `q`, `chat_id`, `message_id`) |
| GET | `/api/events` | Server-Sent Event stream: `log.created` |
| GET | `/api/system` | non-secret persistence/recovery state |

Error responses use `{ "detail": "safe description" }`. Credential values, codes and passwords are never returned or logged.

All chat, message, scheduler and log endpoints use the currently selected local account profile. The Event stream likewise sends account-scoped entries plus system events only.
