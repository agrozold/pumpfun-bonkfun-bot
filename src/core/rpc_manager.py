"""
Global RPC Manager - Helius PRIMARY, Alchemy/Public as FALLBACK.

HELIUS FREE TIER LIMITS:
- 1,000,000 credits/month = ~33,333 credits/day
- Standard RPC: 1 credit per request
- Enhanced API: 50 credits per request
- With 6 bots running 24/7: ~1,388 credits/hour = ~231 credits/min

STRATEGY:
1. Helius = PRIMARY (best quality, use 80% of budget)
2. Alchemy = FALLBACK #1 (when Helius rate limited)
3. Public Solana = FALLBACK #2 (last resort)

Usage:
    from core.rpc_manager import get_rpc_manager

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

# =============================================================================
# HELIUS BUDGET CALCULATION (1,000,000 credits/month)
# =============================================================================
# Daily budget: 1,000,000 / 30 = 33,333 credits/day
# Hourly budget: 33,333 / 24 = 1,388 credits/hour
# Per-minute budget: 1,388 / 60 = 23 credits/minute
# With 6 bots: 23 / 6 = ~3.8 credits/minute per bot
# Standard RPC = 1 credit, so ~3.8 req/min = 0.063 req/s per bot
# BUT we want to use 80% Helius, 20% fallback, so: 0.05 req/s per bot
# =============================================================================

HELIUS_MONTHLY_CREDITS = 1_000_000
HELIUS_DAILY_CREDITS = HELIUS_MONTHLY_CREDITS // 30  # 33,333
HELIUS_HOURLY_CREDITS = HELIUS_DAILY_CREDITS // 24  # 1,388
NUM_BOTS = 6

# Conservative rate: use 70% of budget to leave room for spikes
HELIUS_SAFE_HOURLY = int(HELIUS_HOURLY_CREDITS * 0.7)  # 971 credits/hour
HELIUS_REQ_PER_SEC = HELIUS_SAFE_HOURLY / 3600 / NUM_BOTS  # ~0.045 req/s per bot

# Enhanced API costs 50 credits, so much more limited
HELIUS_ENHANCED_REQ_PER_SEC = HELIUS_REQ_PER_SEC / 50  # ~0.0009 req/s


class RPCProvider(Enum):
    """Supported RPC providers."""

    HELIUS = "helius"
    ALCHEMY = "alchemy"
    PUBLIC_SOLANA = "public_solana"


@dataclass
class ProviderConfig:
    """Configuration for an RPC provider."""

    name: str
    http_endpoint: str
    wss_endpoint: str | None = None
    rate_limit_per_second: float = 10.0  # requests per second
    priority: int = 1  # lower = higher priority
    enabled: bool = True
    is_primary: bool = False  # Primary provider flag

    # Runtime state
    last_request_time: float = field(default=0.0, repr=False)
    consecutive_errors: int = field(default=0, repr=False)
    total_requests: int = field(default=0, repr=False)
    total_errors: int = field(default=0, repr=False)
    backoff_until: float = field(default=0.0, repr=False)
    daily_requests: int = field(default=0, repr=False)
    daily_reset_time: float = field(default=0.0, repr=False)


class RPCManager:
    """Global RPC manager - Helius PRIMARY, others FALLBACK.

    Features:
    - Helius as primary RPC (best quality)
    - Alchemy as fallback #1
    - Public Solana as fallback #2
    - Daily budget tracking for Helius
    - Automatic fallback on rate limits
    """

    _instance: "RPCManager | None" = None
    _lock = asyncio.Lock()

    HTTP_OK = 200
    HTTP_RATE_LIMITED = 429

    # HARDCODED HELIUS RPC - правильный ключ! (только HTTP, НЕТ WSS у Helius!)
    HELIUS_RPC = (
        "https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_API_KEY"
    )
    HELIUS_ENHANCED = "https://api-mainnet.helius-rpc.com/v0/transactions/?api-key=YOUR_HELIUS_API_KEY"

    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {}
        self._session: aiohttp.ClientSession | None = None
        self._initialized = False

        # Metrics
        self._metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "rate_limited": 0,
            "fallback_used": 0,
            "cache_hits": 0,
            "helius_credits_used": 0,
        }

        # Response cache
        self._cache: dict[str, tuple[Any, float]] = {}
        self._cache_ttl = 60.0
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
        """Initialize providers - Helius PRIMARY, others FALLBACK."""
        if self._initialized:
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(limit=50, limit_per_host=10),
        )

        # =================================================================
        # HELIUS = PRIMARY (hardcoded correct key!) - ТОЛЬКО HTTP!
        # 1M credits/month = 33k/day = 1388/hour = 23/min
        # With 6 bots: ~4 req/min per bot for standard RPC
        # But they run independently, so use 0.1 req/s (6 req/min) to be safe
        # =================================================================
        self.providers["helius"] = ProviderConfig(
            name="Helius",
            http_endpoint=self.HELIUS_RPC,
            wss_endpoint=None,  # Helius НЕ имеет WSS!
            rate_limit_per_second=0.1,  # 6 req/min - conservative for 6 bots
            priority=0,  # HIGHEST priority
            is_primary=True,
        )
        logger.info("[RPC] ✓ HELIUS PRIMARY configured (0.1 req/s = 6 req/min)")
        logger.info(f"[RPC]   Daily budget: {HELIUS_DAILY_CREDITS} credits")

        # Helius Enhanced API (50 credits per request!)
        # 33k daily / 50 = 666 enhanced requests/day max
        # With 6 bots running independently, each gets ~110 requests/day
        # That's ~4.5 req/hour = 0.00125 req/s per bot
        # Use 0.02 req/s (1.2 req/min) to be safe
        self.providers["helius_enhanced"] = ProviderConfig(
            name="Helius Enhanced",
            http_endpoint=self.HELIUS_ENHANCED,
            wss_endpoint=None,  # Helius НЕ имеет WSS!
            rate_limit_per_second=0.02,  # 1.2 req/min - very conservative for 6 bots
            priority=0,
            is_primary=True,
        )
        logger.info("[RPC] ✓ Helius Enhanced configured (0.02 req/s = 1.2 req/min)")

        # =================================================================
        # ALCHEMY = FALLBACK #1 (take more load when Helius rate limited)
        # =================================================================
        alchemy_endpoint = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy_endpoint:
            self.providers["alchemy"] = ProviderConfig(
                name="Alchemy",
                http_endpoint=alchemy_endpoint,
                rate_limit_per_second=1.0,  # 60 req/min - higher for fallback
                priority=5,  # Lower priority than Helius
                is_primary=False,
            )
            logger.info("[RPC] ✓ Alchemy FALLBACK #1 configured (1.0 req/s)")
        else:
            logger.warning("[RPC] ⚠ ALCHEMY_RPC_ENDPOINT not set - no fallback #1")

        # =================================================================
        # PUBLIC SOLANA = FALLBACK #2 (last resort, heavily rate limited)
        # =================================================================
        self.providers["public_solana"] = ProviderConfig(
            name="Public Solana",
            http_endpoint="https://api.mainnet-beta.solana.com",
            wss_endpoint="wss://api.mainnet-beta.solana.com",
            rate_limit_per_second=0.5,  # 30 req/min - public is limited
            priority=10,  # LOWEST priority
            is_primary=False,
        )
        logger.info("[RPC] ✓ Public Solana FALLBACK #2 configured (0.5 req/s)")

        self._initialized = True
        logger.info(f"[RPC] Manager initialized: {len(self.providers)} providers")
        logger.info("[RPC] Priority: Helius -> Alchemy -> Public Solana")

    def _get_available_provider(
        self, exclude: set[str] | None = None, prefer_primary: bool = True
    ) -> ProviderConfig | None:
        """Get best available provider - Helius first, then fallbacks."""
        exclude = exclude or set()
        now = time.time()

        available = []
        for name, provider in self.providers.items():
            if name in exclude or not provider.enabled:
                continue

            # Skip if in backoff
            if now < provider.backoff_until:
                continue

            # Check rate limit
            time_since_last = now - provider.last_request_time
            min_interval = 1.0 / provider.rate_limit_per_second
            if time_since_last < min_interval:
                continue

            # Reset daily counter if new day
            if now - provider.daily_reset_time > 86400:
                provider.daily_requests = 0
                provider.daily_reset_time = now

            available.append((provider.priority, name, provider))

        if not available:
            return None

        # Sort by priority (lower = better) - Helius first!
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
        provider.daily_requests += 1
        self._metrics["total_requests"] += 1
        self._metrics["successful_requests"] += 1

        # Track Helius credits
        if provider.is_primary:
            if "enhanced" in provider.name.lower():
                self._metrics["helius_credits_used"] += 50  # Enhanced = 50 credits
            else:
                self._metrics["helius_credits_used"] += 1  # Standard = 1 credit

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
        """Get current metrics including Helius budget usage."""
        provider_stats = {}
        for name, provider in self.providers.items():
            provider_stats[name] = {
                "total_requests": provider.total_requests,
                "daily_requests": provider.daily_requests,
                "total_errors": provider.total_errors,
                "consecutive_errors": provider.consecutive_errors,
                "enabled": provider.enabled,
                "is_primary": provider.is_primary,
                "in_backoff": time.time() < provider.backoff_until,
            }

        helius_daily_used = self._metrics.get("helius_credits_used", 0)
        helius_daily_remaining = HELIUS_DAILY_CREDITS - helius_daily_used
        helius_usage_percent = (helius_daily_used / HELIUS_DAILY_CREDITS) * 100

        return {
            **self._metrics,
            "helius_daily_budget": HELIUS_DAILY_CREDITS,
            "helius_daily_remaining": helius_daily_remaining,
            "helius_usage_percent": f"{helius_usage_percent:.1f}%",
            "cache_size": len(self._cache),
            "providers": provider_stats,
        }

    def log_metrics(self) -> None:
        """Log current metrics with Helius budget info."""
        m = self._metrics
        success_rate = (
            m["successful_requests"] / m["total_requests"] * 100
            if m["total_requests"] > 0
            else 0
        )

        helius_used = m.get("helius_credits_used", 0)
        helius_pct = (helius_used / HELIUS_DAILY_CREDITS) * 100

        logger.info(
            f"[RPC STATS] Total: {m['total_requests']}, Success: {success_rate:.1f}%, "
            f"Rate limited: {m['rate_limited']}, Fallbacks: {m['fallback_used']}"
        )
        logger.info(
            f"[RPC HELIUS] Credits used today: {helius_used}/{HELIUS_DAILY_CREDITS} "
            f"({helius_pct:.1f}%), Cache hits: {m['cache_hits']}"
        )

        for name, provider in self.providers.items():
            if provider.total_requests > 0:
                role = "PRIMARY" if provider.is_primary else "FALLBACK"
                logger.info(
                    f"[RPC] {provider.name} ({role}): {provider.total_requests} req, "
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
