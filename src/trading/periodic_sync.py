"""
Periodic wallet synchronization - runs every 5 minutes.
UPGRADED: Redis integration + RPC failure protection.
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


def get_rpc_endpoints():
    endpoints = []
    if os.getenv("DRPC_RPC_ENDPOINT"):
        endpoints.append(os.getenv("DRPC_RPC_ENDPOINT"))
    if os.getenv("HELIUS_RPC_ENDPOINT"):
        endpoints.append(os.getenv("HELIUS_RPC_ENDPOINT"))
    if os.getenv("ALCHEMY_RPC_ENDPOINT"):
        endpoints.append(os.getenv("ALCHEMY_RPC_ENDPOINT"))
    if os.getenv("SOLANA_NODE_RPC_ENDPOINT"):
        endpoints.append(os.getenv("SOLANA_NODE_RPC_ENDPOINT"))
    return endpoints


async def get_wallet_tokens_for_sync(wallet: str) -> tuple[set, bool]:
    """Get wallet tokens. Returns (mints, success)."""
    mints = set()
    for rpc in get_rpc_endpoints():
        try:
            logger.info(f"[SYNC] Trying RPC: {rpc[:50]}...")
            success = False
            for prog_id in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
                payload = {
                    "jsonrpc": "2.0", 
                    "id": 1, 
                    "method": "getTokenAccountsByOwner", 
                    "params": [wallet, {"programId": prog_id}, {"encoding": "jsonParsed"}]
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(rpc, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        data = await resp.json()
                if "error" in data:
                    logger.error(f"[SYNC] RPC returned error: {data.get('error')}")
                    break
                accounts = data.get("result", {}).get("value", [])
                success = True
                for acc in accounts:
                    try:
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        mint = info.get("mint", "")
                        ui_amount = float(info.get("tokenAmount", {}).get("uiAmount") or 0)
                        if ui_amount >= 1:
                            mints.add(mint)
                    except Exception:
                        pass
            if success:
                logger.info(f"[SYNC] RPC success, found {len(mints)} tokens")
                return mints, True
        except asyncio.TimeoutError:
            logger.warning(f"[SYNC] RPC timeout: {rpc[:50]}...")
        except Exception as e:
            logger.error(f"[SYNC] RPC error: {e}")
    logger.error("[SYNC] All RPCs failed!")
    return mints, False


async def _get_redis_state():
    """Get Redis state manager."""
    try:
        from trading.redis_state import get_redis_state
        return await get_redis_state()
    except:
        return None


async def run_periodic_sync():
    """Main sync loop."""
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    if not pk:
        logger.error("[SYNC] Missing SOLANA_PRIVATE_KEY")
        return
    kp = Keypair.from_bytes(base58.b58decode(pk))
    wallet = str(kp.pubkey())
    logger.warning(f"[SYNC] Periodic sync started (every {SYNC_INTERVAL}s)")
    
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            logger.info("[SYNC] Running periodic wallet sync...")
            wallet_mints, rpc_success = await get_wallet_tokens_for_sync(wallet)
            
            if not rpc_success:
                logger.warning("[SYNC] RPC FAILED - skipping sync to PROTECT positions")
                continue
            
            # Get positions from Redis first, fallback to JSON
            state = await _get_redis_state()
            positions = []
            use_redis = False
            
            if state and await state.is_connected():
                data = await state.get_all_positions()
                if data:
                    positions = data
                    use_redis = True
                    logger.info(f"[SYNC] Loaded {len(positions)} positions from Redis")
            
            if not positions:
                # Fallback to JSON
                if POSITIONS_FILE.exists():
                    with open(POSITIONS_FILE) as f:
                        positions = json.load(f)
                    logger.info(f"[SYNC] Loaded {len(positions)} positions from JSON")
            
            if not positions:
                logger.info("[SYNC] No positions to check")
                continue
            
            phantoms = []
            valid = []
            
            # Get sold mints to skip
            from trading.redis_state import is_sold_mint
            
            for pos in positions:
                mint = pos.get("mint", "")
                # Skip sold tokens
                if await is_sold_mint(mint):
                    logger.info(f"[SYNC] Skipping SOLD: {pos.get('symbol', '?')} - already sold")
                    continue
                if mint in wallet_mints:
                    valid.append(pos)
                else:
                    # Grace period: don't remove positions younger than 60 seconds
                    # (transaction may not be confirmed yet)
                    entry_time_str = pos.get("entry_time", "")
                    if entry_time_str:
                        try:
                            from datetime import datetime
                            entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                            age_seconds = (datetime.now(entry_time.tzinfo) - entry_time).total_seconds()
                            if age_seconds < 60:
                                logger.info(f"[SYNC] {pos.get('symbol', '?')} is new ({age_seconds:.0f}s old), keeping despite no tokens on wallet")
                                valid.append(pos)
                                continue
                        except Exception as e:
                            logger.warning(f"[SYNC] Cannot parse entry_time: {e}")
                    phantoms.append(pos)
            
            if phantoms:
                logger.warning(f"[SYNC] Removing {len(phantoms)} PHANTOM positions:")
                for p in phantoms:
                    symbol = p.get('symbol', '?')
                    mint = p.get('mint', '')
                    logger.warning(f"  - {symbol} ({mint[:16]}...)")
                    
                    # Remove from Redis
                    if state and use_redis:
                        await state.remove_position(mint)
                
                # Save valid to JSON
                with open(POSITIONS_FILE, "w") as f:
                    json.dump(valid, f, indent=2)
                logger.info(f"[SYNC] Saved {len(valid)} valid positions")
            else:
                logger.info(f"[SYNC] All {len(positions)} positions OK")
            
            # Export Redis to JSON backup
            if state and use_redis:
                await state.export_to_json(str(POSITIONS_FILE))
                
        except Exception as e:
            logger.error(f"[SYNC] Periodic sync error: {e}")


def start_periodic_sync():
    """Start periodic sync task."""
    asyncio.create_task(run_periodic_sync())
    logger.warning("[SYNC] Periodic sync task scheduled (every 5 min)")
