#!/usr/bin/env bash
set -euo pipefail

if [ ! -x .venv/bin/python ]; then
  echo "Run ./install.sh first." >&2
  exit 1
fi
if [ ! -f .env ]; then
  echo "Create .env first: cp .env.example .env" >&2
  exit 1
fi
mkdir -p data
if [ -f data/app.pid ] && kill -0 "$(cat data/app.pid)" 2>/dev/null; then
  echo "Application is already running (PID $(cat data/app.pid))."
  exit 0
fi
if [ -f data/app.pid ]; then
  rm -f data/app.pid
fi
APP_PORT="$(.venv/bin/python -c 'from app.config import AppConfig; print(AppConfig.load().port)')"
APP_HOST="$(.venv/bin/python -c 'from app.config import AppConfig; print(AppConfig.load().host)')"
PORT_OWNER="$(lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
if [ -n "$PORT_OWNER" ]; then
  echo "Port $APP_PORT is already used by PID $PORT_OWNER. Stop that process or change PORT in .env." >&2
  exit 1
fi
nohup .venv/bin/python -m app > data/server.log 2>&1 &
APP_PID=$!
sleep 1
if ! kill -0 "$APP_PID" 2>/dev/null; then
  echo "Application could not start. See data/server.log." >&2
  exit 1
fi
echo "$APP_PID" > data/app.pid
echo "Started: http://$APP_HOST:$APP_PORT (PID $(cat data/app.pid))"
echo "Live terminal reports: tail -f data/server.log"
