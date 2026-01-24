#!/bin/bash
# Автоматический бэкап pumpfun-bot
# Использование: ./commands/backup.sh

BACKUP_DIR="/opt/backups/pumpfun-bot"
PROJECT_DIR="/opt/pumpfun-bonkfun-bot"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

# Бэкап данных (позиции, события, конфиги)
tar --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='.env' \
    --exclude='*.key' \
    -czvf "${BACKUP_DIR}/data_${DATE}.tar.gz" \
    "${PROJECT_DIR}/data" \
    "${PROJECT_DIR}/bots" \
    "${PROJECT_DIR}/logs/traces" 2>/dev/null

# Удаление бэкапов старше 7 дней
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: ${BACKUP_DIR}/data_${DATE}.tar.gz"
