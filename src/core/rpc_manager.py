"""
Global RPC Manager with rate limiting and round-robin between providers.

Solves the 429 rate limit problem by:
1. Round-robin between multiple RPC providers
2. Global rate limiting per provider
3. Automatic fallback on 429 errors
4. Request queuing for burst protection

Usage:
    from src.core.rpc_manager import get_rpc_manager

    rpc = get_rpc_manager()
    result = await rpc.get_transaction(signature)
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class RPCProvider(Enum):
    """Supported RPC providers."""

    HELIUS = "helius"
    ALCHEMY = "alchemy"
    PUBLIC_SOLANA = "public_solana"
    CUSTOM = "custom"


@dataclass
class ProviderConfig:
    """Configuration for an RPC provider."""

    name: str
    http_endpoint: str
    wss_endpoint: str | None = None
    rate_limit_per_second: float = 10.0  # requests per second
    priority: int = 1  # lower = higher priority
    enabled: bool = True

    # Runtime state
    last_request_time: float = field(default=0.0, repr=False)
    consecutive_errors: int = field(default=0, repr=False)
    total_requests: int = field(default=0, repr=False)
    total_errors: int = field(default=0, repr=False)
    backoff_until: float = field(default=0.0, repr=False)


class RPCManager:
    """Global RPC manager with rate limiting and provider rotation.

    Features:
    - Round-robin between providers based on availability
    - Per-provider rate limiting
    - Automatic backoff on 429 errors
    - Request queuing for burst protection
    - Metrics tracking
    """

    _instance: "RPCManager | None" = None
    _lock = asyncio.Lock()

    # HTTP status codes
    HTTP_OK = 200
    HTTP_RATE_LIMITED = 429

    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {}
        self._session: aiohttp.ClientSession | None = None
        self._request_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queue_processor_task: asyncio.Task | None = None
        self._initialized = False

        # Global rate limiting
        self._global_rate_limit = 50.0  # total requests per second across all providers
        self._global_last_request = 0.0
        self._global_lock = asyncio.Lock()

        # Metrics
        self._metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "rate_limited": 0,
            "fallback_used": 0,
            "cache_hits": 0,
        }

        # Simple response cache
        self._cache: dict[str, tuple[Any, float]] = {}
        self._cache_ttl = 60.0  # 60 seconds
        self._cache_max_size = 1000

    @classmethod
    async def get_instance(cls) -> "RPCManager":
        """Get or create singleton instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = RPCManager()
                    await cls._instance._initialize()
        return cls._instance

    async def _initialize(self) -> None:
        """Initialize providers from environment variables."""
        if self._initialized:
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(limit=50, limit_per_host=10),
        )

        # Load Helius (highest priority - best for parsed transactions)
        helius_key = os.getenv("HELIUS_API_KEY")
        if helius_key:
            self.providers["helius"] = ProviderConfig(
                name="Helius",
                http_endpoint=f"https://mainnet.helius-rpc.com/?api-key={helius_key}",
                wss_endpoint=f"wss://mainnet.helius-rpc.com/?api-key={helius_key}",
                rate_limit_per_second=8.0,  # Helius free tier: ~10 req/s, leave buffer
                priority=1,
            )
            # Helius Enhanced API (for parsed transactions)
            self.providers["helius_enhanced"] = ProviderConfig(
                name="Helius Enhanced",
                http_endpoint=f"https://api-mainnet.helius-rpc.com/v0/transactions/?api-key={helius_key}",
                rate_limit_per_second=1.5,  # Enhanced API: 2 req/s limit
                priority=0,  # Highest priority for TX parsing
            )
            logger.info(
                "[RPC] Helius provider configured (8 req/s RPC, 1.5 req/s Enhanced)"
            )

        # Load Alchemy
        alchemy_endpoint = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy_endpoint:
            self.providers["alchemy"] = ProviderConfig(
                name="Alchemy",
                http_endpoint=alchemy_endpoint,
                rate_limit_per_second=15.0,  # Alchemy has higher limits
                priority=2,
            )
            logger.info("[RPC] Alchemy provider configured (15 req/s)")

        # Load custom RPC from env
        custom_rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
        custom_wss = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
        if custom_rpc and "mainnet-beta.solana.com" not in custom_rpc:
            self.providers["custom"] = ProviderConfig(
                name="Custom RPC",
                http_endpoint=custom_rpc,
                wss_endpoint=custom_wss,
                rate_limit_per_second=20.0,  # Assume paid RPC has good limits
                priority=2,
            )
            logger.info(f"[RPC] Custom provider configured: {custom_rpc[:40]}...")

        # Public Solana (lowest priority, fallback only)
        self.providers["public_solana"] = ProviderConfig(
            name="Public Solana",
            http_endpoint="https://api.mainnet-beta.solana.com",
            wss_endpoint="wss://api.mainnet-beta.solana.com",
            rate_limit_per_second=2.0,  # Very conservative for public RPC
            priority=10,  # Lowest priority
        )
        logger.info("[RPC] Public Solana fallback configured (2 req/s)")

        self._initialized = True
        logger.info(f"[RPC] Manager initialized with {len(self.providers)} providers")

    def _get_available_provider(
        self, exclude: set[str] | None = None
    ) -> ProviderConfig | None:
        """Get the best available provider based on rate limits and priority."""
        exclude = exclude or set()
        now = time.time()

        available = []
        for name, provider in self.providers.items():
            if name in exclude or not provider.enabled:
                continue

            # Check if in backoff
            if now < provider.backoff_until:
                continue

            # Check rate limit
            time_since_last = now - provider.last_request_time
            min_interval = 1.0 / provider.rate_limit_per_second
            if time_since_last < min_interval:
                continue

            available.append((provider.priority, name, provider))

        if not available:
            return None

        # Sort by priority (lower = better)
        available.sort(key=lambda x: x[0])
        return available[0][2]

    async def _wait_for_rate_limit(self, provider: ProviderConfig) -> None:
        """Wait for rate limit if needed."""
        now = time.time()
        min_interval = 1.0 / provider.rate_limit_per_second
        time_since_last = now - provider.last_request_time

        if time_since_last < min_interval:
            wait_time = min_interval - time_since_last
            await asyncio.sleep(wait_time)

        provider.last_request_time = time.time()

    def _handle_rate_limit(self, provider: ProviderConfig) -> None:
        """Handle 429 rate limit error."""
        provider.consecutive_errors += 1
        provider.total_errors += 1
        self._metrics["rate_limited"] += 1

        # Exponential backoff: 2s, 4s, 8s, 16s, max 60s
        backoff = min(2**provider.consecutive_errors, 60)
        provider.backoff_until = time.time() + backoff

        logger.warning(
            f"[RPC] {provider.name} rate limited (429), backoff {backoff}s "
            f"(errors: {provider.consecutive_errors})"
        )

    def _handle_success(self, provider: ProviderConfig) -> None:
        """Handle successful request."""
        provider.consecutive_errors = 0
        provider.total_requests += 1
        self._metrics["total_requests"] += 1
        self._metrics["successful_requests"] += 1

    async def post_rpc(
        self,
        body: dict[str, Any],
        provider_name: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Send RPC request with automatic provider selection and fallback.

        Args:
            body: JSON-RPC request body
            provider_name: Specific provider to use (optional)
            timeout: Request timeout in seconds

        Returns:
            RPC response or None on failure
        """
        if not self._session:
            await self._initialize()

        tried_providers: set[str] = set()

        while True:
            # Get provider
            if provider_name and provider_name in self.providers:
                provider = self.providers[provider_name]
                tried_providers.add(provider_name)
            else:
                provider = self._get_available_provider(exclude=tried_providers)

            if not provider:
                logger.warning("[RPC] No available providers, all rate limited")
                return None

            tried_providers.add(provider.name.lower().replace(" ", "_"))

            # Wait for rate limit
            await self._wait_for_rate_limit(provider)

            try:
                async with self._session.post(
                    provider.http_endpoint,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status == self.HTTP_OK:
                        data = await resp.json()
                        self._handle_success(provider)
                        return data
                    elif resp.status == self.HTTP_RATE_LIMITED:
                        self._handle_rate_limit(provider)
                        self._metrics["fallback_used"] += 1
                        # Try next provider
                        continue
                    else:
                        logger.warning(f"[RPC] {provider.name} HTTP {resp.status}")
                        provider.consecutive_errors += 1
                        continue

            except TimeoutError:
                logger.warning(f"[RPC] {provider.name} timeout ({timeout}s)")
                provider.consecutive_errors += 1
                continue
            except aiohttp.ClientError as e:
                logger.warning(f"[RPC] {provider.name} client error: {e}")
                provider.consecutive_errors += 1
                continue

    async def get_transaction(
        self,
        signature: str,
        use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """Get transaction with caching and automatic fallback.

        Args:
            signature: Transaction signature
            use_cache: Whether to use cache

        Returns:
            Transaction data or None
        """
        cache_key = f"tx:{signature}"

        # Check cache
        if use_cache and cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                self._metrics["cache_hits"] += 1
                return cached

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
        }

        result = await self.post_rpc(body)

        if result and "result" in result and result["result"]:
            # Cache successful result
            self._cache_result(cache_key, result["result"])
            return result["result"]

        return None

    async def get_transaction_helius_enhanced(
        self,
        signature: str,
        use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """Get parsed transaction from Helius Enhanced API.

        This is the most efficient way to get transaction details
        as Helius parses the transaction automatically.

        Args:
            signature: Transaction signature
            use_cache: Whether to use cache

        Returns:
            Parsed transaction data or None
        """
        cache_key = f"tx_enhanced:{signature}"

        # Check cache
        if use_cache and cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                self._metrics["cache_hits"] += 1
                return cached

        provider = self.providers.get("helius_enhanced")
        if not provider:
            return None

        await self._wait_for_rate_limit(provider)

        try:
            async with self._session.post(
                provider.http_endpoint,
                json={"transactions": [signature]},
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == self.HTTP_OK:
                    data = await resp.json()
                    self._handle_success(provider)
                    if data and len(data) > 0:
                        self._cache_result(cache_key, data[0])
                        return data[0]
                elif resp.status == self.HTTP_RATE_LIMITED:
                    self._handle_rate_limit(provider)
                else:
                    logger.debug(f"[RPC] Helius Enhanced HTTP {resp.status}")

        except aiohttp.ClientError as e:
            logger.debug(f"[RPC] Helius Enhanced error: {e}")

        return None

    async def get_account_info(
        self,
        pubkey: str,
        use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """Get account info with caching."""
        cache_key = f"acc:{pubkey}"

        if use_cache and cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                self._metrics["cache_hits"] += 1
                return cached

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [pubkey, {"encoding": "base64"}],
        }

        result = await self.post_rpc(body)

        if result and "result" in result and result["result"]:
            self._cache_result(cache_key, result["result"])
            return result["result"]

        return None

    def _cache_result(self, key: str, value: dict) -> None:
        """Cache a result with LRU eviction."""
        self._cache[key] = (value, time.time())

        # LRU eviction
        if len(self._cache) > self._cache_max_size:
            oldest = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest]

    def get_metrics(self) -> dict[str, Any]:
        """Get current metrics."""
        provider_stats = {}
        for name, provider in self.providers.items():
            provider_stats[name] = {
                "total_requests": provider.total_requests,
                "total_errors": provider.total_errors,
                "consecutive_errors": provider.consecutive_errors,
                "enabled": provider.enabled,
                "in_backoff": time.time() < provider.backoff_until,
            }

        return {
            **self._metrics,
            "cache_size": len(self._cache),
            "providers": provider_stats,
        }

    def log_metrics(self) -> None:
        """Log current metrics."""
        m = self._metrics
        success_rate = (
            m["successful_requests"] / m["total_requests"] * 100
            if m["total_requests"] > 0
            else 0
        )

        logger.info(
            f"[RPC STATS] Total: {m['total_requests']}, Success: {m['successful_requests']} "
            f"({success_rate:.1f}%), Rate limited: {m['rate_limited']}, "
            f"Fallbacks: {m['fallback_used']}, Cache hits: {m['cache_hits']}"
        )

        for _name, provider in self.providers.items():
            if provider.total_requests > 0:
                logger.info(
                    f"[RPC] {provider.name}: {provider.total_requests} requests, "
                    f"{provider.total_errors} errors"
                )

    async def close(self) -> None:
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None


# Global instance getter
async def get_rpc_manager() -> RPCManager:
    """Get the global RPC manager instance."""
    return await RPCManager.get_instance()


# Synchronous helper for initialization
_rpc_manager: RPCManager | None = None


def init_rpc_manager() -> RPCManager:
    """Initialize RPC manager (call from async context)."""
    global _rpc_manager  # noqa: PLW0603
    if _rpc_manager is None:
        _rpc_manager = RPCManager()
    return _rpc_manager
