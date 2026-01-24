#!/bin/bash
# Запуск тестов
# Использование: ./commands/run-tests.sh [unit|integration|all]

cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

TEST_TYPE="${1:-unit}"

case "$TEST_TYPE" in
    unit)
        echo "Running unit tests..."
        python -m pytest tests/unit/ -v --tb=short
        ;;
    integration)
        echo "Running integration tests..."
        python -m pytest tests/integration/ -v --tb=short
        ;;
    all)
        echo "Running all tests..."
        python -m pytest tests/ -v --tb=short
        ;;
    *)
        echo "Usage: run-tests.sh [unit|integration|all]"
        exit 1
        ;;
esac
