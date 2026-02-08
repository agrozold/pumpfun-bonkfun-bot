# Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.

## Возможности

- Whale Copy Trading — отслеживание китов через Helius webhooks
- Stop Loss / TSL / Take Profit — автоматическое управление позициями
- NO_SL — защита отдельных токенов от продажи по стоп-лоссу
- DCA — усреднение при просадке
- Moonbag — сохранение части позиции после TSL
- Redis — быстрая синхронизация позиций
- Поддержка DEX — Pump.fun, PumpSwap, Jupiter, Raydium
- Dust cleanup — автоочистка мусорных токенов с возвратом ренты

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

~~~bash
cp smart_money_wallets.example.json smart_money_wallets.json
nano smart_money_wallets.json
~~~

Формат:

~~~json
{
  "whales": [
    { "wallet": "АДРЕС_1", "label": "whale-1" },
    { "wallet": "АДРЕС_2", "label": "whale-2" }
  ]
}
~~~

---

## Команды

### 🤖 Управление ботом

| Команда | Описание |
|---------|----------|
| `bot-start` | Запуск бота |
| `bot-stop` | Остановка бота |
| `bot-restart` | Перезапуск бота |
| `bot-status` | Статус + webhook stats |
| `bot-health` | Проверка здоровья (webhook, redis, позиции) |
| `bot-config` | Открыть конфиг (nano) |
| `bot-edit` | Редактировать конфиг |

### 📜 Логи

| Команда | Описание |
|---------|----------|
| `bot-logs` | Логи live (Ctrl+C выход) |
| `bot-trades` | Последние покупки/продажи |
| `bot-whales` | Сигналы китов |
| `bot-errors` | Ошибки |

### 💰 Торговля

| Команда | Описание |
|---------|----------|
| `buy <TOKEN> <SOL>` | Покупка токена |
| `sell <TOKEN> <PERCENT>` | Продажа по проценту |
| `sell10 <TOKEN>` ... `sell100 <TOKEN>` | Быстрая продажа (10%-100%) |
| `wsync` | Синхронизация кошелька с ботом |

### 🐋 Управление китами

| Команда | Описание |
|---------|----------|
| `whale add <ADDRESS> [label]` | Добавить кита + sync webhook |
| `whale del <ADDRESS\|LABEL>` | Удалить кита + sync webhook |
| `whale list` | Список всех китов |
| `whale list insider` | Поиск китов по слову |
| `whale info <ADDRESS\|LABEL>` | Подробности о ките |
| `whale sync` | Принудительный sync webhook |
| `whale <MINT>` | Найти кита по mint адресу токена |
| `whale <SYMBOL>` | Найти кита по символу (SOBAT, Chud...) |

### 🗑️ Очистка мусорных токенов

| Команда | Описание |
|---------|----------|
| `dust` | Сжечь всё < $0.40 (дефолт) |
| `dust 0.5` | Сжечь всё < $0.50 |
| `dust-dry` | Показать что удалится (без удаления) |
| `dust 0.3 --dry` | Показать что < $0.30 (без удаления) |

Скрипт `dust` сканирует ВСЕ токены (SPL + Token2022), защищает позиции бота и NO_SL токены, сжигает мусор и возвращает ~0.002 SOL ренты за каждый закрытый аккаунт.

### 🛡️ NO_SL — защита токенов от стоп-лосса

| Команда | Описание |
|---------|----------|
| `no-sl list` | Показать токены без SL |
| `no-sl add <MINT>` | Добавить токен в исключения |
| `no-sl remove <MINT>` | Удалить токен из исключений |

Токены в NO_SL списке **никогда** не будут проданы по стоп-лоссу — ни по обычному SL, ни по hard SL, ни по emergency SL при крашах или потере цены. Только TP и ручная продажа.

---

## Настройки TSL (Trailing Stop Loss)

~~~yaml
tsl_activation_pct: 0.2   # Активация при +20%
tsl_trail_pct: 0.5         # Трейлинг 50%
tsl_sell_pct: 0.9          # Продаёт 90%
moon_bag_percentage: 10    # Оставляет 10%
stop_loss: 20%
take_profit: 10000%
~~~

---

## Алиасы

Добавь в `~/.bashrc`:

~~~bash
# === WHALE BOT ===
BOT_DIR="/opt/pumpfun-bonkfun-bot"

# Управление ботом
alias bot-start='cd $BOT_DIR && ./start.sh'
alias bot-stop='cd $BOT_DIR && ./stop.sh'
alias bot-restart='bot-stop && sleep 3 && bot-start'
alias bot-status='ps aux | grep bot_runner | grep -v grep'
alias bot-logs='tail -f $BOT_DIR/logs/bot-whale-copy.log'
alias bot-errors='grep -h "ERROR\|FAILED" $BOT_DIR/logs/*.log | tail -30'
alias bot-health='curl -s http://localhost:8000/health 2>/dev/null | jq || echo "Бот не запущен"'
alias bot-config='nano $BOT_DIR/bots/bot-whale-copy.yaml'

# Синхронизация
alias wsync='cd $BOT_DIR && source venv/bin/activate && python3 wsync.py'

# Очистка мусора
alias dust='cd $BOT_DIR && source venv/bin/activate && python3 cleanup_dust.py'
alias dust-dry='cd $BOT_DIR && source venv/bin/activate && python3 cleanup_dust.py 0.4 --dry'

# Управление китами
whale() {
    cd $BOT_DIR && source venv/bin/activate && python3 whale_manage.py "$@"
}
~~~

Применить: `source ~/.bashrc`

---

## Helius Webhooks

Webhook создаётся автоматически при первом запуске. Адреса китов синхронизируются из `smart_money_wallets.json` при каждом старте бота и при использовании `whale add/del`.

Ручная проверка:

~~~bash
# Здоровье webhook сервера
curl -s http://localhost:8000/health | jq

# Тест webhook
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '[{"type":"SWAP","signature":"test"}]'
~~~

---

## Структура проекта

~~~
├── bots/                       # Конфиги ботов (YAML)
│   └── bot-whale-copy.yaml
├── src/
│   ├── bot_runner.py           # Главный запуск
│   ├── monitoring/
│   │   ├── whale_webhook.py    # Helius webhook сервер
│   │   └── whale_tracker.py    # Трекинг позиций
│   ├── trading/
│   │   ├── universal_trader.py # Торговая логика + NO_SL
│   │   └── position.py         # Управление позициями
│   └── utils/
│       └── helius_webhook_sync.py  # Синхронизация webhook
├── cleanup_dust.py             # Очистка мусорных токенов
├── find_whale.py               # Поиск кита по токену
├── whale_manage.py             # Управление списком китов
├── wsync.py                    # Синхронизация кошелька
├── smart_money_wallets.example.json  # Шаблон списка китов
├── .env.example                # Шаблон переменных окружения
└── positions.json              # Текущие позиции (auto)
~~~

---

## Disclaimer

Торговля криптовалютой связана с высоким риском. Начинайте с небольших сумм.
