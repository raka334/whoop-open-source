#!/usr/bin/env bash
# Start the WHOOP live pipeline: serial bridge + dashboard, immune to hangups.
# Usage: ./tools/start_live.sh [seconds]   (default 3600)
set -u
cd "$(dirname "$0")/.."
PY="${WHOOP_PY:-$HOME/whoop-re/.venv-fw/bin/python}"
SECS="${1:-3600}"
pkill -f tools/esp32_bridge.py 2>/dev/null; pkill -f tools/live_dashboard.py 2>/dev/null
sleep 1
setsid nohup "$PY" tools/esp32_bridge.py --seconds "$SECS" >/tmp/whoop_bridge.log 2>&1 &
echo "bridge pid $! -> /tmp/whoop_bridge.log"
setsid nohup "$PY" tools/live_dashboard.py --port 8790 >/tmp/whoop_dash.log 2>&1 &
echo "dashboard pid $! -> http://127.0.0.1:8790"
sleep 5
tail -2 /tmp/whoop_bridge.log
