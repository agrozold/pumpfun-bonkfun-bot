"""
Test script to verify BONK (letsbonk.fun) token detection via PumpPortal.

WARNING: This test will NOT detect bonk.fun tokens because PumpPortal
does NOT send them! Use test_bonk_logs_listener.py instead.

This script is kept for reference to show that PumpPortal only sends pump.fun tokens.

Usage:
    uv run learning-examples/test_bonk_listener.py
"""

import asyncio
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

from interfaces.core import Platform, TokenInfo
from monitoring.listener_factory import ListenerFactory
from platforms.letsbonk.address_provider import LetsBonkAddresses

load_dotenv()


async def test_bonk_listener():
    """Test BONK token listener using PumpPortal."""
    print("=" * 80)
    print("BONK (letsbonk.fun) Token Listener Test")
    print("=" * 80)
    print()
    print(f"Raydium LaunchLab Program ID: {LetsBonkAddresses.PROGRAM}")
    print(f"This listener uses PumpPortal and detects bonk tokens by mint suffix")
    print()
    print("DETECTION METHOD:")
    print("  - PumpPortal sends ALL tokens with pool='pump'")
    print("  - We detect bonk.fun tokens by mint address ending with 'bonk'")
    print("  - Example: 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU -> pump")
    print("  - Example: 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgbonk -> bonk")
    print()
    
    # Get WSS endpoint from environment
    wss_endpoint = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
    if not wss_endpoint:
        print("ERROR: SOLANA_NODE_WSS_ENDPOINT environment variable not set")
        print("Please set it in your .env file")
        return
    
    print(f"WSS Endpoint: {wss_endpoint[:50]}...")
    print()
    
    # Check compatible listeners for LETS_BONK
    compatible = ListenerFactory.get_platform_compatible_listeners(Platform.LETS_BONK)
    print(f"Compatible listeners for LETS_BONK: {compatible}")
    
    # Verify pumpportal IS in the list
    pumpportal_platforms = ListenerFactory.get_pumpportal_supported_platforms()
    print(f"PumpPortal supported platforms: {[p.value for p in pumpportal_platforms]}")
    
    if Platform.LETS_BONK in pumpportal_platforms:
        print("✓ Correctly: LETS_BONK IS in PumpPortal supported platforms")
    else:
        print("ERROR: LETS_BONK should be in PumpPortal supported platforms!")
    
    print()
    print("Creating PumpPortal listener for LETS_BONK platform...")
    
    # Create listener specifically for LETS_BONK using pumpportal
    try:
        listener = ListenerFactory.create_listener(
            listener_type="pumpportal",
            wss_endpoint=wss_endpoint,
            platforms=[Platform.LETS_BONK],
            enable_fallback=False,  # Disable fallback to test pumpportal directly
        )
        print("✓ PumpPortal listener created successfully")
    except Exception as e:
        print(f"ERROR creating listener: {e}")
        import traceback
        traceback.print_exc()
        return
    
    print()
    print("Starting to listen for BONK tokens...")
    print("(This will wait for new bonk.fun token creations)")
    print("Press Ctrl+C to stop")
    print()
    print("-" * 80)
    
    token_count = 0
    pump_count = 0
    bonk_count = 0
    
    async def on_new_token(token_info: TokenInfo) -> None:
        """Callback for new BONK tokens."""
        nonlocal token_count, pump_count, bonk_count
        token_count += 1
        
        mint_str = str(token_info.mint)
        is_bonk = mint_str.lower().endswith("bonk")
        
        if is_bonk:
            bonk_count += 1
        else:
            pump_count += 1
        
        print()
        print("=" * 80)
        if is_bonk:
            print(f"🔥 NEW BONK TOKEN #{bonk_count}")
        else:
            print(f"📦 NEW PUMP TOKEN #{pump_count}")
        print("=" * 80)
        print(f"Name:             {token_info.name}")
        print(f"Symbol:           {token_info.symbol}")
        print(f"Mint:             {token_info.mint}")
        print(f"Platform:         {token_info.platform.value}")
        print(f"Creator:          {token_info.creator}")
        print(f"Pool State:       {token_info.pool_state}")
        print(f"Base Vault:       {token_info.base_vault}")
        print(f"Quote Vault:      {token_info.quote_vault}")
        
        # Check mint suffix
        if is_bonk:
            print(f"Mint suffix:      ends with 'bonk' ✓ (BONK.FUN TOKEN!)")
        else:
            print(f"Mint suffix:      '{mint_str[-4:]}' (pump.fun token)")
        
        print("=" * 80)
        print(f"Stats: Total={token_count}, Bonk={bonk_count}, Pump={pump_count}")
        print()
    
    try:
        await listener.listen_for_tokens(on_new_token)
    except KeyboardInterrupt:
        print()
        print(f"Stopped. Detected {token_count} tokens total:")
        print(f"  - BONK tokens: {bonk_count}")
        print(f"  - PUMP tokens: {pump_count}")
    except Exception as e:
        print(f"Listener error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print()
    asyncio.run(test_bonk_listener())
