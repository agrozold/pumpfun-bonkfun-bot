#!/bin/bash
# Start Whale Copy Bot only (no snipers)
cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

echo "============================================"
echo "  WHALE COPY BOT - Starting..."
echo "============================================"
echo "Config: bots/bot-whale-copy.yaml"
echo "Wallets: smart_money_wallets.json"
echo ""

# Check if already running
if [ -f whale_copy.pid ]; then
    OLD_PID=$(cat whale_copy.pid)
    if ps -p $OLD_PID > /dev/null 2>&1; then
        echo "Bot already running with PID: $OLD_PID"
        echo "To restart: kill $OLD_PID && ./start_whale_copy.sh"
        exit 1
    fi
fi

# Add src to PYTHONPATH
export PYTHONPATH="/opt/pumpfun-bonkfun-bot/src:$PYTHONPATH"

# Run in background with nohup
LOG_FILE="logs/whale_copy_$(date +%Y%m%d_%H%M%S).log"
nohup python /opt/pumpfun-bonkfun-bot/src/bot_runner.py bots/bot-whale-copy.yaml > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > whale_copy.pid

sleep 3

# Verify started
if ps -p $PID > /dev/null 2>&1; then
    echo "Started successfully!"
    echo "PID: $PID (saved to whale_copy.pid)"
    echo "Log: $LOG_FILE"
    echo ""
    echo "Commands:"
    echo "  Monitor:  tail -f $LOG_FILE"
    echo "  Stop:     kill \$(cat whale_copy.pid)"
    echo "  Status:   ps aux | grep whale"
else
    echo "ERROR: Failed to start! Check logs:"
    tail -30 "$LOG_FILE"
    exit 1
fi
