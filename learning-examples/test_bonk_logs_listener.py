"""
Test script for the new BonkLogsListener.

This listener subscribes directly to Raydium LaunchLab program logs
and fetches full transactions to parse token creation data.

PumpPortal does NOT send bonk.fun tokens, so this is the correct approach!

Usage:
    uv run learning-examples/test_bonk_logs_listener.py
"""

import asyncio
import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

from interfaces.core import Platform, TokenInfo
from monitoring.bonk_logs_listener import BonkLogsListener
from platforms.letsbonk.address_provider import LetsBonkAddresses

load_dotenv()


async def test_bonk_logs_listener():
    """Test the specialized BonkLogsListener."""
    print("=" * 80)
    print("BonkLogsListener Test - Direct Raydium LaunchLab Subscription")
    print("=" * 80)
    print()
    print(f"Program ID: {LetsBonkAddresses.PROGRAM}")
    print()
    print("This listener:")
    print("  1. Subscribes to logsSubscribe for Raydium LaunchLab program")
    print("  2. Detects 'initialize' instructions in logs")
    print("  3. Fetches full transaction via getTransaction")
    print("  4. Parses instruction data to extract token info")
    print()
    
    wss_endpoint = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
    rpc_endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    
    if not wss_endpoint:
        print("ERROR: SOLANA_NODE_WSS_ENDPOINT not set")
        return
    
    if not rpc_endpoint:
        # Try to derive from WSS
        rpc_endpoint = wss_endpoint.replace("wss://", "https://").replace("ws://", "http://")
        print(f"Derived RPC endpoint: {rpc_endpoint[:50]}...")
    
    print(f"WSS: {wss_endpoint[:50]}...")
    print(f"RPC: {rpc_endpoint[:50]}...")
    print()
    
    print("Creating BonkLogsListener...")
    try:
        listener = BonkLogsListener(
            wss_endpoint=wss_endpoint,
            rpc_endpoint=rpc_endpoint,
        )
        print("✓ Listener created successfully")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return
    
    print()
    print("Starting to listen for bonk.fun tokens...")
    print("Press Ctrl+C to stop")
    print()
    print("-" * 80)
    
    token_count = 0
    
    async def on_new_token(token_info: TokenInfo) -> None:
        """Callback for new tokens."""
        nonlocal token_count
        token_count += 1
        
        print()
        print("=" * 80)
        print(f"🔥 BONK TOKEN #{token_count}")
        print("=" * 80)
        print(f"Name:             {token_info.name}")
        print(f"Symbol:           {token_info.symbol}")
        print(f"Mint:             {token_info.mint}")
        print(f"Platform:         {token_info.platform.value}")
        print(f"Creator:          {token_info.creator}")
        print(f"Pool State:       {token_info.pool_state}")
        print(f"Base Vault:       {token_info.base_vault}")
        print(f"Quote Vault:      {token_info.quote_vault}")
        print(f"Global Config:    {token_info.global_config}")
        print(f"Platform Config:  {token_info.platform_config}")
        print(f"Token Program:    {token_info.token_program_id}")
        print("=" * 80)
        print()
    
    try:
        await listener.listen_for_tokens(on_new_token)
    except KeyboardInterrupt:
        print()
        print(f"Stopped. Detected {token_count} bonk.fun tokens.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print()
    asyncio.run(test_bonk_logs_listener())
