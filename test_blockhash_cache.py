#!/usr/bin/env python3
"""Test script for BlockhashCache functionality."""

import asyncio
import os
import sys

# Add src to path
sys.path.insert(0, 'src')

from dotenv import load_dotenv
load_dotenv()

from core.blockhash_cache import (
    get_blockhash_cache,
    init_blockhash_cache,
    stop_blockhash_cache,
)


async def test_blockhash_cache():
    """Test the BlockhashCache functionality."""
    rpc_endpoint = os.environ.get("ALCHEMY_RPC_ENDPOINT") or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    
    if not rpc_endpoint:
        print("❌ No RPC endpoint configured in .env")
        return False
    
    print(f"🔧 Testing BlockhashCache with endpoint: {rpc_endpoint[:50]}...")
    
    try:
        # Initialize cache
        print("\n1️⃣ Initializing cache...")
        cache = await init_blockhash_cache(rpc_endpoint)
        print(f"   ✅ Cache initialized, running: {cache.is_running}")
        
        # Get blockhash (should be from cache)
        print("\n2️⃣ Getting blockhash (first call)...")
        blockhash1 = await cache.get_blockhash()
        print(f"   ✅ Blockhash: {str(blockhash1)[:20]}...")
        
        # Get again (should be cache hit)
        print("\n3️⃣ Getting blockhash (second call - should be cache hit)...")
        blockhash2 = await cache.get_blockhash()
        print(f"   ✅ Blockhash: {str(blockhash2)[:20]}...")
        
        # Check metrics
        print("\n4️⃣ Checking metrics...")
        metrics = cache.get_metrics()
        print(f"   Cache hits: {metrics['cache_hits']}")
        print(f"   Cache misses: {metrics['cache_misses']}")
        print(f"   Hit rate: {metrics['cache_hit_rate_pct']}%")
        print(f"   Cache age: {metrics['cache_age_seconds']}s")
        
        # Test with info
        print("\n5️⃣ Getting blockhash with info...")
        info = await cache.get_blockhash_with_info()
        print(f"   Hash: {str(info.hash)[:20]}...")
        print(f"   Age: {info.age_seconds:.2f}s")
        print(f"   Fresh: {info.is_fresh()}")
        
        # Stop cache
        print("\n6️⃣ Stopping cache...")
        await stop_blockhash_cache()
        print("   ✅ Cache stopped")
        
        print("\n" + "="*50)
        print("✅ All tests passed!")
        return True
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_blockhash_cache())
    sys.exit(0 if success else 1)
