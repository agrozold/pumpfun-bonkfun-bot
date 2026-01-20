# Сессия 006: Кэширование Blockhash

**Дата:** 2026-01-20
**Приоритет:** ВЫСОКИЙ
**Статус:** ✅ ЗАВЕРШЕНО

## Цель
Реализовать фоновое обновление blockhash для экономии времени при отправке транзакций.

## Изменения

### Новые файлы
| Файл | Описание |
|------|----------|
| `src/core/blockhash_cache.py` | Глобальный синглтон с фоновым обновлением каждые 30 сек |
| `test_blockhash_cache.py` | Тестовый скрипт для проверки |

### Изменённые файлы
| Файл | Изменение |
|------|-----------|
| `buy.py` | Интегрирован кэш в `buy_via_pumpswap()` и `buy_via_pumpfun()` |
| `sell.py` | Интегрирован кэш в `sell_via_pumpswap()` и `sell_via_pumpfun()` |
| `src/core/client.py` | Упрощён - использует глобальный `BlockhashCache` |
| `src/core/__init__.py` | Добавлены экспорты |

## Конфигурация (оптимизировано для trial лимитов)
- UPDATE_INTERVAL: 30 сек
- MAX_CACHE_AGE: 30 сек (fallback на свежий запрос)
- BLOCKHASH_VALIDITY: 60 сек

## Экономия RPC запросов
- До: каждая транзакция = 1 вызов get_latest_blockhash
- После: 1 вызов на 30 сек для всех транзакций
- Экономия: ~95% уменьшение blockhash запросов

## Тесты
- Cache hits: 100%
- Кэширование работает (0ms повторные вызовы)

## Коммит
5722fa8 feat(blockhash-cache): background blockhash caching for faster transactions
