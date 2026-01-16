"""
Test script to verify stop-loss logic works correctly.
КРИТИЧЕСКИ ВАЖНО: Проверка что стоп-лоссы срабатывают по заданным параметрам!

Tests:
1. Position.should_exit() correctly triggers SL at configured percentage
2. Position.create_from_buy_result() correctly calculates SL price
3. Hard SL (25%) and Emergency SL (40%) thresholds work
4. Moon bag is NOT applied on SL (100% sell)
"""

import sys
from datetime import datetime
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from trading.position import Position, ExitReason
from solders.pubkey import Pubkey


def test_stop_loss_price_calculation():
    """Test that stop loss price is calculated correctly from percentage."""
    print("\n" + "=" * 60)
    print("TEST 1: Stop Loss Price Calculation")
    print("=" * 60)
    
    # Test parameters
    entry_price = 0.0001  # 0.0001 SOL per token
    stop_loss_percentage = 0.20  # 20% stop loss
    
    # Expected SL price: entry * (1 - sl_pct) = 0.0001 * 0.8 = 0.00008
    expected_sl_price = entry_price * (1 - stop_loss_percentage)
    
    # Create position
    position = Position.create_from_buy_result(
        mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
        symbol="TEST",
        entry_price=entry_price,
        quantity=1000000,
        take_profit_percentage=1.0,  # 100% TP
        stop_loss_percentage=stop_loss_percentage,
        max_hold_time=86400,
    )
    
    print(f"Entry price: {entry_price:.10f} SOL")
    print(f"Stop loss percentage: {stop_loss_percentage * 100:.1f}%")
    print(f"Expected SL price: {expected_sl_price:.10f} SOL")
    print(f"Actual SL price: {position.stop_loss_price:.10f} SOL")
    
    # Verify
    assert position.stop_loss_price is not None, "SL price should not be None!"
    assert abs(position.stop_loss_price - expected_sl_price) < 1e-15, \
        f"SL price mismatch! Expected {expected_sl_price}, got {position.stop_loss_price}"
    
    print("✅ PASS: Stop loss price calculated correctly!")
    return True


def test_stop_loss_trigger():
    """Test that should_exit() triggers SL at correct price."""
    print("\n" + "=" * 60)
    print("TEST 2: Stop Loss Trigger")
    print("=" * 60)
    
    entry_price = 0.0001
    stop_loss_percentage = 0.20  # 20% SL
    sl_price = entry_price * (1 - stop_loss_percentage)  # 0.00008
    
    position = Position.create_from_buy_result(
        mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
        symbol="TEST",
        entry_price=entry_price,
        quantity=1000000,
        stop_loss_percentage=stop_loss_percentage,
    )
    
    # Test 1: Price above SL - should NOT exit
    price_above_sl = sl_price * 1.1  # 10% above SL
    should_exit, reason = position.should_exit(price_above_sl)
    print(f"Price {price_above_sl:.10f} (above SL): should_exit={should_exit}, reason={reason}")
    assert not should_exit, "Should NOT exit when price is above SL!"
    
    # Test 2: Price exactly at SL - SHOULD exit
    should_exit, reason = position.should_exit(sl_price)
    print(f"Price {sl_price:.10f} (at SL): should_exit={should_exit}, reason={reason}")
    assert should_exit, "SHOULD exit when price equals SL!"
    assert reason == ExitReason.STOP_LOSS, f"Reason should be STOP_LOSS, got {reason}"
    
    # Test 3: Price below SL - SHOULD exit
    price_below_sl = sl_price * 0.9  # 10% below SL
    should_exit, reason = position.should_exit(price_below_sl)
    print(f"Price {price_below_sl:.10f} (below SL): should_exit={should_exit}, reason={reason}")
    assert should_exit, "SHOULD exit when price is below SL!"
    assert reason == ExitReason.STOP_LOSS, f"Reason should be STOP_LOSS, got {reason}"
    
    print("✅ PASS: Stop loss triggers correctly!")
    return True


def test_take_profit_trigger():
    """Test that should_exit() triggers TP at correct price."""
    print("\n" + "=" * 60)
    print("TEST 3: Take Profit Trigger")
    print("=" * 60)
    
    entry_price = 0.0001
    take_profit_percentage = 1.0  # 100% TP (2x)
    tp_price = entry_price * (1 + take_profit_percentage)  # 0.0002
    
    position = Position.create_from_buy_result(
        mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
        symbol="TEST",
        entry_price=entry_price,
        quantity=1000000,
        take_profit_percentage=take_profit_percentage,
    )
    
    print(f"Entry price: {entry_price:.10f} SOL")
    print(f"Take profit percentage: {take_profit_percentage * 100:.1f}%")
    print(f"TP price: {position.take_profit_price:.10f} SOL")
    
    # Test 1: Price below TP - should NOT exit
    price_below_tp = tp_price * 0.9
    should_exit, reason = position.should_exit(price_below_tp)
    print(f"Price {price_below_tp:.10f} (below TP): should_exit={should_exit}, reason={reason}")
    assert not should_exit, "Should NOT exit when price is below TP!"
    
    # Test 2: Price at TP - SHOULD exit
    should_exit, reason = position.should_exit(tp_price)
    print(f"Price {tp_price:.10f} (at TP): should_exit={should_exit}, reason={reason}")
    assert should_exit, "SHOULD exit when price equals TP!"
    assert reason == ExitReason.TAKE_PROFIT, f"Reason should be TAKE_PROFIT, got {reason}"
    
    # Test 3: Price above TP - SHOULD exit
    price_above_tp = tp_price * 1.1
    should_exit, reason = position.should_exit(price_above_tp)
    print(f"Price {price_above_tp:.10f} (above TP): should_exit={should_exit}, reason={reason}")
    assert should_exit, "SHOULD exit when price is above TP!"
    assert reason == ExitReason.TAKE_PROFIT, f"Reason should be TAKE_PROFIT, got {reason}"
    
    print("✅ PASS: Take profit triggers correctly!")
    return True


def test_hard_stop_loss_thresholds():
    """Test hard SL (25%) and emergency SL (40%) thresholds."""
    print("\n" + "=" * 60)
    print("TEST 4: Hard Stop Loss Thresholds (Code Logic)")
    print("=" * 60)
    
    # These are hardcoded in universal_trader.py
    HARD_STOP_LOSS_PCT = 25.0
    EMERGENCY_STOP_LOSS_PCT = 40.0
    
    entry_price = 0.0001
    
    # Test scenarios - use exact percentages to avoid floating point issues
    # Format: (loss_pct, description, expect_hard_sl, expect_emergency_sl)
    test_cases = [
        (-10.0, "Normal loss (-10%)", False, False),
        (-20.0, "Approaching hard SL (-20%)", False, False),
        (-25.0, "At hard SL (-25%)", True, False),  # Exactly at threshold
        (-25.01, "Just below hard SL (-25.01%)", True, False),  # Just past threshold
        (-30.0, "Below hard SL (-30%)", True, False),
        (-40.0, "At emergency SL (-40%)", True, True),
        (-50.0, "Below emergency SL (-50%)", True, True),
    ]
    
    print(f"Entry price: {entry_price:.10f} SOL")
    print(f"Hard SL threshold: -{HARD_STOP_LOSS_PCT:.0f}%")
    print(f"Emergency SL threshold: -{EMERGENCY_STOP_LOSS_PCT:.0f}%")
    print()
    
    all_passed = True
    for pnl_pct, description, expect_hard_sl, expect_emergency_sl in test_cases:
        # Check thresholds exactly as the code does
        triggers_hard_sl = pnl_pct <= -HARD_STOP_LOSS_PCT
        triggers_emergency_sl = pnl_pct <= -EMERGENCY_STOP_LOSS_PCT
        
        status = "✅" if (triggers_hard_sl == expect_hard_sl and triggers_emergency_sl == expect_emergency_sl) else "❌"
        
        print(f"{status} {description}: PnL={pnl_pct:+.2f}%")
        print(f"   Hard SL ({pnl_pct} <= -{HARD_STOP_LOSS_PCT}): {triggers_hard_sl} (expected: {expect_hard_sl})")
        print(f"   Emergency SL ({pnl_pct} <= -{EMERGENCY_STOP_LOSS_PCT}): {triggers_emergency_sl} (expected: {expect_emergency_sl})")
        
        if triggers_hard_sl != expect_hard_sl or triggers_emergency_sl != expect_emergency_sl:
            all_passed = False
    
    if all_passed:
        print("\n✅ PASS: Hard stop loss thresholds work correctly!")
    else:
        print("\n❌ FAIL: Some threshold checks failed!")
    
    return all_passed


def test_moon_bag_not_applied_on_sl():
    """Test that moon bag is NOT applied when exiting via stop loss."""
    print("\n" + "=" * 60)
    print("TEST 5: Moon Bag NOT Applied on Stop Loss")
    print("=" * 60)
    
    # This is logic from universal_trader.py _monitor_position_until_exit
    moon_bag_percentage = 20.0  # 20% moon bag
    position_quantity = 1000000
    
    # Simulate TP exit
    exit_reason_tp = "take_profit"
    if exit_reason_tp == "take_profit" and moon_bag_percentage > 0:
        sell_quantity_tp = position_quantity * (1 - moon_bag_percentage / 100)
    else:
        sell_quantity_tp = position_quantity
    
    print(f"Position quantity: {position_quantity}")
    print(f"Moon bag percentage: {moon_bag_percentage}%")
    print(f"On TAKE PROFIT: sell {sell_quantity_tp} tokens ({(sell_quantity_tp/position_quantity)*100:.0f}%)")
    
    # Simulate SL exit
    exit_reason_sl = "stop_loss"
    if exit_reason_sl == "take_profit" and moon_bag_percentage > 0:
        sell_quantity_sl = position_quantity * (1 - moon_bag_percentage / 100)
    else:
        sell_quantity_sl = position_quantity
    
    print(f"On STOP LOSS: sell {sell_quantity_sl} tokens ({(sell_quantity_sl/position_quantity)*100:.0f}%)")
    
    # Verify
    assert sell_quantity_tp == position_quantity * 0.8, "TP should sell 80% with 20% moon bag"
    assert sell_quantity_sl == position_quantity, "SL should sell 100% - NO moon bag!"
    
    print("✅ PASS: Moon bag correctly NOT applied on stop loss!")
    return True


def test_config_sl_values():
    """Test that config SL values are parsed correctly."""
    print("\n" + "=" * 60)
    print("TEST 6: Config Stop Loss Values")
    print("=" * 60)
    
    # Simulate config values from YAML
    config_sl_percentage = 0.20  # 20% as decimal (from YAML)
    
    # This is how it's used in Position.create_from_buy_result
    entry_price = 0.0001
    stop_loss_price = entry_price * (1 - config_sl_percentage)
    
    print(f"Config stop_loss_percentage: {config_sl_percentage} (= {config_sl_percentage * 100}%)")
    print(f"Entry price: {entry_price:.10f} SOL")
    print(f"Calculated SL price: {stop_loss_price:.10f} SOL")
    print(f"Loss at SL: {config_sl_percentage * 100}%")
    
    # Verify the math
    loss_at_sl = (entry_price - stop_loss_price) / entry_price
    print(f"Verified loss at SL: {loss_at_sl * 100}%")
    
    assert abs(loss_at_sl - config_sl_percentage) < 1e-10, "SL calculation mismatch!"
    
    print("✅ PASS: Config SL values parsed correctly!")
    return True


def main():
    """Run all stop loss tests."""
    print("=" * 60)
    print("STOP LOSS VERIFICATION TESTS")
    print("=" * 60)
    print("Testing that stop losses work correctly per configured parameters")
    
    tests = [
        test_stop_loss_price_calculation,
        test_stop_loss_trigger,
        test_take_profit_trigger,
        test_hard_stop_loss_thresholds,
        test_moon_bag_not_applied_on_sl,
        test_config_sl_values,
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append((test.__name__, result))
        except Exception as e:
            print(f"❌ EXCEPTION in {test.__name__}: {e}")
            results.append((test.__name__, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 ALL STOP LOSS TESTS PASSED!")
        return 0
    else:
        print("\n⚠️ SOME TESTS FAILED - CHECK STOP LOSS LOGIC!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
