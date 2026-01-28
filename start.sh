#!/bin/bash
set -e

BOT_DIR="/opt/pumpfun-bonkfun-bot"
BOT_CONFIG="bots/bot-whale-copy.yaml"
PID_FILE="/tmp/whale-bot.pid"
LOG_FILE="logs/bot-whale-copy.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

log_info "=== STEP 1: Killing old processes ==="

# Kill by PID file
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ]; then
        kill -9 "$OLD_PID" 2>/dev/null && log_warn "Killed PID $OLD_PID" || true
    fi
    rm -f "$PID_FILE"
fi

# Kill all bot processes
pkill -9 -f "bot_runner.py" 2>/dev/null && log_warn "Killed bot_runner processes" || true
sleep 1

# Kill port 8000
fuser -k 8000/tcp 2>/dev/null && log_warn "Killed port 8000" || true
sleep 1

log_info "All old processes killed"

log_info "=== STEP 2: Clearing Redis locks ==="
redis-cli DEL whale:bot_lock 2>/dev/null || true
redis-cli KEYS "whale:buying:*" 2>/dev/null | xargs -r redis-cli DEL 2>/dev/null || true
log_info "Redis locks cleared"

log_info "=== STEP 3: Verifying Redis ==="
if redis-cli PING 2>/dev/null | grep -q "PONG"; then
    POS_COUNT=$(redis-cli HLEN whale:positions 2>/dev/null || echo "0")
    log_info "Redis OK, positions: $POS_COUNT"
else
    log_error "Redis not running!"
    exit 1
fi

log_info "=== STEP 4: Starting bot ==="
cd "$BOT_DIR"
source venv/bin/activate
mkdir -p logs

# Start WITHOUT pre-writing PID (bot writes it itself)
nohup python3 src/bot_runner.py "$BOT_CONFIG" >> "$LOG_FILE" 2>&1 &

log_info "=== STEP 5: Waiting for startup ==="
sleep 5

# Check if running
if [ -f "$PID_FILE" ]; then
    NEW_PID=$(cat "$PID_FILE")
    if kill -0 "$NEW_PID" 2>/dev/null; then
        log_info "Bot running with PID: $NEW_PID"
    else
        log_error "Bot died after start!"
        tail -30 "$LOG_FILE"
        exit 1
    fi
else
    # Fallback: find by process
    NEW_PID=$(pgrep -f "bot_runner.py" | head -1)
    if [ -n "$NEW_PID" ]; then
        echo "$NEW_PID" > "$PID_FILE"
        log_info "Bot running with PID: $NEW_PID (detected)"
    else
        log_error "Bot failed to start!"
        tail -30 "$LOG_FILE"
        exit 1
    fi
fi

# Health check
HEALTH=$(curl -s http://localhost:8000/health 2>/dev/null || echo "")
if echo "$HEALTH" | grep -q "ok"; then
    log_info "Health check: OK"
    echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"
else
    log_warn "Health endpoint not ready yet"
fi

echo ""
log_info "=== STARTUP COMPLETE ==="
echo "  PID: $(cat $PID_FILE)"
echo "  Log: tail -f $LOG_FILE"
echo "  Health: curl http://localhost:8000/health"
echo "  Stop: kill \$(cat $PID_FILE)"
