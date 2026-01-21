# RPC Manager - Rate Limiting Solution

## Problem
Multiple bots making parallel RPC requests cause 429 (Too Many Requests) errors from:
- Helius API
- Alchemy RPC
- Public Solana RPC

## Solution
Global RPC Manager (`src/core/rpc_manager.py`) with:

1. **Multi-provider load balancing** - Helius + Chainstack as CO-PRIMARY, Alchemy/Public as fallback
2. **Per-provider rate limiting** - Respects each provider's rate limits
3. **Automatic backoff on 429** - Exponential backoff when rate limited
4. **Request caching** - Reduces duplicate API calls
5. **Metrics tracking** - Monitor usage and errors

## Combined Budget for 2-3 Weeks
| Provider | Monthly Budget | Daily Budget |
|----------|---------------|--------------|
| Helius | 800,000 credits | ~26,666/day |
| Chainstack | 1,000,000 requests | ~33,333/day |
| **TOTAL** | **1,800,000** | **~60,000/day** |

## Configuration
RPC Manager reads from environment variables:
- `HELIUS_API_KEY` - Helius API key (CO-PRIMARY)
- `CHAINSTACK_RPC_ENDPOINT` - Chainstack HTTP endpoint (CO-PRIMARY)
- `CHAINSTACK_WSS_ENDPOINT` - Chainstack WebSocket endpoint (CO-PRIMARY)
- `ALCHEMY_RPC_ENDPOINT` - Alchemy RPC URL (FALLBACK #1)
- `SOLANA_NODE_RPC_ENDPOINT` - Custom RPC (if not public Solana)

## Rate Limits
| Provider | Rate Limit | Priority | Role |
|----------|------------|----------|------|
| Helius RPC | 0.1 req/s (6/min) | 0 | CO-PRIMARY |
| Chainstack | 0.12 req/s (7/min) | 1 | CO-PRIMARY |
| Alchemy | 1.0 req/s (60/min) | 5 | FALLBACK #1 |
| Public Solana | 0.5 req/s (30/min) | 10 | FALLBACK #2 |

## Usage
The `whale_tracker.py` and `dev_reputation.py` now automatically use RPC Manager when available.

### Manual Usage
```python
from src.core.rpc_manager import get_rpc_manager

async def example():
    rpc = await get_rpc_manager()
    
    # Get transaction (auto-selects best provider)
    tx = await rpc.get_transaction(signature)
    
    # Get parsed transaction from Helius
    tx_parsed = await rpc.get_transaction_helius_enhanced(signature)
    
    # Raw RPC call
    result = await rpc.post_rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getHealth",
    })
    
    # Check metrics
    metrics = rpc.get_metrics()
    print(f"Total requests: {metrics['total_requests']}")
```

## Testing
```bash
uv run python learning-examples/test_rpc_manager.py
```

## Restart Bots
After deploying, restart all bots to use the new RPC Manager:
```bash
# Stop all bots
pkill -f pump_bot

# Start bots again
pump_bot
```

---

## Update 2026-01-21: dRPC Integration & Health Check

### New Providers Priority

| Provider | Priority | Role | Rate Limit |
|----------|----------|------|------------|
| Chainstack | 1 | CO-PRIMARY | 0.12 req/s |
| Helius | 2 | PRIMARY | 0.08 req/s |
| Alchemy | 5 | FALLBACK #1 | 0.05 req/s |
| **dRPC** | **8** | **FALLBACK #2** | **0.15 req/s** |
| Public Solana | 20 | LAST RESORT | 0.02 req/s |

### New Files

1. **`src/health_check.py`** - RPC endpoint health monitoring
2. **`bots/config/bot_rpc_config.py`** - Bot-specific RPC profiles

### Bot RPC Distribution

| Bot | Primary HTTP | Primary WSS |
|-----|--------------|-------------|
| PUMP | Helius → Chainstack → dRPC | Chainstack → dRPC |
| BONK | Chainstack → Helius → dRPC | Chainstack → dRPC |
| BAGS | Chainstack → dRPC → Helius | Chainstack → dRPC |
| WHALE | dRPC → Alchemy → Chainstack | dRPC → Chainstack |
| COPY | dRPC → Chainstack → Alchemy | dRPC → Chainstack |

### Usage

```python
# Health Check
from src.health_check import get_health_checker
checker = await get_health_checker()
await checker.start()
checker.print_status()

# Bot RPC Profile
from bots.config.bot_rpc_config import get_bot_rpc_profile
profile = get_bot_rpc_profile("bot-sniper-0-pump.yaml")
print(profile.primary_http)  # [HELIUS, CHAINSTACK, DRPC]


---

## Update 2026-01-21: dRPC + Failover + Load Balancing

### Provider Configuration

| Provider | HTTP | WSS | Rate Limit | Priority | Role |
|----------|------|-----|------------|----------|------|
| Helius | ✅ | ❌ | 0.08 req/s | 2 | PRIMARY HTTP |
| Chainstack | ✅ | ✅ | 0.12 req/s | 1 | CO-PRIMARY |
| Alchemy | ✅ | ❌ | 0.05 req/s | 5 | FALLBACK #1 |
| **dRPC** | ✅ | ✅ | 0.15 req/s | 8 | **FALLBACK #2** |
| Public | ✅ | ✅ | 0.02 req/s | 20 | LAST RESORT |

### WSS Fallback Chain

Chainstack → dRPC → Public Solana


### New Files
- `src/health_check.py` - RPC health monitoring
- `bots/config/bot_rpc_config.py` - Bot-specific RPC profiles

### Bot Load Distribution
- **PUMP/BONK** (speed critical): Helius/Chainstack HTTP, Chainstack WSS
- **WHALE/COPY** (less critical): dRPC/Alchemy HTTP, dRPC WSS
