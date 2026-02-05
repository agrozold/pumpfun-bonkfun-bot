# Whale Copy Trading Bot

Бот следит за кошельками крупных трейдеров (китов) и автоматически копирует их покупки на pump.fun, Raydium, Jupiter.

---

## Шаг 1: Обновляем систему

sudo apt update && sudo apt upgrade -y

---

## Шаг 2: Ставим нужные программы

sudo apt install python3.10 python3.10-venv python3-pip redis-server git curl jq -y

---

## Шаг 3: Запускаем Redis

sudo systemctl enable redis-server
sudo systemctl start redis-server

Проверяем (должно ответить PONG):

redis-cli ping

---

## Шаг 4: Скачиваем бота

cd /opt
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot

---

## Шаг 5: Создаём виртуальное окружение Python

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e .

---

## Шаг 6: Получаем API ключи

Нужно зарегистрироваться и получить бесплатные ключи:

- Helius — https://helius.dev (обязательно, для вебхуков)
- Alchemy — https://alchemy.com (резервный RPC)
- Jupiter — https://station.jup.ag (для торговли)

---

## Шаг 7: Настраиваем .env

cp .env.example .env
nano .env

Заполни своими ключами:

SOLANA_PRIVATE_KEY=твой_приватный_ключ_base58
SOLANA_NODE_RPC_ENDPOINT=https://mainnet.helius-rpc.com/?api-key=HELIUS_KEY
SOLANA_NODE_WSS_ENDPOINT=wss://mainnet.helius-rpc.com/?api-key=HELIUS_KEY
CHAINSTACK_WSS_ENDPOINT=wss://mainnet.helius-rpc.com/?api-key=HELIUS_KEY
HELIUS_API_KEY=твой_helius_key
JUPITER_TRADE_API_KEY=твой_jupiter_key
ALCHEMY_RPC_ENDPOINT=https://solana-mainnet.g.alchemy.com/v2/ALCHEMY_KEY
JITO_ENABLED=true
JITO_TIP_LAMPORTS=200000

---

## Шаг 8: Настраиваем параметры торговли

nano bots/bot-whale-copy.yaml

Основные параметры:

buy_amount: 0.01 — сколько SOL тратить на сделку
stop_loss_percentage: 0.3 — стоп-лосс -30%
tsl_enabled: true — trailing stop loss включён
tsl_activation_pct: 0.3 — TSL активируется при +30%

---

## Шаг 9: Настраиваем базу китов

Файл smart_money_wallets.json содержит кошельки китов.

Посмотреть список:

cat smart_money_wallets.json | jq '.whales[].wallet'

Добавить кита — открой файл и добавь в массив whales:

{
  "wallet": "АДРЕС_КОШЕЛЬКА",
  "win_rate": 0.7,
  "trades_count": 0,
  "label": "whale",
  "source": "manual",
  "added_date": "2026-02-05T12:00:00Z"
}

После изменений синхронизируй:

wsync && bot-restart

---

## Шаг 10: Создаём папку для логов и даём права

mkdir -p logs
chmod +x start.sh stop.sh

---

## Шаг 11: Добавляем алиасы (удобные команды)

cat >> ~/.bashrc << 'ALIASEOF'

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
ALIASEOF

source ~/.bashrc

---

## Шаг 12: Запускаем бота

bot-start

---

## Шаг 13: Проверяем

bot-status
bot-logs
bot-health

---

## Команды на каждый день

bot-start — запустить
bot-stop — остановить
bot-restart — перезапустить
bot-logs — смотреть логи
bot-errors — показать ошибки
bot-health — проверить здоровье
bot-config — редактировать настройки
bot-whales-edit — редактировать китов
bot-whales-count — количество китов
wsync — синхронизировать вебхуки
bot-update — обновить с GitHub
bot-reset — полный сброс

---

## Если что-то сломалось

Посмотреть ошибки:

bot-errors

Полный сброс:

bot-reset

Проверить Redis:

redis-cli ping

---

## Важно

- Начни с buy_amount: 0.01 SOL для тестов
- После добавления китов делай wsync && bot-restart
- Приватный ключ никому не показывай
