"""
Periodic purchase_history cleanup - runs every 24 hours.
Removes entries older than 24h from purchased_tokens_history.json.
Also syncs trader._bought_tokens in-memory set.
"""

import asyncio
import json
import logging
import fcntl
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL = 86400  # 24 hours
MAX_AGE_SECONDS = 86400  # 24 hours
HISTORY_FILE = Path("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json")


async def run_periodic_purchase_cleanup():
    """Main purchase_history cleanup loop. Removes entries older than 24h."""
    logger.warning(f"[PURCHASE_CLEANUP] Periodic purchase_history cleanup scheduled (every {CLEANUP_INTERVAL // 3600}h, max age {MAX_AGE_SECONDS // 3600}h)")
    # FIX S28-5: Run first cleanup immediately on startup (was delayed 24h)

    while True:
        try:
            if not HISTORY_FILE.exists():
                logger.info("[PURCHASE_CLEANUP] No history file found, skipping")
                await asyncio.sleep(CLEANUP_INTERVAL)
                continue

            with open(HISTORY_FILE, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            tokens = data.get("purchased_tokens", {})
            total = len(tokens)
            cutoff = datetime.utcnow() - timedelta(seconds=MAX_AGE_SECONDS)
            cutoff_str = cutoff.isoformat()

            # Keep only entries newer than 24h
            fresh = {}
            removed = 0
            for mint, info in tokens.items():
                ts = info.get("timestamp", "")
                if ts and ts > cutoff_str:
                    fresh[mint] = info
                else:
                    removed += 1

            if removed > 0:
                data["purchased_tokens"] = fresh
                # Atomic write
                tmp = HISTORY_FILE.with_suffix('.tmp')
                with open(tmp, 'w') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(data, f, indent=2)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                tmp.rename(HISTORY_FILE)

                # Sync trader._bought_tokens in-memory
                try:
                    from trading.trader_registry import get_trader
                    trader = get_trader()
                    if trader and hasattr(trader, '_bought_tokens'):
                        trader._bought_tokens = set(fresh.keys())
                        logger.info(f"[PURCHASE_CLEANUP] Synced _bought_tokens in-memory: {len(fresh)} tokens")
                except Exception as sync_err:
                    logger.warning(f"[PURCHASE_CLEANUP] Could not sync _bought_tokens: {sync_err}")

                logger.warning(f"[PURCHASE_CLEANUP] Removed {removed} entries older than 24h (was {total}, now {len(fresh)})")
            else:
                logger.info(f"[PURCHASE_CLEANUP] Nothing to clean (all {total} entries are < 24h old)")
        except Exception as e:
            logger.error(f"[PURCHASE_CLEANUP] Error: {e}")

        await asyncio.sleep(CLEANUP_INTERVAL)


def start_periodic_purchase_cleanup():
    """Start periodic purchase_history cleanup task."""
    asyncio.create_task(run_periodic_purchase_cleanup())
