#!/bin/bash
# Optional. Switches the bot to a local Telegram Bot API server, which accepts files up to 2 GB. Run once, after
# $CONFIG_DIR/telegram-local.env exists (TELEGRAM_API_ID and TELEGRAM_API_HASH from https://my.telegram.org, permission 600).
# Order of the official procedure: stop the bot, logOut from the official API, start the local server on 127.0.0.1 only
# (no port open to the outside), point the bot at it.
# After logOut the token cannot return to the official API for 10 min. To go back: logOut on the local server and remove TELEGRAM_API from telegram.env.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_ROOT="${CENTRAL_ROOT:-$HOME/claude-telegram-central}"
DATA="${CENTRAL_HOME:-$CENTRAL_ROOT/data}"
CONFIG_DIR="${CENTRAL_CONFIG_DIR:-$HOME/.config/claude-telegram-central}"
C="$CONFIG_DIR/telegram.env"
L="$CONFIG_DIR/telegram-local.env"
BIN="$CENTRAL_ROOT/telegram-local/bin/telegram-bot-api"
[ -x "$BIN" ] || { echo "telegram-bot-api is not built at $BIN"; exit 1; }
[ "$(stat -c %a "$L")" = 600 ] || { echo "$L must have permission 600"; exit 1; }
grep -q '^TELEGRAM_API=' "$C" && { echo "already on"; exit 0; }
TOKEN=$(sed -n 's/^TELEGRAM_TOKEN=//p' "$C")
mkdir -p "$CENTRAL_ROOT/telegram-local/data" "$DATA"

# stop the bot: cron only starts it again once the pause file is gone
touch "$DATA/paused"
pkill -f "$HERE/central.py$" || true
sleep 2

# the token goes to curl through stdin so it never shows in the process list
printf 'url = "https://api.telegram.org/bot%s/logOut"\n' "$TOKEN" | curl -s -K - | grep -q '"ok":true' || { rm -f "$DATA/paused"; echo "logOut refused"; exit 1; }
sed -i '\#^TELEGRAM_API=#d' "$C"
echo 'TELEGRAM_API=http://127.0.0.1:8081' >> "$C"
(crontab -l 2>/dev/null | grep -v telegram-bot-api; echo "* * * * * flock -n /tmp/central-botapi.lock $HERE/run_local_bot_api.sh") | crontab -
nohup flock -n /tmp/central-botapi.lock "$HERE/run_local_bot_api.sh" >/dev/null 2>&1 &

for i in $(seq 1 30); do
  sleep 2
  printf 'url = "http://127.0.0.1:8081/bot%s/getMe"\n' "$TOKEN" | curl -s -K - | grep -q '"ok":true' && { rm -f "$DATA/paused"; echo "on"; exit 0; }
done
echo "the local server did not answer; the bot stays paused"; exit 1
