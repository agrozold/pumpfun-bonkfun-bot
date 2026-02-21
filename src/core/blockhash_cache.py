"""
Global Blockhash Cache - dual mode: gRPC (fast) + HTTP fallback.

Mode 1 (gRPC): Uses existing Geyser channel GetLatestBlockhash (~13ms)
Mode 2 (HTTP): Polls RPC endpoint every N seconds (~51ms)

Both modes keep blockhash in memory — TX build gets it in 0ms.

Usage:
    from core.blockhash_cache import get_blockhash_cache

    cache = await get_blockhash_cache(rpc_endpoint="https://...")
    blockhash = await cache.get_blockhash()  # Returns cached, 0ms
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
    source: str = "http"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    def is_fresh(self, max_age: float = 5.0) -> bool:
        return self.age_seconds < max_age


class BlockhashCache:
    """
    High-performance blockhash cache with gRPC primary + HTTP fallback.
    
    Starts with HTTP polling immediately.
    When gRPC channel becomes available, switches to gRPC (faster, free).
    """

    _instance: Optional["BlockhashCache"] = None
    _lock = asyncio.Lock()

    # Configuration
    HTTP_POLL_INTERVAL = 5.0    # seconds between HTTP polls
    GRPC_POLL_INTERVAL = 2.0    # seconds between gRPC polls (faster)
    MAX_CACHE_AGE = 10.0        # max age before fallback fetch
    BLOCKHASH_VALIDITY = 60     # Solana blockhash valid ~60-90s

    def __init__(self):
        self._cached: Optional[CachedBlockhash] = None
        self._update_lock = asyncio.Lock()
        self._updater_task: Optional[asyncio.Task] = None
        self._running = False
        self._rpc_endpoint: Optional[str] = None
        self._client: Optional[AsyncClient] = None
        
        # gRPC mode
        self._grpc_stub = None
        self._grpc_mode = False
        
        # Metrics
        self._metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "fallback_fetches": 0,
            "update_errors": 0,
            "total_updates": 0,
            "grpc_updates": 0,
            "http_updates": 0,
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
        """Initialize with HTTP polling. Call once at startup."""
        if self._running and self._rpc_endpoint == rpc_endpoint:
            return  # Already initialized with same endpoint
            
        self._rpc_endpoint = rpc_endpoint

        if self._client:
            await self._client.close()

        self._client = AsyncClient(rpc_endpoint)
        
        # First fetch
        try:
            await self._fetch_http()
            logger.info(f"[BlockhashCache] First blockhash fetched via HTTP")
        except Exception as e:
            logger.warning(f"[BlockhashCache] First fetch failed: {e}")

        if not self._running:
            await self.start()

        logger.info(
            f"[BlockhashCache] Initialized HTTP mode "
            f"(interval: {self.HTTP_POLL_INTERVAL}s)"
        )

    def enable_grpc(self, grpc_stub) -> None:
        """
        Switch to gRPC mode. Call when Geyser channel is ready.
        
        Args:
            grpc_stub: GeyserStub with GetLatestBlockhash method
        """
        self._grpc_stub = grpc_stub
        self._grpc_mode = True
        logger.warning(
            f"[BlockhashCache] Switched to gRPC mode "
            f"(interval: {self.GRPC_POLL_INTERVAL}s)"
        )

    def disable_grpc(self) -> None:
        """Fall back to HTTP mode (e.g., on gRPC disconnect)."""
        self._grpc_mode = False
        self._grpc_stub = None
        logger.warning("[BlockhashCache] Fell back to HTTP mode")

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
        """Background loop — gRPC or HTTP based on mode."""
        while self._running:
            try:
                if self._grpc_mode and self._grpc_stub:
                    await self._fetch_grpc()
                    interval = self.GRPC_POLL_INTERVAL
                else:
                    await self._fetch_http()
                    interval = self.HTTP_POLL_INTERVAL
                self._metrics["total_updates"] += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._metrics["update_errors"] += 1
                # If gRPC fails, fall back to HTTP
                if self._grpc_mode:
                    # FIX S19-6: Auto-disable gRPC after 3 consecutive failures (channel likely dead)
                    self._metrics['grpc_consecutive_fails'] = self._metrics.get('grpc_consecutive_fails', 0) + 1
                    _gfails = self._metrics['grpc_consecutive_fails']
                    if _gfails <= 3:
                        logger.warning(f"[BlockhashCache] gRPC failed ({_gfails}/3), trying HTTP: {e}")
                    elif _gfails == 4:
                        logger.warning(f"[BlockhashCache] gRPC failed 4x — switching to HTTP-only (channel dead)")
                        self.disable_grpc()
                    # else: silently use HTTP (already switched)
                    try:
                        await self._fetch_http()
                        interval = self.HTTP_POLL_INTERVAL
                    except Exception as e2:
                        logger.warning(f"[BlockhashCache] HTTP also failed: {e2}")
                        interval = self.HTTP_POLL_INTERVAL
                else:
                    logger.warning(f"[BlockhashCache] Update failed: {e}")
                    interval = self.HTTP_POLL_INTERVAL

            await asyncio.sleep(interval)

    async def _fetch_grpc(self) -> CachedBlockhash:
        """Fetch blockhash via gRPC GetLatestBlockhash."""
        from geyser.generated import geyser_pb2
        
        req = geyser_pb2.GetLatestBlockhashRequest()
        resp = await asyncio.wait_for(
            self._grpc_stub.GetLatestBlockhash(req), 
            timeout=5.0
        )
        
        blockhash = Hash.from_string(resp.blockhash)
        self._cached = CachedBlockhash(
            hash=blockhash,
            timestamp=time.time(),
            slot=resp.slot,
            source="grpc",
        )
        self._metrics["grpc_updates"] += 1
        return self._cached

    async def _fetch_http(self) -> CachedBlockhash:
        """Fetch blockhash via HTTP RPC."""
        if not self._client:
            raise RuntimeError("BlockhashCache HTTP client not initialized")

        async with self._update_lock:
            response = await self._client.get_latest_blockhash(commitment="processed")
            self._cached = CachedBlockhash(
                hash=response.value.blockhash,
                timestamp=time.time(),
                slot=response.context.slot if hasattr(response, 'context') else 0,
                source="http",
            )
            self._metrics["http_updates"] += 1
            return self._cached

    async def get_blockhash(self, max_age: float = None) -> Hash:
        """
        Get blockhash — from cache if fresh, otherwise fetch.
        
        In normal operation, cache is always fresh (updated every 2-5s).
        Direct fetch only happens if background updater is behind.
        """
        if max_age is None:
            max_age = self.MAX_CACHE_AGE

        if self._cached and self._cached.is_fresh(max_age):
            self._metrics["cache_hits"] += 1
            return self._cached.hash

        # Cache miss — fetch now
        self._metrics["cache_misses"] += 1
        self._metrics["fallback_fetches"] += 1
        
        try:
            if self._grpc_mode and self._grpc_stub:
                cached = await self._fetch_grpc()
            else:
                cached = await self._fetch_http()
            return cached.hash
        except Exception as e:
            if self._cached and self._cached.age_seconds < self.BLOCKHASH_VALIDITY:
                logger.warning(f"[BlockhashCache] Fetch failed, using stale: {e}")
                return self._cached.hash
            raise RuntimeError(f"Failed to get blockhash: {e}") from e

    async def get_blockhash_with_info(self) -> CachedBlockhash:
        """Get blockhash with full metadata."""
        await self.get_blockhash()
        return self._cached

    def get_cached_sync(self) -> Optional[Hash]:
        """Synchronous cache read. Returns None if stale."""
        if self._cached and self._cached.is_fresh(self.MAX_CACHE_AGE):
            return self._cached.hash
        return None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_grpc_mode(self) -> bool:
        return self._grpc_mode

    @property
    def cache_age(self) -> Optional[float]:
        return self._cached.age_seconds if self._cached else None

    def get_metrics(self) -> dict:
        total = self._metrics["cache_hits"] + self._metrics["cache_misses"]
        hit_rate = (self._metrics["cache_hits"] / total * 100) if total > 0 else 0
        return {
            **self._metrics,
            "cache_hit_rate_pct": round(hit_rate, 1),
            "cache_age_seconds": round(self.cache_age, 2) if self.cache_age else None,
            "is_running": self._running,
            "mode": "grpc" if self._grpc_mode else "http",
        }

    def log_metrics(self) -> None:
        m = self.get_metrics()
        logger.info(
            f"[BlockhashCache] Mode: {m['mode']} | Hits: {m['cache_hits']} "
            f"Misses: {m['cache_misses']} | Rate: {m['cache_hit_rate_pct']}% | "
            f"gRPC: {m['grpc_updates']} HTTP: {m['http_updates']} Errors: {m['update_errors']}"
        )


# ============================================================
# Convenience functions
# ============================================================

async def get_blockhash_cache(rpc_endpoint: Optional[str] = None) -> BlockhashCache:
    """Get the global BlockhashCache instance."""
    return await BlockhashCache.get_instance(rpc_endpoint)


async def get_cached_blockhash(rpc_endpoint: Optional[str] = None) -> Hash:
    """Convenience: get blockhash in one call."""
    cache = await get_blockhash_cache(rpc_endpoint)
    return await cache.get_blockhash()


async def init_blockhash_cache(rpc_endpoint: str) -> BlockhashCache:
    """Initialize at startup. Call once."""
    cache = await get_blockhash_cache(rpc_endpoint)
    await cache.initialize(rpc_endpoint)
    return cache


async def stop_blockhash_cache() -> None:
    """Stop at shutdown."""
    if BlockhashCache._instance:
        await BlockhashCache._instance.stop()
        BlockhashCache._instance = None
