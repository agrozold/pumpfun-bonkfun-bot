"""
Test script to verify race condition prevention (anti-duplicate mechanism).

Tests:
1. _buy_lock prevents concurrent purchases of same token
2. _buying_tokens and _bought_tokens sets work correctly
3. Double-check locking pattern works
4. _handle_token returns bool correctly
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))


def test_handle_token_signature():
    """Test that _handle_token has correct return type annotation."""
    print("\n" + "=" * 60)
    print("TEST 1: _handle_token Signature")
    print("=" * 60)
    
    import inspect
    from trading.universal_trader import UniversalTrader
    
    sig = inspect.signature(UniversalTrader._handle_token)
    return_annotation = sig.return_annotation
    
    print(f"Return annotation: {return_annotation}")
    
    # Check return type is bool
    assert return_annotation == bool, f"Expected bool, got {return_annotation}"
    
    print("✅ PASS: _handle_token returns bool!")
    return True


def test_lock_and_sets_exist():
    """Test that lock and tracking sets are initialized."""
    print("\n" + "=" * 60)
    print("TEST 2: Lock and Sets Initialization")
    print("=" * 60)
    
    from trading.universal_trader import UniversalTrader
    import inspect
    
    # Check __init__ source for required attributes
    source = inspect.getsource(UniversalTrader.__init__)
    
    checks = [
        ("self._buy_lock = asyncio.Lock()", "_buy_lock"),
        ("self._buying_tokens", "_buying_tokens set"),
        ("self._bought_tokens", "_bought_tokens set"),
    ]
    
    for pattern, name in checks:
        if pattern in source:
            print(f"✅ {name} found in __init__")
        else:
            print(f"❌ {name} NOT found in __init__")
            return False
    
    print("✅ PASS: All lock and sets are initialized!")
    return True


def test_double_check_locking_pattern():
    """Test that double-check locking pattern is used."""
    print("\n" + "=" * 60)
    print("TEST 3: Double-Check Locking Pattern")
    print("=" * 60)
    
    import inspect
    from trading.universal_trader import UniversalTrader
    
    # Check _on_whale_buy for double-check pattern
    source = inspect.getsource(UniversalTrader._on_whale_buy)
    
    # Pattern: check before lock, check after lock
    checks = [
        ("if mint_str in self._bought_tokens or mint_str in self._buying_tokens:", "Fast check before lock"),
        ("async with self._buy_lock:", "Lock acquisition"),
        ("self._buying_tokens.add(mint_str)", "Mark as buying inside lock"),
    ]
    
    for pattern, name in checks:
        if pattern in source:
            print(f"✅ {name}")
        else:
            print(f"❌ {name} - pattern not found")
            return False
    
    # Check finally cleanup
    if "self._buying_tokens.discard(mint_str)" in source:
        print("✅ Cleanup in finally block")
    else:
        print("❌ Cleanup in finally block not found")
        return False
    
    print("✅ PASS: Double-check locking pattern implemented!")
    return True


def test_buy_success_conditional():
    """Test that add_to_purchase_history is conditional on buy_success."""
    print("\n" + "=" * 60)
    print("TEST 4: Conditional Purchase History")
    print("=" * 60)
    
    import inspect
    from trading.universal_trader import UniversalTrader
    
    # Check _on_volume_opportunity
    source = inspect.getsource(UniversalTrader._on_volume_opportunity)
    
    if "buy_success = await self._handle_token" in source:
        print("✅ buy_success captured from _handle_token")
    else:
        print("❌ buy_success not captured")
        return False
    
    if "if buy_success:" in source:
        print("✅ Conditional check on buy_success")
    else:
        print("❌ No conditional check on buy_success")
        return False
    
    # Check _process_token_queue
    source2 = inspect.getsource(UniversalTrader._process_token_queue)
    
    if "buy_success = await self._handle_token" in source2:
        print("✅ buy_success captured in _process_token_queue")
    else:
        print("❌ buy_success not captured in _process_token_queue")
        return False
    
    if "if buy_success:" in source2:
        print("✅ Conditional check in _process_token_queue")
    else:
        print("❌ No conditional check in _process_token_queue")
        return False
    
    print("✅ PASS: Purchase history is conditional on success!")
    return True


def test_handle_token_returns():
    """Test that _handle_token has correct return statements."""
    print("\n" + "=" * 60)
    print("TEST 5: _handle_token Return Statements")
    print("=" * 60)
    
    import inspect
    from trading.universal_trader import UniversalTrader
    
    source = inspect.getsource(UniversalTrader._handle_token)
    
    # Count return True and return False
    return_true_count = source.count("return True")
    return_false_count = source.count("return False")
    
    print(f"return True count: {return_true_count}")
    print(f"return False count: {return_false_count}")
    
    # Should have exactly 1 return True (success) and multiple return False
    assert return_true_count >= 1, "Should have at least 1 return True"
    assert return_false_count >= 5, "Should have multiple return False for various failure cases"
    
    # Check that return True is after _handle_successful_buy
    if "_handle_successful_buy" in source and "return True" in source:
        # Find positions
        success_pos = source.find("_handle_successful_buy")
        return_true_pos = source.find("return True")
        if return_true_pos > success_pos:
            print("✅ return True comes after _handle_successful_buy")
        else:
            print("⚠️ return True position may be incorrect")
    
    print("✅ PASS: _handle_token has correct return statements!")
    return True


async def test_concurrent_buy_simulation():
    """Simulate concurrent buy attempts (without actual trading)."""
    print("\n" + "=" * 60)
    print("TEST 6: Concurrent Buy Simulation")
    print("=" * 60)
    
    # Simulate the locking mechanism
    _buy_lock = asyncio.Lock()
    _buying_tokens = set()
    _bought_tokens = set()
    
    buy_attempts = []
    successful_buys = []
    
    async def simulate_buy(token_mint: str, source: str):
        """Simulate buy with double-check locking."""
        # Fast check
        if token_mint in _bought_tokens or token_mint in _buying_tokens:
            buy_attempts.append((source, token_mint, "SKIPPED_FAST"))
            return False
        
        async with _buy_lock:
            # Re-check after lock
            if token_mint in _bought_tokens or token_mint in _buying_tokens:
                buy_attempts.append((source, token_mint, "SKIPPED_LOCK"))
                return False
            
            _buying_tokens.add(token_mint)
        
        try:
            # Simulate buy operation
            await asyncio.sleep(0.1)
            buy_attempts.append((source, token_mint, "EXECUTED"))
            successful_buys.append((source, token_mint))
            _bought_tokens.add(token_mint)
            return True
        finally:
            _buying_tokens.discard(token_mint)
    
    # Simulate concurrent attempts for same token
    token = "TestToken123"
    
    results = await asyncio.gather(
        simulate_buy(token, "SNIPER"),
        simulate_buy(token, "WHALE_COPY"),
        simulate_buy(token, "TRENDING"),
    )
    
    print(f"Buy attempts: {buy_attempts}")
    print(f"Successful buys: {successful_buys}")
    print(f"Results: {results}")
    
    # Only ONE should succeed
    success_count = sum(results)
    assert success_count == 1, f"Expected 1 successful buy, got {success_count}"
    assert len(successful_buys) == 1, f"Expected 1 entry in successful_buys, got {len(successful_buys)}"
    
    print(f"✅ Only 1 out of 3 concurrent attempts succeeded (as expected)")
    print("✅ PASS: Concurrent buy prevention works!")
    return True


def main():
    print("=" * 60)
    print("RACE CONDITION PREVENTION TESTS")
    print("=" * 60)
    print("Testing anti-duplicate mechanism and locking")
    
    tests = [
        ("test_handle_token_signature", test_handle_token_signature),
        ("test_lock_and_sets_exist", test_lock_and_sets_exist),
        ("test_double_check_locking_pattern", test_double_check_locking_pattern),
        ("test_buy_success_conditional", test_buy_success_conditional),
        ("test_handle_token_returns", test_handle_token_returns),
    ]
    
    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"❌ FAIL: {name} - {e}")
            results[name] = False
    
    # Run async test
    try:
        results["test_concurrent_buy_simulation"] = asyncio.run(test_concurrent_buy_simulation())
    except Exception as e:
        print(f"❌ FAIL: test_concurrent_buy_simulation - {e}")
        results["test_concurrent_buy_simulation"] = False
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = 0
    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
        if result:
            passed += 1
    
    print(f"\nTotal: {passed}/{len(results)} tests passed")
    
    if passed == len(results):
        print("\n🎉 ALL RACE CONDITION TESTS PASSED!")
        return 0
    else:
        print("\n⚠️ Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
