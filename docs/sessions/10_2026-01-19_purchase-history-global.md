# Session 010 - 2026-01-19

## Тема: Global Purchase History + Sniper Enabled Flag

## Проблема
- Боты могли покупать один и тот же токен повторно
- Не было централизованного способа отслеживать все покупки
- Нужен был способ включать/выключать снайпер без удаления конфига

## Решение

### 1. Global Purchase History
- Создан модуль `src/trading/purchase_history.py`
- JSON файл `data/purchase_history.json` хранит все покупки
- Функции: `add_to_purchase_history()`, `is_already_purchased()`, `get_purchase_history()`

### 2. Sniper Enabled Flag
- Добавлен флаг `sniper_enabled: true/false` в конфигах ботов
- Позволяет отключать снайпер без удаления секции

## Файлы изменены
- `src/trading/purchase_history.py` — новый модуль
- `src/trading/universal_trader.py` — интеграция purchase history
- `bots/*.yaml` — добавлен sniper_enabled флаг

## Коммит
`73b49bb feat: global purchase history + sniper_enabled flag`
