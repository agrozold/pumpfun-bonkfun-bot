"""Check whale activity via Zerion API (1 req/sec limit)"""
import asyncio
import json
import os
import aiohttp
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

ZERION_API_KEY = os.getenv("ZERION_API_KEY")

async def check_wallet(session, wallet: str, label: str):
    """Проверить активность кошелька через Zerion"""
    url = f"https://api.zerion.io/v1/wallets/{wallet}/transactions/"
    headers = {
        "accept": "application/json",
        "authorization": f"Basic {ZERION_API_KEY}"
    }
    params = {
        "filter[chain_ids]": "solana",
        "page[size]": 5
    }
    
    try:
        async with session.get(url, headers=headers, params=params, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                txs = data.get("data", [])
                if txs:
                    recent = txs[0]
                    attrs = recent.get("attributes", {})
                    mined_at = attrs.get("mined_at")
                    tx_type = attrs.get("operation_type", "unknown")
                    return {"wallet": wallet, "label": label, "last_tx": mined_at, "type": tx_type, "count": len(txs)}
                return {"wallet": wallet, "label": label, "last_tx": None, "type": "no_txs", "count": 0}
            elif resp.status == 429:
                return {"wallet": wallet, "label": label, "error": "rate_limit"}
            else:
                return {"wallet": wallet, "label": label, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"wallet": wallet, "label": label, "error": str(e)[:30]}

async def main():
    with open("smart_money_wallets.json") as f:
        data = json.load(f)
    
    whales = data.get("whales", [])
    total = len(whales)
    
    print(f"🐋 Проверяем {total} китов через Zerion API")
    print(f"⏱️  Rate limit: 1 req/sec = ~{total} секунд")
    print("=" * 80)
    
    active_1h = []
    active_24h = []
    inactive = []
    errors = []
    now = datetime.utcnow()
    
    async with aiohttp.ClientSession() as session:
        for i, w in enumerate(whales, 1):
            wallet = w.get("wallet")
            label = w.get("label", "whale")
            
            result = await check_wallet(session, wallet, label)
            
            if "error" in result:
                errors.append(result)
                print(f"[{i:3}/{total}] ❌ {label[:20]:20} | {result['error']}")
            elif result.get("last_tx"):
                try:
                    tx_time = datetime.fromisoformat(result["last_tx"].replace("Z", ""))
                    age = now - tx_time
                    hours = age.total_seconds() / 3600
                    
                    if hours < 1:
                        active_1h.append(result)
                        print(f"[{i:3}/{total}] 🔥 {label[:20]:20} | {result['type']:12} | {hours*60:.0f} min ago")
                    elif hours < 24:
                        active_24h.append(result)
                        print(f"[{i:3}/{total}] ✅ {label[:20]:20} | {result['type']:12} | {hours:.1f}h ago")
                    else:
                        inactive.append(result)
                        print(f"[{i:3}/{total}] ⏸️  {label[:20]:20} | {result['type']:12} | {hours/24:.1f}d ago")
                except Exception as e:
                    print(f"[{i:3}/{total}] ⚠️  {label[:20]:20} | parse error")
            else:
                inactive.append(result)
                print(f"[{i:3}/{total}] 💤 {label[:20]:20} | no transactions")
            
            # Rate limit: 1 request per second
            await asyncio.sleep(1.1)
    
    # Summary
    print("\n" + "=" * 80)
    print("📊 ИТОГО:")
    print(f"   🔥 Активны < 1 час:  {len(active_1h)}")
    print(f"   ✅ Активны < 24 час: {len(active_24h)}")
    print(f"   ⏸️  Неактивны:        {len(inactive)}")
    print(f"   ❌ Ошибки:           {len(errors)}")
    
    if active_1h:
        print("\n🔥 АКТИВНЫЕ ПРЯМО СЕЙЧАС:")
        for r in active_1h:
            print(f"   {r['wallet'][:20]}... | {r['label']} | {r['type']}")

if __name__ == "__main__":
    asyncio.run(main())
