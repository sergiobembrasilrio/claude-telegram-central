#!/bin/bash
# Local Telegram Bot API server, bound to 127.0.0.1 only. Cron every minute under flock: if it dies, it comes back.
# Requires telegram-bot-api built from https://github.com/tdlib/telegram-bot-api at $CENTRAL_ROOT/telegram-local/bin/.
CENTRAL_ROOT="${CENTRAL_ROOT:-$HOME/claude-telegram-central}"
CONFIG_DIR="${CENTRAL_CONFIG_DIR:-$HOME/.config/claude-telegram-central}"
mkdir -p "$CENTRAL_ROOT/telegram-local/tmp"
set -a
. "$CONFIG_DIR/telegram-local.env"
set +a
exec "$CENTRAL_ROOT/telegram-local/bin/telegram-bot-api" --local --http-ip-address=127.0.0.1 --http-port=8081 \
  --dir="$CENTRAL_ROOT/telegram-local/data" --temp-dir="$CENTRAL_ROOT/telegram-local/tmp" \
  --log="$CENTRAL_ROOT/telegram-local/server.log" --verbosity=1
