# RPC Manager - Rate Limiting Solution

## Problem
Multiple bots making parallel RPC requests cause 429 (Too Many Requests) errors from:
- Helius API
- Alchemy RPC
- Public Solana RPC

## Solution
Global RPC Manager (`src/core/rpc_manager.py`) with:

1. **Round-robin between providers** - Automatically rotates between Helius, Alchemy, and public Solana
2. **Per-provider rate limiting** - Respects each provider's rate limits
3. **Automatic backoff on 429** - Exponential backoff when rate limited
4. **Request caching** - Reduces duplicate API calls
5. **Metrics tracking** - Monitor usage and errors

## Configuration
RPC Manager reads from environment variables:
- `HELIUS_API_KEY` - Helius API key (highest priority)
- `ALCHEMY_RPC_ENDPOINT` - Alchemy RPC URL
- `SOLANA_NODE_RPC_ENDPOINT` - Custom RPC (if not public Solana)

## Rate Limits
| Provider | Rate Limit |
|----------|------------|
| Helius RPC | 8 req/s |
| Helius Enhanced | 1.5 req/s |
| Alchemy | 15 req/s |
| Public Solana | 2 req/s |

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
