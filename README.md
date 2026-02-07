# Whale Copy Trading Bot (Solana)

Бот для копирования сделок “smart money/whales” в сети Solana (Helius webhooks → авто-покупка/продажа).  
Поддерживает SL/TSL/TP, DCA, moonbag, хранит состояние в Redis.

## Быстрый старт (Ubuntu 20.04+)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3.10 python3.10-venv python3-pip redis-server git -y
sudo systemctl enable redis-server && sudo systemctl start redis-server

cd /opt
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

nano bots/bot-whale-copy.yaml
nano smart_money_wallets.json

chmod +x start.sh stop.sh
./start.sh
Проверка:

bash
tail -f logs/bot-whale-copy.log
curl -s http://localhost:8000/health
Документация
Установка и эксплуатация: docs/setup.md

Команды/шпаргалка: BOT_COMMANDS.md

Disclaimer
Торговля криптовалютой связана с высоким риском. Начинайте с небольших сумм.

text

***

## docs/setup.md (новый файл)

```md
# Setup / Operations

## Требования

- Ubuntu 20.04+
- Python 3.10+
- Redis
- API ключи: Helius, RPC (Alchemy/DRPC), Jupiter

## .env

```bash
cp .env.example .env
nano .env
Минимально:

text
SOLANA_PRIVATE_KEY=ваш_приватный_ключ_base58
HELIUS_API_KEY=ваш_helius_ключ

ALCHEMY_RPC_ENDPOINT=https://solana-mainnet.g.alchemy.com/v2/ВАШ_КЛЮЧ
DRPC_RPC_ENDPOINT=https://lb.drpc.org/ogrpc?network=solana&dkey=ВАШ_КЛЮЧ

JUPITER_TRADE_API_KEY=ваш_jupiter_ключ
Конфиг бота
bash
nano bots/bot-whale-copy.yaml
Пример:

text
buy_amount: 0.01
min_whale_buy: 0.5
stop_loss_pct: 30

tsl_enabled: true
tsl_activation_pct: 0.3
tsl_sell_pct: 0.9
База китов
bash
nano smart_money_wallets.json
Формат (схема):

json
{
  "whales": [
    { "wallet": "АДРЕС_1", "label": "whale-1" },
    { "wallet": "АДРЕС_2", "label": "whale-2" }
  ]
}
После изменений:

bash
wsync && bot-restart
Запуск / остановка
bash
./start.sh
./stop.sh
Логи и диагностика
Логи:

bash
tail -f logs/bot-whale-copy.log
Ошибки:

bash
grep -h "ERROR\|FAILED" logs/*.log | tail -50
Health:

bash
curl -s http://localhost:8000/health
Алиасы (опционально)
bash
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
Helius Webhooks (пример)
Создание webhook:

bash
curl -X POST "https://api.helius.xyz/v0/webhooks?api-key=ВАШ_HELIUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "webhookURL": "http://ВАШ_IP:8000/webhook",
    "transactionTypes": ["SWAP"],
    "accountAddresses": [],
    "webhookType": "enhanced"
  }'
Тест:

bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '[{"type":"SWAP","signature":"test"}]'

# Disclaimer
Торговля криптовалютой связана с высоким риском. Начинайте с 0.01 SOL.
 
