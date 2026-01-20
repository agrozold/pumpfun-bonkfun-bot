# Сессия 7: Аудит BONK.FUN слушателя

**Дата:** 2026-01-20
**Коммит:** b060a52

## Цель

Проверить bonk_logs_listener.py, корректность конфигурации и соответствие алгоритмам.

## Выявленные проблемы

### 1. PumpPortal НЕ поддерживает bonk.fun
- PumpPortal присылает только pump.fun токены (mint заканчивается на `pump`)
- bonk.fun токены (mint заканчивается на `bonk`) НЕ приходят через PumpPortal

### 2. Неправильный listener_type в конфиге
- Было: `listener_type: pumpportal`
- Стало: `listener_type: bonk_logs`

### 3. Фильтр ловил покупки вместо создания токенов
- `InitializeAccount3` — это покупка/swap
- `InitializeV2`, `InitializeMint` — это создание нового токена

### 4. Transaction not found при fetch
- Публичный Solana RPC слишком медленный
- Решение: Helius RPC для fetch транзакций

## Исправления

1. Переключение на `bonk_logs` listener
2. Распределение RPC: WSS публичный Solana, HTTP Helius
3. Фильтр на InitializeV2/InitializeMint
4. Задержка 2s перед fetch

## Изменённые файлы

- `bots/bot-sniper-0-bonkfun.yaml`
- `src/monitoring/bonk_logs_listener.py`
- `src/monitoring/listener_factory.py`
- `src/monitoring/universal_pumpportal_listener.py`

## Статус

- ✅ Bonk listener работает
- ✅ Подписка на логи подтверждена
- ✅ Фильтр настроен
- ⏳ Ожидание создания нового токена для полной проверки
