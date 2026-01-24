#!/bin/bash
set -e
cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

echo "=== Smoke Test $(date) ==="

echo "1. Core imports (Waves 1-2)..."
python -c "
from src.analytics.trace_context import TraceContext, get_trace_id
from src.analytics.trace_recorder import TraceRecorder
from src.analytics.metrics_server import MetricsServer
from src.security.file_guard import FileGuard
from src.security.secrets_manager import SecretsManager
from src.core.sender import SendResult, SendStatus
from src.core.sender_registry import SenderRegistry
print('   ✓ Waves 1-2 imports OK')
"

echo "2. Wave 3 imports (Event Sourcing)..."
python -c "
from src.trading.position_state import PositionState, StateMachine
from src.trading.position_redis import PositionRedisSync
from src.trading.event_store import EventStore, EventType, Event
print('   ✓ Wave 3 imports OK')
"

echo "3. Wave 4 imports (Limits & Resilience)..."
python -c "
from src.trading.dynamic_decimals import DecimalsResolver, get_token_decimals
from src.trading.trading_limits import TradingLimits, LimitsTracker, AutoSweepConfig
from src.core.circuit_breaker import CircuitBreaker, retry_with_backoff, RetryConfig
print('   ✓ Wave 4 imports OK')
"

echo "4. TraceContext lifecycle..."
python -c "
from src.analytics.trace_context import TraceContext
ctx = TraceContext.start('buy', 'TestMint', 'test')
assert ctx.trace_id and len(ctx.trace_id) == 12
ctx.mark_build_complete()
ctx.mark_sent(provider='test')
ctx.mark_finalized(success=True)
assert ctx.total_latency_ms > 0
ctx.finish()
print('   ✓ TraceContext OK')
"

echo "5. Event Store..."
python -c "
from src.trading.event_store import Event, EventType
event = Event.create(EventType.BUY_INITIATED, 'pos1', 'mint1', amount=0.05)
assert event.event_id
data = event.to_dict()
restored = Event.from_dict(data)
assert restored.event_type == EventType.BUY_INITIATED
print('   ✓ Event Store OK')
"

echo "6. Trading Limits..."
python -c "
from decimal import Decimal
from src.trading.trading_limits import TradingLimits, LimitsTracker
limits = TradingLimits(max_buy_amount_sol=Decimal('0.1'))
assert limits.max_buy_amount_sol == Decimal('0.1')
print('   ✓ Trading Limits OK')
"

echo "7. Circuit Breaker..."
python -c "
from src.core.circuit_breaker import CircuitBreaker, CircuitState
cb = CircuitBreaker('test')
assert cb.state == CircuitState.CLOSED
print('   ✓ Circuit Breaker OK')
"

echo "8. Bot configs..."
for cfg in bots/*.yaml; do
    python -c "import yaml; yaml.safe_load(open('$cfg'))" && echo "   ✓ $cfg"
done

echo ""
echo "=== Smoke Test PASSED ==="
