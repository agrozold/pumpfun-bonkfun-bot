# Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.

## Возможности

- Whale Copy Trading — отслеживание 140+ китов через Helius webhooks
- Stop Loss / TSL / Take Profit — автоматическое управление позициями
- DCA — усреднение при просадке
- Moonbag — сохранение 10% после TSL
- Redis — быстрая синхронизация позиций
- Поддержка DEX — Pump.fun, PumpSwap, Jupiter, Raydium

## Необходимые API ключи

- Helius (https://helius.dev) — для webhooks
- Alchemy (https://alchemy.com) — Solana RPC
- DRPC (https://drpc.org) — резервный RPC
- Jupiter (https://station.jup.ag/docs) — для свапов

## Установка

### 1. Подготовка сервера (Ubuntu 20.04+)

sudo apt update && sudo apt upgrade -y
sudo apt install python3.10 python3.10-venv python3-pip redis-server git -y
sudo systemctl enable redis-server && sudo systemctl start redis-server

### 2. Клонирование

cd /opt
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot

### 3. Виртуальное окружение

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

### 4. Настройка .env

cp .env.example .env
nano .env

Заполните:

SOLANA_PRIVATE_KEY=ваш_приватный_ключ_base58
ALCHEMY_RPC_ENDPOINT=https://solana-mainnet.g.alchemy.com/v2/ваш_ключ
DRPC_RPC_ENDPOINT=https://lb.drpc.org/ogrpc?network=solana&dkey=ваш_ключ
HELIUS_API_KEY=ваш_helius_ключ
JUPITER_TRADE_API_KEY=ваш_jupiter_ключ
JITO_TIP_ACCOUNT=Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY
JITO_TIP_AMOUNT=100000

### 5. Конфиг бота

nano bots/bot-whale-copy.yaml

Основные параметры:

buy_amount: 0.01        # SOL на сделку
min_whale_buy: 0.5      # Мин. покупка кита
stop_loss_pct: 30       # Стоп-лосс -30%
tsl_enabled: true       # Trailing stop
tsl_activation_pct: 0.3 # Активация TSL при +30%
tsl_sell_pct: 0.9       # Продать 90% от максимума

### 6. База китов

Файл smart_money_wallets.json содержит кошельки китов.

Добавить кита:

nano smart_money_wallets.json

Формат записи:

{
  "wallet": "АДРЕС_КОШЕЛЬКА",
  "win_rate": 0.7,
  "trades_count": 0,
  "label": "whale",
  "source": "manual",
  "added_date": "2026-01-01T00:00:00Z"
}

После изменений:

wsync && bot-restart

### 7. Создаём папку логов

mkdir -p logs
chmod +x start.sh stop.sh

### 8. Добавляем алиасы

cat >> ~/.bashrc << 'EOF'

# === WHALE BOT ===
BOT_DIR="/opt/pumpfun-bonkfun-bot"

alias bot-start='cd $BOT_DIR && ./start.sh'
alias bot-stop='cd $BOT_DIR && ./stop.sh'
alias bot-restart='bot-stop && sleep 3 && bot-start'
alias bot-status='ps aux | grep bot_runner | grep -v grep'
alias bot-logs='tail -f $BOT_DIR/logs/bot-whale-copy.log'
alias bot-errors='grep -h "ERROR\|FAILED" $BOT_DIR/logs/*.log | tail -30'
alias wsync='cd $BOT_DIR && source venv/bin/activate && python3 wsync.py'
alias bot-health='curl -s http://localhost:8000/health 2>/dev/null | jq || echo "Бот не запущен"'
alias bot-config='nano $BOT_DIR/bots/bot-whale-copy.yaml'
alias bot-env='nano $BOT_DIR/.env'
alias bot-whales-edit='nano $BOT_DIR/smart_money_wallets.json'
alias bot-whales-count='cat $BOT_DIR/smart_money_wallets.json | jq ".whales | length"'
alias bot-update='cd $BOT_DIR && git pull && bot-restart'
alias bot-reset='bot-stop && redis-cli DEL whale:positions && redis-cli DEL whale:bot_lock && wsync && bot-start'
EOF

source ~/.bashrc

### 9. Запуск

bot-start

### 10. Проверка

bot-status
bot-logs
bot-health

## Команды

- bot-start — запустить
- bot-stop — остановить
- bot-restart — перезапустить
- bot-logs — логи
- bot-errors — ошибки
- bot-health — статус
- bot-config — редактировать конфиг
- bot-whales-edit — редактировать китов
- bot-whales-count — количество китов
- wsync — синхронизировать вебхуки
- bot-update — обновить с GitHub
- bot-reset — полный сброс

## Быстрые команды

Синхронизация после изменений:

wsync && bot-restart

Полный сброс Redis:

redis-cli del whale:positions && wsync && bot-restart

## Disclaimer

Торговля криптовалютой связана с высоким риском. Начинайте с 0.01 SOL.
