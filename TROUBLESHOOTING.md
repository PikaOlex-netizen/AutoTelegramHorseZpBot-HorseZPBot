# Troubleshooting

## API_ID/API_HASH is required

Create an application at [my.telegram.org/apps](https://my.telegram.org/apps), populate both fields in `.env`, then restart. A BotFather token is not a replacement: this application uses a user account session for group membership and sending.

## QR expires or does not complete

QR login tokens are real Telegram tokens with a short lifetime. Click **Создать QR** again and scan it from the Telegram mobile app's Devices screen. If the account has 2FA enabled, submit the 2FA password after scan.

## A manual group cannot be added

The current Telegram account must be able to open the entered Chat ID. The application does not inspect participants; check the ID and account access instead.

## Messages are not deleted

Check **Messages** and **Logs**. `ALREADY_DELETED` is success-like: Telegram did not find the message, so no delete is attempted. Temporary failures retry up to the configured limit. The application must remain running for on-time deletion; missed tasks run after the next startup.

## Port 8000 is already in use

Another copy is already listening on that port. Stop it with `./stop.sh` if it was launched by this project, or change `PORT` in `.env`, restart and open the new address. The default `HOST=127.0.0.1` intentionally does not expose the dashboard to the local network.

## Gemini does not answer

Confirm all four conditions: `GEMINI_API_KEY` is present in `.env`; the application was restarted after adding it; Gemini is enabled in **Settings** for the selected account; and the target group has its **Gemini** switch enabled in **Chats**. Gemini deliberately ignores private dialogs, outgoing messages and groups that were not explicitly enabled.

## «Вкрасти» does not reply

In **Chats**, enable **ОМ/ОЧ/ОБ** for the relevant manual group and make sure the matching account is authorized. The trigger must be exactly `ом`, `оч` or `об`; a message such as `об!` does not trigger a reply. There are two saved limits across all enabled groups: one reply per account each minute and one reply per Telegram sender each hour. A skipped trigger may be inside either cooldown.

## I do not see activity in the terminal

Run the app in the foreground with `python -m app`. A real send, delete, retry and Gemini reply is printed immediately. During a quiet period the process prints a state report every 60 seconds by default. For a background launch, use `tail -f data/server.log`.

## Reset a broken local session

Use **Выйти из Telegram** first. If the session is corrupted and logout cannot connect, stop the app and move only `data/telegram.session*` to a safe backup location; then start and log in again. Keep `scheduler.sqlite3` so chat history, states and logs remain intact.
