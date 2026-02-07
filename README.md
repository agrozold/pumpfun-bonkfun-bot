# Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.

## Возможности

- Whale Copy Trading — отслеживание китов через Helius webhooks
- Stop Loss / TSL / Take Profit — автоматическое управление позициями
- DCA — усреднение при просадке
- Moonbag — сохранение части позиции после TSL
- Redis — быстрая синхронизация позиций
- Поддержка DEX — Pump.fun, PumpSwap, Jupiter, Raydium

## Необходимые ключи и RPC

**Helius**
- Helius (https://helius.dev) — webhooks + (опционально) Solana RPC.

**RPC (Solana)**
Тебе нужен хотя бы один RPC endpoint. В проекте предусмотрены несколько переменных (можно использовать один или несколько провайдеров):

- `SOLANA_NODE_RPC_ENDPOINT` — любой свой RPC (свой нод или любой провайдер)
- `ALCHEMY_RPC_ENDPOINT` — Alchemy (https://alchemy.com) — Solana RPC
- `DRPC_RPC_ENDPOINT` — dRPC (https://drpc.org) — Solana RPC

Другие популярные варианты RPC провайдеров (их можно использовать в `SOLANA_NODE_RPC_ENDPOINT`):
- Helius RPC URLs and endpoints: https://www.helius.dev/docs/api-reference/endpoints
- QuickNode / Chainstack / Ankr и др.

**Jupiter**
- Jupiter (https://station.jup.ag/docs) — свапы / trade API

---

## Установка (для новичков)

### 1) Подготовка сервера (Ubuntu 20.04+)

~~~bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3.10 python3.10-venv python3-pip redis-server git -y
sudo systemctl enable redis-server && sudo systemctl start redis-server
~~~

### 2) Клонирование

~~~bash
cd /opt
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot
~~~

### 3) Виртуальное окружение

~~~bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
~~~

Если видишь `(venv)` в терминале — ок.

### 4) Настройка .env

~~~bash
cp .env.example .env
nano .env
~~~

Заполни как минимум:
- SOLANA_PRIVATE_KEY
- HELIUS_API_KEY
- ALCHEMY_RPC_ENDPOINT (или SOLANA_NODE_RPC_ENDPOINT)
- DRPC_RPC_ENDPOINT (если используешь)
- JUPITER_TRADE_API_KEY
- WEBHOOK_URL

### 5) Конфиг бота

~~~bash
nano bots/bot-whale-copy.yaml
~~~

Пример ключевых параметров:

~~~yaml
buy_amount: 0.01        # SOL на сделку
min_whale_buy: 0.5      # Мин. покупка кита
stop_loss_pct: 30       # Стоп-лосс -30%
tsl_enabled: true       # Trailing stop
tsl_activation_pct: 0.3 # Активация TSL при +30%
tsl_sell_pct: 0.9       # Продать 90% от максимума
~~~

### 6) База китов (smart_money_wallets.json)

Открыть:

~~~bash
nano smart_money_wallets.json
~~~

Правильный формат:

~~~json
{
  "whales": [
    { "wallet": "АДРЕС_1", "label": "whale-1" },
    { "wallet": "АДРЕС_2", "label": "whale-2" }
  ]
}
~~~

Проверить JSON и количество китов:

~~~bash
python3 -c "import json; d=json.load(open('smart_money_wallets.json')); print('Китов:', len(d.get('whales', [])))"
~~~

После любых изменений списка китов:

~~~bash
wsync && bot-restart
~~~

---

## Добавить/удалить кита (команды)

### Добавить кита

~~~bash
python3 << 'PYEOF'
import json

new_wallet = "АДРЕС_КОШЕЛЬКА"
label = "whale-new"

with open("smart_money_wallets.json") as f:
    data = json.load(f)

data.setdefault("whales", [])
exists = any(w.get("wallet") == new_wallet for w in data["whales"])

if not exists:
    data["whales"].append({"wallet": new_wallet, "label": label})
    with open("smart_money_wallets.json", "w") as f:
        json.dump(data, f, indent=2)
    print("✅ Добавлен:", label)
else:
    print("❌ Уже есть")
PYEOF
~~~

Потом:

~~~bash
wsync && bot-restart
~~~

### Удалить кита

~~~bash
python3 << 'PYEOF'
import json

wallet_to_remove = "АДРЕС_КОШЕЛЬКА"

with open("smart_money_wallets.json") as f:
    data = json.load(f)

before = len(data.get("whales", []))
data["whales"] = [w for w in data.get("whales", []) if w.get("wallet") != wallet_to_remove]
after = len(data["whales"])

with open("smart_money_wallets.json", "w") as f:
    json.dump(data, f, indent=2)

print("✅ Удалён" if after < before else "❌ Не найден")
PYEOF
~~~

Потом:

~~~bash
wsync && bot-restart
~~~

---

## Логи и запуск

Создаём папку логов:

~~~bash
mkdir -p logs
chmod +x start.sh stop.sh
~~~

Запуск:

~~~bash
./start.sh
~~~

Остановка:

~~~bash
./stop.sh
~~~

## Проверка

~~~bash
ps aux | grep bot_runner | grep -v grep
tail -f logs/bot-whale-copy.log
curl -s http://localhost:8000/health
~~~

---

## Полезные команды (grep)

~~~bash
# Ошибки
grep -h "ERROR\|FAILED" logs/*.log | tail -30

# Последние сделки
grep -h "Successfully bought" logs/*.log | tail -20
grep -h "Successfully sold" logs/*.log | tail -20

# Whale copy trades
grep -h "whale buy\|WHALE" logs/*.log | tail -20
grep -h "Skipping whale" logs/*.log | tail -10

# PnL по позициям
grep "Position PnL" logs/*.log | tail -20

# Take Profit / Stop Loss
grep "TAKE_PROFIT" logs/*.log | tail -20
grep "STOP_LOSS" logs/*.log | tail -20

# Moonbag
grep "moon bag" logs/*.log | tail -20
~~~

---

## Быстрые фиксы (sed)

~~~bash
# Изменить buy_amount во всех YAML
sed -i 's/buy_amount: [0-9.]*/buy_amount: 0.02/g' bots/*.yaml

# Изменить max_hold_time на 24 часа (86400 секунд)
sed -i 's/max_hold_time: [0-9]*/max_hold_time: 86400/g' bots/*.yaml
~~~

После правок:

~~~bash
bot-restart
~~~

---

## Алиасы (опционально)

~~~bash
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
~~~

---

## Helius Webhooks

Создание webhook (пример):

~~~bash
curl -X POST "https://api.helius.xyz/v0/webhooks?api-key=ВАШ_HELIUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "webhookURL": "http://ВАШ_IP:8000/webhook",
    "transactionTypes": ["SWAP"],
    "accountAddresses": [],
    "webhookType": "enhanced"
  }'
~~~

Тест webhook локально:

~~~bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '[{"type":"SWAP","signature":"test"}]'
~~~

---

## Disclaimer

Торговля криптовалютой связана с высоким риском. Начинайте с небольших сумм.
