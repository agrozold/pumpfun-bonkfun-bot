"""
Check who bought a specific token on pump.fun.
Проверяет кто покупал конкретный токен и сравнивает с whale списком.
"""

import asyncio
import json
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT") or os.getenv("ALCHEMY_RPC_ENDPOINT")


async def get_token_transactions(mint: str, limit: int = 100) -> list[dict]:
    """Get transactions for a token mint using RPC getSignaturesForAddress."""
    if not RPC_ENDPOINT:
        print("❌ No RPC endpoint configured")
        return []
    
    async with aiohttp.ClientSession() as session:
        # Get signatures
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [mint, {"limit": limit}]
        }
        
        async with session.post(RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                print(f"❌ RPC error: {resp.status}")
                return []
            data = await resp.json()
            signatures = data.get("result", [])
        
        print(f"📝 Found {len(signatures)} transactions for {mint[:16]}...")
        
        # Get transaction details
        transactions = []
        for i, sig_info in enumerate(signatures[:20]):  # Limit to first 20
            sig = sig_info.get("signature")
            if not sig:
                continue
            
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
            }
            
            async with session.post(RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result")
                    if result:
                        transactions.append({"signature": sig, "data": result})
            
            if (i + 1) % 5 == 0:
                print(f"  Fetched {i + 1}/{min(20, len(signatures))} transactions...")
            await asyncio.sleep(0.2)
        
        return transactions


def extract_buyer_info(tx: dict) -> dict | None:
    """Extract buyer wallet and SOL amount from transaction."""
    data = tx.get("data", {})
    meta = data.get("meta", {})
    message = data.get("transaction", {}).get("message", {})
    
    # Check for errors
    if meta.get("err"):
        return None
    
    # Get account keys
    account_keys = message.get("accountKeys", [])
    if not account_keys:
        return None
    
    # Fee payer is first account
    first_key = account_keys[0]
    fee_payer = first_key.get("pubkey", "") if isinstance(first_key, dict) else str(first_key)
    
    # Calculate SOL spent
    pre = meta.get("preBalances", [])
    post = meta.get("postBalances", [])
    sol_spent = (pre[0] - post[0]) / 1e9 if pre and post else 0
    
    # Check if it's a buy (positive SOL spent, got tokens)
    post_token_balances = meta.get("postTokenBalances", [])
    got_tokens = any(bal.get("owner") == fee_payer for bal in post_token_balances)
    
    if sol_spent > 0.001 and got_tokens:  # Minimum 0.001 SOL
        return {
            "wallet": fee_payer,
            "sol_spent": sol_spent,
            "signature": tx.get("signature", "")[:16],
            "block_time": data.get("blockTime", 0)
        }
    
    return None


async def check_token_buyers(mint: str):
    """Check all buyers of a token and compare with whale list."""
    # Load whale wallets
    wallets_file = Path("smart_money_wallets.json")
    whale_wallets = set()
    
    if wallets_file.exists():
        with open(wallets_file) as f:
            data = json.load(f)
        for whale in data.get("whales", []):
            wallet = whale.get("wallet", "")
            if wallet:
                whale_wallets.add(wallet)
        print(f"📋 Loaded {len(whale_wallets)} whale wallets\n")
    
    # Get transactions
    transactions = await get_token_transactions(mint)
    
    if not transactions:
        print("❌ No transactions found")
        return
    
    # Extract buyers
    buyers = []
    for tx in transactions:
        info = extract_buyer_info(tx)
        if info:
            info["is_whale"] = info["wallet"] in whale_wallets
            buyers.append(info)
    
    # Sort by SOL spent
    buyers.sort(key=lambda x: x["sol_spent"], reverse=True)
    
    # Print results
    print(f"\n{'='*70}")
    print(f"🔍 TOKEN BUYERS: {mint[:20]}...")
    print(f"{'='*70}\n")
    
    whale_buyers = [b for b in buyers if b["is_whale"]]
    non_whale_buyers = [b for b in buyers if not b["is_whale"]]
    
    if whale_buyers:
        print(f"🐋 WHALE BUYERS ({len(whale_buyers)}):")
        for b in whale_buyers:
            print(f"  ✅ {b['wallet'][:16]}... - {b['sol_spent']:.4f} SOL - {b['signature']}...")
    else:
        print("❌ NO WHALE BUYERS FOUND!")
        print("   This token was NOT bought by any tracked whale.")
    
    print(f"\n👤 OTHER BUYERS ({len(non_whale_buyers)}):")
    for b in non_whale_buyers[:10]:  # Top 10
        print(f"  • {b['wallet'][:16]}... - {b['sol_spent']:.4f} SOL")
    
    if non_whale_buyers:
        top_buyer = non_whale_buyers[0]
        print(f"\n💡 TOP NON-WHALE BUYER: {top_buyer['wallet']}")
        print(f"   Consider adding to smart_money_wallets.json if profitable")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python check_token_buyers.py <TOKEN_MINT>")
        print("Example: python check_token_buyers.py C32QN1pukEAEXfvmxYieME8brWnTsTzBMKHvDNhZBAGS")
        sys.exit(1)
    
    mint = sys.argv[1]
    asyncio.run(check_token_buyers(mint))
