#!/bin/bash

BACKUP_DIR="./backups"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_NAME="backup-$TIMESTAMP"

mkdir -p $BACKUP_DIR/$BACKUP_NAME

# Копируем что есть (без ошибок если нет)
cp -r src/ $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null
cp -r bots/ $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null
cp .env $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null
cp smartmoneywallets.json $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null
cp BOTCOMMANDS.md $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null
cp -r trades/ $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null
cp tradestrades.log $BACKUP_DIR/$BACKUP_NAME/ 2>/dev/null

# Архивируем
tar -czf $BACKUP_DIR/$BACKUP_NAME.tar.gz -C $BACKUP_DIR $BACKUP_NAME
rm -rf $BACKUP_DIR/$BACKUP_NAME

# Удаляем старые (старше 7 дней)
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "✅ Backup created: $BACKUP_DIR/$BACKUP_NAME.tar.gz"
ls -lah $BACKUP_DIR/ | tail -5
