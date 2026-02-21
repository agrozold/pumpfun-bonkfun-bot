"""
Periodic sold_mints cleanup - runs every 24 hours.
Removes entries older than 24h from Redis ZSET (score = timestamp).
Fresh entries (< 24h) are kept to protect wallet_sync from resurrecting sold tokens.
Active positions are additionally protected by _bought_tokens (in-memory) and positions (Redis+JSON).
"""

import asyncio
import logging
import time

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL = 86400  # 24 hours
SOLD_MINTS_KEY = "sold_mints"
MAX_AGE_SECONDS = 86400  # 24 hours


async def run_periodic_sold_cleanup():
    """Main sold_mints cleanup loop. Removes entries older than 24h."""
    logger.warning(f"[SOLD_CLEANUP] Periodic sold_mints cleanup scheduled (every {CLEANUP_INTERVAL // 3600}h, max age {MAX_AGE_SECONDS // 3600}h)")
    await asyncio.sleep(CLEANUP_INTERVAL)

    while True:
        try:
            r = aioredis.Redis()
            total = await r.zcard(SOLD_MINTS_KEY)
            cutoff = time.time() - MAX_AGE_SECONDS
            removed = await r.zremrangebyscore(SOLD_MINTS_KEY, 0, cutoff)
            remaining = await r.zcard(SOLD_MINTS_KEY)
            await r.aclose()

            if removed > 0:
                logger.warning(f"[SOLD_CLEANUP] Removed {removed} entries older than 24h (was {total}, now {remaining})")
            else:
                logger.info(f"[SOLD_CLEANUP] Nothing to clean (all {total} entries are < 24h old)")
        except Exception as e:
            logger.error(f"[SOLD_CLEANUP] Error: {e}")

        await asyncio.sleep(CLEANUP_INTERVAL)


def start_periodic_sold_cleanup():
    """Start periodic sold_mints cleanup task."""
    asyncio.create_task(run_periodic_sold_cleanup())
