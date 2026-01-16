#!/usr/bin/env python3
"""
Test Volume Pattern Analyzer.

Тестирует новую стратегию Volume Pattern Sniping:
- Сканирует токены на паттерны объёмов
- Анализирует health score и opportunity score
- Выводит найденные возможности

Usage:
    uv run learning-examples/test_volume_pattern_analyzer.py
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.monitoring.volume_pattern_analyzer import (
    TokenVolumeAnalysis,
    VolumePatternAnalyzer,
)


async def on_opportunity(analysis: TokenVolumeAnalysis) -> None:
    """Callback для найденных возможностей."""
    print("\n" + "=" * 60)
    print(f"🎯 OPPORTUNITY DETECTED: {analysis.symbol}")
    print("=" * 60)
    print(f"Mint: {analysis.mint}")
    print(f"Health Score: {analysis.health_score}/100")
    print(f"Opportunity Score: {analysis.opportunity_score}/100")
    print(f"Recommendation: {analysis.recommendation}")
    print(f"Risk Level: {analysis.risk_level.value}")
    print()
    print("Volume Metrics:")
    print(f"  - Volume 5m: ${analysis.volume_5m:,.0f}")
    print(f"  - Volume 1h: ${analysis.volume_1h:,.0f}")
    print(f"  - Spike Ratio: {analysis.volume_spike_ratio:.1f}x")
    print()
    print("Trade Metrics:")
    print(f"  - Buys 5m: {analysis.buys_5m}")
    print(f"  - Sells 5m: {analysis.sells_5m}")
    print(f"  - Buy Pressure 5m: {analysis.buy_pressure_5m:.0%}")
    print(f"  - Buy Pressure 1h: {analysis.buy_pressure_1h:.0%}")
    print()
    print("Patterns Detected:")
    for pattern in analysis.patterns:
        print(
            f"  - {pattern.pattern_type.value}: strength={pattern.strength:.2f}, confidence={pattern.confidence:.2f}"
        )
    print("=" * 60 + "\n")


async def main() -> None:
    """Main test function."""
    print("=" * 60)
    print("Volume Pattern Analyzer Test")
    print("=" * 60)
    print()
    print("This test will scan tokens for volume patterns.")
    print("Looking for: volume spikes, whale accumulation, organic growth...")
    print()

    # Create analyzer with test settings
    analyzer = VolumePatternAnalyzer(
        min_volume_1h=5000,  # Lower threshold for testing
        volume_spike_threshold=2.0,  # Lower threshold for testing
        min_trades_5m=10,
        min_buy_pressure=0.50,
        scan_interval=60,  # 1 minute for testing
        max_tokens_per_scan=30,
    )

    # Set callback
    analyzer.set_callbacks(on_opportunity=on_opportunity)

    print("Starting analyzer...")
    print("Press Ctrl+C to stop\n")

    try:
        await analyzer.start()

        # Run for a while
        scan_count = 0
        while True:
            await asyncio.sleep(30)
            scan_count += 1
            stats = analyzer.get_stats()
            print(
                f"[Scan #{scan_count}] Tokens tracked: {stats['tokens_tracked']}, Signals: {stats['signals_processed']}"
            )

    except KeyboardInterrupt:
        print("\nStopping analyzer...")
    finally:
        await analyzer.stop()
        print("Done!")


async def test_single_token(mint: str) -> None:
    """Test analysis of a single token."""
    print(f"Analyzing token: {mint}")
    print()

    analyzer = VolumePatternAnalyzer()

    try:
        analysis = await analyzer.analyze_specific_token(mint)

        if analysis:
            print(f"Symbol: {analysis.symbol}")
            print(f"Health Score: {analysis.health_score}/100")
            print(f"Opportunity Score: {analysis.opportunity_score}/100")
            print(f"Recommendation: {analysis.recommendation}")
            print(f"Risk Level: {analysis.risk_level.value}")
            print()
            print(f"Volume 5m: ${analysis.volume_5m:,.0f}")
            print(f"Volume 1h: ${analysis.volume_1h:,.0f}")
            print(f"Spike Ratio: {analysis.volume_spike_ratio:.1f}x")
            print(f"Buy Pressure: {analysis.buy_pressure_5m:.0%}")
            print()
            print("Patterns:")
            for p in analysis.patterns:
                print(f"  - {p.pattern_type.value}")
        else:
            print("No data found for this token")

    finally:
        await analyzer.stop()


if __name__ == "__main__":
    # Check if specific token provided
    if len(sys.argv) > 1:
        token_mint = sys.argv[1]
        asyncio.run(test_single_token(token_mint))
    else:
        asyncio.run(main())
