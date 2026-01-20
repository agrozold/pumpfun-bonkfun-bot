# Сессия 005: BAGS фильтр + Moon Bag настройка

**Дата:** 2026-01-19
**Статус:** ✅ Завершено

## Проблемы

### 1. BAGS listener спамил API (42,950 запросов, 21,474 ошибок 429)
- Фильтр `"initialize" in log.lower()` ловил ВСЕ транзакции Meteora DBC
- Реальных BAGS токенов всего ~11, но fetch запросов 42k
- Helius rate limit исчерпывался за минуты

### 2. Moon bag не настроен
- `moon_bag_percentage: 0` везде — при TP продавалось 100%
- Нужно: при x2 продать 50%, оставить 50% на рост

### 3. Whale tracker — сломанный Helius endpoint
- Хардкод `{helius_key}` не подставлялся (просто строка)
- Запросы шли без API ключа

## Исправления

### 1. bags_logs_listener.py — ужесточён фильтр
```python
# Было (ловило всё):
"initialize" in log.lower()

# Стало (только создание токенов):
False  # disabled: too broad
or "Program log: Instruction: InitializeVirtualPoolWithSplToken" in log
or "InitializeVirtualPool" in log

2. bags_logs_listener.py — добавлен rate limit
Copyawait asyncio.sleep(0.5)  # Rate limit: max 2 req/sec
token_info = await self._fetch_and_parse_transaction(signature)
3. Все bots/*.yaml — moon_bag_percentage: 50
Copymoon_bag_percentage: 50  # При TP продаём 50%, держим 50%
4. whale_tracker.py — исправлен Helius endpoint
Copy# Было:
"?api-key={helius_key}"  # не работало

# Стало:
f"?api-key={os.getenv('HELIUS_API_KEY')}"
Добавлен import os в начало файла.

Дополнительно
Очистка логов
Было: 5.8 ГБ (логи 5.6 ГБ)
Стало: 770 МБ
Настроен cron для автоочистки каждые 2 дня
Copy# crontab -l
0 3 */2 * * find /opt/pumpfun-bonkfun-bot/logs -name "*.log" -mtime +2 -delete
Изменённые файлы
Файл	Изменение
src/monitoring/bags_logs_listener.py	Фильтр + rate limit
src/monitoring/whale_tracker.py	Helius endpoint + import os
bots/*.yaml (6 файлов)	moon_bag_percentage: 50
Текущие настройки TP/SL
Параметр	Значение
stop_loss_percentage	20%
take_profit_percentage	100% (x2)
moon_bag_percentage	50% (оставляем на рост)
При x2: продаём 50%, держим 50% moon bag. При -20%: продаём 100% (стоп-лосс). EOF


Теперь обновим README и запушим:

```bash
# Обновим README
cat > /opt/pumpfun-bonkfun-bot/docs/session-logs/README.md << 'EOF'
# Session Logs

Логи сессий разработки с Claude AI.

## Список сессий

| # | Дата | Описание | Статус |
|---|------|----------|--------|
| 001 | 2025-01-19 | API оптимизация (Helius + SQLite кэш) | ✅ Done |
| 002 | 2026-01-19 | Volume Analyzer fix + imports | ✅ Done |
| 003 | 2026-01-19 | Security + RPC fix | ✅ Done |
| 004 | 2026-01-19 | Whale tracker optimization | ✅ Done |
| 005 | 2026-01-19 | BAGS фильтр + Moon Bag настройка | ✅ Done |
