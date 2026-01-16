# BONK.FUN и BAGS.FM Token Detection Fix

## Проблема

Бот не ловил токены с bonk.fun (letsbonk.fun) и bags.fm потому что:

1. **PumpPortal НЕ отправляет bonk.fun и bags.fm токены!**
   - Тестирование показало: за 2 минуты 48 токенов, из них 0 bonk, 0 bags
   - PumpPortal отправляет только pump.fun токены (с суффиксом "pump")

2. **Старый logsSubscribe listener не парсил bonk/bags токены**
   - `LetsBonkEventParser.parse_token_creation_from_logs()` возвращал `None`
   - `BagsEventParser.parse_token_creation_from_logs()` не правильно детектил события
   - Raydium LaunchLab и Meteora DBC не эмитят специфичные логи для создания токенов

## Решение

Созданы специализированные listeners:

### BonkLogsListener (для bonk.fun)
1. Подписывается на `logsSubscribe` для Raydium LaunchLab program (`LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`)
2. Детектирует "initialize" инструкции в логах
3. Получает полную транзакцию через `getTransaction`
4. Парсит instruction data для извлечения информации о токене

### BagsLogsListener (для bags.fm)
1. Подписывается на `logsSubscribe` для Meteora DBC program (`dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`)
2. Детектирует "initialize" инструкции в логах
3. Получает полную транзакцию через `getTransaction`
4. Парсит instruction data для извлечения информации о токене

## Изменения

### Новые файлы
- `src/monitoring/bonk_logs_listener.py` - специализированный listener для bonk.fun
- `src/monitoring/bags_logs_listener.py` - специализированный listener для bags.fm

### Обновлённые файлы
- `src/monitoring/listener_factory.py` - добавлены типы `bonk_logs` и `bags_logs`
- `src/monitoring/fallback_listener.py` - добавлена поддержка `bonk_logs` и `bags_logs`
- `src/monitoring/universal_pumpportal_listener.py` - убрана поддержка bonk/bags
- `bots/bot-sniper-0-bonkfun.yaml` - изменён `listener_type: bonk_logs`
- `bots/bot-sniper-0-bags.yaml` - изменён `listener_type: bags_logs`
- `bots/bags-example.yaml` - изменён `listener_type: bags_logs`

### Тестовые скрипты
- `learning-examples/test_bonk_logs_listener.py` - тест BONK listener
- `learning-examples/bags/test_bags_logs_listener.py` - тест BAGS listener
- `learning-examples/debug_pumpportal_bonk.py` - debug PumpPortal данных

## Использование

### Конфигурация бота для bonk.fun

```yaml
platform: lets_bonk
filters:
  listener_type: bonk_logs  # Специализированный listener для bonk.fun
```

### Конфигурация бота для bags.fm

```yaml
platform: bags
filters:
  listener_type: bags_logs  # Специализированный listener для bags.fm
```

### Тестирование

```bash
# Тест BONK listener
uv run learning-examples/test_bonk_logs_listener.py

# Тест BAGS listener
uv run learning-examples/bags/test_bags_logs_listener.py

# Debug PumpPortal (показывает что bonk/bags токены не приходят)
uv run learning-examples/debug_pumpportal_bonk.py
```

## Важно

- **PumpPortal поддерживает ТОЛЬКО pump.fun** - не пытайтесь использовать его для bonk.fun или bags.fm
- **bonk.fun требует `bonk_logs` listener** - прямая подписка на Raydium LaunchLab program
- **bags.fm требует `bags_logs` listener** - прямая подписка на Meteora DBC program
