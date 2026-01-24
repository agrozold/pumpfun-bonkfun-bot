#!/bin/bash
set -e
cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

echo "=== Smoke Test $(date) ==="

echo "1. Analytics imports (Wave 1)..."
python -c "
from src.analytics.trace_context import TraceContext, get_trace_id
from src.analytics.trace_recorder import TraceRecorder
from src.analytics.metrics_server import MetricsServer
print('   ✓ Analytics OK')
"

echo "2. Security imports (Wave 1)..."
python -c "
from src.security.file_guard import FileGuard
from src.security.secrets_manager import SecretsManager
print('   ✓ Security OK')
"

echo "3. Core imports (Wave 2)..."
python -c "
from src.core.sender import SendResult, SendStatus
from src.core.sender_registry import SenderRegistry, SendStrategy
from src.core.circuit_breaker import CircuitBreaker, CircuitState, retry_with_backoff, RetryConfig
print('   ✓ Core OK')
"

echo "4. Trading imports (Waves 3-4)..."
python -c "
from src.trading.position_state import PositionState, StateMachine
from src.trading.event_store import EventStore, EventType, Event
from src.trading.dynamic_decimals import DecimalsResolver, TokenInfo
from src.trading.trading_limits import TradingLimits, LimitsTracker, AutoSweepConfig
print('   ✓ Trading OK')
"

echo "5. TraceContext lifecycle..."
python -c "
from src.analytics.trace_context import TraceContext
ctx = TraceContext.start('buy', 'TestMint', 'test')
assert ctx.trace_id and len(ctx.trace_id) == 12
ctx.mark_build_complete()
ctx.mark_sent(provider='test')
ctx.mark_finalized(success=True)
assert ctx.total_latency_ms > 0
ctx.finish()
print('   ✓ TraceContext lifecycle OK')
"

echo "6. Event Store..."
python -c "
from src.trading.event_store import Event, EventType
event = Event.create(EventType.BUY_INITIATED, 'pos1', 'mint1', amount=0.05)
assert event.event_id
data = event.to_dict()
restored = Event.from_dict(data)
assert restored.event_type == EventType.BUY_INITIATED
print('   ✓ Event Store OK')
"

echo "7. Trading Limits..."
python -c "
from decimal import Decimal
from src.trading.trading_limits import TradingLimits
limits = TradingLimits(max_buy_amount_sol=Decimal('0.1'))
assert limits.max_buy_amount_sol == Decimal('0.1')
print('   ✓ Trading Limits OK')
"

echo "8. Circuit Breaker..."
python -c "
from src.core.circuit_breaker import CircuitBreaker, CircuitState
cb = CircuitBreaker('test')
assert cb.state == CircuitState.CLOSED
print('   ✓ Circuit Breaker OK')
"

echo "9. Dynamic Decimals..."
python -c "
from src.trading.dynamic_decimals import TokenInfo, KNOWN_DECIMALS
from decimal import Decimal
ti = TokenInfo(mint='test', decimals=6)
assert ti.decimal_factor == 1000000
assert ti.to_ui_amount(1000000) == Decimal('1')
print('   ✓ Dynamic Decimals OK')
"

echo "10. Bot configs..."
for cfg in bots/*.yaml; do
    python -c "import yaml; yaml.safe_load(open('$cfg'))" && echo "   ✓ $cfg"
done

echo ""
echo "=== Smoke Test PASSED ==="
