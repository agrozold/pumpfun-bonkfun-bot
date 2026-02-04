# 🐋 Whale Copy Trading Bot for Solana

Автоматический бот для копирования сделок крупных трейдеров (китов) на Solana.

## ✨ Возможности

- Whale Copy Trading — отслеживание 140+ китов через Helius webhooks
- Stop Loss / TSL / Take Profit — автоматическое управление позициями  
- DCA — усреднение при просадке
- Moonbag — сохранение 10% после TSL
- Redis — быстрая синхронизация позиций
- Поддержка DEX — Pump.fun, PumpSwap, Jupiter, Raydium

## 🔑 Необходимые API ключи

| Сервис | Для чего | Где получить |
|--------|----------|--------------|
| Helius | Webhooks | https://helius.dev |
| Alchemy | Solana RPC | https://alchemy.com |
| DRPC | Резервный RPC | https://drpc.org |
| Jupiter | Свапы | https://station.jup.ag/docs |

## 🚀 Установка

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

    buy_amount: 0.01        # SOL на покупку
    min_whale_buy: 0.5      # Мин. покупка кита
    stop_loss_pct: 30       # Стоп-лосс -30%
    tsl_enabled: true       # Trailing stop
    tsl_activation_pct: 0.3 # Активация при +30%
    tsl_sell_pct: 0.9       # Продать 90%

### 6. Systemd сервис

    sudo nano /etc/systemd/system/whale-bot.service

Содержимое:

    [Unit]
    Description=Whale Copy Trading Bot
    After=network.target redis.service

    [Service]
    Type=simple
    User=root
    WorkingDirectory=/opt/pumpfun-bonkfun-bot
    Environment=PATH=/opt/pumpfun-bonkfun-bot/venv/bin
    ExecStart=/opt/pumpfun-bonkfun-bot/venv/bin/python3 -m bots.bot-whale-copy
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target

Активация:

    sudo systemctl daemon-reload
    sudo systemctl enable whale-bot
    sudo systemctl start whale-bot

### 7. Добавление китов

    nano data/whales.json

Формат:

    {
      "whales": {
        "АДРЕС_КОШЕЛЬКА": "описание"
      }
    }

## 📋 Команды

| Команда | Описание |
|---------|----------|
| bot-start | Запуск |
| bot-stop | Остановка |
| bot-restart | Перезапуск |
| bot-logs | Логи |
| bot-health | Статус |
| wsync | Синхронизация |
| buy MINT 0.01 | Покупка |
| sell MINT | Продажа |

## 🔧 Проблемы

Позиции не мониторятся:

    wsync && bot-restart

Redis сломан:

    redis-cli del whale:positions && wsync && bot-restart

## ⚠️ Disclaimer

Торговля криптовалютами связана с риском. Начните с 0.01 SOL.
