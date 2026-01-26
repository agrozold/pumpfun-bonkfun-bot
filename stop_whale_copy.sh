#!/bin/bash
cd /opt/pumpfun-bonkfun-bot

if [ -f whale_copy.pid ]; then
    PID=$(cat whale_copy.pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "Stopping Whale Copy Bot (PID: $PID)..."
        kill $PID
        sleep 2
        if ps -p $PID > /dev/null 2>&1; then
            echo "Force killing..."
            kill -9 $PID
        fi
        rm -f whale_copy.pid
        echo "Stopped."
    else
        echo "Process not running (stale PID file)"
        rm -f whale_copy.pid
    fi
else
    echo "No PID file found. Checking for running processes..."
    pkill -f "python.*bot-whale-copy" 2>/dev/null && echo "Killed." || echo "No process found."
fi
