# Сессия 005: Динамический расчёт Priority Fee

**Дата:** 2026-01-20
**Статус:** ЗАВЕРШЕНО

## Изменения
- dynamic_fee.py - стратегии (conservative/aggressive/sniper), кэш, DEX аккаунты
- manager.py - параметры strategy/min/max
- universal_trader.py - 3 новых параметра
- bot_runner.py - чтение из конфига
- bot-sniper-*.yaml - enable_dynamic: true

## Стратегии
- CONSERVATIVE: 50% перцентиль x1.0
- AGGRESSIVE: 75% перцентиль x1.5
- SNIPER: 90% перцентиль x1.2

## Тесты
- SNIPER: ~3,600,000 uL
- Кэширование работает (0ms повторные вызовы)

