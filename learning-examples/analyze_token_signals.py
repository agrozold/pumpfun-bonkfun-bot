#!/usr/bin/env python3
"""Analyze why a token didn't trigger a buy signal.

Usage:
    uv run learning-examples/analyze_token_signals.py <MINT_ADDRESS>
"""

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BIRDEYE_API_URL = "https://public-api.birdeye.so"
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# Current bot thresholds
THRESHOLDS = {
    "min_patterns_to_buy": 2,
    "min_signal_strength": 0.7,
    "volume_spike_threshold": 3.0,
    "buy_pressure_threshold": 0.7,  # 70%
    "trade_velocity_threshold": 20,
    "price_momentum_threshold": 0.1,  # 10%
    "min_whale_buys": 2,
}


async def fetch_token_data(mint: str) -> dict | None:
    """Fetch token data from Birdeye."""
    if not BIRDEYE_API_KEY:
        print("❌ BIRDEYE_API_KEY not set")
        return None
    
    async with aiohttp.ClientSession() as session:
        url = f"{BIRDEYE_API_URL}/defi/token_overview?address={mint}"
        headers = {
            "X-API-KEY": BIRDEYE_API_KEY,
            "x-chain": "solana",
        }
        
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("success"):
                    return data.get("data", {})
            else:
                print(f"❌ API error: {resp.status}")
    return None


def analyze_patterns(data: dict) -> list[tuple[str, float, bool, str]]:
    """Analyze which patterns would trigger.
    
    Returns list of (pattern_name, strength, triggered, reason)
    """
    patterns = []
    
    # 1. Buy Pressure
    buys = data.get("buy5m", 0) or 0
    sells = data.get("sell5m", 0) or 0
    total = buys + sells
    
    if total >= 5:
        buy_ratio = buys / total
        strength = min(1.0, 0.5 + (buy_ratio - 0.5))
        triggered = buy_ratio >= THRESHOLDS["buy_pressure_threshold"]
        patterns.append((
            "BUY_PRESSURE",
            strength if triggered else buy_ratio,
            triggered,
            f"{buy_ratio*100:.1f}% buys ({buys}/{total}) - need {THRESHOLDS['buy_pressure_threshold']*100}%"
        ))
    else:
        patterns.append(("BUY_PRESSURE", 0, False, f"Not enough trades ({total} < 5)"))
    
    # 2. Trade Velocity
    trade_count = total
    if trade_count >= THRESHOLDS["trade_velocity_threshold"]:
        strength = min(1.0, 0.5 + (trade_count / 100))
        patterns.append((
            "TRADE_VELOCITY",
            strength,
            True,
            f"{trade_count} trades in 5min - need {THRESHOLDS['trade_velocity_threshold']}"
        ))
    else:
        patterns.append((
            "TRADE_VELOCITY",
            0,
            False,
            f"{trade_count} trades in 5min - need {THRESHOLDS['trade_velocity_threshold']}"
        ))
    
    # 3. Price Momentum
    price_change = data.get("priceChange5mPercent", 0) or 0
    price_change_decimal = price_change / 100
    
    if price_change_decimal >= THRESHOLDS["price_momentum_threshold"]:
        strength = min(1.0, 0.5 + price_change_decimal)
        patterns.append((
            "PRICE_MOMENTUM",
            strength,
            True,
            f"+{price_change:.1f}% in 5min - need +{THRESHOLDS['price_momentum_threshold']*100}%"
        ))
    else:
        patterns.append((
            "PRICE_MOMENTUM",
            0,
            False,
            f"{price_change:+.1f}% in 5min - need +{THRESHOLDS['price_momentum_threshold']*100}%"
        ))
    
    # 4. Volume Spike (simplified - would need history)
    volume_5m = (data.get("vBuy5mUSD", 0) or 0) + (data.get("vSell5mUSD", 0) or 0)
    volume_24h = data.get("v24hUSD", 0) or 0
    
    if volume_24h > 0:
        avg_5m_volume = volume_24h / 288  # 288 5-min periods in 24h
        if avg_5m_volume > 0:
            volume_ratio = volume_5m / avg_5m_volume
            triggered = volume_ratio >= THRESHOLDS["volume_spike_threshold"]
            patterns.append((
                "VOLUME_SPIKE",
                min(1.0, 0.5 + volume_ratio / 10) if triggered else 0,
                triggered,
                f"{volume_ratio:.1f}x avg volume - need {THRESHOLDS['volume_spike_threshold']}x"
            ))
        else:
            patterns.append(("VOLUME_SPIKE", 0, False, "No average volume data"))
    else:
        patterns.append(("VOLUME_SPIKE", 0, False, "No 24h volume data"))
    
    # 5. Whale Cluster (can't check without whale tracker data)
    patterns.append(("WHALE_CLUSTER", 0, False, "Requires whale tracker (not available in API)"))
    
    return patterns


async def analyze_token(mint: str):
    """Full analysis of a token."""
    print(f"\n{'='*60}")
    print(f"🔍 Analyzing token: {mint}")
    print(f"{'='*60}\n")
    
    data = await fetch_token_data(mint)
    if not data:
        print("❌ Could not fetch token data")
        return
    
    # Basic info
    print("📊 TOKEN INFO:")
    print(f"   Name: {data.get('name', 'Unknown')}")
    print(f"   Symbol: {data.get('symbol', 'Unknown')}")
    print(f"   Price: ${data.get('price', 0):.10f}")
    print(f"   24h Volume: ${data.get('v24hUSD', 0):,.2f}")
    print(f"   Market Cap: ${data.get('mc', 0):,.2f}")
    print()
    
    # 5-minute stats
    print("📈 5-MINUTE STATS:")
    print(f"   Buys: {data.get('buy5m', 0)}")
    print(f"   Sells: {data.get('sell5m', 0)}")
    print(f"   Buy Volume: ${data.get('vBuy5mUSD', 0):,.2f}")
    print(f"   Sell Volume: ${data.get('vSell5mUSD', 0):,.2f}")
    print(f"   Price Change: {data.get('priceChange5mPercent', 0):+.2f}%")
    print()
    
    # Pattern analysis
    print("🎯 PATTERN ANALYSIS:")
    print(f"   (Thresholds: min_patterns={THRESHOLDS['min_patterns_to_buy']}, min_strength={THRESHOLDS['min_signal_strength']})")
    print()
    
    patterns = analyze_patterns(data)
    triggered_patterns = []
    
    for name, strength, triggered, reason in patterns:
        status = "✅" if triggered else "❌"
        print(f"   {status} {name}")
        print(f"      {reason}")
        if triggered:
            print(f"      Strength: {strength:.2f}")
            triggered_patterns.append((name, strength))
        print()
    
    # Final verdict
    print("📋 VERDICT:")
    num_patterns = len(triggered_patterns)
    avg_strength = sum(s for _, s in triggered_patterns) / num_patterns if num_patterns > 0 else 0
    
    print(f"   Triggered patterns: {num_patterns} (need {THRESHOLDS['min_patterns_to_buy']})")
    print(f"   Average strength: {avg_strength:.2f} (need {THRESHOLDS['min_signal_strength']})")
    
    if num_patterns >= THRESHOLDS["min_patterns_to_buy"] and avg_strength >= THRESHOLDS["min_signal_strength"]:
        print("\n   🚀 WOULD BUY - All conditions met!")
    else:
        print("\n   ⛔ WOULD NOT BUY - Conditions not met:")
        if num_patterns < THRESHOLDS["min_patterns_to_buy"]:
            print(f"      - Not enough patterns ({num_patterns} < {THRESHOLDS['min_patterns_to_buy']})")
        if avg_strength < THRESHOLDS["min_signal_strength"]:
            print(f"      - Signal too weak ({avg_strength:.2f} < {THRESHOLDS['min_signal_strength']})")


async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run learning-examples/analyze_token_signals.py <MINT_ADDRESS>")
        print("\nExample:")
        print("  uv run learning-examples/analyze_token_signals.py FRmgvbbwQWbAYJxmSmokS6yfVwk7H6Uj1WcqdvMFpump")
        sys.exit(1)
    
    mint = sys.argv[1]
    await analyze_token(mint)


if __name__ == "__main__":
    asyncio.run(main())
