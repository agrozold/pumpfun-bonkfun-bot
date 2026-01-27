#!/usr/bin/env python3
"""
Emergency sell ALL tokens from wallet (Token + Token-2022).
Usage: python src/sell_all_wallet.py [--dry-run]
"""

import asyncio
import aiohttp
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from solders.pubkey import Pubkey

load_dotenv()

# Tokens to SKIP
SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# Both token programs
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def get_all_tokens(wallet_pubkey) -> list[dict]:
    """Get all token accounts (Token + Token-2022)."""
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    tokens = []

    async with aiohttp.ClientSession() as session:
        for program_id in [TOKEN_PROGRAM, TOKEN_2022_PROGRAM]:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(wallet_pubkey),
                    {"programId": program_id},
                    {"encoding": "jsonParsed"}
                ]
            }

            async with session.post(rpc_endpoint, json=payload) as resp:
                data = await resp.json()

            if "result" in data and "value" in data["result"]:
                for account in data["result"]["value"]:
                    info = account["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    balance = float(info["tokenAmount"]["uiAmount"] or 0)
                    raw_amount = int(info["tokenAmount"]["amount"])

                    if balance > 0 and mint not in SKIP_TOKENS:
                        tokens.append({
                            "mint": mint,
                            "balance": balance,
                            "raw_amount": raw_amount,
                            "program": "token-2022" if program_id == TOKEN_2022_PROGRAM else "token",
                        })

    return tokens


async def sell_all_wallet_tokens(dry_run: bool = False):
    """Sell all tokens from wallet."""
    from core.client import SolanaClient
    from core.wallet import Wallet
    from trading.fallback_seller import FallbackSeller

    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    jupiter_api_key = os.getenv("JUPITER_API_KEY")

    wallet = Wallet(private_key)
    client = SolanaClient(rpc_endpoint)

    print(f"\nWallet: {wallet.pubkey}")
    print("Fetching all token accounts (Token + Token-2022)...\n")

    tokens = await get_all_tokens(wallet.pubkey)

    if not tokens:
        print("No tokens to sell!")
        return

    print(f"{'='*70}")
    print(f"SELL ALL WALLET TOKENS - {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*70}")
    print(f"Found {len(tokens)} tokens with balance:\n")

    for i, t in enumerate(tokens, 1):
        print(f"  {i}. {t['mint']}")
        print(f"     Balance: {t['balance']:.4f} ({t['program']})")
        print()

    if dry_run:
        print("DRY RUN - no actual sells")
        return

    confirm = input("\nType 'SELL ALL' to confirm: ")
    if confirm != "SELL ALL":
        print("Cancelled.")
        return

    seller = FallbackSeller(
        client=client,
        wallet=wallet,
        slippage=0.5,
        priority_fee=500_000,
        max_retries=3,
        jupiter_api_key=jupiter_api_key,
    )

    print(f"\nSelling {len(tokens)} tokens...\n")

    success = 0
    failed = 0

    for t in tokens:
        mint = Pubkey.from_string(t["mint"])
        print(f"Selling {t['mint'][:20]}... ({t['balance']:.4f})")

        try:
            # Use the public sell method
            ok, sig, error = await seller.sell(
                mint=mint,
                token_amount=t["raw_amount"],  # Use raw amount (lamports)
                symbol=t["mint"][:8],
            )

            if ok:
                print(f"  ✅ SOLD - TX: {sig}")
                success += 1
            else:
                print(f"  ❌ FAILED: {error}")
                failed += 1
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            failed += 1

        await asyncio.sleep(2)

    print(f"\n{'='*70}")
    print(f"DONE: {success} sold, {failed} failed")
    print(f"{'='*70}\n")


def main():
    dry_run = "--dry-run" in sys.argv
    asyncio.run(sell_all_wallet_tokens(dry_run))


if __name__ == "__main__":
    main()
