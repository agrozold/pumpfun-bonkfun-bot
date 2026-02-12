# Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.
Полный pipeline от обнаружения TX кита до отправки нашей покупки — **~0.5 секунды**.

---

## Возможности

- **Whale Copy Trading** — отслеживание 16+ кошельков китов через два канала одновременно (gRPC + Helius Webhook) с дедупликацией сигналов
- **Ultra-low latency** — локальный парсинг TX из protobuf за ~2ms (вместо ~650ms через Helius API)
- **Параллельная отправка TX** — Jito + RPC одновременно, первый успешный ответ побеждает
- **Параллельный scoring + quote** — оценка токена и запрос Jupiter Quote запускаются одновременно
- **Real-time цены** — gRPC подписки на vault-аккаунты пулов для мгновенного SL/TP
- **Stop Loss / TSL / Take Profit** — автоматическое управление позициями
- **DCA** — усреднение при просадке или росте, максимум 2 покупки
- **Dual-channel watchdog** — мониторинг здоровья gRPC и Webhook каналов
- **Token scoring** — фильтрация мусорных токенов по объёму, давлению покупок, моментуму, ликвидности
- **Поддержка DEX** — Pump.fun, PumpSwap, Jupiter, Raydium, Orca, Meteora
- **Dust cleanup** — автоочистка мусорных токенов с возвратом ренты

---

## Замеры скорости (production)

| Этап | До оптимизации | После оптимизации |
|------|---------------|-------------------|
| Парсинг TX | ~650ms (Helius API) | **~2ms** (локальный protobuf) |
| Scoring + Quote | ~200ms (sequential) | **~170ms** (parallel) |
| Отправка TX | ~500ms (Jito only) | **~350ms** (Jito + RPC parallel) |
| **Полный pipeline** | **~2850ms** | **~528ms** |
| Стабильность gRPC | RST каждые 20-40мин | **0 disconnects** |
| SL/TP реакция | 1-3 сек (polling) | **~300-500ms** (gRPC stream) |
| Каналы приёма | 1 | **2** (gRPC + Webhook) |

---

## Как работает pipeline

1. **Кит покупает токен** — TX попадает в Solana (~700ms на распространение)
2. **gRPC ловит TX** — локальный парсер декодирует swap за ~2ms (Phase 1)
3. **Webhook ловит тот же TX** — ~9 сек позже, SignalDedup отсекает дубликат (Phase 2)
4. **Параллельно запускаются** scoring (DexScreener ~100ms) + Jupiter Quote (~100ms) = ~170ms (Phase 3.3)
5. **TX отправляется** через Jito + RPC одновременно = ~350ms (Phase 3.1)
6. **Итого: ~528ms** от сигнала до отправки нашей TX

---

## Необходимые API ключи

Все бесплатные тарифы достаточны для работы бота.

### 1. Solana RPC (обязательно хотя бы один)

Нужен для отправки транзакций, проверки балансов, получения данных аккаунтов.

| Провайдер | Переменная в .env | Бесплатный план | Где взять |
|-----------|------------------|-----------------|-----------|
| **Chainstack** | `CHAINSTACK_RPC_ENDPOINT` | 3M запросов/мес | https://chainstack.com |
| **Alchemy** | `ALCHEMY_RPC_ENDPOINT` | 300M CU/мес | https://alchemy.com |
| **dRPC** | `DRPC_RPC_ENDPOINT` | Бесплатно | https://drpc.org |

Достаточно одного, но лучше два-три для fallback. Бот автоматически переключается при ошибках.

### 2. Helius (обязательно)

Для webhook подписки на транзакции китов + fallback парсинг TX.

| Переменная | Назначение |
|-----------|-----------|
| `HELIUS_API_KEY` | Webhook подписка на кошельки китов |
| `GEYSER_PARSE_API_KEY` | Fallback парсинг TX (если локальный парсер не справился) |

**Бесплатно:** 1M кредитов/мес (бот расходует ~10-50K/мес)

**Где взять:** https://helius.dev

Можно использовать один ключ для обоих переменных.

### 3. gRPC Yellowstone (обязательно)

Для ultra-fast отслеживания транзакций китов + real-time цены.

| Переменная | Назначение |
|-----------|-----------|
| `GEYSER_ENDPOINT` | gRPC сервер (по умолчанию PublicNode) |
| `GEYSER_API_KEY` | Токен авторизации |

**Бесплатно:** Без лимитов

**Где взять:** https://www.publicnode.com — Products — Solana — gRPC (Yellowstone)

#### Как получить gRPC токен (бесплатно)

1. Зайти на [solana.publicnode.com](https://solana.publicnode.com/?yellowstone) → вкладка **Yellowstone GRPC** → кнопка **"Get token →"**
2. Перекинет на [allnodes.com/portfolio](https://www.allnodes.com/portfolio) — зарегистрироваться (бесплатно) → нажать **GET TOKEN** (можно до 5 штук)
3. Скопировать токен, вставить в `.env`:

GEYSER_ENDPOINT=solana-yellowstone-grpc.publicnode.com:443 GEYSER_API_KEY=ваш_токен_с_allnodes


> Токен даёт swQoS (Staked Weighted Quality of Service) — приоритетный и стабильный доступ к gRPC.


### 4. Jupiter (обязательно)

Для свопов (покупка/продажа токенов) и мониторинга цен.

| Переменная | Назначение |
|-----------|-----------|
| `JUPITER_API_KEY` | Мониторинг цен (Price API) |
| `JUPITER_TRADE_API_KEY` | Свопы (Quote + Swap API) |

**Бесплатно:** 60 запросов/мин на каждый bucket

**Где взять:** https://station.jup.ag/docs

### 5. Jito (рекомендуется)

Для приоритетной отправки TX через Jito block engine.

| Переменная | Значение по умолчанию |
|-----------|----------------------|
| `JITO_ENABLED` | `true` |
| `JITO_TIP_LAMPORTS` | `500000` (0.0005 SOL) |
| `JITO_BLOCK_ENGINE_URL` | `https://frankfurt.mainnet.block-engine.jito.wtf` |

**Не требует API ключа.** Tip оплачивается из баланса кошелька.

Регионы: `frankfurt` (Европа), `ny` (US East), `tokyo` (Азия). Выберите ближайший к VPS.

---

## Установка

### 1. Подготовка сервера (Ubuntu 20.04+)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3.10 python3.10-venv python3-pip redis-server git -y
sudo systemctl enable redis-server && sudo systemctl start redis-server
```

### 2. Клонирование

```bash
cd /opt
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot
```

### 3. Виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Настройка .env

```bash
cp .env.example .env
nano .env
```

Заполните все API ключи (см. раздел "Необходимые API ключи" выше).

### 5. Конфиг бота

```bash
cp bots/bot-whale-copy.example.yaml bots/bot-whale-copy.yaml
nano bots/bot-whale-copy.yaml
```

### 6. База китов

```bash
cp smart_money_wallets.example.json smart_money_wallets.json
nano smart_money_wallets.json
```

Формат:

```json
{
  "whales": [
    { "wallet": "АДРЕС_КОШЕЛЬКА_1", "label": "whale-1" },
    { "wallet": "АДРЕС_КОШЕЛЬКА_2", "label": "smart-money-2" }
  ]
}
```

### 7. Systemd сервис

```bash
sudo nano /etc/systemd/system/whale-bot.service
```

Содержимое:

```ini
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
ExecStartPre=/bin/bash -c 'rm -f /tmp/whale-bot.pid'
ExecStart=/opt/pumpfun-bonkfun-bot/venv/bin/python3 src/bot_runner.py bots/bot-whale-copy.yaml
ExecStopPost=/bin/bash -c 'rm -f /tmp/whale-bot.pid'
Restart=on-failure
RestartSec=10
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=15
StandardOutput=append:/opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log
StandardError=append:/opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable whale-bot
sudo systemctl start whale-bot
```

---

## CLI-команды (aliases)

Бот включает файл `aliases.sh` с удобными короткими командами для терминала.

### Установка алиасов

```bash
echo 'source /opt/pumpfun-bonkfun-bot/aliases.sh' >> ~/.bashrc
source ~/.bashrc
```

После этого все команды ниже доступны из любой директории.

### Управление ботом

| Команда | Описание |
|---------|----------|
| `bot-start` | Запуск бота |
| `bot-stop` | Остановка бота |
| `bot-restart` | Перезапуск (убивает старый процесс, без дублей) |
| `bot-status` | Статус systemd сервиса |
| `bot-health` | Проверка webhook сервера |
| `bot-mode` | Показать текущий режим (gRPC+Webhook или Webhook-only) |
| `bot-webhook` | Переключить на Webhook-only |
| `bot-geyser` | Переключить на gRPC+Webhook |

### Команды

### Управление ботом

| Команда | Описание |
|---------|----------|
| `bot-start` | Запуск бота |
| `bot-stop` | Остановка бота |
| `bot-restart` | Перезапуск (убивает старый процесс, запускает новый, без дублей) |
| `bot-status` | Статус systemd сервиса |
| `bot-health` | Проверка webhook сервера |

### Логи

| Команда | Описание |
|---------|----------|
| `bot-logs` | Live логи (Ctrl+C выход) |
| `bot-trades` | Последние покупки/продажи |
| `bot-whales` | Сигналы китов |
| `bot-errors` | Последние ошибки |
| `bot-watchdog` | Статус watchdog (здоровье каналов) |

### Торговля

| Команда | Описание |
|---------|----------|
| `buy TOKEN SOL_AMOUNT` | Ручная покупка токена |
| `sell TOKEN PERCENT` | Продажа по проценту |
| `sell10 TOKEN` ... `sell100 TOKEN` | Быстрая продажа (10%-100%) |

### Утилиты

| Команда | Описание |
|---------|----------|
| `dust` | Сжечь мусорные токены < $0.40 (возврат ренты ~0.002 SOL каждый) |
| `dust-dry` | Показать что удалится (без удаления) |
| `no-sl list` | Показать токены без стоп-лосса |
| `no-sl add MINT` | Добавить токен в исключения SL |
| `wsync` | Синхронизация кошелька с ботом |

---

## Оптимизации (технические детали)

Все оптимизации реализованы и работают в production. Каждая фаза описана с коммитом для отката.

### Phase 1: Локальный парсер TX (коммит `d3167bd`)

**Файл:** `src/monitoring/local_tx_parser.py` (578 строк)

Полный локальный парсер свопов из gRPC protobuf данных. Два метода парсинга: Pump.fun discriminator (первые 8 байт instruction data) и Universal balance diff (для любого DEX). Распознаёт 11 DEX по program ID. Blacklist из 54 токенов (стейблкоины, LST, wrapped активы). Экономия: ~649ms на каждой транзакции.

### Phase 5.1: Bidirectional gRPC Keepalive (коммит `d3167bd`)

Клиент отправляет ping каждые 10 секунд, сервер отвечает pong. RST_STREAM обрабатывается fast reconnect (0.5s). Результат: 0 disconnects вместо разрыва каждые 20-40 минут.

### Phase 3.1: Параллельная отправка Jito + RPC (коммит `fdcf2c5`)

Обе отправки запускаются через `asyncio.create_task`, первый успех побеждает. Одна signature — Solana гарантирует идемпотентность. Экономия: ~200-400ms.

### Phase 2: Параллельный gRPC + Webhook с дедупликацией (коммит `9f0081f`)

**Файл:** `src/monitoring/signal_dedup.py` (63 строки)

Оба канала работают одновременно. Первый поймавший TX побеждает, второй отсекается. Три уровня защиты от дублей: SignalDedup (по signature), buying/bought tokens (по mint), Redis dedup (межпроцессный).

### Phase 5.3: Dual-Channel Watchdog (коммит `51639ca`)

**Файл:** `src/monitoring/watchdog.py` (~115 строк)

Мониторинг здоровья gRPC и Webhook каналов. Если оба молчат > 5 минут — ERROR. Если один молчит — WARNING.

### Phase 4: Real-time Price Stream через gRPC (коммит `f65c2fc`)

**Файл:** `src/monitoring/price_stream.py` (525 строк)

Второе gRPC соединение для подписки на vault-аккаунты пулов. При любом свопе в пуле — мгновенное обновление цены. Для PumpSwap позиций — vault data пробрасывается автоматически. Для Jupiter — fallback на Jupiter Price API polling. Экономия: SL/TP реакция ~300-500ms вместо 1-3 секунд.

### Phase 3.3: Pre-fetch Jupiter Quote (коммит `f65c2fc`)

Scoring и Jupiter Quote запускаются параллельно. Если scoring отклонит — quote отменяется. Если пройдёт — quote уже готов. Экономия: ~30-300ms (зависит от нагрузки API).

---

## Структура проекта

```
pumpfun-bonkfun-bot/
|-- bots/
|   |-- bot-whale-copy.yaml          # Конфиг бота (ваш, не в git)
|   |-- bot-whale-copy.example.yaml  # Шаблон конфига
|-- src/
|   |-- bot_runner.py                # Главный запуск
|   |-- monitoring/
|   |   |-- whale_geyser.py          # gRPC receiver (ловит TX китов)
|   |   |-- whale_webhook.py         # Webhook receiver (backup канал)
|   |   |-- local_tx_parser.py       # Локальный парсер свопов из protobuf
|   |   |-- signal_dedup.py          # Дедупликатор сигналов
|   |   |-- watchdog.py              # Мониторинг здоровья каналов
|   |   |-- price_stream.py          # Real-time цены через gRPC
|   |-- trading/
|   |   |-- universal_trader.py      # Главная торговая логика
|   |   |-- position.py              # Позиции (dataclass + persistence)
|   |   |-- fallback_seller.py       # Buy/Sell через PumpSwap/Jupiter
|   |   |-- platform_aware.py        # Buy/Sell через bonding curve
|   |-- core/
|   |   |-- client.py                # SolanaClient (TX build/send/confirm)
|   |   |-- tx_verifier.py           # Верификация TX после отправки
|   |   |-- tx_callbacks.py          # Callbacks после подтверждения TX
|   |-- utils/
|       |-- batch_price_service.py   # Jupiter Price API polling
|-- cleanup_dust.py                  # Очистка мусорных токенов
|-- buy.py / sell.py                 # Ручная покупка/продажа
|-- smart_money_wallets.json         # Список китов (ваш, не в git)
|-- positions.json                   # Текущие позиции (auto)
|-- .env                             # API ключи (ваш, не в git)
|-- .env.example                     # Шаблон .env
```

---

## Troubleshooting

**Бот не стартует:** `bot-status` покажет ошибку. Чаще всего — не заполнен `.env` или нет redis.

**LOW BALANCE:** Пополните кошелёк. Бот не покупает если баланс < 0.1 SOL.

**Нет сигналов китов:** `bot-whales` покажет были ли сигналы. `bot-watchdog` покажет живы ли каналы. Если "gRPC silent" — проверьте `GEYSER_API_KEY`. Если "Webhook silent" — это нормально, webhook молчит когда киты не торгуют.

**Двойные строки в логах:** Запущено два процесса. Убедитесь что работает только `whale-bot`: `systemctl is-active whale-bot` (active), `systemctl is-active pumpfun-bot` (должен быть inactive).

---

## Disclaimer

Торговля криптовалютой связана с высоким риском. Начинайте с небольших сумм. Автор не несёт ответственности за финансовые потери.