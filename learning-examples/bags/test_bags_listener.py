"""
Test script to verify BAGS token detection via logsSubscribe.

This script tests that the BAGS listener properly detects new tokens
created on bags.fm (Meteora DBC program).

IMPORTANT: bags.fm tokens are identified by Meteora DBC program activity,
NOT by mint address suffix!

NOTE: Use test_bags_logs_listener.py for the specialized BagsLogsListener.
This script uses the generic UniversalLogsListener for comparison.

Usage:
    uv run learning-examples/bags/test_bags_listener.py
"""

import asyncio
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from dotenv import load_dotenv

from interfaces.core import Platform, TokenInfo
from monitoring.listener_factory import ListenerFactory
from platforms.bags.address_provider import BagsAddresses

load_dotenv()


async def test_bags_listener():
    """Test BAGS token listener using logsSubscribe."""
    print("=" * 80)
    print("BAGS Token Listener Test")
    print("=" * 80)
    print()
    print(f"Meteora DBC Program ID: {BagsAddresses.PROGRAM}")
    print(f"This listener subscribes to Meteora DBC program logs")
    print()
    
    # Get WSS endpoint from environment
    wss_endpoint = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
    if not wss_endpoint:
        print("ERROR: SOLANA_NODE_WSS_ENDPOINT environment variable not set")
        print("Please set it in your .env file")
        return
    
    print(f"WSS Endpoint: {wss_endpoint[:50]}...")
    print()
    
    # Check compatible listeners for BAGS
    compatible = ListenerFactory.get_platform_compatible_listeners(Platform.BAGS)
    print(f"Compatible listeners for BAGS: {compatible}")
    
    # Verify pumpportal is NOT in the list
    pumpportal_platforms = ListenerFactory.get_pumpportal_supported_platforms()
    print(f"PumpPortal supported platforms: {[p.value for p in pumpportal_platforms]}")
    
    if Platform.BAGS in pumpportal_platforms:
        print("WARNING: BAGS should NOT be in PumpPortal supported platforms!")
    else:
        print("✓ Correctly: BAGS is NOT in PumpPortal supported platforms")
    
    print()
    print("Creating logs listener for BAGS platform...")
    
    # Create listener specifically for BAGS using logs (NOT pumpportal)
    try:
        listener = ListenerFactory.create_listener(
            listener_type="logs",  # Use logs, NOT pumpportal!
            wss_endpoint=wss_endpoint,
            platforms=[Platform.BAGS],
            enable_fallback=False,  # Disable fallback to test logs directly
        )
        print("✓ Logs listener created successfully")
    except Exception as e:
        print(f"ERROR creating listener: {e}")
        return
    
    print()
    print("Starting to listen for BAGS tokens...")
    print("(This will wait for new bags.fm token creations)")
    print("Press Ctrl+C to stop")
    print()
    print("-" * 80)
    
    token_count = 0
    
    async def on_new_token(token_info: TokenInfo) -> None:
        """Callback for new BAGS tokens."""
        nonlocal token_count
        token_count += 1
        
        print()
        print("=" * 80)
        print(f"🎒 NEW BAGS TOKEN #{token_count}")
        print("=" * 80)
        print(f"Name:             {token_info.name}")
        print(f"Symbol:           {token_info.symbol}")
        print(f"Mint:             {token_info.mint}")
        print(f"Platform:         {token_info.platform.value}")
        print(f"Creator:          {token_info.creator}")
        print(f"Pool State:       {token_info.pool_state}")
        print(f"Base Vault:       {token_info.base_vault}")
        print(f"Quote Vault:      {token_info.quote_vault}")
        
        # Check if mint ends with "bags" (just for info)
        mint_str = str(token_info.mint)
        if mint_str.lower().endswith("bags"):
            print(f"Mint suffix:      ends with 'bags' ✓")
        else:
            print(f"Mint suffix:      '{mint_str[-4:]}' (NOT 'bags' - this is normal!)")
        
        print("=" * 80)
        print()
    
    try:
        await listener.listen_for_tokens(on_new_token)
    except KeyboardInterrupt:
        print()
        print(f"Stopped. Detected {token_count} BAGS tokens.")
    except Exception as e:
        print(f"Listener error: {e}")


if __name__ == "__main__":
    print()
    asyncio.run(test_bags_listener())
