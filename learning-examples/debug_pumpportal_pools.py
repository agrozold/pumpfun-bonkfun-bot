"""
Debug script to see what pool names PumpPortal sends.
This will help identify why BONK and BAGS tokens are not being detected.

FINDING: PumpPortal sends ALL tokens with pool="pump", regardless of actual platform!
The actual platform must be detected from mint address suffix:
- ...pump -> pump.fun
- ...bonk -> letsbonk.fun  
- ...bags -> bags
"""

import asyncio
import json
from collections import Counter

import websockets

WS_URL = "wss://pumpportal.fun/api/data"


def detect_platform_from_mint(mint: str) -> str:
    """Detect actual platform from mint address suffix."""
    mint_lower = mint.lower()
    if mint_lower.endswith("pump"):
        return "PUMP"
    elif mint_lower.endswith("bonk"):
        return "BONK"
    elif mint_lower.endswith("bags"):
        return "BAGS"
    return "UNKNOWN"


async def debug_pumpportal_pools():
    """Listen to PumpPortal and log all pool names received."""
    print(f"Connecting to PumpPortal: {WS_URL}")
    print("Listening for ALL tokens to see pool names...")
    print("=" * 70)

    pool_counter: Counter = Counter()
    platform_counter: Counter = Counter()
    token_count = 0

    async with websockets.connect(WS_URL, ping_interval=20) as websocket:
        # Subscribe to new token events
        await websocket.send(json.dumps({"method": "subscribeNewToken", "params": []}))
        print("Subscribed to new token events\n")

        while True:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=60)
                data = json.loads(message)

                # Parse token data
                token_data = None
                if "method" in data and data["method"] == "newToken":
                    params = data.get("params", [])
                    if params:
                        token_data = params[0]
                elif "signature" in data and "mint" in data:
                    token_data = data

                if not token_data:
                    continue

                token_count += 1
                pool = token_data.get("pool", "UNKNOWN")
                mint = token_data.get("mint", "")
                symbol = token_data.get("symbol", "")
                name = token_data.get("name", "")

                # Detect actual platform from mint suffix
                actual_platform = detect_platform_from_mint(mint)
                
                pool_counter[pool] += 1
                platform_counter[actual_platform] += 1

                # Highlight non-pump tokens
                highlight = ""
                if actual_platform in ("BONK", "BAGS"):
                    highlight = f" *** {actual_platform} TOKEN FOUND! ***"

                # Log every token with pool info
                print(
                    f"[{token_count:4d}] pool={pool:8s} | platform={actual_platform:7s} | "
                    f"{symbol:10s} | {mint[:20]}...{highlight}"
                )

                # Print summary every 50 tokens
                if token_count % 50 == 0:
                    print("\n" + "=" * 70)
                    print(f"SUMMARY after {token_count} tokens:")
                    print("  Pool field (from PumpPortal):")
                    for pool_name, count in pool_counter.most_common():
                        print(f"    {pool_name}: {count} tokens")
                    print("  Actual platform (from mint suffix):")
                    for platform, count in platform_counter.most_common():
                        print(f"    {platform}: {count} tokens")
                    print("=" * 70 + "\n")

            except asyncio.TimeoutError:
                print("No data for 60s, still listening...")
            except websockets.exceptions.ConnectionClosed:
                print("Connection closed, reconnecting...")
                break
            except Exception as e:
                print(f"Error: {e}")


if __name__ == "__main__":
    print("PumpPortal Pool Debug Script")
    print("=" * 70)
    print("IMPORTANT: PumpPortal sends ALL tokens with pool='pump'!")
    print("Actual platform is detected from mint address suffix:")
    print("  - ...pump -> pump.fun")
    print("  - ...bonk -> letsbonk.fun")
    print("  - ...bags -> bags")
    print("=" * 70)
    print("Press Ctrl+C to stop\n")

    try:
        asyncio.run(debug_pumpportal_pools())
    except KeyboardInterrupt:
        print("\nStopped by user")
