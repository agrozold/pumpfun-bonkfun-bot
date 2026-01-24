#!/bin/bash
# Продажа конкретного токена
# Использование: ./commands/bot-sell.sh <mint_address>

MINT="$1"
PROJECT_DIR="/opt/pumpfun-bonkfun-bot"

if [[ -z "$MINT" ]]; then
    echo "Usage: bot-sell.sh <mint_address>"
    exit 1
fi

cd "$PROJECT_DIR"
source venv/bin/activate

python sell.py "$MINT"
