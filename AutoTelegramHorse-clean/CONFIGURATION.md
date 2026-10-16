# Configuration

Copy `.env.example` to `.env`. Do not commit the resulting file.

| Variable | Default | Meaning |
|---|---:|---|
| `API_ID` | — | Telegram application id from my.telegram.org |
| `API_HASH` | — | Telegram application secret |
| `DELETE_AFTER_SECONDS` | `60` | Initial delay before a PCH/PB/manual outbound message is checked and deleted (Farm and Gemini are never deleted) |
| `MAX_DELETE_RETRIES` | `3` | Maximum temporary delete retries; further failure is `FAILED` |
| `PCH_GROUPS` | `6` | Initial number of PЧ groups |
| `PB_GROUPS` | `12` | Initial number of ПБ groups |
| `GROUP_DELAY_SECONDS` | `3600` | Initial delay between groups, not individual messages |
| `LOG_LEVEL` | `INFO` | Initial audit level preference |
| `AUTO_RECONNECT` | `true` | Initial Telethon auto-reconnect preference |
| `HOST` | `127.0.0.1` | Local bind address |
| `PORT` | `8000` | Local HTTP port |
| `GEMINI_API_KEY` | — | Optional Gemini API key; read only from `.env`, never from the UI or database |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | Default model for newly configured account assistants |
| `TERMINAL_IDLE_REPORT_SECONDS` | `60` | Seconds between terminal reports while no send/delete action is in progress |

The settings page writes its values to SQLite so changes persist across restarts. Environment values seed the database only when the setting is absent. Group-count and delay changes affect the next new run; an already snapshotted run remains stable.

Never enter `API_HASH`, `GEMINI_API_KEY`, SMS/login codes, a 2FA password or the contents of `data/telegram*.session` into logs, tickets or source control.
