#!/bin/bash
# Остановка бота (или всех ботов)
# Использование: ./commands/bot-stop.sh [bot-name]

PROJECT_DIR="/opt/pumpfun-bonkfun-bot"
BOT_NAME="$1"

if [[ -n "$BOT_NAME" ]]; then
    # Остановка конкретного бота
    PID_FILE="$PROJECT_DIR/data/${BOT_NAME}.pid"
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping $BOT_NAME (PID: $PID)..."
            kill -SIGTERM "$PID"
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                echo "Force killing..."
                kill -9 "$PID"
            fi
        fi
        rm -f "$PID_FILE"
        echo "Bot stopped: $BOT_NAME"
    else
        echo "No PID file found for: $BOT_NAME"
    fi
else
    # Остановка всех ботов
    echo "Stopping all bots..."
    pkill -f "bot_runner.py" || true
    rm -f "$PROJECT_DIR"/data/*.pid
    echo "All bots stopped"
fi
