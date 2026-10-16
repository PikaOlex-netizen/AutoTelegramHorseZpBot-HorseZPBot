# Manual Telegram integration checklist

The automated tests deliberately do not contact Telegram: no test account, phone code, QR token or 2FA password is ever faked or stored. Run this checklist with a dedicated account and real groups before production use.

1. Configure `API_ID` / `API_HASH`, start the application, and complete Phone Login. Repeat using a real QR token and an account with 2FA if applicable.
2. Restart the app and verify that the existing session reconnects without a new code.
3. Add groups manually by Chat ID, select each for PЧ, ПБ, or both, then remove one and confirm it disappears from the working list while message history remains.
4. Assign at least 7 ПЧ and 13 ПБ groups. Temporarily set `GROUP_DELAY_SECONDS=5` and `DELETE_AFTER_SECONDS=10` in Settings. Verify each mailing advances independently and its group sizes differ by at most one. Verify Logs record cycle completion and the repeat-protection wait; restore the normal one-hour group delay for production.
5. Send a short test message. Check the Messages page records the Telegram message id; let the delete worker check and remove it. Manually delete another sent message first and verify `ALREADY_DELETED`.
6. While a scheduler is waiting, stop and relaunch the app. Verify current group/next time are restored, completed targets are not sent twice, and pending delete tasks continue.
7. Inspect Logs for a non-sensitive event trail, and terminate with Ctrl-C to confirm graceful shutdown.
