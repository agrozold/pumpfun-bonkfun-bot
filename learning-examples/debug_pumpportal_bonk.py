"""
Debug script to see RAW PumpPortal data and check for bonk tokens.

This script connects directly to PumpPortal WebSocket and logs ALL raw messages
to understand what data format is being sent for different platforms.

Usage:
    uv run learning-examples/debug_pumpportal_bonk.py
"""

import asyncio
import json
import os
import sys

import websockets
from dotenv import load_dotenv

load_dotenv()


async def debug_pumpportal():
    """Debug PumpPortal raw messages."""
    print("=" * 80)
    print("PumpPortal RAW Debug - Looking for BONK tokens")
    print("=" * 80)
    print()
    print("Connecting to PumpPortal WebSocket...")
    print("Will show ALL raw messages to analyze data format")
    print()
    print("LOOKING FOR:")
    print("  - Tokens with mint ending in 'bonk'")
    print("  - Tokens with pool='bonk' or similar")
    print("  - Any indication of bonk.fun tokens")
    print()
    print("-" * 80)
    
    url = "wss://pumpportal.fun/api/data"
    
    token_count = 0
    bonk_count = 0
    pump_count = 0
    other_count = 0
    
    try:
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            # Subscribe to new tokens
            await ws.send(json.dumps({"method": "subscribeNewToken", "params": []}))
            print("✓ Subscribed to newToken events")
            print()
            
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = json.loads(msg)
                    
                    # Extract token data
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
                    
                    mint = token_data.get("mint", "")
                    pool = token_data.get("pool", "")
                    name = token_data.get("name", "")
                    symbol = token_data.get("symbol", "")
                    
                    # Detect platform from mint suffix
                    mint_lower = mint.lower()
                    if mint_lower.endswith("bonk"):
                        platform = "BONK"
                        bonk_count += 1
                        emoji = "🔥"
                    elif mint_lower.endswith("pump"):
                        platform = "PUMP"
                        pump_count += 1
                        emoji = "📦"
                    elif mint_lower.endswith("bags"):
                        platform = "BAGS"
                        other_count += 1
                        emoji = "🎒"
                    else:
                        platform = f"OTHER({mint[-4:]})"
                        other_count += 1
                        emoji = "❓"
                    
                    print(f"{emoji} #{token_count} | {platform} | pool={pool}")
                    print(f"   Name: {name} ({symbol})")
                    print(f"   Mint: {mint}")
                    
                    # Show full raw data for bonk tokens
                    if platform == "BONK":
                        print(f"   === BONK TOKEN FOUND! RAW DATA ===")
                        print(f"   {json.dumps(token_data, indent=2)}")
                        print(f"   ===================================")
                    
                    print(f"   Stats: Total={token_count} | Bonk={bonk_count} | Pump={pump_count} | Other={other_count}")
                    print()
                    
                except asyncio.TimeoutError:
                    print("... waiting for tokens (60s timeout)")
                except json.JSONDecodeError:
                    print("... invalid JSON received")
                    
    except KeyboardInterrupt:
        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print(f"Total tokens:  {token_count}")
        print(f"BONK tokens:   {bonk_count}")
        print(f"PUMP tokens:   {pump_count}")
        print(f"Other tokens:  {other_count}")
        print()
        if bonk_count == 0:
            print("⚠️  NO BONK TOKENS DETECTED!")
            print("   This could mean:")
            print("   1. No bonk.fun tokens were created during this session")
            print("   2. PumpPortal doesn't send bonk.fun tokens")
            print("   3. bonk.fun tokens have different mint suffix")
        print("=" * 80)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(debug_pumpportal())
