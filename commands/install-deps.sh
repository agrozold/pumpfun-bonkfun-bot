#!/bin/bash
# Установка/обновление зависимостей
# Использование: ./commands/install-deps.sh

PROJECT_DIR="/opt/pumpfun-bonkfun-bot"
cd "$PROJECT_DIR"

# Активация venv
source venv/bin/activate

# Обновление pip
pip install --upgrade pip

# Установка основных зависимостей
pip install -e .

# Установка дополнительных зависимостей для новых модулей
pip install \
    prometheus_client \
    tenacity \
    redis \
    aiosqlite

# Опциональные зависимости для мониторинга
pip install \
    psutil \
    aiofiles

echo "Dependencies installed successfully"
pip list | grep -E "prometheus|tenacity|redis|aiosqlite|psutil"
