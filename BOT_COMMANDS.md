# 🤖 Pump.Fun & Bonk Bot - Команды и Алиасы

## 🚀 Быстрый старт

```bash
# Добавить все алиасы в ~/.bashrc
cat >> ~/.bashrc << 'EOF'
# === PUMP BOT ALIASES ===
BOT_DIR="/opt/pumpfun-bonkfun-bot"

# Управление ботом
alias bot-start='sudo systemctl start pumpfun-bot'
alias bot-stop='sudo systemctl stop pumpfun-bot'
alias bot-restart='sudo systemctl restart pumpfun-bot'
alias bot-status='sudo systemctl status pumpfun-bot'

# Логи
alias bot-logs='sudo journalctl -u pumpfun-bot -f'
alias bot-logs-100='sudo journalctl -u pumpfun-bot -n 100'
alias bot-logs-today='sudo journalctl -u pumpfun-bot --since today'

# Статистика сделок
alias bot-buys='grep -h "Successfully bought" $BOT_DIR/logs/*.log | tail -20'
alias bot-sells='grep -h "Successfully sold" $BOT_DIR/logs/*.log | tail -20'
alias bot-wins='grep -h "Successfully" $BOT_DIR/logs/*.log | tail -20'
alias bot-count='grep -c "Successfully bought" $BOT_DIR/logs/*.log 2>/dev/null | awk -F: "{sum+=\$2} END {print \"Total buys:\", sum}"'
alias bot-count-sells='grep -c "Successfully sold" $BOT_DIR/logs/*.log 2>/dev/null | awk -F: "{sum+=\$2} END {print \"Total sells:\", sum}"'

# Pattern Detection
alias bot-patterns='grep -h "PATTERN\|PUMP SIGNAL" $BOT_DIR/logs/*.log | tail -20'
alias bot-whales='grep -h "WHALE" $BOT_DIR/logs/*.log | tail -20'
alias bot-signals='grep -h "🚀" $BOT_DIR/logs/*.log | tail -20'

# Ошибки
alias bot-errors='grep -h "ERROR\|FAILED" $BOT_DIR/logs/*.log | tail -20'
alias bot-warnings='grep -h "WARNING" $BOT_DIR/logs/*.log | tail -20'

# Конфиги
alias bot-config='cat $BOT_DIR/bots/*.yaml'
alias bot-edit-pump='nano $BOT_DIR/bots/bot-sniper-0-pump.yaml'
alias bot-edit-bonk='nano $BOT_DIR/bots/bot-sniper-0-bonkfun.yaml'

# Whale database
alias bot-whales-list='cat $BOT_DIR/smart_money_wallets.json | jq ".whales[].wallet"'
alias bot-whales-count='cat $BOT_DIR/smart_money_wallets.json | jq ".whales | length"'

# Trending scanner
alias bot-trending='grep -h "TRENDING" $BOT_DIR/logs/*.log | tail -20'
alias bot-trending-stats='grep -h "Daily budget\|API Budget" $BOT_DIR/logs/*.log | tail -10'
alias bot-rotated='grep -h "Rotated" $BOT_DIR/logs/*.log | tail -10'

# Whale copy trading
alias bot-whale-buys='grep -h "whale buy\|WHALE" $BOT_DIR/logs/*.log | tail -20'
alias bot-whale-skip='grep -h "Skipping whale" $BOT_DIR/logs/*.log | tail -10'

# Быстрые проверки
alias bot-balance='grep -h "SOL balance" $BOT_DIR/logs/*.log | tail -5'
alias bot-last-trade='grep -h "Successfully" $BOT_DIR/logs/*.log | tail -1'

# Git операции
alias bot-pull='cd $BOT_DIR && git pull origin main'
alias bot-diff='cd $BOT_DIR && git diff'
EOF

source ~/.bashrc
```

---

## 📋 Основные команды

### Управление ботом
| Команда | Описание |
|---------|----------|
| `bot-start` | Запустить бота |
| `bot-stop` | Остановить бота |
| `bot-restart` | Перезапустить бота |
| `bot-status` | Статус бота |

### Логи
| Команда | Описание |
|---------|----------|
| `bot-logs` | Live логи (follow) |
| `bot-logs-100` | Последние 100 строк |
| `bot-logs-today` | Логи за сегодня |

### Статистика
| Команда | Описание |
|---------|----------|
| `bot-buys` | Последние 20 покупок |
| `bot-sells` | Последние 20 продаж |
| `bot-wins` | Все успешные сделки |
| `bot-count` | Общее количество покупок |
| `bot-count-sells` | Общее количество продаж |

### Pattern Detection
| Команда | Описание |
|---------|----------|
| `bot-patterns` | Обнаруженные паттерны |
| `bot-whales` | Whale покупки |
| `bot-signals` | Pump сигналы (🚀) |

### Trending Scanner
| Команда | Описание |
|---------|----------|
| `bot-trending` | Найденные трендовые токены |
| `bot-trending-stats` | Статистика API бюджетов |
| `bot-rotated` | Ротация токенов |

### Whale Copy Trading
| Команда | Описание |
|---------|----------|
| `bot-whale-buys` | Скопированные whale покупки |
| `bot-whale-skip` | Пропущенные whale сигналы |

### Отладка
| Команда | Описание |
|---------|----------|
| `bot-errors` | Последние ошибки |
| `bot-warnings` | Последние предупреждения |

---

## ⚙️ Конфигурация Trending Scanner

Добавь в YAML конфиг бота:

```yaml
# Trending Scanner - мониторинг трендовых токенов
trending_scanner:
  enabled: true                    # Включить сканер
  min_volume_1h: 50000            # Минимум $50k объёма за час
  min_market_cap: 10000           # Минимум $10k маркеткап
  max_market_cap: 5000000         # Максимум $5M маркеткап
  min_price_change_5m: 5          # Минимум +5% за 5 минут
  min_price_change_1h: 20         # Минимум +20% за час
  min_buy_pressure: 0.65          # 65% покупок
  scan_interval: 30               # Сканировать каждые 30 сек
```

### Источники данных
| Источник | Лимит | Описание |
|----------|-------|----------|
| DexScreener | unlimited | Основной источник |
| Jupiter | 10k/day | Pump.fun токены |
| Birdeye | 1k/day | Требует API key |

---

## ⚙️ Конфигурация Pattern Detection

Добавь в YAML конфиг бота:

```yaml
# Pattern Detection - отслеживание паттернов перед пампами
pattern_detection:
  enabled: true                    # Включить детектор паттернов
  volume_spike_threshold: 3.0      # Объём вырос в 3x = сигнал
  holder_growth_threshold: 0.5     # Холдеры +50% за минуту = сигнал
  min_whale_buys: 2                # Минимум 2 whale покупки за 30 сек
  min_patterns_to_buy: 2           # Минимум паттернов для сигнала
  pattern_only_mode: false         # true = покупать ТОЛЬКО при паттернах
```

### Типы паттернов

| Паттерн | Описание |
|---------|----------|
| `VOLUME_SPIKE` | Объём торговли вырос в 3x+ от среднего |
| `HOLDER_GROWTH` | Количество холдеров выросло на 50%+ за минуту |
| `ACCUMULATION` | Цена растёт на малом объёме (накопление) |
| `WHALE_CLUSTER` | 2+ whale покупки за 30 секунд |
| `CURVE_ACCELERATION` | Bonding curve прыгнула на 5%+ |

---

## 🐋 Whale Database

### Добавить whale вручную

```bash
# Редактировать файл
nano /opt/pumpfun-bonkfun-bot/smart_money_wallets.json

# Добавить в массив "whales":
{
  "wallet": "WALLET_ADDRESS_HERE",
  "win_rate": 0.75,
  "trades_count": 0,
  "label": "whale",
  "source": "manual",
  "added_date": "2026-01-14T00:00:00Z"
}
```

### Проверить whale'ов

```bash
# Список всех whale адресов
cat /opt/pumpfun-bonkfun-bot/smart_money_wallets.json | jq '.whales[].wallet'

# Количество whale'ов
cat /opt/pumpfun-bonkfun-bot/smart_money_wallets.json | jq '.whales | length'
```

---

## 📊 Полезные grep команды

```bash
# Найти все сделки по токену
grep "TOKEN_SYMBOL" /opt/pumpfun-bonkfun-bot/logs/*.log

# Найти транзакцию по сигнатуре
grep "TX_SIGNATURE" /opt/pumpfun-bonkfun-bot/logs/*.log

# PnL по позициям
grep "Position PnL" /opt/pumpfun-bonkfun-bot/logs/*.log | tail -20

# Take Profit срабатывания
grep "TAKE_PROFIT" /opt/pumpfun-bonkfun-bot/logs/*.log

# Stop Loss срабатывания
grep "STOP_LOSS" /opt/pumpfun-bonkfun-bot/logs/*.log

# Moon bag продажи
grep "moon bag" /opt/pumpfun-bonkfun-bot/logs/*.log

# Trending токены
grep "TRENDING" /opt/pumpfun-bonkfun-bot/logs/*.log | tail -20

# API бюджет статус
grep "Daily budget" /opt/pumpfun-bonkfun-bot/logs/*.log | tail -5

# Whale copy trades
grep "whale buy" /opt/pumpfun-bonkfun-bot/logs/*.log | tail -20

# Ротация токенов
grep "Rotated" /opt/pumpfun-bonkfun-bot/logs/*.log | tail -10
```

---

## 🔧 Быстрые фиксы

### Ошибка "Transaction exceeded max loaded accounts data size cap"
```bash
# Убрать account_data_size из конфига
sed -i 's/account_data_size:/#account_data_size:/g' /opt/pumpfun-bonkfun-bot/bots/*.yaml
bot-restart
```

### Изменить max_hold_time на 24 часа
```bash
sed -i 's/max_hold_time: [0-9]*/max_hold_time: 86400/g' /opt/pumpfun-bonkfun-bot/bots/*.yaml
bot-restart
```

### Изменить buy_amount
```bash
sed -i 's/buy_amount: [0-9.]*/buy_amount: 0.02/g' /opt/pumpfun-bonkfun-bot/bots/*.yaml
bot-restart
```

---

## 📁 Структура проекта

```
/opt/pumpfun-bonkfun-bot/
├── bots/                    # YAML конфиги ботов
│   ├── bot-sniper-0-pump.yaml
│   └── bot-sniper-0-bonkfun.yaml
├── logs/                    # Логи ботов
├── trades/                  # Информация о сделках
├── smart_money_wallets.json # База whale'ов
├── src/
│   ├── trading/
│   │   ├── universal_trader.py    # Главная логика
│   │   └── platform_aware.py      # Buy/Sell операции
│   ├── monitoring/
│   │   ├── pump_pattern_detector.py  # Детектор паттернов
│   │   └── smart_money_detector.py   # Whale tracking
│   └── core/
│       └── client.py              # RPC клиент
└── learning-examples/       # Тестовые скрипты
```

---

## 🧪 Тестирование

```bash
# Тест pattern detector
cd /opt/pumpfun-bonkfun-bot
uv run learning-examples/test_pump_patterns.py

# Тест manual buy (осторожно - реальные деньги!)
uv run learning-examples/manual_buy.py

# Тест fetch price
uv run learning-examples/fetch_price.py
```

---

## 🔄 Git операции

```bash
# Обновить код с GitHub
cd /opt/pumpfun-bonkfun-bot
git pull origin main
bot-restart

# Посмотреть изменения
git diff

# Откатить изменения
git checkout -- .
```
