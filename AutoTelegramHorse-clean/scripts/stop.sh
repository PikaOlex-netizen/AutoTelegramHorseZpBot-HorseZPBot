#!/usr/bin/env bash
set -euo pipefail

if [ ! -f data/app.pid ]; then
  echo "No managed application PID file found."
  exit 0
fi
APP_PID="$(cat data/app.pid)"
if kill -0 "$APP_PID" 2>/dev/null; then
  kill -TERM "$APP_PID"
  echo "Sent graceful shutdown signal to PID $APP_PID."
else
  echo "PID $APP_PID is no longer running."
fi
rm -f data/app.pid
