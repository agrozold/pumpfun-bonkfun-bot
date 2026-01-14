#!/usr/bin/env python3
"""
Find smart money wallets by analyzing early buyers of successful tokens.
Uses Solscan/Helius API to get transaction history.
"""

import asyncio
import os
import json
from collections import defaultdict
from datetime import datetime

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# Successful pump tokens to analyze
PUMP_TOKENS = [
    # Recent pumps
    ("jk1T35eWK41MBMM8AWoYVaNbjHEEQzMDetTsfnqpump", "SOL Trophy Tomato"),
    # Old successful ones
    ("61V8vBaqAGMpgDQi4JcAwo1dmBGHsyhzodcPqnEVpump", "ARC"),
    ("DKu9kykSfbN5LBfFXtNNDPaX35o4Fv6vJ9FKk7pZpump", "AVA"),
    # Cabal runners
    ("FT6ZnLbmaQbUmxbpe69qwRgPi9tU8QGY8S7gqt4Wbonk", "BIG"),
    ("CSrwNk6B1DwWCHRMsaoDVUfD5bBMQCJPY72ZG3Nnpump", "Franklin"),
]

# Known bot/exchange wallets to exclude
EXCLUDED_WALLETS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
}


class SmartWalletFinder:
    def __init__(self):
        self.helius_key = os.getenv("HELIUS_API_KEY", "")
        self.wallet_stats = defaultdict(lambda: {
            "tokens_bought": [],
            "early_buys": 0,
            "total_profit_estimate": 0,
        })
        
    async def get_token_transactions(
        self, session: aiohttp.ClientSession, mint: str, limit: int = 100
    ) -> list:
        """Get early transactions for a token using Helius API."""
        if not self.helius_key:
            # Fallback to public Solscan API
            return await self._get_solscan_transactions(session, mint, limit)
        
        url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions"
        params = {"api-key": self.helius_key, "limit": limit}
        
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                print(f"Helius error: {resp.status}")
                return []
        except Exception as e:
            print(f"Helius exception: {e}")
            return []
    
    async def _get_solscan_transactions(
        self, session: aiohttp.ClientSession, mint: str, limit: int
    ) -> list:
        """Fallback to Solscan public API."""
        url = f"https://api-v2.solscan.io/v2/token/transfer"
        params = {
            "address": mint,
            "page": 1,
            "page_size": min(limit, 50),
            "sort_by": "block_time",
            "sort_order": "asc",  # Earliest first
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                print(f"Solscan error: {resp.status}")
                return []
        except Exception as e:
            print(f"Solscan exception: {e}")
            return []

    async def get_dexscreener_trades(
        self, session: aiohttp.ClientSession, mint: str
    ) -> list:
        """Get recent trades from Dexscreener."""
        # First get pair address
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                if not data.get("pairs"):
                    return []
                
                pair = data["pairs"][0]
                pair_address = pair.get("pairAddress", "")
                
                # Get trades for this pair
                trades_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                async with session.get(trades_url) as trades_resp:
                    if trades_resp.status == 200:
                        trades_data = await trades_resp.json()
                        return trades_data.get("pair", {}).get("txns", {})
                    return []
        except Exception as e:
            print(f"Dexscreener exception: {e}")
            return []

    def analyze_transactions(self, transactions: list, token_name: str) -> list:
        """Extract early buyers from transactions."""
        early_buyers = []
        
        for i, tx in enumerate(transactions[:50]):  # First 50 transactions
            # Handle different API formats
            if isinstance(tx, dict):
                # Helius format
                if "feePayer" in tx:
                    wallet = tx.get("feePayer", "")
                    tx_type = tx.get("type", "")
                    if tx_type in ["SWAP", "TOKEN_MINT"] and wallet:
                        if wallet not in EXCLUDED_WALLETS:
                            early_buyers.append({
                                "wallet": wallet,
                                "position": i + 1,
                                "type": tx_type,
                            })
                # Solscan format
                elif "from_address" in tx:
                    wallet = tx.get("from_address", "")
                    if wallet and wallet not in EXCLUDED_WALLETS:
                        early_buyers.append({
                            "wallet": wallet,
                            "position": i + 1,
                            "type": "transfer",
                        })
        
        return early_buyers

    async def analyze_token(
        self, session: aiohttp.ClientSession, mint: str, name: str
    ):
        """Analyze a single token for early buyers."""
        print(f"\n📊 Analyzing {name} ({mint[:8]}...)")
        
        transactions = await self.get_token_transactions(session, mint)
        
        if not transactions:
            print(f"   ❌ No transactions found")
            return
        
        early_buyers = self.analyze_transactions(transactions, name)
        print(f"   Found {len(early_buyers)} early buyers")
        
        for buyer in early_buyers[:10]:  # Top 10
            wallet = buyer["wallet"]
            self.wallet_stats[wallet]["tokens_bought"].append(name)
            self.wallet_stats[wallet]["early_buys"] += 1
            print(f"   #{buyer['position']}: {wallet[:8]}...")

    async def find_smart_wallets(self) -> list:
        """Find wallets that appear in multiple successful tokens."""
        async with aiohttp.ClientSession() as session:
            for mint, name in PUMP_TOKENS:
                await self.analyze_token(session, mint, name)
                await asyncio.sleep(1)  # Rate limit
        
        # Find wallets with multiple hits
        smart_wallets = []
        for wallet, stats in self.wallet_stats.items():
            if stats["early_buys"] >= 2:  # Bought 2+ successful tokens early
                smart_wallets.append({
                    "wallet": wallet,
                    "tokens": stats["tokens_bought"],
                    "early_buys": stats["early_buys"],
                    "win_rate": 0.8,  # Estimate
                    "label": "smart_money",
                    "source": "pattern_analysis",
                    "added_date": datetime.utcnow().isoformat(),
                })
        
        # Sort by number of early buys
        smart_wallets.sort(key=lambda x: x["early_buys"], reverse=True)
        return smart_wallets

    def save_to_json(self, wallets: list, filepath: str = "smart_money_wallets.json"):
        """Save or merge with existing smart money wallets."""
        existing = {"whales": []}
        
        if os.path.exists(filepath):
            with open(filepath) as f:
                existing = json.load(f)
        
        existing_addresses = {w["wallet"] for w in existing.get("whales", [])}
        
        new_count = 0
        for wallet in wallets:
            if wallet["wallet"] not in existing_addresses:
                existing["whales"].append(wallet)
                new_count += 1
        
        with open(filepath, "w") as f:
            json.dump(existing, f, indent=2)
        
        return new_count


async def main():
    print("=" * 60)
    print("SMART WALLET FINDER")
    print("Analyzing early buyers of successful pump tokens")
    print("=" * 60)
    
    finder = SmartWalletFinder()
    smart_wallets = await finder.find_smart_wallets()
    
    print("\n" + "=" * 60)
    print("RESULTS - Smart Money Wallets")
    print("=" * 60)
    
    if not smart_wallets:
        print("❌ No smart wallets found (need more data or API access)")
        print("\nTip: Set HELIUS_API_KEY in .env for better results")
        return
    
    for wallet in smart_wallets:
        print(f"\n🐋 {wallet['wallet']}")
        print(f"   Early buys: {wallet['early_buys']}")
        print(f"   Tokens: {', '.join(wallet['tokens'])}")
    
    # Save to file
    new_count = finder.save_to_json(smart_wallets)
    print(f"\n✅ Added {new_count} new wallets to smart_money_wallets.json")


if __name__ == "__main__":
    asyncio.run(main())
