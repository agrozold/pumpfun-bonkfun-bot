#!/bin/bash
# Проверка Prometheus метрик
# Использование: ./commands/check-metrics.sh [port]

PORT="${1:-9090}"

echo "Checking metrics endpoint on port $PORT..."

# Health check
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/health")
if [[ "$HEALTH" == "200" ]]; then
    echo "✓ Health check: OK"
else
    echo "✗ Health check: FAILED (HTTP $HEALTH)"
    exit 1
fi

# Metrics endpoint
METRICS=$(curl -s "http://localhost:${PORT}/metrics" | head -20)
if [[ -n "$METRICS" ]]; then
    echo "✓ Metrics endpoint: OK"
    echo ""
    echo "Sample metrics:"
    curl -s "http://localhost:${PORT}/metrics" | grep -E "^bot_" | head -10
else
    echo "✗ Metrics endpoint: FAILED"
    exit 1
fi
