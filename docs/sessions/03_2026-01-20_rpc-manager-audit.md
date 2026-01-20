# Session #2: RPCManager Audit & Fixes

**Дата:** 2026-01-20  
**Приоритет:** КРИТИЧЕСКИЙ  
**Статус:** ✅ ЗАВЕРШЕНО

## Проблемы (выявлены при аудите)

1. **Неправильные приоритеты провайдеров** — Alchemy (fallback) выбирался раньше Helius/Chainstack (primary)
2. **Нет WSS менеджера** — WebSocket endpoints не использовались
3. **Нет автоотключения "больных" провайдеров** — провайдер с ошибками продолжал использоваться
4. **Rate limits не оптимизированы** — риск превысить бюджет 2M запросов

## Решения

### Патч #1: Исправление приоритетов
| Провайдер | До | После | Роль |
|-----------|-----|-------|------|
| Chainstack | priority=1, rate=0.10 | priority=1, rate=0.12 | PRIMARY (WSS+HTTP) |
| Helius | priority=5, rate=0.08 | priority=2, rate=0.08 | SECONDARY (HTTP) |
| Alchemy | priority=0, rate=0.08 | priority=5, rate=0.05 | FALLBACK |
| Helius Enhanced | priority=5, rate=0.015 | priority=10, rate=0.008 | Whale tracker only |
| Public Solana | priority=10, rate=0.3 | priority=20, rate=0.02 | LAST RESORT |

### Патч #2: WSS менеджер
Добавлены методы:
- `get_wss_provider()` — возвращает (name, wss_endpoint) лучшего провайдера
- `get_wss_endpoint()` — возвращает только URL
- `report_wss_error(provider_name)` — трекинг ошибок WSS
- `report_wss_success(provider_name)` — сброс счётчика ошибок

### Патч #3: Автоотключение провайдеров
- `MAX_CONSECUTIVE_ERRORS = 5` — порог отключения
- `PROVIDER_COOLDOWN_SECONDS = 300` — 5 минут cooldown
- `disabled_until` поле в ProviderConfig
- `_check_provider_health()` — проверка и отключение
- `_try_recover_provider()` — автовосстановление

## Файлы изменены
- `src/core/rpc_manager.py`

## Бюджет на 14+ дней


2,000,000 запросов / 14 дней = 142,857/день 142,857 / 6 ботов = 23,809/день на бота 23,809 / 24 часа = 992/час на бота Safe limit (70%): ~0.19 req/s на бота


## Тестирование
```bash
python3 -m py_compile src/core/rpc_manager.py  # ✅ Syntax OK
Git commit
42b9410 - Session #1: RPCManager audit & fixes
