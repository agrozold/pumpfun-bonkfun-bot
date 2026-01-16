"""
Check whale activity for the last hour.
Проверяет сделки whale'ов из smart_money_wallets.json за последний час.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# Helius API (free tier: 100 req/day)
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
HELIUS_URL = f"https://api.helius.xyz/v0/addresses/{{address}}/transactions?api-key={HELIUS_API_KEY}"

# Pump.fun program
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


async def get_wallet_transactions(session: aiohttp.ClientSession, wallet: str, limit: int = 20):
    """Get recent transactions for a wallet."""
    url = HELIUS_URL.format(address=wallet) + f"&limit={limit}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"  Error fetching {wallet[:8]}...: {e}")
    return []


def is_pump_fun_buy(tx: dict) -> bool:
    """Check if transaction is a pump.fun buy."""
    # Check if pump.fun program is involved
    account_keys = tx.get("accountData", [])
    for acc in account_keys:
        if acc.get("account") == PUMP_FUN_PROGRAM:
            return True
    
    # Check instructions
    instructions = tx.get("instructions", [])
    for ix in instructions:
        if ix.get("programId") == PUMP_FUN_PROGRAM:
            return True
    
    return False


def format_time_ago(timestamp: int) -> str:
    """Format timestamp as time ago."""
    dt = datetime.fromtimestamp(timestamp)
    delta = datetime.now() - dt
    
    if delta.seconds < 60:
        return f"{delta.seconds}s ago"
    elif delta.seconds < 3600:
        return f"{delta.seconds // 60}m ago"
    else:
        return f"{delta.seconds // 3600}h {(delta.seconds % 3600) // 60}m ago"


async def check_whale_activity():
    """Check all whales for recent pump.fun activity."""
    # Load whale wallets
    wallets_file = Path("smart_money_wallets.json")
    if not wallets_file.exists():
        print("❌ smart_money_wallets.json not found")
        return
    
    with open(wallets_file) as f:
        data = json.load(f)
    
    whales = data.get("whales", [])
    print(f"📊 Checking {len(whales)} whale wallets for activity...\n")
    
    one_hour_ago = datetime.now() - timedelta(hours=1)
    one_hour_ts = int(one_hour_ago.timestamp())
    
    active_whales = []
    total_buys = 0
    
    async with aiohttp.ClientSession() as session:
        for i, whale in enumerate(whales):
            wallet = whale.get("wallet", "")
            label = whale.get("label", "whale")
            
            if not wallet:
                continue
            
            print(f"[{i+1}/{len(whales)}] Checking {wallet[:8]}... ({label})")
            
            txs = await get_wallet_transactions(session, wallet)
            
            recent_buys = []
            for tx in txs:
                ts = tx.get("timestamp", 0)
                if ts < one_hour_ts:
                    continue
                
                if is_pump_fun_buy(tx):
                    sig = tx.get("signature", "")[:16]
                    time_ago = format_time_ago(ts)
                    recent_buys.append((sig, time_ago, ts))
            
            if recent_buys:
                active_whales.append({
                    "wallet": wallet,
                    "label": label,
                    "buys": recent_buys
                })
                total_buys += len(recent_buys)
                print(f"  ✅ {len(recent_buys)} pump.fun buys in last hour!")
            else:
                print(f"  ⏸️  No recent activity")
            
            await asyncio.sleep(0.5)  # Rate limit
    
    # Summary
    print("\n" + "="*60)
    print(f"📈 SUMMARY: {len(active_whales)} active whales, {total_buys} total buys\n")
    
    if active_whales:
        print("🐋 ACTIVE WHALES (last hour):")
        for whale in active_whales:
            print(f"\n  {whale['wallet'][:16]}... ({whale['label']})")
            for sig, time_ago, _ in whale['buys'][:5]:
                print(f"    • {sig}... - {time_ago}")
    else:
        print("😴 No whale activity in the last hour")
        print("   Consider adding more active wallets to smart_money_wallets.json")


if __name__ == "__main__":
    if not HELIUS_API_KEY:
        print("❌ HELIUS_API_KEY not set in .env")
        print("   Get free API key at https://helius.xyz")
    else:
        asyncio.run(check_whale_activity())
