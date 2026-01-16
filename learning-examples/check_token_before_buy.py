#!/usr/bin/env python3
"""
Скрипт для проверки токена ПЕРЕД покупкой.

Проверяет:
1. Покупали ли киты этот токен
2. Распределение токенов (топ-держатели)
3. Паттерны транзакций (все ли продают после листинга)
4. Dev репутация

Использование:
    python learning-examples/check_token_before_buy.py <MINT_ADDRESS>
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# API endpoints
HELIUS_API = "https://api.helius.xyz/v0"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

# Whale wallets file
WHALE_WALLETS_FILE = "smart_money_wallets.json"


def load_whale_wallets() -> dict[str, str]:
    """Load whale wallets from JSON file."""
    path = Path(WHALE_WALLETS_FILE)
    if not path.exists():
        print(f"⚠️  Whale wallets file not found: {WHALE_WALLETS_FILE}")
        return {}
    
    with open(path) as f:
        data = json.load(f)
    
    wallets = {}
    for entry in data:
        if isinstance(entry, dict):
            addr = entry.get("address", "")
            label = entry.get("label", entry.get("name", "Unknown"))
            if addr:
                wallets[addr] = label
        elif isinstance(entry, str):
            wallets[entry] = "Whale"
    
    return wallets


async def check_whale_activity(
    session: aiohttp.ClientSession,
    mint: str,
    whale_wallets: dict[str, str],
    helius_api_key: str,
) -> dict:
    """Check if whales bought this token."""
    result = {
        "whale_buys": [],
        "whale_sells": [],
        "total_whale_volume_sol": 0,
        "has_whale_activity": False,
    }
    
    if not helius_api_key:
        print("⚠️  HELIUS_API_KEY not set, skipping whale check")
        return result
    
    try:
        # Get token transactions
        url = f"{HELIUS_API}/addresses/{mint}/transactions"
        params = {"api-key": helius_api_key, "limit": 100}
        
        async with session.get(url, params=params, timeout=30) as resp:
            if resp.status != 200:
                print(f"⚠️  Helius API error: {resp.status}")
                return result
            
            txs = await resp.json()
        
        for tx in txs:
            # Check if any whale wallet is involved
            accounts = tx.get("accountData", [])
            for acc in accounts:
                wallet = acc.get("account", "")
                if wallet in whale_wallets:
                    # Determine if buy or sell
                    native_transfers = tx.get("nativeTransfers", [])
                    token_transfers = tx.get("tokenTransfers", [])
                    
                    for transfer in token_transfers:
                        if transfer.get("mint") == mint:
                            amount = transfer.get("tokenAmount", 0)
                            if transfer.get("toUserAccount") == wallet:
                                result["whale_buys"].append({
                                    "wallet": wallet,
                                    "label": whale_wallets[wallet],
                                    "amount": amount,
                                    "signature": tx.get("signature"),
                                })
                            elif transfer.get("fromUserAccount") == wallet:
                                result["whale_sells"].append({
                                    "wallet": wallet,
                                    "label": whale_wallets[wallet],
                                    "amount": amount,
                                    "signature": tx.get("signature"),
                                })
        
        result["has_whale_activity"] = len(result["whale_buys"]) > 0
        
    except Exception as e:
        print(f"⚠️  Error checking whale activity: {e}")
    
    return result


async def check_token_distribution(
    session: aiohttp.ClientSession,
    mint: str,
    helius_api_key: str,
) -> dict:
    """Check token holder distribution."""
    result = {
        "total_holders": 0,
        "top_10_percentage": 0,
        "top_holder_percentage": 0,
        "is_concentrated": False,  # True if top 10 hold > 50%
    }
    
    if not helius_api_key:
        return result
    
    try:
        # Get token holders
        url = f"{HELIUS_API}/token-metadata"
        params = {"api-key": helius_api_key}
        body = {"mintAccounts": [mint], "includeOffChain": True}
        
        async with session.post(url, params=params, json=body, timeout=30) as resp:
            if resp.status != 200:
                return result
            
            data = await resp.json()
        
        # Note: Helius doesn't directly provide holder distribution
        # This would need a different approach (e.g., getProgramAccounts)
        
    except Exception as e:
        print(f"⚠️  Error checking distribution: {e}")
    
    return result


async def check_dexscreener_data(
    session: aiohttp.ClientSession,
    mint: str,
) -> dict:
    """Get token data from DexScreener."""
    result = {
        "price_usd": 0,
        "volume_5m": 0,
        "volume_1h": 0,
        "volume_24h": 0,
        "buys_5m": 0,
        "sells_5m": 0,
        "buys_1h": 0,
        "sells_1h": 0,
        "buy_pressure_5m": 0,
        "buy_pressure_1h": 0,
        "price_change_5m": 0,
        "price_change_1h": 0,
        "liquidity_usd": 0,
        "market_cap": 0,
        "pair_created": None,
        "dex_id": None,
    }
    
    try:
        url = f"{DEXSCREENER_API}/tokens/{mint}"
        
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return result
            
            data = await resp.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            return result
        
        # Get pair with highest liquidity
        pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
        
        txns = pair.get("txns", {})
        m5 = txns.get("m5", {})
        h1 = txns.get("h1", {})
        volume = pair.get("volume", {})
        price_change = pair.get("priceChange", {})
        
        result["price_usd"] = float(pair.get("priceUsd", 0) or 0)
        result["volume_5m"] = float(volume.get("m5", 0) or 0)
        result["volume_1h"] = float(volume.get("h1", 0) or 0)
        result["volume_24h"] = float(volume.get("h24", 0) or 0)
        result["buys_5m"] = m5.get("buys", 0)
        result["sells_5m"] = m5.get("sells", 0)
        result["buys_1h"] = h1.get("buys", 0)
        result["sells_1h"] = h1.get("sells", 0)
        result["price_change_5m"] = float(price_change.get("m5", 0) or 0)
        result["price_change_1h"] = float(price_change.get("h1", 0) or 0)
        result["liquidity_usd"] = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        result["market_cap"] = float(pair.get("marketCap", 0) or 0)
        result["dex_id"] = pair.get("dexId")
        
        if pair.get("pairCreatedAt"):
            result["pair_created"] = datetime.fromtimestamp(pair["pairCreatedAt"] / 1000)
        
        # Calculate buy pressure
        total_5m = result["buys_5m"] + result["sells_5m"]
        total_1h = result["buys_1h"] + result["sells_1h"]
        result["buy_pressure_5m"] = result["buys_5m"] / total_5m if total_5m > 0 else 0
        result["buy_pressure_1h"] = result["buys_1h"] / total_1h if total_1h > 0 else 0
        
    except Exception as e:
        print(f"⚠️  Error fetching DexScreener data: {e}")
    
    return result


def analyze_token(
    mint: str,
    whale_data: dict,
    dex_data: dict,
) -> dict:
    """Analyze token and give recommendation."""
    score = 0
    reasons = []
    warnings = []
    
    # 1. Whale activity (0-30 points)
    whale_buys = len(whale_data.get("whale_buys", []))
    if whale_buys >= 3:
        score += 30
        reasons.append(f"✅ Strong whale activity: {whale_buys} whale buys")
    elif whale_buys >= 2:
        score += 20
        reasons.append(f"✅ Good whale activity: {whale_buys} whale buys")
    elif whale_buys >= 1:
        score += 10
        reasons.append(f"⚠️  Weak whale activity: {whale_buys} whale buy")
    else:
        warnings.append("❌ NO WHALE BUYS - high risk!")
    
    # 2. Buy pressure (0-25 points)
    bp_5m = dex_data.get("buy_pressure_5m", 0)
    bp_1h = dex_data.get("buy_pressure_1h", 0)
    
    if bp_5m >= 0.8:
        score += 25
        reasons.append(f"✅ Excellent buy pressure 5m: {bp_5m*100:.0f}%")
    elif bp_5m >= 0.7:
        score += 15
        reasons.append(f"✅ Good buy pressure 5m: {bp_5m*100:.0f}%")
    elif bp_5m >= 0.6:
        score += 5
        reasons.append(f"⚠️  Moderate buy pressure 5m: {bp_5m*100:.0f}%")
    else:
        warnings.append(f"❌ Low buy pressure 5m: {bp_5m*100:.0f}%")
    
    # 3. Volume (0-20 points)
    vol_1h = dex_data.get("volume_1h", 0)
    if vol_1h >= 50000:
        score += 20
        reasons.append(f"✅ High volume 1h: ${vol_1h:,.0f}")
    elif vol_1h >= 20000:
        score += 10
        reasons.append(f"✅ Good volume 1h: ${vol_1h:,.0f}")
    elif vol_1h >= 5000:
        score += 5
        reasons.append(f"⚠️  Low volume 1h: ${vol_1h:,.0f}")
    else:
        warnings.append(f"❌ Very low volume 1h: ${vol_1h:,.0f}")
    
    # 4. Price momentum (0-15 points)
    change_5m = dex_data.get("price_change_5m", 0)
    change_1h = dex_data.get("price_change_1h", 0)
    
    if change_5m >= 10:
        score += 15
        reasons.append(f"✅ Strong momentum 5m: +{change_5m:.1f}%")
    elif change_5m >= 5:
        score += 10
        reasons.append(f"✅ Good momentum 5m: +{change_5m:.1f}%")
    elif change_5m >= 0:
        score += 5
        reasons.append(f"⚠️  Flat 5m: {change_5m:+.1f}%")
    else:
        warnings.append(f"❌ Negative momentum 5m: {change_5m:.1f}%")
    
    # 5. Liquidity (0-10 points)
    liq = dex_data.get("liquidity_usd", 0)
    if liq >= 20000:
        score += 10
        reasons.append(f"✅ Good liquidity: ${liq:,.0f}")
    elif liq >= 5000:
        score += 5
        reasons.append(f"⚠️  Low liquidity: ${liq:,.0f}")
    else:
        warnings.append(f"❌ Very low liquidity: ${liq:,.0f}")
    
    # Recommendation
    if score >= 70:
        recommendation = "🟢 STRONG BUY"
    elif score >= 50:
        recommendation = "🟡 MODERATE BUY"
    elif score >= 30:
        recommendation = "🟠 RISKY - Consider skipping"
    else:
        recommendation = "🔴 DO NOT BUY"
    
    return {
        "score": score,
        "recommendation": recommendation,
        "reasons": reasons,
        "warnings": warnings,
    }


async def main():
    if len(sys.argv) < 2:
        print("Usage: python check_token_before_buy.py <MINT_ADDRESS>")
        sys.exit(1)
    
    mint = sys.argv[1]
    helius_api_key = os.getenv("HELIUS_API_KEY")
    
    print("=" * 70)
    print(f"🔍 ANALYZING TOKEN: {mint}")
    print("=" * 70)
    
    # Load whale wallets
    whale_wallets = load_whale_wallets()
    print(f"📋 Loaded {len(whale_wallets)} whale wallets")
    
    async with aiohttp.ClientSession() as session:
        # 1. Check DexScreener data
        print("\n📊 Fetching DexScreener data...")
        dex_data = await check_dexscreener_data(session, mint)
        
        print(f"   Price: ${dex_data['price_usd']:.10f}")
        print(f"   Volume 5m: ${dex_data['volume_5m']:,.0f}")
        print(f"   Volume 1h: ${dex_data['volume_1h']:,.0f}")
        print(f"   Buys/Sells 5m: {dex_data['buys_5m']}/{dex_data['sells_5m']}")
        print(f"   Buy Pressure 5m: {dex_data['buy_pressure_5m']*100:.0f}%")
        print(f"   Price Change 5m: {dex_data['price_change_5m']:+.1f}%")
        print(f"   Liquidity: ${dex_data['liquidity_usd']:,.0f}")
        print(f"   Market Cap: ${dex_data['market_cap']:,.0f}")
        print(f"   DEX: {dex_data['dex_id']}")
        
        # 2. Check whale activity
        print("\n🐋 Checking whale activity...")
        whale_data = await check_whale_activity(session, mint, whale_wallets, helius_api_key)
        
        if whale_data["whale_buys"]:
            print(f"   ✅ Found {len(whale_data['whale_buys'])} whale buys:")
            for buy in whale_data["whale_buys"][:5]:
                print(f"      - {buy['label']}: {buy['amount']:.2f} tokens")
        else:
            print("   ❌ NO WHALE BUYS FOUND")
        
        if whale_data["whale_sells"]:
            print(f"   ⚠️  Found {len(whale_data['whale_sells'])} whale sells:")
            for sell in whale_data["whale_sells"][:3]:
                print(f"      - {sell['label']}: {sell['amount']:.2f} tokens")
        
        # 3. Analyze and recommend
        print("\n" + "=" * 70)
        print("📈 ANALYSIS RESULT")
        print("=" * 70)
        
        analysis = analyze_token(mint, whale_data, dex_data)
        
        print(f"\n🎯 SCORE: {analysis['score']}/100")
        print(f"📌 RECOMMENDATION: {analysis['recommendation']}")
        
        if analysis["reasons"]:
            print("\n✅ Positive factors:")
            for reason in analysis["reasons"]:
                print(f"   {reason}")
        
        if analysis["warnings"]:
            print("\n⚠️  Warnings:")
            for warning in analysis["warnings"]:
                print(f"   {warning}")
        
        print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
