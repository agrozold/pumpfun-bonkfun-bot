#!/usr/bin/env python3
"""
Cleanup zombie positions - removes positions that were actually sold.
Run this to sync positions.json with actual wallet state.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from core.client import SolanaClient
from core.wallet import Wallet
from solders.pubkey import Pubkey


async def cleanup_positions():
    """Remove positions where tokens are no longer in wallet."""
    
    positions_file = Path("positions.json")
    if not positions_file.exists():
        print("No positions.json found")
        return
    
    positions = json.loads(positions_file.read_text())
    print(f"\nFound {len(positions)} positions in file")
    
    # Init client
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    
    if not rpc or not pk:
        print("ERROR: Missing RPC or PRIVATE_KEY in .env")
        return
    
    client = SolanaClient(rpc)
    wallet = Wallet(pk)
    
    print(f"Wallet: {wallet.pubkey}\n")
    
    to_remove = []
    to_keep = []
    
    for pos in positions:
        mint_str = pos.get("mint")
        symbol = pos.get("symbol", "")
        is_active = pos.get("is_active", True)
        
        if not is_active:
            print(f"  [SKIP] {symbol} ({mint_str[:12]}...) - already closed")
            continue
        
        print(f"  Checking {symbol} ({mint_str[:16]}...)...", end=" ")
        
        try:
            mint = Pubkey.from_string(mint_str)
            balance = await client.get_token_account_balance(
                mint, wallet.pubkey, commitment="finalized"
            )
            
            if balance is None or balance <= 0.001:
                print(f"SOLD (balance: {balance or 0})")
                to_remove.append(pos)
            else:
                print(f"ACTIVE (balance: {balance:.2f})")
                to_keep.append(pos)
                
        except Exception as e:
            print(f"ERROR: {e}")
            # Keep on error to be safe
            to_keep.append(pos)
        
        await asyncio.sleep(0.3)  # Rate limit
    
    print(f"\n{'='*60}")
    print(f"SUMMARY:")
    print(f"  Active positions: {len(to_keep)}")
    print(f"  Sold (to remove): {len(to_remove)}")
    
    if to_remove:
        print(f"\nPositions to remove:")
        for pos in to_remove:
            print(f"  - {pos.get('symbol', '')} ({pos.get('mint', '')[:16]}...)")
        
        confirm = input("\nRemove sold positions? (yes/no): ")
        if confirm.lower() == "yes":
            # Backup
            backup = positions_file.with_suffix(f'.json.backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
            backup.write_text(positions_file.read_text())
            print(f"Backup saved to: {backup}")
            
            # Save only active
            positions_file.write_text(json.dumps(to_keep, indent=2))
            print(f"Saved {len(to_keep)} active positions")
        else:
            print("Cancelled")
    else:
        print("\nNo cleanup needed!")
    
    await client.close()


if __name__ == "__main__":
    asyncio.run(cleanup_positions())
