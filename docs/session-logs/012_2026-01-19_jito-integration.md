# Session 012 - 2026-01-19

## Тема: JITO Integration

## Проблема
- Транзакции могли застревать в mempool
- Нужен приоритет для быстрого landing транзакций
- MEV protection

## Решение

### 1. JITO Block Engine Integration
- Создан модуль `src/trading/jito_sender.py`
- Отправка транзакций через JITO Block Engine
- Поддержка bundles (до 5 транзакций)
- Автоматический выбор tip account

### 2. Настройки в .env
JITO_ENABLED=true JITO_TIP_LAMPORTS=10000 JITO_BLOCK_ENGINE_URL=https://mainnet.block-engine.jito.wtf


### 3. Fallback
- При ошибке JITO автоматически fallback на обычный RPC

## Файлы изменены
- `src/trading/jito_sender.py` — новый модуль
- `src/core/client.py` — интеграция JITO sender
- `.env` — JITO настройки

## Коммит
`808bde4 Session 012: fix volume purchase history + remove whale_copy from snipers`
