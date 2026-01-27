"""
Global Blockhash Cache - high-performance caching for sniper bots.

Provides sub-second blockhash access by maintaining a background
update loop. Critical for sniper bots where every millisecond counts.

Usage:
    from core.blockhash_cache import get_blockhash_cache

    cache = await get_blockhash_cache()
    blockhash = await cache.get_blockhash()  # Returns cached or fresh
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solders.hash import Hash

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CachedBlockhash:
    """Blockhash with timestamp for freshness checking."""
    hash: Hash
    timestamp: float
    slot: int = 0

    @property
    def age_seconds(self) -> float:
        """How old is this blockhash in seconds."""
        return time.time() - self.timestamp

    def is_fresh(self, max_age: float = 2.0) -> bool:
        """Check if blockhash is still fresh enough to use."""
        return self.age_seconds < max_age


class BlockhashCache:
    """
    High-performance blockhash cache with background updates.

    Features:
    - Background update every 1-2 seconds
    - Automatic fallback to fresh fetch if cache is stale
    - Thread-safe with asyncio.Lock
    - Singleton pattern for global access
    """

    _instance: Optional["BlockhashCache"] = None
    _lock = asyncio.Lock()

    # Configuration
    UPDATE_INTERVAL = 30.0  # seconds between updates
    MAX_CACHE_AGE = 30.0    # seconds before cache is considered stale
    BLOCKHASH_VALIDITY = 60  # Solana blockhash valid for ~60-90 seconds

    def __init__(self):
        self._cached: Optional[CachedBlockhash] = None
        self._update_lock = asyncio.Lock()
        self._updater_task: Optional[asyncio.Task] = None
        self._running = False
        self._rpc_endpoint: Optional[str] = None
        self._client: Optional[AsyncClient] = None

        # Metrics
        self._metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "fallback_fetches": 0,
            "update_errors": 0,
            "total_updates": 0,
        }

    @classmethod
    async def get_instance(cls, rpc_endpoint: Optional[str] = None) -> "BlockhashCache":
        """Get singleton instance of BlockhashCache."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = BlockhashCache()
                    if rpc_endpoint:
                        await cls._instance.initialize(rpc_endpoint)
        elif rpc_endpoint and cls._instance._rpc_endpoint != rpc_endpoint:
            await cls._instance.initialize(rpc_endpoint)
        return cls._instance

    async def initialize(self, rpc_endpoint: str) -> None:
        """Initialize the cache with RPC endpoint and start background updater."""
        self._rpc_endpoint = rpc_endpoint

        if self._client:
            await self._client.close()

        self._client = AsyncClient(rpc_endpoint)
        await self._fetch_and_cache()

        if not self._running:
            await self.start()

        logger.info(f"[BlockhashCache] Initialized (interval: {self.UPDATE_INTERVAL}s)")

    async def start(self) -> None:
        """Start the background update loop."""
        if self._running:
            return

        self._running = True
        self._updater_task = asyncio.create_task(self._update_loop())
        logger.info("[BlockhashCache] Background updater started")

    async def stop(self) -> None:
        """Stop the background update loop and cleanup."""
        self._running = False

        if self._updater_task:
            self._updater_task.cancel()
            try:
                await self._updater_task
            except asyncio.CancelledError:
                pass
            self._updater_task = None

        if self._client:
            await self._client.close()
            self._client = None

        logger.info("[BlockhashCache] Stopped")

    async def _update_loop(self) -> None:
        """Background loop that continuously updates the blockhash."""
        while self._running:
            try:
                await self._fetch_and_cache()
                self._metrics["total_updates"] += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._metrics["update_errors"] += 1
                logger.warning(f"[BlockhashCache] Update failed: {e}")

            await asyncio.sleep(self.UPDATE_INTERVAL)

    async def _fetch_and_cache(self) -> CachedBlockhash:
        """Fetch fresh blockhash from RPC and cache it."""
        if not self._client:
            raise RuntimeError("BlockhashCache not initialized")

        async with self._update_lock:
            response = await self._client.get_latest_blockhash(commitment="processed")

            self._cached = CachedBlockhash(
                hash=response.value.blockhash,
                timestamp=time.time(),
                slot=response.context.slot if hasattr(response, 'context') else 0,
            )
            return self._cached

    async def get_blockhash(self, max_age: float = None) -> Hash:
        """
        Get blockhash - from cache if fresh, otherwise fetch new one.

        Args:
            max_age: Maximum acceptable cache age in seconds (default: MAX_CACHE_AGE)

        Returns:
            Valid blockhash Hash object
        """
        if max_age is None:
            max_age = self.MAX_CACHE_AGE

        # Fast path: return cached if fresh
        if self._cached and self._cached.is_fresh(max_age):
            self._metrics["cache_hits"] += 1
            return self._cached.hash

        # Cache miss or stale
        self._metrics["cache_misses"] += 1

        try:
            self._metrics["fallback_fetches"] += 1
            cached = await self._fetch_and_cache()
            return cached.hash
        except Exception as e:
            # If fetch fails but we have somewhat recent cache, use it
            if self._cached and self._cached.age_seconds < self.BLOCKHASH_VALIDITY:
                logger.warning(f"[BlockhashCache] Fetch failed, using stale cache: {e}")
                return self._cached.hash
            raise RuntimeError(f"Failed to get blockhash: {e}") from e

    async def get_blockhash_with_info(self) -> CachedBlockhash:
        """Get blockhash with full info (timestamp, slot, etc)."""
        await self.get_blockhash()
        return self._cached

    def get_cached_sync(self) -> Optional[Hash]:
        """Get cached blockhash synchronously (no fetch). Returns None if stale."""
        if self._cached and self._cached.is_fresh(self.MAX_CACHE_AGE):
            return self._cached.hash
        return None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cache_age(self) -> Optional[float]:
        return self._cached.age_seconds if self._cached else None

    def get_metrics(self) -> dict:
        """Get cache performance metrics."""
        total = self._metrics["cache_hits"] + self._metrics["cache_misses"]
        hit_rate = (self._metrics["cache_hits"] / total * 100) if total > 0 else 0

        return {
            **self._metrics,
            "cache_hit_rate_pct": round(hit_rate, 1),
            "cache_age_seconds": round(self.cache_age, 2) if self.cache_age else None,
            "is_running": self._running,
        }

    def log_metrics(self) -> None:
        """Log current metrics."""
        m = self.get_metrics()
        logger.info(
            f"[BlockhashCache] Hits: {m['cache_hits']}, Misses: {m['cache_misses']}, "
            f"Rate: {m['cache_hit_rate_pct']}%, Errors: {m['update_errors']}"
        )


# ============================================================
# Convenience functions
# ============================================================

async def get_blockhash_cache(rpc_endpoint: Optional[str] = None) -> BlockhashCache:
    """Get the global BlockhashCache instance."""
    return await BlockhashCache.get_instance(rpc_endpoint)


async def get_cached_blockhash(rpc_endpoint: Optional[str] = None) -> Hash:
    """Convenience function to get blockhash in one call."""
    cache = await get_blockhash_cache(rpc_endpoint)
    return await cache.get_blockhash()


async def init_blockhash_cache(rpc_endpoint: str) -> BlockhashCache:
    """Initialize the global blockhash cache. Call once at startup."""
    cache = await get_blockhash_cache(rpc_endpoint)
    await cache.initialize(rpc_endpoint)
    return cache


async def stop_blockhash_cache() -> None:
    """Stop the global blockhash cache. Call at shutdown."""
    if BlockhashCache._instance:
        await BlockhashCache._instance.stop()
        BlockhashCache._instance = None
