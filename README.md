# Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.

## Возможности

- Whale Copy Trading — отслеживание 140+ китов через Helius webhooks
- Stop Loss / TSL / Take Profit — автоматическое управление позициями
- DCA — усреднение при просадке
- Moonbag — сохранение 10% после TSL
- Redis — быстрая синхронизация позиций
- Поддержка DEX — Pump.fun, PumpSwap, Jupiter, Raydium

## API ключи

- Helius — https://helius.dev
- Alchemy — https://alchemy.com
- DRPC — https://drpc.org
- Jupiter — https://station.jup.ag/docs

## Установка (Ubuntu 20.04+)

### 1) Подготовка сервера

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3.10 python3.10-venv python3-pip redis-server git -y
sudo systemctl enable redis-server && sudo systemctl start redis-server
2) Клонирование
bash
cd /opt
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot
3) Виртуальное окружение
bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
4) Настройка .env
bash
cp .env.example .env
nano .env
Минимально нужно заполнить (пример):

text
SOLANA_PRIVATE_KEY=ваш_приватный_ключ_base58
ALCHEMY_RPC_ENDPOINT=https://solana-mainnet.g.alchemy.com/v2/ВАШ_КЛЮЧ
DRPC_RPC_ENDPOINT=https://lb.drpc.org/ogrpc?network=solana&dkey=ВАШ_КЛЮЧ
HELIUS_API_KEY=ВАШ_HELIUS_КЛЮЧ
JUPITER_TRADE_API_KEY=ВАШ_JUPITER_КЛЮЧ
5) Конфиг бота
bash
nano bots/bot-whale-copy.yaml
Пример ключевых параметров:

text
buy_amount: 0.01
min_whale_buy: 0.5
stop_loss_pct: 30

tsl_enabled: true
tsl_activation_pct: 0.3
tsl_sell_pct: 0.9
6) База китов
Файл smart_money_wallets.json содержит кошельки китов.

bash
nano smart_money_wallets.json
После изменений:

bash
wsync && bot-restart
7) Логи
bash
mkdir -p logs
chmod +x start.sh stop.sh
Запуск и проверка
Запуск:

bash
./start.sh
Остановка:

bash
./stop.sh
Проверка:

bash
ps aux | grep bot_runner | grep -v grep
tail -f logs/bot-whale-copy.log
curl -s http://localhost:8000/health
Алиасы (опционально)
Добавить в ~/.bashrc:

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
Helius Webhooks (кратко)
После добавления/удаления китов синхронизируй webhook:

bash
wsync && bot-restart
Тест webhook локально:

bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '[{"type":"SWAP","signature":"test"}]'

Disclaimer
Торговля криптовалютой связана с высоким риском. Начинайте с 0.01 SOL.
 
