#!/usr/bin/env python3
"""
Тестовый скрипт для PumpPatternDetector.
Демонстрирует как использовать детектор паттернов.
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from monitoring.pump_pattern_detector import PumpPatternDetector


async def on_pump_signal(mint: str, symbol: str, patterns: list, strength: float):
    """Callback когда обнаружен потенциальный памп."""
    print(f"\n🚀 PUMP SIGNAL DETECTED!")
    print(f"   Token: {symbol} ({mint[:8]}...)")
    print(f"   Strength: {strength:.2f}")
    print(f"   Patterns:")
    for p in patterns:
        print(f"     - {p.pattern_type}: {p.description}")
    print()


async def simulate_pump_scenario():
    """Симуляция сценария пампа для тестирования."""
    
    detector = PumpPatternDetector(
        volume_spike_threshold=3.0,
        holder_growth_threshold=0.5,
        min_whale_buys=2,
        whale_window_seconds=30,
        min_patterns_to_signal=2,
    )
    
    detector.set_pump_signal_callback(on_pump_signal)
    
    # Fake token
    mint = "FakeToken111111111111111111111111111111111"
    symbol = "FAKE"
    
    print(f"Starting pattern detection for {symbol}...")
    detector.start_tracking(mint, symbol)
    
    # Simulate normal activity
    print("\n📊 Phase 1: Normal activity...")
    for i in range(5):
        await detector.record_price(mint, price=0.00001 + i * 0.000001, volume=0.1)
        await detector.record_holder_count(mint, count=10 + i)
        await asyncio.sleep(0.1)
    
    # Simulate whale buys
    print("\n🐋 Phase 2: Whale buys coming in...")
    await detector.record_whale_buy(mint, "Whale1111111111111111111111111111111111111", 1.5)
    await asyncio.sleep(0.2)
    await detector.record_whale_buy(mint, "Whale2222222222222222222222222222222222222", 2.0)
    await asyncio.sleep(0.2)
    await detector.record_whale_buy(mint, "Whale3333333333333333333333333333333333333", 0.8)
    
    # Simulate volume spike
    print("\n📈 Phase 3: Volume spike...")
    for i in range(5):
        await detector.record_price(mint, price=0.00002 + i * 0.000005, volume=0.5 + i * 0.3)
        await asyncio.sleep(0.1)
    
    # Simulate holder growth
    print("\n👥 Phase 4: Holder growth...")
    await detector.record_holder_count(mint, count=25)
    await asyncio.sleep(0.1)
    await detector.record_holder_count(mint, count=40)
    
    # Simulate curve acceleration
    print("\n📊 Phase 5: Curve acceleration...")
    await detector.record_curve_progress(mint, 5.0)
    await asyncio.sleep(0.1)
    await detector.record_curve_progress(mint, 12.0)
    
    # Print final status
    print("\n📋 Final token status:")
    status = detector.get_token_status(mint)
    if status:
        print(f"   Price points: {status['price_points']}")
        print(f"   Volume points: {status['volume_points']}")
        print(f"   Holder points: {status['holder_points']}")
        print(f"   Whale buys: {status['whale_buys']}")
        print(f"   Curve progress: {status['curve_progress']:.1f}%")
        print(f"   Patterns detected: {len(status['patterns_detected'])}")
        for p in status['patterns_detected']:
            print(f"     - {p['type']}: {p['description']}")
    
    print("\n✅ Test completed!")


if __name__ == "__main__":
    asyncio.run(simulate_pump_scenario())
