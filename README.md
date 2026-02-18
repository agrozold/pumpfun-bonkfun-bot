# Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.
Полный pipeline от обнаружения TX кита до отправки нашей покупки — **~155ms**.

---

## Возможности

- **Whale Copy Trading** — отслеживание нескольких кошельков китов через три канала (Dual gRPC + Helius Webhook) с дедупликацией сигналов
- **Dual gRPC Yellowstone** — два параллельных gRPC канала, первый сигнал выигрывает
- **Ultra-low latency** — локальный парсинг TX из protobuf за ~2ms (вместо ~650ms через Helius API)
- **Async pipeline** — DexScreener и deployer check вынесены в фон, не блокируют покупку
- **Параллельная отправка TX** — Jito + RPC одновременно, первый успешный ответ побеждает
- **Real-time цены** — gRPC подписки на bonding curve accounts для мгновенного SL/TP (<5ms)
- **Dynamic Stop Loss** — адаптивный SL в зависимости от возраста позиции (защита от импакт-дипа)
- **Trailing Stop Loss** — активируется при заданном профите, трейлит от максимума
- **Take Profit** — частичная продажа при TP, остаток на TSL
- **HARD SL / EMERGENCY SL** — аварийные стоп-лоссы
- **INSTANT позиции** — позиция создаётся ДО подтверждения TX (с guard-барьерами)
- **Reactive SL/TP** — gRPC price stream тригерит продажу за <5ms (не ждёт polling interval)
- **Balance RPC chain** — несколько RPC провайдеров с fallback
- **Deployer blacklist** — автоматическая блокировка токенов от scam-deployers
- **DCA** — усреднение при просадке или росте
- **Dual-channel watchdog** — мониторинг здоровья gRPC и Webhook каналов
- **Token scoring** — фильтрация токенов по объёму, давлению покупок, моментуму, ликвидности
- **Поддержка DEX** — Pump.fun, PumpSwap, Jupiter, Raydium, Orca, Meteora
- **Dust cleanup** — автоочистка мусорных токенов с возвратом ренты

---

## Замеры скорости (production)

| Этап | До оптимизации | После оптимизации |
|------|---------------|-------------------|
| Парсинг TX | ~650ms (Helius API) | **~2ms** (локальный protobuf) |
| DexScreener symbol | ~200ms (blocking) | **0ms** (async background) |
| Deployer check | ~265ms (blocking RPC) | **0ms** (async background) |
| Scoring + Quote | ~200ms (sequential) | **~170ms** (parallel) |
| Отправка TX | ~500ms (Jito only) | **~350ms** (Jito + RPC parallel) |
| **Полный pipeline** | **~2850ms** | **~155ms** |
| Стабильность gRPC | RST каждые 20-40мин | **0 disconnects** |
| SL/TP реакция | 1-3 сек (polling) | **<5ms** (gRPC stream) |
| Каналы приёма | 1 | **3** (Dual gRPC + Webhook) |

---

## Как работает pipeline

1. **Кит покупает токен** — TX попадает в Solana
2. **Dual gRPC ловит TX** — оба канала параллельно, первый выигрывает (~1-2ms latency)
3. **Локальный парсер** — декодирует swap из protobuf за ~2ms
4. **INSTANT покупка** — Jupiter swap TX отправляется через Jito (~150ms)
5. **Фоновые задачи** — DexScreener symbol, deployer check, TX confirmation — параллельно
6. **Позиция активна** — gRPC price stream мониторит SL/TP/TSL в реальном времени
7. **Webhook** — дублирующий канал, ловит тот же TX через ~6с (SignalDedup отсекает)

---

## Три канала приёма сигналов

| Канал | Тип | Latency | Роль |
|-------|-----|---------|------|
| gRPC Yellowstone #1 | gRPC stream | ~1-2ms | PRIMARY |
| gRPC Yellowstone #2 | gRPC stream | ~1-2ms | SECONDARY |
| Helius Webhook | HTTP POST | ~6-9s | BACKUP |

Оба gRPC канала работают параллельно. Signal dedup через shared set — первый канал выигрывает. Webhook — страховка на случай gRPC disconnects.

---

## Управление позициями

### Dynamic Stop Loss (защита от импакт-дипа)

При копировании крупной покупки кита цена часто проседает на 15-30% в первые секунды (price impact). Обычный SL тут же продаёт в убыток, хотя цена восстановится. Dynamic SL адаптирует пороги в зависимости от возраста позиции.

### Reactive SL/TP

gRPC подписка на bonding curve account даёт обновления цены в реальном времени (<5ms). При достижении SL/TP/TSL порога — мгновенная продажа без ожидания polling interval.

### INSTANT позиции

Позиция создаётся СРАЗУ после отправки BUY TX (до подтверждения). Guard-барьеры (buy_confirmed, tokens_arrived) защищают от продажи до получения токенов.

---

## API ключи

Бот использует несколько внешних сервисов. Все ключи хранятся в `.env`.

### 1. Solana RPC (несколько провайдеров)

Используются для проверки баланса, подтверждения TX и fallback. Несколько провайдеров с автоматическим переключением.

| Переменная | Описание |
|-----------|----------|
| `CHAINSTACK_RPC_ENDPOINT` | Основной RPC |
| `DRPC_RPC_ENDPOINT` | Fallback RPC |
| `ALCHEMY_RPC_ENDPOINT` | Fallback RPC |
| `SOLANA_PUBLIC_RPC_ENDPOINT` | Бесплатный fallback |

### 2. Helius

Webhook приём + fallback парсинг TX.

| Переменная | Описание |
|-----------|----------|
| `HELIUS_API_KEY` | Webhook + Enhanced TX API |

Сайт: https://helius.dev

### 3. gRPC Yellowstone (Dual)

Ultra-fast приём TX + real-time цены.

| Переменная | Описание |
|-----------|----------|
| `GEYSER_ENDPOINT` | gRPC endpoint #1 |
| `GEYSER_API_KEY` | Auth token #1 |
| `CHAINSTACK_GEYSER_ENDPOINT` | gRPC endpoint #2 |
| `CHAINSTACK_GEYSER_TOKEN` | Auth token #2 |

Бесплатный gRPC: https://www.publicnode.com — Products > Solana > gRPC (Yellowstone)

### 4. Jupiter

Покупка/продажа токенов.

| Переменная | Описание |
|-----------|----------|
| `JUPITER_API_KEY` | Price API |
| `JUPITER_TRADE_API_KEY` | Quote + Swap API |

Документация: https://station.jup.ag/docs

### 5. Jito

Отправка TX через Jito block engine.

| Переменная | Описание |
|-----------|----------|
| `JITO_ENABLED` | true / false |
| `JITO_TIP_LAMPORTS` | Tip в lamports |
| `JITO_BLOCK_ENGINE_URL` | URL block engine |

Доступные регионы: frankfurt (EU), ny (US East), tokyo (Asia). Выбирать ближайший к VPS.

---

## Установка

### 1. Зависимости (Ubuntu 20.04+)

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

### 4. Конфигурация

    cp .env.example .env
    nano .env                    # API ключи

    cp bots/bot-whale-copy.example.yaml bots/bot-whale-copy.yaml
    nano bots/bot-whale-copy.yaml  # Настройки торговли

    cp smart_money_wallets.example.json smart_money_wallets.json
    nano smart_money_wallets.json  # Кошельки китов

Формат smart_money_wallets.json:

    {
      "whales": [
        { "wallet": "АДРЕС_КОШЕЛЬКА_1", "label": "whale-1" },
        { "wallet": "АДРЕС_КОШЕЛЬКА_2", "label": "smart-money-2" }
      ]
    }

### 5. Systemd сервис

    sudo nano /etc/systemd/system/whale-bot.service

Содержимое:

    [Unit]
    Description=Whale Copy Trading Bot
    After=network.target redis.service
    Wants=redis.service

    [Service]
    Type=simple
    User=root
    WorkingDirectory=/opt/pumpfun-bonkfun-bot
    Environment=PATH=/opt/pumpfun-bonkfun-bot/venv/bin:/usr/local/bin:/usr/bin:/bin
    EnvironmentFile=/opt/pumpfun-bonkfun-bot/.env
    ExecStart=/opt/pumpfun-bonkfun-bot/venv/bin/python3 src/bot_runner.py bots/bot-whale-copy.yaml
    Restart=on-failure
    RestartSec=10
    KillMode=control-group
    KillSignal=SIGTERM
    TimeoutStopSec=15
    StandardOutput=append:/opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log
    StandardError=append:/opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log

    [Install]
    WantedBy=multi-user.target

Запуск:

    sudo systemctl daemon-reload
    sudo systemctl enable whale-bot
    sudo systemctl start whale-bot

---

## CLI-алиасы

Файл `aliases.sh` содержит удобные команды. Подключение:

    echo 'source /opt/pumpfun-bonkfun-bot/aliases.sh' >> ~/.bashrc
    source ~/.bashrc

### Управление ботом

| Команда | Описание |
|---------|----------|
| `bot-start` | Запуск бота |
| `bot-stop` | Остановка бота |
| `bot-restart` | Перезапуск |
| `bot-status` | Статус сервиса |
| `bot-health` | Проверка webhook |

### Мониторинг

| Команда | Описание |
|---------|----------|
| `bot-logs` | Live логи (Ctrl+C для выхода) |
| `bot-trades` | Последние сделки |
| `bot-whales` | Активность китов |
| `bot-errors` | Ошибки |

### Ручная торговля

| Команда | Описание |
|---------|----------|
| `buy TOKEN SOL_AMOUNT` | Купить токен |
| `sell TOKEN PERCENT` | Продать % токена |
| `sell10 TOKEN` ... `sell100 TOKEN` | Быстрые продажи (10%-100%) |

### Утилиты

| Команда | Описание |
|---------|----------|
| `dust` | Очистка мелких токенов |
| `dust-dry` | Предпросмотр очистки |
| `no-sl list` | Список токенов без SL |
| `no-sl add MINT` | Отключить SL для токена |
| `wsync` | Синхронизация кошелька |

---

## Архитектура

    pumpfun-bonkfun-bot/
    |-- bots/
    |   |-- bot-whale-copy.yaml          # Конфиг (не в git)
    |   |-- bot-whale-copy.example.yaml  # Пример конфига
    |-- src/
    |   |-- bot_runner.py                # Точка входа
    |   |-- monitoring/
    |   |   |-- whale_geyser.py          # Dual gRPC receiver + reactive SL/TP
    |   |   |-- whale_webhook.py         # Webhook receiver (backup канал)
    |   |   |-- local_tx_parser.py       # Локальный парсер TX из protobuf
    |   |   |-- signal_dedup.py          # Дедупликация сигналов
    |   |   |-- watchdog.py              # Мониторинг каналов
    |   |-- trading/
    |   |   |-- universal_trader.py      # Главный торговый модуль
    |   |   |-- position.py             # Позиция (dataclass + persistence)
    |   |   |-- fallback_seller.py       # Buy/Sell через Jupiter
    |   |   |-- deployer_blacklist.py    # Блокировка scam-deployers
    |   |-- core/
    |   |   |-- client.py                # SolanaClient (TX build/send/confirm)
    |   |   |-- tx_verifier.py           # Фоновая проверка TX
    |   |   |-- tx_callbacks.py          # Callbacks после подтверждения TX
    |   |-- utils/
    |       |-- batch_price_service.py   # Jupiter Price API polling
    |       |-- token_math.py            # Утилиты для работы с балансами
    |-- blacklisted_deployers.json       # Blacklist scam-deployers
    |-- smart_money_wallets.json         # Кошельки китов (не в git)
    |-- positions.json                   # Активные позиции (auto)
    |-- .env                             # API ключи (не в git)
    |-- .env.example                     # Пример .env

---

## Troubleshooting

**Бот не запускается:** Проверить `bot-status`. Проверить `.env` и наличие redis.

**LOW BALANCE:** Пополнить кошелёк. При балансе < min_sol_balance бот не покупает.

**Нет сигналов от китов:** Проверить `bot-whales`. Если "gRPC silent" — проверить gRPC токены в `.env`. Если "Webhook silent" — проверить webhook URL в Helius.

**Конфликт сервисов:** Убедиться что запущен только один инстанс бота.

---

## Disclaimer

Бот предназначен для образовательных целей. Торговля криптовалютами сопряжена с высоким риском. Используйте на свой страх и риск.
