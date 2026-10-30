# HTTP API

Усі ендпоінти за замовчуванням локальні за адресою `http://127.0.0.1:8000`. Записи JSON використовують `Content-Type: application/json`.

| Метод | Ендпоінт | Призначення |
|---|---|---|
| GET | `/api/status` | підсумок інформаційної панелі |
| GET | `/api/accounts` | до трьох профілів акаунтів та їх нестійкі стани Telegram |
| POST | `/api/accounts` | створити профіль акаунту: `{ "label": "Work" }` |
| POST | `/api/accounts/{id}/select` | вибрати область дії акаунту, що використовується UI та наступними запитами |
| GET | `/api/auth/status` | нестійкий стан Telegram |
| POST | `/api/auth/phone` | запросити код: `{ "phone": "+…" }` |
| POST | `/api/auth/code` | надіслати код: `{ "code": "…" }` |
| POST | `/api/auth/2fa` | надіслати пароль: `{ "password": "…" }` |
| POST | `/api/auth/qr` | створити діючий QR токен та PNG data URI |
| POST | `/api/auth/logout` | вийти та анулювати сесію |
| GET | `/api/chats` | збережені групи |
| POST | `/api/chats/refresh` | імпортувати поточні діалоги Telegram у ручний список |
| POST | `/api/chats` | додати чат Telegram за `{ "chat_id": 123 }` |
| PUT | `/api/chats/{id}` | встановити `pch_enabled`, `pb_enabled`, `gemini_enabled`, `keyword_reply_enabled` або `enabled` |
| DELETE | `/api/chats/{id}` | видалити з обох активних списків розсилки |
| GET | `/api/messages` | збережені вихідні повідомлення та стан задачі видалення |
| POST | `/api/messages/send` | ручна перевірена відправка `{chat_id,text,message_type}` |
| GET | `/api/scheduler/status` | обидва збережені стани планувальника |
| GET/PUT | `/api/farm` | отримати або налаштувати розклад Ферми для одного чату (3 години 58 хвилин) |
| POST | `/api/scheduler/{pch\|pb}/start` | запустити знімок, опціонально `{text}` |
| POST | `/api/scheduler/{pch\|pb}/pause` | призупинити після поточної безпечної операції |
| POST | `/api/scheduler/{pch\|pb}/resume` | відновити існуючий план |
| POST | `/api/scheduler/{pch\|pb}/stop` | зупинити існуючий план |
| GET/PUT | `/api/scheduler/{pch\|pb}/template` | отримати/зберегти реальний текст відправки |
| GET/PUT | `/api/settings` | отримати/зберегти валідовані налаштування |
| GET/PUT | `/api/gemini/settings` | отримати/зберегти налаштування Gemini для вибраного акаунту; увімкнення вимагає `GEMINI_API_KEY` |
| GET | `/api/logs` | фільтровані постійні журнали (`level`, `module`, `q`, `chat_id`, `message_id`) |
| GET | `/api/events` | потік Server-Sent Event: `log.created` |
| GET | `/api/system` | нестійкий стан збереження/відновлення |

Відповіді з помилками використовують `{ "detail": "безпечний опис" }`. Значення облікових даних, коди та паролі ніколи не повертаються або не записуються в журнал.

Усі ендпоінти чатів, повідомлень, планувальника та журналів використовують поточний вибраний локальний профіль акаунту. Потік подій аналогічно надсилає записи області дії акаунту плюс лише системні події.
