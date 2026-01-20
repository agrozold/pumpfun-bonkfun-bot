# Session 011 - 2026-01-19

## Тема: PumpPortal Fallback for Selling + Emergency Sell Scripts

## Проблема
- После миграции токена на PumpSwap, продажа через bonding curve невозможна
- Нужен был fallback механизм для продажи мигрированных токенов
- Ручные скрипты для экстренной продажи

## Решение

### 1. PumpPortal/PumpSwap Fallback
- Добавлен fallback seller в `src/trading/fallback_seller.py`
- Автоматическое определение что токен мигрировал
- Продажа через PumpSwap API при ошибке bonding curve

### 2. Emergency Sell Scripts
- `sell_all.py` — продать все позиции
- `sell_token.py` — продать конкретный токен

## Файлы изменены
- `src/trading/fallback_seller.py` — новый модуль
- `src/trading/universal_trader.py` — интеграция fallback
- `sell_all.py`, `sell_token.py` — утилиты

## Коммит
`d32aa6c Add PumpPortal fallback for selling + emergency sell scripts`
