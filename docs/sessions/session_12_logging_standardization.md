# Сессия 12: Стандартизация логирования

**Дата:** 2026-01-20  
**Приоритет:** НИЗКИЙ  
**Статус:** ВЫПОЛНЕНО

## Цель
Унифицировать формат логов для упрощения отладки и добавить ротацию.

## Проблемы до исправления
- 898 файлов логов, 664MB без ротации
- Нет системной ротации (logrotate)
- Нет JSON логирования для критических событий

## Выполненные задачи

### 1. Улучшенный logger.py
Файл: src/utils/logger.py

Новые функции:
- get_logger(name, level) - получение логгера
- setup_file_logging(filename, level, use_rotation) - с ротацией
- setup_console_logging(level) - консольный вывод
- setup_json_logging(filename) - JSON для критических событий
- log_trade_event() - структурированные торговые события
- log_critical_error() - критические ошибки с кодами
- cleanup_old_logs(days) - очистка старых логов
- get_log_stats() - статистика

### 2. Настройки ротации
- RotatingFileHandler: 10MB max, 5 бэкапов
- Системный logrotate: daily, 7 дней, compress

### 3. Очистка
- До: 898 файлов, 664MB
- После: 286 файлов, 15MB

## Формат логов
Стандартный: TIMESTAMP - MODULE - LEVEL - message
JSON: critical_events.jsonl для торговых событий

## Файлы изменены
- src/utils/logger.py - переписан
- src/utils/__init__.py - обновлены экспорты
- /etc/logrotate.d/pumpfun-bot - создан
