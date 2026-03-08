#!/bin/sh
set -e
# Том /data монтируется с правами root; даём botuser право записи для bot_state.pickle и payments
if [ -d /data ]; then
  chown -R botuser:botuser /data 2>/dev/null || true
fi
exec su botuser -c "cd /app && exec python bot.py"
