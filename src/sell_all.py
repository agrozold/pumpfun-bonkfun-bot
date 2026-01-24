#!/usr/bin/env python3
"""
Emergency sell all tokens - продать все позиции.
Использование: python src/sell_all.py [--dry-run]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.wallet import Wallet
from trading.fallback_seller import FallbackSeller
from trading.position import load_positions, remove_position
from utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)


async def sell_all_positions(dry_run: bool = False):
    """Sell all active positions."""

    # Load positions
    positions = load_positions()

    if not positions:
        print("No active positions to sell!")
        return

    print(f"\n{'='*60}")
    print(f"SELL ALL TOKENS - {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}")
    print(f"Found {len(positions)} active positions:\n")

    for i, pos in enumerate(positions, 1):
        print(f"  {i}. {pos.symbol} ({str(pos.mint)[:8]}...)")
        print(f"     Entry: {pos.entry_price:.10f} SOL")
        print(f"     Quantity: {pos.quantity:.2f}")
        print(f"     Platform: {pos.platform}")
        print()

    if dry_run:
        print("DRY RUN - no actual sells will be executed")
        return

    # Confirm
    confirm = input("\nType 'SELL' to confirm selling ALL positions: ")
    if confirm != "SELL":
        print("Cancelled.")
        return

    # Initialize components
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    jupiter_api_key = os.getenv("JUPITER_API_KEY")

    if not rpc_endpoint or not private_key:
        print("ERROR: Missing SOLANA_NODE_RPC_ENDPOINT or SOLANA_PRIVATE_KEY in .env")
        return

    client = SolanaClient(rpc_endpoint)
    wallet = Wallet(private_key)

    seller = FallbackSeller(
        client=client,
        wallet=wallet,
        slippage=0.35,  # Higher slippage for emergency sell
        priority_fee=500_000,
        max_retries=3,
        jupiter_api_key=jupiter_api_key,
    )

    print(f"\nSelling {len(positions)} positions...\n")

    success_count = 0
    fail_count = 0

    for pos in positions:
        print(f"Selling {pos.symbol} ({str(pos.mint)[:8]}...)...")

        try:
            # Try to get token balance
            token_balance = await client.get_token_balance(wallet.pubkey, pos.mint)

            if token_balance <= 0:
                print(f"  ⚠️  No balance for {pos.symbol}, removing from positions")
                remove_position(pos.mint)
                continue

            # Sell via Jupiter (works for all tokens)
            success, sig, error = await seller.sell_via_jupiter(
                mint=pos.mint,
                token_amount=token_balance,
                symbol=pos.symbol,
            )

            if success:
                print(f"  ✅ SOLD {pos.symbol} - TX: {sig}")
                remove_position(pos.mint)
                success_count += 1
            else:
                print(f"  ❌ FAILED {pos.symbol}: {error}")
                fail_count += 1

        except Exception as e:
            print(f"  ❌ ERROR {pos.symbol}: {e}")
            fail_count += 1

        # Small delay between sells
        await asyncio.sleep(1)

    print(f"\n{'='*60}")
    print(f"RESULTS: {success_count} sold, {fail_count} failed")
    print(f"{'='*60}\n")


def main():
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    asyncio.run(sell_all_positions(dry_run))


if __name__ == "__main__":
    main()
