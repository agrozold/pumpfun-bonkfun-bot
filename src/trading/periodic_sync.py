"""
Periodic wallet synchronization - runs every 5 minutes.
Removes phantom positions that don't exist in wallet.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
import base58
from solders.keypair import Keypair

logger = logging.getLogger(__name__)

POSITIONS_FILE = Path("positions.json")
SYNC_INTERVAL = 300  # 5 minutes


async def get_wallet_tokens_for_sync(rpc: str, wallet: str) -> set:
    """Get all token mints in wallet."""
    mints = set()
    
    for prog_id in [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    ]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet,
                {"programId": prog_id},
                {"encoding": "jsonParsed"}
            ]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc, json=payload, timeout=30) as resp:
                    data = await resp.json()
            
            accounts = data.get("result", {}).get("value", [])
            
            for acc in accounts:
                try:
                    parsed = acc.get("account", {}).get("data", {}).get("parsed", {})
                    info = parsed.get("info", {})
                    mint = info.get("mint", "")
                    token_amount = info.get("tokenAmount", {})
                    ui_amount = float(token_amount.get("uiAmount") or 0)
                    
                    if ui_amount >= 1:
                        mints.add(mint)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[SYNC] RPC error: {e}")
    
    return mints


def load_positions_sync() -> list:
    if not POSITIONS_FILE.exists():
        return []
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_positions_sync(positions: list):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


async def run_periodic_sync():
    rpc = os.getenv("HELIUS_RPC_ENDPOINT") or os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    
    if not pk or not rpc:
        logger.error("[SYNC] Missing SOLANA_PRIVATE_KEY or RPC endpoint")
        return
    
    kp = Keypair.from_bytes(base58.b58decode(pk))
    wallet = str(kp.pubkey())
    
    logger.warning(f"[SYNC] Periodic sync started (every {SYNC_INTERVAL}s)")
    
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        
        try:
            logger.info("[SYNC] Running periodic wallet sync...")
            
            wallet_mints = await get_wallet_tokens_for_sync(rpc, wallet)
            positions = load_positions_sync()
            
            if not positions:
                logger.info("[SYNC] No positions to check")
                continue
            
            phantoms = []
            valid = []
            
            for pos in positions:
                mint = pos.get("mint", "")
                if mint in wallet_mints:
                    valid.append(pos)
                else:
                    phantoms.append(pos)
            
            if phantoms:
                logger.warning(f"[SYNC] Removing {len(phantoms)} PHANTOM positions:")
                for p in phantoms:
                    logger.warning(f"  - {p.get('symbol', '?')} ({p.get('mint', '')[:16]}...)")
                
                save_positions_sync(valid)
                logger.info(f"[SYNC] Saved {len(valid)} valid positions")
            else:
                logger.info(f"[SYNC] All {len(positions)} positions OK")
                
        except Exception as e:
            logger.error(f"[SYNC] Periodic sync error: {e}")


def start_periodic_sync():
    asyncio.create_task(run_periodic_sync())
    logger.warning("[SYNC] Periodic sync task scheduled (every 5 min)")
