#!/bin/bash
#
# stop.sh - Stop whale-copy bot gracefully
#

PID_FILE="/tmp/whale-bot.pid"
BOT_DIR="/opt/pumpfun-bonkfun-bot"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${GREEN}[INFO]${NC} Stopping bot (PID: $PID)..."
        kill -TERM "$PID" 2>/dev/null
        
        for i in {1..10}; do
            if ! kill -0 "$PID" 2>/dev/null; then
                echo -e "${GREEN}[INFO]${NC} Bot stopped gracefully"
                rm -f "$PID_FILE"
                break
            fi
            sleep 1
        done
        
        if kill -0 "$PID" 2>/dev/null; then
            echo -e "${RED}[WARN]${NC} Force killing..."
            kill -9 "$PID" 2>/dev/null
            rm -f "$PID_FILE"
        fi
    else
        echo -e "${RED}[WARN]${NC} Process not running (stale PID file)"
        rm -f "$PID_FILE"
    fi
else
    echo -e "${RED}[WARN]${NC} No PID file found"
    PIDS=$(pgrep -f "bot_runner.py" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo -e "${GREEN}[INFO]${NC} Found running processes: $PIDS"
        echo "$PIDS" | xargs kill -9 2>/dev/null || true
    fi
fi

# Export positions backup
echo -e "${GREEN}[INFO]${NC} Exporting positions backup..."
cd "$BOT_DIR"
source venv/bin/activate 2>/dev/null || true

python3 << 'PYEOF'
import json
import redis
from datetime import datetime

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
try:
    data = r.hgetall('whale:positions')
    if data:
        positions = [json.loads(v) for v in data.values()]
        backup_file = f"positions.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(backup_file, 'w') as f:
            json.dump(positions, f, indent=2)
        with open('positions.json', 'w') as f:
            json.dump(positions, f, indent=2)
        print(f"[BACKUP] Saved {len(positions)} positions")
except Exception as e:
    print(f"[BACKUP] Error: {e}")
PYEOF

# Kill any remaining bot_runner processes
pkill -9 -f "bot_runner.py" 2>/dev/null || true
sleep 1
echo -e "${GREEN}[INFO]${NC} Done"
