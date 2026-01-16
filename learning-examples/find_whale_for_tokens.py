"""
Find which whales bought specific tokens.
Находит каких китов из smart_money_wallets.json купили указанные токены.

Usage:
    uv run learning-examples/find_whale_for_tokens.py <TOKEN1> <TOKEN2> ...
    
Example:
    uv run learning-examples/find_whale_for_tokens.py DA99T15RJoZhmcDD7o6xkHiMPfiQaAMFf7AYrSw8pump HrZa1wiBbYoUd5MbhFmkgVLYkcjhQuSbi65A8rC5pump
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT") or os.getenv("ALCHEMY_RPC_ENDPOINT")

# Helius Enhanced API for parsed transactions
HELIUS_PARSE_TX_URL = f"https://api-mainnet.helius-rpc.com/v0/transactions/?api-key={HELIUS_API_KEY}"


async def get_token_signatures(session: aiohttp.ClientSession, mint: str, limit: int = 100) -> list[str]:
    """Get transaction signatures for a token mint."""
    if not RPC_ENDPOINT:
        print("❌ No RPC endpoint configured")
        return []
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [mint, {"limit": limit}]
    }
    
    try:
        async with session.post(RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                print(f"❌ RPC error: {resp.status}")
                return []
            data = await resp.json()
            signatures = [s.get("signature") for s in data.get("result", []) if s.get("signature")]
            return signatures
    except Exception as e:
        print(f"❌ Error getting signatures: {e}")
        return []


async def get_transaction_details(session: aiohttp.ClientSession, signature: str) -> dict | None:
    """Get transaction details using RPC."""
    if not RPC_ENDPOINT:
        return None
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
    }
    
    try:
        async with session.post(RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result")
    except Exception as e:
        pass
    return None


def extract_buyer_from_tx(tx: dict) -> dict | None:
    """Extract buyer wallet and SOL amount from transaction."""
    if not tx:
        return None
    
    meta = tx.get("meta", {})
    message = tx.get("transaction", {}).get("message", {})
    
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
            "block_time": tx.get("blockTime", 0)
        }
    
    return None


async def find_whales_for_token(
    session: aiohttp.ClientSession,
    mint: str,
    whale_wallets: dict[str, dict],
    max_txs: int = 50
) -> list[dict]:
    """Find which whales bought a specific token."""
    print(f"\n🔍 Analyzing token: {mint}")
    
    # Get signatures
    signatures = await get_token_signatures(session, mint, limit=max_txs)
    if not signatures:
        print(f"  ❌ No transactions found")
        return []
    
    print(f"  📝 Found {len(signatures)} transactions, analyzing...")
    
    whale_buys = []
    
    for i, sig in enumerate(signatures):
        tx = await get_transaction_details(session, sig)
        if not tx:
            continue
        
        buyer = extract_buyer_from_tx(tx)
        if buyer and buyer["wallet"] in whale_wallets:
            whale_info = whale_wallets[buyer["wallet"]]
            whale_buys.append({
                "wallet": buyer["wallet"],
                "label": whale_info.get("label", "whale"),
                "sol_spent": buyer["sol_spent"],
                "block_time": buyer["block_time"],
                "signature": sig,
                "source": whale_info.get("source", "unknown"),
                "win_rate": whale_info.get("win_rate", 0.0),
            })
        
        # Rate limit
        if (i + 1) % 10 == 0:
            print(f"  Processed {i + 1}/{len(signatures)} transactions...")
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(0.1)
    
    return whale_buys


def format_time(timestamp: int) -> str:
    """Format unix timestamp to readable string."""
    if not timestamp:
        return "unknown"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


async def main(token_mints: list[str]):
    """Main function to find whales for multiple tokens."""
    # Load whale wallets
    wallets_file = Path("smart_money_wallets.json")
    if not wallets_file.exists():
        print("❌ smart_money_wallets.json not found")
        return
    
    with open(wallets_file) as f:
        data = json.load(f)
    
    whale_wallets = {}
    for whale in data.get("whales", []):
        wallet = whale.get("wallet", "")
        if wallet:
            whale_wallets[wallet] = whale
    
    print(f"📋 Loaded {len(whale_wallets)} whale wallets")
    print(f"🎯 Analyzing {len(token_mints)} tokens\n")
    print("=" * 80)
    
    results = {}
    
    async with aiohttp.ClientSession() as session:
        for mint in token_mints:
            whale_buys = await find_whales_for_token(session, mint, whale_wallets)
            results[mint] = whale_buys
            
            if whale_buys:
                print(f"\n  🐋 WHALE BUYERS FOUND ({len(whale_buys)}):")
                for buy in sorted(whale_buys, key=lambda x: x["sol_spent"], reverse=True):
                    print(f"    ✅ {buy['wallet'][:20]}...")
                    print(f"       Label: {buy['label']}")
                    print(f"       SOL: {buy['sol_spent']:.4f}")
                    print(f"       Win Rate: {buy['win_rate']*100:.0f}%")
                    print(f"       Source: {buy['source']}")
                    print(f"       Time: {format_time(buy['block_time'])}")
                    print(f"       TX: {buy['signature'][:20]}...")
                    print()
            else:
                print(f"  ❌ NO WHALE BUYERS - token was NOT bought by tracked whales")
            
            print("-" * 80)
    
    # Summary
    print("\n" + "=" * 80)
    print("📊 SUMMARY")
    print("=" * 80)
    
    tokens_with_whales = [m for m, buys in results.items() if buys]
    tokens_without_whales = [m for m, buys in results.items() if not buys]
    
    print(f"\n✅ Tokens bought by whales ({len(tokens_with_whales)}):")
    for mint in tokens_with_whales:
        buys = results[mint]
        whale_names = [b["label"] for b in buys]
        total_sol = sum(b["sol_spent"] for b in buys)
        print(f"  • {mint[:20]}... - {len(buys)} whales, {total_sol:.4f} SOL total")
        print(f"    Whales: {', '.join(set(whale_names))}")
    
    print(f"\n❌ Tokens NOT bought by whales ({len(tokens_without_whales)}):")
    for mint in tokens_without_whales:
        print(f"  • {mint}")
    
    # Unique whales across all tokens
    all_whale_wallets = set()
    for buys in results.values():
        for buy in buys:
            all_whale_wallets.add(buy["wallet"])
    
    if all_whale_wallets:
        print(f"\n🐋 Unique whales that bought these tokens ({len(all_whale_wallets)}):")
        for wallet in all_whale_wallets:
            info = whale_wallets.get(wallet, {})
            print(f"  • {wallet}")
            print(f"    Label: {info.get('label', 'unknown')}, Win Rate: {info.get('win_rate', 0)*100:.0f}%")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python find_whale_for_tokens.py <TOKEN1> <TOKEN2> ...")
        print("\nExample tokens from your request:")
        print("  DA99T15RJoZhmcDD7o6xkHiMPfiQaAMFf7AYrSw8pump")
        print("  HrZa1wiBbYoUd5MbhFmkgVLYkcjhQuSbi65A8rC5pump")
        print("  A1CZv2y9aJ8HJSKB2TKPdJuetyCvTv7pjQTSToeWpump")
        print("  8D1ENR6NrAGLonqiFZoFjqipU9nM3LhH5tU7gYVypump")
        print("  H1eSxJFXJoUsysZnR8KSSykz3iqdjXhWMoFVnWJdpump")
        print("  BKGoAvnFgiuwPcg9Bw5uTRrRLQxUjwprV9HJHdkFpump")
        print("  FVM2LDs9QfAyWSNLDxMTjWD3CAh5SNRWAenZBmK9pump")
        sys.exit(1)
    
    tokens = sys.argv[1:]
    asyncio.run(main(tokens))
