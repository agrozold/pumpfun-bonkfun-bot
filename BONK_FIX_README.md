# BONK.FUN Token Detection Fix

## Проблема

Бот не ловил токены с bonk.fun (letsbonk.fun) потому что:

1. **PumpPortal НЕ отправляет bonk.fun токены!**
   - Тестирование показало: за 2 минуты 48 токенов, из них 0 bonk
   - PumpPortal отправляет только pump.fun токены (с суффиксом "pump")

2. **Старый logsSubscribe listener не парсил bonk токены**
   - `LetsBonkEventParser.parse_token_creation_from_logs()` возвращал `None`
   - Raydium LaunchLab не эмитит специфичные логи для создания токенов

## Решение

Создан специализированный `BonkLogsListener`:

1. Подписывается на `logsSubscribe` для Raydium LaunchLab program (`LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`)
2. Детектирует "initialize" инструкции в логах
3. Получает полную транзакцию через `getTransaction`
4. Парсит instruction data для извлечения информации о токене

## Изменения

### Новые файлы
- `src/monitoring/bonk_logs_listener.py` - специализированный listener для bonk.fun

### Обновлённые файлы
- `src/monitoring/listener_factory.py` - добавлен тип `bonk_logs`
- `src/monitoring/fallback_listener.py` - добавлена поддержка `bonk_logs`
- `src/monitoring/universal_pumpportal_listener.py` - убрана поддержка bonk/bags
- `bots/bot-sniper-0-bonkfun.yaml` - изменён `listener_type: bonk_logs`

### Тестовые скрипты
- `learning-examples/test_bonk_logs_listener.py` - тест нового listener
- `learning-examples/debug_pumpportal_bonk.py` - debug PumpPortal данных

## Использование

### Конфигурация бота для bonk.fun

```yaml
platform: lets_bonk
filters:
  listener_type: bonk_logs  # Специализированный listener для bonk.fun
```

### Тестирование

```bash
# Тест нового listener
.venv\Scripts\python learning-examples/test_bonk_logs_listener.py

# Debug PumpPortal (показывает что bonk токены не приходят)
.venv\Scripts\python learning-examples/debug_pumpportal_bonk.py
```

## Важно

- **PumpPortal поддерживает ТОЛЬКО pump.fun** - не пытайтесь использовать его для bonk.fun или bags.fm
- **bonk.fun требует `bonk_logs` listener** - прямая подписка на Raydium LaunchLab program
- **bags.fm требует `logs` listener** - прямая подписка на Meteora DBC program
