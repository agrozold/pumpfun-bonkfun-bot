"""
Debug script to understand how to properly detect bags.fm tokens.

PROBLEM: Current detection uses mint suffix "bags", but bags.fm tokens
don't always end with "bags" (e.g., BAGWORKER token ends with "GS").

This script will:
1. Listen to PumpPortal for ALL tokens
2. Log the full data structure to understand what fields identify bags.fm tokens
3. Check if there's a specific pool name or other identifier

Based on DexScreener, BAGWORKER is on "Meteora DBC" which is the bags.fm infrastructure.
"""

import asyncio
import json
import os
import sys
from datetime import datetime

import websockets

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

WS_URL = "wss://pumpportal.fun/api/data"

# Known bags.fm program IDs
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
BAGS_FEE_SHARE_V2 = "FEE2tBhCKAt7shrod19QttSVREUYPiyMzoku1mL1gqVK"


def analyze_token_data(token_data: dict, count: int) -> None:
    """Analyze token data to understand platform detection."""
    mint = token_data.get("mint", "")
    pool = token_data.get("pool", "")
    name = token_data.get("name", "")
    symbol = token_data.get("symbol", "")
    
    # Check various detection methods
    ends_with_bags = mint.lower().endswith("bags")
    ends_with_pump = mint.lower().endswith("pump")
    ends_with_bonk = mint.lower().endswith("bonk")
    
    # Log ALL fields to understand the structure
    print(f"\n[{count}] Token: {symbol} ({name})")
    print(f"  Mint: {mint}")
    print(f"  Pool field: '{pool}'")
    print(f"  Suffix detection: bags={ends_with_bags}, pump={ends_with_pump}, bonk={ends_with_bonk}")
    
    # Log all keys in the data
    print(f"  All fields: {list(token_data.keys())}")
    
    # If pool is not "pump", highlight it
    if pool and pool.lower() != "pump":
        print(f"  *** NON-PUMP POOL DETECTED: {pool} ***")
    
    # Check for any bags-related fields
    for key, value in token_data.items():
        if isinstance(value, str) and "bags" in value.lower():
            print(f"  *** BAGS reference in {key}: {value} ***")
        if isinstance(value, str) and "meteora" in value.lower():
            print(f"  *** METEORA reference in {key}: {value} ***")
        if isinstance(value, str) and "dbc" in value.lower():
            print(f"  *** DBC reference in {key}: {value} ***")


async def debug_pumpportal():
    """Listen to PumpPortal and analyze all token data."""
    print("=" * 80)
    print("BAGS.FM Token Detection Debug")
    print("=" * 80)
    print(f"Connecting to: {WS_URL}")
    print(f"Looking for bags.fm identifiers...")
    print("=" * 80)

    async with websockets.connect(WS_URL, ping_interval=20) as websocket:
        await websocket.send(json.dumps({"method": "subscribeNewToken", "params": []}))
        print("Subscribed to new token events\n")

        count = 0
        pools_seen = set()

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

                count += 1
                pool = token_data.get("pool", "unknown")
                pools_seen.add(pool)
                
                # Analyze every token
                analyze_token_data(token_data, count)

                # Summary every 20 tokens
                if count % 20 == 0:
                    print("\n" + "=" * 80)
                    print(f"SUMMARY after {count} tokens:")
                    print(f"  Unique pools seen: {pools_seen}")
                    print("=" * 80 + "\n")

            except asyncio.TimeoutError:
                print("No data for 60s, still listening...")
            except websockets.exceptions.ConnectionClosed:
                print("Connection closed, reconnecting...")
                break
            except Exception as e:
                print(f"Error: {e}")


if __name__ == "__main__":
    print("Press Ctrl+C to stop\n")
    try:
        asyncio.run(debug_pumpportal())
    except KeyboardInterrupt:
        print("\nStopped by user")
