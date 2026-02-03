#!/usr/bin/env python3
import json, asyncio, aiohttp, os
from datetime import datetime
from dotenv import load_dotenv

ZERION_BASE_URL = "https://api.zerion.io/v1"

class Analyzer:
    def __init__(self, api_key):
        self.headers = {"accept": "application/json", "authorization": f"Basic {api_key}"}
        self.results = []
    
    async def get_portfolio(self, session, wallet):
        try:
            url = f"{ZERION_BASE_URL}/wallets/{wallet}/portfolio"
            async with session.get(url, headers=self.headers, params={"currency": "usd"}) as r:
                if r.status == 200:
                    return (await r.json()).get("data", {}).get("attributes", {})
                if r.status == 429:
                    await asyncio.sleep(5)
                    return await self.get_portfolio(session, wallet)
        except:
            pass
        return {}
    
    async def get_txs(self, session, wallet):
        try:
            url = f"{ZERION_BASE_URL}/wallets/{wallet}/transactions"
            params = {"currency": "usd", "page[size]": 100, "filter[chain_ids]": "solana"}
            async with session.get(url, headers=self.headers, params=params) as r:
                if r.status == 200:
                    return (await r.json()).get("data", [])
                if r.status == 429:
                    await asyncio.sleep(5)
                    return await self.get_txs(session, wallet)
        except:
            pass
        return []
    
    def analyze_txs(self, txs):
        stats = {"buys": 0, "sells": 0, "bought": 0, "sold": 0, "quick_flips": 0}
        buy_times = {}
        for tx in txs:
            attrs = tx.get("attributes", {})
            for tr in attrs.get("transfers", []):
                d = tr.get("direction")
                v = float(tr.get("value", 0) or 0)
                sym = tr.get("fungible_info", {}).get("symbol", "X")
                if d == "in" and attrs.get("operation_type") == "trade":
                    stats["buys"] += 1
                    stats["bought"] += v
                    buy_times[sym] = attrs.get("mined_at")
                elif d == "out" and attrs.get("operation_type") == "trade":
                    stats["sells"] += 1
                    stats["sold"] += v
                    if sym in buy_times:
                        try:
                            bt = datetime.fromisoformat(buy_times[sym].replace("Z", "+00:00"))
                            st = datetime.fromisoformat(attrs.get("mined_at", "").replace("Z", "+00:00"))
                            if (st - bt).total_seconds() < 600:
                                stats["quick_flips"] += 1
                        except:
                            pass
        stats["pnl"] = stats["sold"] - stats["bought"]
        return stats
    
    async def analyze_whale(self, session, wallet, label):
        p = await self.get_portfolio(session, wallet)
        await asyncio.sleep(1.1)
        txs = await self.get_txs(session, wallet)
        await asyncio.sleep(1.1)
        s = self.analyze_txs(txs)
        return {"wallet": wallet, "label": label, "portfolio": p.get("total", {}).get("positions", 0), **s}
    
    async def run(self, whales):
        total = len(whales)
        print(f"\n🐋 Analyzing {total} whales (~{total * 2 // 60} min)...\n")
        async with aiohttp.ClientSession() as sess:
            for i, w in enumerate(whales):
                waddr = w.get("wallet", "")
                label = w.get("label", "?")
                print(f"  [{i + 1}/{total}] {label[:40]}")
                self.results.append(await self.analyze_whale(sess, waddr, label))
    
    def report(self):
        sr = sorted(self.results, key=lambda x: x.get("pnl", 0), reverse=True)
        print("\n" + "=" * 90)
        print(f"{'Label':<40} {'Portfolio':>10} {'Buys':>6} {'Sells':>6} {'PnL':>12} {'Flips':>6}")
        print("-" * 90)
        pumpers = []
        for r in sr:
            pnl = r.get("pnl", 0)
            ps = f"${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"
            pf = r.get("portfolio", 0)
            print(f"{r['label'][:38]:<40} ${pf:>8,.0f} {r.get('buys', 0):>6} {r.get('sells', 0):>6} {ps:>12} {r.get('quick_flips', 0):>6}")
            if r.get("quick_flips", 0) / max(r.get("sells", 1), 1) > 0.5 and r.get("sells", 0) > 3:
                pumpers.append(r)
        print("-" * 90)
        
        if pumpers:
            print("\n⚠️  PUMP & DUMPERS (remove these!):")
            for p in pumpers:
                print(f"   ❌ {p['label']:<35} {p['wallet']}")
        
        print("\n🏆 TOP 10 PROFITABLE:")
        for t in sr[:10]:
            if t.get("pnl", 0) > 0:
                print(f"   ✅ {t['label']:<35} ${t.get('pnl', 0):,.0f}")
        
        print("\n💸 WORST 10:")
        for t in sr[-10:]:
            if t.get("pnl", 0) < 0:
                print(f"   ❌ {t['label']:<35} ${t.get('pnl', 0):,.0f}")
        
        with open("whale_report.json", "w") as f:
            json.dump({"results": sr, "pumpers": [p["wallet"] for p in pumpers]}, f, indent=2)
        print("\n💾 Saved: whale_report.json")


async def main():
    load_dotenv()
    key = os.getenv("ZERION_API_KEY", "")
    if not key:
        print("No ZERION_API_KEY in .env")
        return
    
    with open("smart_money_wallets.json") as f:
        data = json.load(f)
    
    # Handle format: {"whales": [...]} or [...] or {addr: {...}, ...}
    if isinstance(data, dict) and "whales" in data:
        whales = data["whales"]
    elif isinstance(data, list):
        whales = data
    else:
        whales = [{"wallet": k, **v} for k, v in data.items()]
    
    print(f"Loaded {len(whales)} whales")
    a = Analyzer(key)
    await a.run(whales)
    a.report()


if __name__ == "__main__":
    asyncio.run(main())
