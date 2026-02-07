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

### 1) Подготовка сервера (Ubuntu 20.04+)


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
Заполните:

text
SOLANA_PRIVATE_KEY=ваш_приватный_ключ_base58
ALCHEMY_RPC_ENDPOINT=https://solana-mainnet.g.alchemy.com/v2/ваш_ключ
DRPC_RPC_ENDPOINT=https://lb.drpc.org/ogrpc?network=solana&dkey=ваш_ключ
HELIUS_API_KEY=ваш_helius_ключ
JUPITER_TRADE_API_KEY=ваш_jupiter_ключ
JITO_TIP_ACCOUNT=Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY
JITO_TIP_AMOUNT=100000
5) Конфиг бота
bash
nano bots/bot-whale-copy.yaml
Основные параметры:

text
buy_amount: 0.01        # SOL на сделку
min_whale_buy: 0.5      # Мин. покупка кита
stop_loss_pct: 30       # Стоп-лосс -30%
tsl_enabled: true       # Trailing stop
tsl_activation_pct: 0.3 # Активация TSL при +30%
tsl_sell_pct: 0.9       # Продать 90% от максимума
6) База китов
Файл smart_money_wallets.json содержит кошельки китов.

Добавить/изменить кита:

bash
nano smart_money_wallets.json
Пример записи:

json
{
  "wallet": "АДРЕС_КОШЕЛЬКА",
  "win_rate": 0.7,
  "trades_count": 0,
  "label": "whale",
  "source": "manual",
  "added_date": "2026-01-01T00:00:00Z"
}
После изменений:

bash
wsync && bot-restart
7) Логи и права
bash
mkdir -p logs
chmod +x start.sh stop.sh
8) Добавляем алиасы (опционально)
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
9) Запуск
bash
bot-start
10) Проверка
bash
bot-status
bot-logs
bot-health
Команды
bot-start — запустить

bot-stop — остановить

bot-restart — перезапустить

bot-logs — логи

bot-errors — ошибки

bot-health — статус

bot-config — редактировать конфиг

bot-whales-edit — редактировать китов

bot-whales-count — количество китов

wsync — синхронизировать вебхуки

bot-update — обновить с GitHub

bot-reset — полный сброс

Быстрые команды
Синхронизация после изменений:

bash
wsync && bot-restart
Полный сброс Redis:

bash
redis-cli del whale:positions && wsync && bot-restart
Helius Webhooks
Бот использует Helius webhooks для мгновенного получения сигналов о сделках китов.

Создание webhook (через API)
bash
curl -X POST "https://api.helius.xyz/v0/webhooks?api-key=ВАШ_HELIUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "webhookURL": "http://ВАШ_IP:8000/webhook",
    "transactionTypes": ["SWAP"],
    "accountAddresses": [],
    "webhookType": "enhanced"
  }'
Сохраните полученный webhookID.

Синхронизация китов с webhook
После добавления/удаления китов в smart_money_wallets.json выполните:

bash
wsync && bot-restart
Проверка webhook локально
bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '[{"type":"SWAP","signature":"test"}]'
DCA (Dollar Cost Averaging)
Бот автоматически усредняет позицию при просадке или росте.

Пример DCA в логах:

text
[DCA] First buy: 0.0100 SOL (50% of 0.0200)
[DCA] Charizard: Price 0.0000020778 <= 0.0000021435 (-25%)
[DCA] Executing second buy for Charizard (-25%)...
[DCA] ✅ SUCCESS! Bought 4715.78 more at 0.0000021205
[DCA] Total tokens: 3498.89 -> 8214.66
[DCA] New entry: 0.0000021205
[DCA] New SL: 0.0000014844 (-30%)
Статус DCA в позиции:

bash
cat positions.json | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    dca = '⏳ ЖДЁТ' if p.get('dca_pending') else ('✅ КУПЛЕН' if p.get('dca_bought') else '❌ ВЫКЛ')
    print(f\"{p.get('symbol'):12} | DCA: {dca}\")
"
Формат smart_money_wallets.json
Правильный формат:

json
{
  "whales": [
    { "wallet": "АДРЕС_1", "label": "whale-1" },
    { "wallet": "АДРЕС_2", "label": "whale-2" }
  ]
}
Частые ошибки:

❌ Неправильно — просто массив адресов:

json
["АДРЕС_1", "АДРЕС_2"]
❌ Неправильно — без обёртки whales:

json
[{ "wallet": "АДРЕС_1", "label": "whale-1" }]
❌ Неправильно — ключ wallets вместо whales:

json
{ "wallets": [] }
Проверка файла:

bash
python3 -c "import json; d=json.load(open('smart_money_wallets.json')); print(f'Китов: {len(d.get(\"whales\", []))}')"


Disclaimer
Торговля криптовалютой связана с высоким риском. Начинайте с 0.01 SOL.
 
