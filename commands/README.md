# Bot Commands

## Управление ботом
- `bot-start.sh <config>` - Запуск бота с конфигом
- `bot-stop.sh` - Остановка всех ботов
- `bot-restart.sh <config>` - Перезапуск бота
- `bot-status.sh` - Статус всех процессов

## Торговля
- `bot-sell.sh <mint>` - Продать конкретный токен
- `bot-sell-all.sh` - Продать все позиции
- `bot-buy.sh <mint> <amount>` - Купить токен

## Мониторинг
- `bot-logs.sh [lines]` - Просмотр логов
- `bot-positions.sh` - Текущие позиции
- `bot-metrics.sh` - Метрики (после внедрения)

## Обслуживание
- `backup.sh` - Создать бэкап
- `update.sh` - Обновить из репозитория
- `install-deps.sh` - Установить зависимости
