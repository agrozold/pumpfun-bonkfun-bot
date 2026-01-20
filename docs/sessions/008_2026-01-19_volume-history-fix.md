# Session 008 - 2026-01-19

## Проблема
- Volume-sniper покупал WhiteBull повторно (3+ раза)
- `_on_volume_opportunity()` не добавлял токены в purchase history после покупки
- Секции whale_copy присутствовали в снайперах (лишний код)

## Решение
1. Добавлен `add_to_purchase_history()` в `_on_volume_opportunity()` (строка 1420)
2. Закомментированы секции whale_copy в снайперах (bags, bonk, pump)
3. WhiteBull добавлен в purchase history вручную
4. Очищен positions.json от сломанной позиции

## Архитектура
| Бот | Whale Copy | Sniper |
|-----|------------|--------|
| bot-whale-copy | ✅ enabled | ❌ disabled |
| bot-sniper-pump | ❌ removed | ✅ enabled |
| bot-sniper-bonk | ❌ removed | ✅ enabled |
| bot-sniper-bags | ❌ removed | ✅ enabled |
| bot-volume-sniper | ❌ disabled | ❌ disabled |

## Файлы изменены
- src/trading/universal_trader.py
- bots/bot-sniper-0-bags.yaml
- bots/bot-sniper-0-bonkfun.yaml
- bots/bot-sniper-0-pump.yaml
