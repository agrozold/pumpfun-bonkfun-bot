#!/bin/bash
# Smoke test основной функциональности
# Использование: ./commands/smoke-test.sh

set -e

cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

echo "=== Smoke Test ==="

# 1. Проверка импортов
echo "1. Checking imports..."
python -c "
from src.analytics.trace_context import TraceContext, get_trace_id
from src.security.file_guard import FileGuard
from src.analytics.metrics_server import start_metrics_server
print('   ✓ All imports OK')
"

# 2. Проверка TraceContext
echo "2. Testing TraceContext..."
python -c "
from src.analytics.trace_context import TraceContext
ctx = TraceContext.start('buy', 'TestMint', 'test')
assert ctx.trace_id is not None
assert len(ctx.events) == 1
ctx.finish()
print('   ✓ TraceContext OK')
"

# 3. Проверка FileGuard
echo "3. Testing FileGuard..."
python -c "
import os
os.environ['AI_AGENT_MODE'] = '1'
from src.security.file_guard import FileGuard, SecurityViolationError
guard = FileGuard()
assert guard.is_forbidden('.env')
assert not guard.is_forbidden('README.md')
print('   ✓ FileGuard OK')
"

# 4. Проверка конфигов
echo "4. Checking bot configs..."
for config in bots/*.yaml; do
    if [[ -f "$config" ]]; then
        python -c "
import yaml
with open('$config') as f:
    cfg = yaml.safe_load(f)
    assert 'platform' in cfg or 'mode' in cfg
"
        echo "   ✓ $config OK"
    fi
done

# 5. Проверка RPC (если настроен)
echo "5. Checking RPC connection..."
python -c "
import os
from dotenv import load_dotenv
load_dotenv()
rpc = os.getenv('SOLANA_NODE_RPC_ENDPOINT', '')
if rpc:
    print(f'   RPC configured: {rpc[:50]}...')
else:
    print('   ⚠ RPC not configured')
"

echo ""
echo "=== Smoke Test Complete ==="
