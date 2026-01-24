#!/bin/bash
# Статус всех ботов и позиций
# Использование: ./commands/bot-status.sh

PROJECT_DIR="/opt/pumpfun-bonkfun-bot"

echo "=== Bot Processes ==="
ps aux | grep -E "bot_runner.py|universal_trader" | grep -v grep || echo "No bots running"

echo ""
echo "=== PID Files ==="
ls -la "$PROJECT_DIR"/data/*.pid 2>/dev/null || echo "No PID files"

echo ""
echo "=== Active Positions ==="
if [[ -f "$PROJECT_DIR/data/positions.json" ]]; then
    cat "$PROJECT_DIR/data/positions.json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list):
    active = [p for p in data if p.get('is_active', False)]
    print(f'Total: {len(data)}, Active: {len(active)}')
    for p in active[:5]:
        print(f\"  - {p.get('mint', 'unknown')[:16]}... state={p.get('state', 'N/A')}\")
else:
    print('Invalid format')
" 2>/dev/null || echo "Could not parse positions.json"
else
    echo "No positions.json found"
fi

echo ""
echo "=== Recent Logs (last 10 lines) ==="
tail -10 "$PROJECT_DIR"/logs/*.log 2>/dev/null | head -30 || echo "No logs"
