#!/bin/bash
# Запуск бота с указанным конфигом
# Использование: ./commands/bot-start.sh bots/bot-sniper-0-pump.yaml

CONFIG="${1:-bots/bot-sniper-0-pump.yaml}"
PROJECT_DIR="/opt/pumpfun-bonkfun-bot"
VENV_DIR="${PROJECT_DIR}/venv"

if [[ ! -f "$PROJECT_DIR/$CONFIG" ]]; then
    echo "ERROR: Config not found: $CONFIG"
    exit 1
fi

cd "$PROJECT_DIR"
source "${VENV_DIR}/bin/activate"

# Проверка .env
if [[ ! -f .env ]]; then
    echo "ERROR: .env file not found"
    exit 1
fi

# Получение имени бота из конфига
BOT_NAME=$(basename "$CONFIG" .yaml)

# Проверка, не запущен ли уже
if pgrep -f "bot_runner.py.*${CONFIG}" > /dev/null; then
    echo "WARNING: Bot already running with config: $CONFIG"
    echo "Use bot-stop.sh first or check bot-status.sh"
    exit 1
fi

# Запуск в фоне с логированием
nohup python src/bot_runner.py "$CONFIG" \
    >> "logs/${BOT_NAME}.log" 2>&1 &

BOT_PID=$!
echo "$BOT_PID" > "data/${BOT_NAME}.pid"

echo "Bot started: $BOT_NAME (PID: $BOT_PID)"
echo "Logs: tail -f logs/${BOT_NAME}.log"
