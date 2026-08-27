#!/usr/bin/env bash
# POSIX counterpart to start_bot.bat, for macOS and Linux.
set -uo pipefail
cd "$(dirname "$0")"

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting bot..."
  python -u main.py
  status=$?

  # Exit code 2 is a configuration or token failure; restarting cannot fix it.
  if [ "$status" -eq 2 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Configuration error, not restarting. Check logs/bot.log"
    exit 2
  fi
  if [ "$status" -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Clean shutdown."
    exit 0
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Exited with code $status. Restarting in 5 seconds..."
  sleep 5
done
