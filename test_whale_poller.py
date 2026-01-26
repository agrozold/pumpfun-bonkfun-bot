#!/usr/bin/env python3
"""
Test script for WhalePoller - verifies HTTP polling mechanism.

Usage:
    python test_whale_poller.py

This will:
1. Load whale wallets from smart_money_wallets.json
2. Start polling for 60 seconds
3. Log any whale buy signals detected
"""

import asyncio
import logging
import os
import sys

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from monitoring.whale_poller import WhalePoller, WhaleBuy


async def on_whale_buy(buy: WhaleBuy):
    """Callback when whale buy is detected."""
    logger.warning("=" * 60)
    logger.warning(f"WHALE BUY CALLBACK RECEIVED!")
    logger.warning(f"  Whale: {buy.whale_label}")
    logger.warning(f"  Token: {buy.token_mint}")
    logger.warning(f"  Amount: {buy.amount_sol} SOL")
    logger.warning(f"  Platform: {buy.platform}")
    logger.warning("=" * 60)


async def main():
    """Test WhalePoller for 60 seconds."""
    logger.info("=" * 60)
    logger.info("WHALE POLLER TEST")
    logger.info("=" * 60)
    
    # Check for RPC endpoints
    alchemy = os.getenv("ALCHEMY_RPC_ENDPOINT")
    drpc = os.getenv("DRPC_RPC_ENDPOINT")
    chainstack = os.getenv("CHAINSTACK_RPC_ENDPOINT")
    
    if not any([alchemy, drpc, chainstack]):
        logger.error("No RPC endpoints configured!")
        logger.error("Please set ALCHEMY_RPC_ENDPOINT, DRPC_RPC_ENDPOINT, or CHAINSTACK_RPC_ENDPOINT")
        return
    
    logger.info(f"Alchemy: {'YES' if alchemy else 'NO'}")
    logger.info(f"dRPC: {'YES' if drpc else 'NO'}")
    logger.info(f"Chainstack: {'YES' if chainstack else 'NO'}")
    
    # Create poller with shorter interval for testing
    poller = WhalePoller(
        wallets_file="smart_money_wallets.json",
        min_buy_amount=0.4,
        poll_interval=15.0,  # Poll every 15 seconds for testing
        max_tx_age=600.0,
    )
    
    poller.set_callback(on_whale_buy)
    
    wallet_count = len(poller.whale_wallets)
    logger.info(f"Loaded {wallet_count} whale wallets")
    
    if wallet_count == 0:
        logger.error("No wallets loaded! Check smart_money_wallets.json")
        return
    
    # Show first 5 wallets
    for i, (w, info) in enumerate(list(poller.whale_wallets.items())[:5]):
        logger.info(f"  {i+1}. {w[:20]}... | {info.get('label')}")
    
    logger.info("")
    logger.info("Starting poller for 60 seconds...")
    logger.info("Watch for WHALE BUY signals...")
    logger.info("")
    
    # Run poller in background
    poller_task = asyncio.create_task(poller.start())
    
    # Wait 60 seconds
    try:
        await asyncio.sleep(60)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    
    # Stop poller
    await poller.stop()
    poller_task.cancel()
    
    # Print stats
    stats = poller.get_stats()
    logger.info("")
    logger.info("=" * 60)
    logger.info("TEST COMPLETE - STATISTICS:")
    logger.info(f"  Polls: {stats['polls']}")
    logger.info(f"  RPC calls: {stats['rpc_calls']}")
    logger.info(f"  RPC errors: {stats['rpc_errors']}")
    logger.info(f"  Signals detected: {stats['signals']}")
    logger.info(f"  Processed signatures: {len(poller._processed_sigs)}")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
