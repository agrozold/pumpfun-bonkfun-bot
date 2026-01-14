#!/usr/bin/env python3
"""
Analyze pump tokens using Dexscreener API.
Looks for patterns that preceded pumps.
"""

import asyncio
import aiohttp
from datetime import datetime


TOKENS = [
    # Последние
    "a3W4qutoEJA4232T2gwZUfgYJTetr96pU4SJMwpppump",
    "jk1T35eWK41MBMM8AWoYVaNbjHEEQzMDetTsfnqpump",
    # Старенькие
    "61V8vBaqAGMpgDQi4JcAwo1dmBGHsyhzodcPqnEVpump",
    "DKu9kykSfbN5LBfFXtNNDPaX35o4Fv6vJ9FKk7pZpump",
    # Кабальные ранеры
    "FT6ZnLbmaQbUmxbpe69qwRgPi9tU8QGY8S7gqt4Wbonk",
    "BkEvgC9nfhy9TpCJDPUGy9ANXbYMxosfmLzk35gqpump",
    "Ep4kgeqi6T5JrdJTbiXQcAjY3ZDAWh6meavJok9Epump",
    "CSrwNk6B1DwWCHRMsaoDVUfD5bBMQCJPY72ZG3Nnpump",
]


async def fetch_token_data(session: aiohttp.ClientSession, token: str) -> dict | None:
    """Fetch token data from Dexscreener."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data
            else:
                print(f"Error fetching {token[:8]}...: {resp.status}")
                return None
    except Exception as e:
        print(f"Exception fetching {token[:8]}...: {e}")
        return None


def analyze_token(data: dict, token: str) -> dict:
    """Analyze token data for pump patterns."""
    if not data or "pairs" not in data or not data["pairs"]:
        return {"token": token, "error": "No pairs found"}
    
    # Get the main pair (usually first one with highest liquidity)
    pair = data["pairs"][0]
    
    analysis = {
        "token": token[:8] + "...",
        "name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
        "dex": pair.get("dexId", "unknown"),
        "price_usd": pair.get("priceUsd", "0"),
        "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
        "price_change_1h": pair.get("priceChange", {}).get("h1", 0),
        "price_change_6h": pair.get("priceChange", {}).get("h6", 0),
        "price_change_24h": pair.get("priceChange", {}).get("h24", 0),
        "volume_5m": pair.get("volume", {}).get("m5", 0),
        "volume_1h": pair.get("volume", {}).get("h1", 0),
        "volume_6h": pair.get("volume", {}).get("h6", 0),
        "volume_24h": pair.get("volume", {}).get("h24", 0),
        "txns_5m_buys": pair.get("txns", {}).get("m5", {}).get("buys", 0),
        "txns_5m_sells": pair.get("txns", {}).get("m5", {}).get("sells", 0),
        "txns_1h_buys": pair.get("txns", {}).get("h1", {}).get("buys", 0),
        "txns_1h_sells": pair.get("txns", {}).get("h1", {}).get("sells", 0),
        "liquidity_usd": pair.get("liquidity", {}).get("usd", 0),
        "fdv": pair.get("fdv", 0),
        "pair_created": pair.get("pairCreatedAt", 0),
    }
    
    # Calculate patterns
    patterns = []
    
    # Volume spike pattern
    vol_5m = analysis["volume_5m"] or 0
    vol_1h = analysis["volume_1h"] or 0
    if vol_1h > 0 and vol_5m > 0:
        # 5min volume vs hourly average (12 periods)
        avg_5m_vol = vol_1h / 12
        if avg_5m_vol > 0 and vol_5m > avg_5m_vol * 3:
            patterns.append(f"VOLUME_SPIKE: {vol_5m/avg_5m_vol:.1f}x")
    
    # Buy pressure pattern
    buys_5m = analysis["txns_5m_buys"] or 0
    sells_5m = analysis["txns_5m_sells"] or 0
    if buys_5m + sells_5m > 0:
        buy_ratio = buys_5m / (buys_5m + sells_5m)
        if buy_ratio > 0.7:
            patterns.append(f"BUY_PRESSURE: {buy_ratio*100:.0f}% buys")
    
    # Price momentum
    price_5m = analysis["price_change_5m"] or 0
    price_1h = analysis["price_change_1h"] or 0
    if price_5m > 10:
        patterns.append(f"PRICE_SPIKE_5M: +{price_5m:.1f}%")
    if price_1h > 50:
        patterns.append(f"PRICE_SPIKE_1H: +{price_1h:.1f}%")
    
    analysis["patterns"] = patterns
    
    return analysis


async def main():
    print("=" * 60)
    print("PUMP TOKEN ANALYSIS - Dexscreener Data")
    print("=" * 60)
    print()
    
    async with aiohttp.ClientSession() as session:
        for token in TOKENS:
            print(f"Fetching {token[:16]}...")
            data = await fetch_token_data(session, token)
            analysis = analyze_token(data, token)
            
            if "error" in analysis:
                print(f"  ❌ {analysis['error']}")
            else:
                print(f"\n📊 {analysis['symbol']} ({analysis['name']})")
                print(f"   DEX: {analysis['dex']}")
                print(f"   Price: ${analysis['price_usd']}")
                print(f"   Price Change: 5m={analysis['price_change_5m']}% | 1h={analysis['price_change_1h']}% | 24h={analysis['price_change_24h']}%")
                print(f"   Volume: 5m=${analysis['volume_5m']} | 1h=${analysis['volume_1h']} | 24h=${analysis['volume_24h']}")
                print(f"   Txns 5m: {analysis['txns_5m_buys']} buys / {analysis['txns_5m_sells']} sells")
                print(f"   Liquidity: ${analysis['liquidity_usd']}")
                print(f"   FDV: ${analysis['fdv']}")
                
                if analysis["patterns"]:
                    print(f"   🚀 PATTERNS: {', '.join(analysis['patterns'])}")
                else:
                    print(f"   ⚪ No active patterns")
            
            print("-" * 60)
            await asyncio.sleep(0.5)  # Rate limit


if __name__ == "__main__":
    asyncio.run(main())
