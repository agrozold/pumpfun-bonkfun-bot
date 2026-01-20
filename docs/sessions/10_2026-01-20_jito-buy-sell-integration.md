# Session 010 - 2026-01-20

## Тема: JITO Integration в buy.py и sell.py

## Проблема
- JITO был интегрирован только в src/core/client.py (для ботов через UniversalTrader)
- Ручные скрипты buy.py и sell.py отправляли транзакции напрямую через RPC
- Это означало отсутствие MEV-защиты и более медленный landing для ручных операций

## Решение

### 1. Добавлен импорт JITO в buy.py и sell.py

### 2. Обновлены 6 мест отправки транзакций

buy.py (3 места):
- Jupiter: Отправка через JITO с fallback на RPC
- PumpSwap: JITO tip + отправка через JITO
- Pump.fun Bonding Curve: JITO tip + отправка через JITO

sell.py (3 места):
- Jupiter: Отправка через JITO с fallback на RPC
- PumpSwap: JITO tip + отправка через JITO
- Pump.fun Bonding Curve: JITO tip + отправка через JITO

### 3. Логика работы
- JITO_ENABLED=true: tip instruction + отправка через JITO Block Engine
- JITO_ENABLED=false: обычный RPC
- При ошибке JITO: автоматический fallback на RPC

### 4. Добавлены JITO переменные в .env.example
- JITO_ENABLED=true
- JITO_TIP_LAMPORTS=10000
- JITO_BLOCK_ENGINE_URL=https://mainnet.block-engine.jito.wtf

## Файлы изменены
- buy.py
- sell.py
- .env.example
- src/utils/tx_sender.py
