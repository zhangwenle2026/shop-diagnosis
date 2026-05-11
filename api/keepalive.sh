#!/bin/bash
# API Keepalive Script
# Checks if shop diagnosis API is running, restarts if not

LOG="/tmp/api_keepalive.log"
API_DIR="/root/.openclaw/workspace/shop-diagnosis/api"
PORT=8765

timestamp() {
  date '+%Y-%m-%d %H:%M:%S BRT'
}

# Check if API is alive
if curl -s --connect-timeout 3 http://localhost:$PORT/health > /dev/null 2>&1; then
  echo "[$(timestamp)] API is alive on port $PORT" >> "$LOG"
  exit 0
fi

# API is down, restart it
echo "[$(timestamp)] API is DOWN, restarting..." >> "$LOG"

# Kill any lingering process on the port
fuser -k $PORT/tcp 2>/dev/null || true
pkill -f "uvicorn.*server:app" 2>/dev/null || true
sleep 2

# Start server
cd "$API_DIR"
nohup python3 -m uvicorn server:app --host 0.0.0.0 --port $PORT --log-level warning >> "$LOG" 2>&1 &
sleep 5

# Verify
if curl -s --connect-timeout 5 http://localhost:$PORT/health > /dev/null 2>&1; then
  echo "[$(timestamp)] API restarted successfully" >> "$LOG"
else
  echo "[$(timestamp)] ERROR: API failed to restart!" >> "$LOG"
fi

# Keep log under 500 lines
tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
