"""
Listen for new BAGS token creations.

BAGS tokens are identified by mint addresses ending with "bags".
BAGS uses Meteora DBC (Dynamic Bonding Curve) for token trading:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN

This example demonstrates how to:
1. Listen for new token creations via PumpPortal
2. Filter for BAGS tokens by address suffix
3. Process BAGS-specific token data

Note: PumpPortal may or may not support BAGS tokens directly.
This example shows how to integrate with the universal listener system.
"""

import asyncio
import json
import os
import sys
from datetime import datetime

import websockets

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from platforms.bags.address_provider import BagsAddresses, is_bags_token

# BAGS uses Meteora DBC Program
BAGS_DBC_PROGRAM_ID = str(BagsAddresses.PROGRAM)

# PumpPortal WebSocket URL
WS_URL = "wss://pumpportal.fun/api/data"


def print_bags_token_info(token_data: dict) -> None:
    """Print BAGS token information in a user-friendly format.
    
    Args:
        token_data: Dictionary containing token fields
    """
    print("\n" + "=" * 80)
    print("🎒 NEW BAGS TOKEN DETECTED")
    print("=" * 80)
    print(f"Name:             {token_data.get('name', 'N/A')}")
    print(f"Symbol:           {token_data.get('symbol', 'N/A')}")
    print(f"Mint:             {token_data.get('mint', 'N/A')}")
    print(f"Timestamp:        {datetime.now().isoformat()}")

    # Market data
    if "initialBuy" in token_data:
        initial_buy_sol = token_data['initialBuy']
        print(f"Initial Buy:      {initial_buy_sol:.6f} SOL")

    if "marketCapSol" in token_data:
        market_cap_sol = token_data['marketCapSol']
        print(f"Market Cap:       {market_cap_sol:.6f} SOL")

    if "bondingCurveKey" in token_data:
        print(f"Bonding Curve:    {token_data['bondingCurveKey']}")

    if "traderPublicKey" in token_data:
        print(f"Creator:          {token_data['traderPublicKey']}")

    # Virtual reserves
    if "vSolInBondingCurve" in token_data:
        v_sol = token_data['vSolInBondingCurve']
        print(f"Virtual SOL:      {v_sol:.6f} SOL")

    if "vTokensInBondingCurve" in token_data:
        v_tokens = token_data['vTokensInBondingCurve']
        print(f"Virtual Tokens:   {v_tokens:,.0f}")

    if "uri" in token_data:
        print(f"URI:              {token_data['uri']}")

    if "signature" in token_data:
        print(f"Signature:        {token_data['signature']}")

    print("=" * 80 + "\n")


async def listen_for_bags_tokens_pumpportal() -> None:
    """Listen for BAGS tokens via PumpPortal WebSocket.
    
    Filters all new tokens and only processes those with
    mint addresses ending in "bags".
    """
    print(f"BAGS uses Meteora DBC Program ID: {BAGS_DBC_PROGRAM_ID}")
    print(f"Connecting to PumpPortal: {WS_URL}")
    print("Filtering for tokens with mint addresses ending in 'bags'...")
    print()

    async with websockets.connect(WS_URL) as websocket:
        # Subscribe to new token events
        await websocket.send(json.dumps({"method": "subscribeNewToken", "params": []}))
        print("Subscribed to new token events")
        print("Waiting for BAGS tokens...\n")

        bags_count = 0
        total_count = 0

        while True:
            try:
                message = await websocket.recv()
                data = json.loads(message)

                # Parse token data from different message formats
                if "method" in data and data["method"] == "newToken":
                    token_info = data.get("params", [{}])[0]
                elif "signature" in data and "mint" in data:
                    token_info = data
                else:
                    continue

                total_count += 1
                mint = token_info.get("mint", "")

                # Check if this is a BAGS token
                if is_bags_token(mint):
                    bags_count += 1
                    print_bags_token_info(token_info)
                else:
                    # Log non-BAGS tokens briefly
                    if total_count % 100 == 0:
                        print(f"Processed {total_count} tokens, found {bags_count} BAGS tokens")

            except websockets.exceptions.ConnectionClosed:
                print("\nWebSocket connection closed. Reconnecting...")
                break
            except json.JSONDecodeError:
                print(f"\nReceived non-JSON message: {message[:100]}...")
            except Exception as e:
                print(f"\nError processing message: {e}")


async def listen_for_bags_tokens_universal() -> None:
    """Listen for BAGS tokens using the universal listener system.
    
    This method uses the bot's built-in listener infrastructure
    which automatically handles platform detection and parsing.
    """
    from interfaces.core import Platform, TokenInfo
    from monitoring.listener_factory import ListenerFactory

    wss_endpoint = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
    if not wss_endpoint:
        print("Error: SOLANA_NODE_WSS_ENDPOINT environment variable not set")
        return

    print(f"Creating universal listener for BAGS platform...")
    print(f"WSS Endpoint: {wss_endpoint}")

    # Create listener specifically for BAGS platform
    listener = ListenerFactory.create_listener(
        listener_type="pumpportal",
        wss_endpoint=wss_endpoint,
        platforms=[Platform.BAGS],
    )

    async def on_new_token(token_info: TokenInfo) -> None:
        """Callback for new BAGS tokens."""
        print("\n" + "=" * 80)
        print("🎒 NEW BAGS TOKEN (via Universal Listener)")
        print("=" * 80)
        print(f"Name:             {token_info.name}")
        print(f"Symbol:           {token_info.symbol}")
        print(f"Mint:             {token_info.mint}")
        print(f"Platform:         {token_info.platform.value}")
        print(f"Creator:          {token_info.user}")
        if token_info.bonding_curve:
            print(f"Bonding Curve:    {token_info.bonding_curve}")
        print("=" * 80 + "\n")

    print("Starting listener...")
    await listener.listen_for_tokens(on_new_token)


async def main() -> None:
    """Main entry point - choose listening method."""
    print("BAGS Token Listener")
    print("=" * 40)
    print()
    print("Choose listening method:")
    print("1. PumpPortal (filter by address suffix)")
    print("2. Universal Listener (requires WSS endpoint)")
    print()

    # Default to PumpPortal method
    method = os.environ.get("BAGS_LISTEN_METHOD", "pumpportal")

    while True:
        try:
            if method == "universal":
                await listen_for_bags_tokens_universal()
            else:
                await listen_for_bags_tokens_pumpportal()
        except KeyboardInterrupt:
            print("\nStopped by user")
            break
        except Exception as e:
            print(f"\nError: {e}")
            print("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
