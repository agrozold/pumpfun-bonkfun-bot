#!/usr/bin/env python3
"""Test script for RPC Manager.

Tests the new RPC Manager with rate limiting and provider rotation.
"""

import asyncio
import os
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from dotenv import load_dotenv

load_dotenv()


async def test_rpc_manager():
    """Test RPC Manager functionality."""
    from src.core.rpc_manager import get_rpc_manager

    print("=" * 60)
    print("RPC Manager Test")
    print("=" * 60)

    # Get manager instance
    rpc = await get_rpc_manager()
    print(f"\nInitialized with {len(rpc.providers)} providers:")
    for name, provider in rpc.providers.items():
        print(f"  - {provider.name}: {provider.rate_limit_per_second} req/s")

    # Test 1: Get a recent transaction (use a known pump.fun tx)
    print("\n--- Test 1: Get Transaction ---")
    # This is a sample pump.fun transaction
    test_sig = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
    
    tx = await rpc.get_transaction(test_sig, use_cache=False)
    if tx:
        print(f"Transaction found: {test_sig[:20]}...")
        print(f"  Block time: {tx.get('blockTime', 'N/A')}")
    else:
        print("Transaction not found (may be too old or invalid)")

    # Test 2: Test Helius Enhanced API
    print("\n--- Test 2: Helius Enhanced API ---")
    if "helius_enhanced" in rpc.providers:
        tx_enhanced = await rpc.get_transaction_helius_enhanced(test_sig)
        if tx_enhanced:
            print(f"Enhanced TX found: {tx_enhanced.get('signature', 'N/A')[:20]}...")
            print(f"  Type: {tx_enhanced.get('type', 'N/A')}")
            print(f"  Fee payer: {tx_enhanced.get('feePayer', 'N/A')[:20]}...")
        else:
            print("Enhanced TX not found")
    else:
        print("Helius Enhanced API not configured")

    # Test 3: Multiple requests to test rate limiting
    print("\n--- Test 3: Rate Limiting (5 requests) ---")
    import time
    start = time.time()
    
    for i in range(5):
        result = await rpc.post_rpc({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
        })
        status = result.get("result", "error") if result else "failed"
        print(f"  Request {i+1}: {status}")
    
    elapsed = time.time() - start
    print(f"  Total time: {elapsed:.2f}s")

    # Test 4: Get metrics
    print("\n--- Test 4: Metrics ---")
    metrics = rpc.get_metrics()
    print(f"  Total requests: {metrics['total_requests']}")
    print(f"  Successful: {metrics['successful_requests']}")
    print(f"  Rate limited: {metrics['rate_limited']}")
    print(f"  Cache hits: {metrics['cache_hits']}")
    print(f"  Cache size: {metrics['cache_size']}")
    
    print("\n  Provider stats:")
    for name, stats in metrics["providers"].items():
        if stats["total_requests"] > 0:
            print(f"    {name}: {stats['total_requests']} requests, {stats['total_errors']} errors")

    # Cleanup
    await rpc.close()
    print("\n" + "=" * 60)
    print("Test completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_rpc_manager())
