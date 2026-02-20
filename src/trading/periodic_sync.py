"""
Periodic wallet synchronization - runs every 5 minutes.
UPGRADED: Redis integration + RPC failure protection + buy_confirmed auto-fix.
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


async def get_wallet_tokens_for_sync(wallet: str) -> tuple[dict, bool]:
    """Get wallet tokens with balances. Returns ({mint: ui_amount}, success)."""
    balances = {}
    for rpc in get_rpc_endpoints():
        try:
            logger.info(f"[SYNC] Trying RPC: {rpc.split(chr(47))[2]}...")
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
                        if mint:
                            balances[mint] = balances.get(mint, 0) + ui_amount
                    except Exception:
                        pass
            if success:
                wallet_mints = {m for m, amt in balances.items() if amt >= 1}
                logger.info(f"[SYNC] RPC success, found {len(wallet_mints)} tokens with balance>=1")
                return balances, True
        except asyncio.TimeoutError:
            logger.warning(f"[SYNC] RPC timeout: {rpc.split(chr(47))[2]}...")
        except Exception as e:
            logger.error(f"[SYNC] RPC error: {e}")
    logger.error("[SYNC] All RPCs failed!")
    return balances, False


async def _get_redis_state():
    """Get Redis state manager."""
    try:
        from trading.redis_state import get_redis_state
        return await get_redis_state()
    except Exception:
        return None


async def run_periodic_sync():
    """Main sync loop."""
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    if not pk:
        logger.error("[SYNC] Missing SOLANA_PRIVATE_KEY")
        return
    kp = Keypair.from_bytes(base58.b58decode(pk))
    wallet = str(kp.pubkey())
    logger.warning(f"[SYNC] Periodic balance sync started (every {SYNC_INTERVAL}s)")

    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            logger.info("[SYNC] Running periodic wallet balance sync...")
            balances, rpc_success = await get_wallet_tokens_for_sync(wallet)

            if not rpc_success:
                logger.warning("[SYNC] RPC FAILED - skipping sync to PROTECT positions")
                continue

            wallet_mints = {m for m, amt in balances.items() if amt >= 1}

            # Load positions from Redis first, fallback to JSON
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
                if POSITIONS_FILE.exists():
                    with open(POSITIONS_FILE) as f:
                        positions = json.load(f)
                    logger.info(f"[SYNC] Loaded {len(positions)} positions from JSON")

            if not positions:
                logger.info("[SYNC] No positions to check")
                continue

            from trading.redis_state import is_sold_mint, forget_position_forever
            from datetime import datetime, timezone

            phantoms = []
            valid = []
            fixed_confirmed = 0

            for pos in positions:
                mint = pos.get("mint", "")
                sym = pos.get("symbol", "?")

                # Skip already sold tokens - they will be dropped from valid list
                # FIX S12-6: Also kill zombie monitors for sold positions
                if await is_sold_mint(mint):
                    logger.info(f"[SYNC] Skipping SOLD zombie: {sym} - removing and killing monitor")
                    # Try to kill monitor task via trader registry
                    try:
                        from trading.trader_registry import get_trader
                        from trading.position import unregister_monitor
                        trader = get_trader()
                        if trader:
                            # Set is_active=False on position in trader.active_positions
                            for p in trader.active_positions:
                                if str(p.mint) == mint or p.get("mint", "") == mint if isinstance(p, dict) else False:
                                    if hasattr(p, 'is_active'):
                                        p.is_active = False
                            # Remove from active_positions list
                            trader.active_positions = [
                                p for p in trader.active_positions
                                if (str(p.mint) if hasattr(p, 'mint') else p.get("mint", "")) != mint
                            ]
                        unregister_monitor(mint)
                    except Exception as _e:
                        logger.debug(f"[SYNC] Could not kill monitor for {sym}: {_e}")
                    # Remove from Redis
                    if state and use_redis:
                        try:
                            await state.remove_position(mint)
                        except Exception:
                            pass
                    continue

                actual_balance = balances.get(mint, 0)

                if actual_balance >= 1:
                    # Tokens ARE on wallet
                    # Auto-fix buy_confirmed=False if tokens actually arrived
                    if not pos.get("buy_confirmed") or not pos.get("tokens_arrived"):
                        logger.warning(f"[SYNC] AUTO-FIX {sym}: tokens present ({actual_balance:.0f}), "
                                       f"setting buy_confirmed=True tokens_arrived=True")
                        pos["buy_confirmed"] = True
                        pos["tokens_arrived"] = True
                        pos["entry_price_source"] = pos.get("entry_price_source", "wallet_sync")
                        fixed_confirmed += 1
                    valid.append(pos)

                else:
                    # Zero balance - check grace period for new positions
                    entry_time_str = pos.get("entry_time", "")
                    age_seconds = 9999
                    if entry_time_str:
                        try:
                            entry_time_str_clean = entry_time_str.replace('Z', '+00:00')
                            entry_time = datetime.fromisoformat(entry_time_str_clean)
                            if entry_time.tzinfo is None:
                                now = datetime.utcnow()
                            else:
                                now = datetime.now(timezone.utc)
                            age_seconds = (now - entry_time).total_seconds()
                        except Exception as e:
                            logger.warning(f"[SYNC] Cannot parse entry_time for {sym}: {e}")

                    if age_seconds < 180:
                        logger.info(f"[SYNC] {sym} is new ({age_seconds:.0f}s), keeping despite 0 balance")
                        valid.append(pos)
                    else:
                        logger.warning(f"[SYNC] PHANTOM detected: {sym} ({mint[:16]}...) "
                                       f"- 0 balance on wallet, age={age_seconds:.0f}s")
                        phantoms.append(pos)

            if fixed_confirmed:
                logger.warning(f"[SYNC] Fixed buy_confirmed on {fixed_confirmed} positions")

            if phantoms:
                logger.warning(f"[SYNC] Removing {len(phantoms)} PHANTOM positions:")
                for p in phantoms:
                    psym = p.get('symbol', '?')
                    pmint = p.get('mint', '')
                    logger.warning(f"  PHANTOM: {psym} ({pmint[:16]}...) - no tokens on wallet")
                    # Add to sold_mints so it never comes back
                    await forget_position_forever(pmint, reason="phantom_zero_balance")
                    # Remove from Redis
                    if state and use_redis:
                        try:
                            await state.remove_position(pmint)
                        except Exception:
                            pass

            # Always save current valid positions back
            with open(POSITIONS_FILE, "w") as f:
                json.dump(valid, f, indent=2)

            logger.info(f"[SYNC] Done: {len(valid)} valid, {len(phantoms)} phantoms removed, "
                        f"{fixed_confirmed} buy_confirmed fixed")

            # Export Redis to JSON backup
            if state and use_redis:
                try:
                    await state.export_to_json(str(POSITIONS_FILE))
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[SYNC] Periodic sync error: {e}", exc_info=True)


def start_periodic_sync():
    """Start periodic sync task."""
    asyncio.create_task(run_periodic_sync())
    logger.warning("[SYNC] Periodic balance sync task scheduled (every 5 min)")
