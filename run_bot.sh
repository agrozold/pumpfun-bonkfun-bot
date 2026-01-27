#!/bin/bash
# Universal bot runner
# Usage: ./run_bot.sh <config_file>
# Example: ./run_bot.sh bots/bot-whale-copy.yaml

cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

CONFIG="${1:-bots/bot-whale-copy.yaml}"
BOT_NAME=$(basename "$CONFIG" .yaml)
LOG_FILE="logs/${BOT_NAME}.log"

# Kill existing instance
pkill -f "bot_runner.py.*${BOT_NAME}" 2>/dev/null || true
sleep 1

# Create logs dir
mkdir -p logs

# Start bot
echo "[$(date)] Starting $BOT_NAME with config: $CONFIG"
nohup python src/bot_runner.py "$CONFIG" > "$LOG_FILE" 2>&1 &
PID=$!
echo "[$(date)] Started with PID: $PID"

# Wait and check
sleep 3
if ps -p $PID > /dev/null 2>&1; then
    echo "✅ Bot running (PID: $PID)"
    echo "📋 Log: tail -f $LOG_FILE"
    tail -20 "$LOG_FILE" | grep -E "(VERSION|ERROR|started|WHALE|positions)" || true
else
    echo "❌ Bot crashed! Check log:"
    tail -50 "$LOG_FILE"
fi
