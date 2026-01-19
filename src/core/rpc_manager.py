"""
Global RPC Manager - Optimized for 6 bots with smart caching.

ENDPOINTS (from .env):
- Helius: PRIMARY for HTTP (1M credits/month)
- Chainstack: PRIMARY for WSS + HTTP fallback (1M req/month)
- Alchemy: FALLBACK #1
- Public Solana: FALLBACK #2 (last resort)

OPTIMIZATION:
- Smart caching with TTL by request type
- Rate limiting per provider
- Daily budget tracking
- Automatic fallback on 429
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
# BUDGET CALCULATION (6 bots sharing resources)
# =============================================================================
# Helius: 1,000,000 credits/month = 33,333/day = 1,388/hour
# Chainstack: 1,000,000 req/month = 33,333/day = 1,388/hour
# Combined: ~2,776/hour for all 6 bots = ~462/hour per bot = 7.7/min per bot
# 
# SAFE LIMITS (70% of budget):
# - Helius: 0.08 req/s per bot (4.8 req/min)
# - Chainstack: 0.10 req/s per bot (6 req/min)
# =============================================================================

NUM_BOTS = 6
HELIUS_MONTHLY_CREDITS = 1_000_000
CHAINSTACK_MONTHLY_REQUESTS = 1_000_000

HELIUS_DAILY = HELIUS_MONTHLY_CREDITS // 30
CHAINSTACK_DAILY = CHAINSTACK_MONTHLY_REQUESTS // 30


class CacheType(Enum):
    """Cache TTL by request type."""
    TRANSACTION = 300      # 5 min - transactions don't change
    ACCOUNT_INFO = 30      # 30 sec - account state changes
    HEALTH = 60            # 1 min - health checks
    SIGNATURE = 120        # 2 min - signature status
    BALANCE = 10           # 10 sec - balance changes often
    TOKEN_ACCOUNTS = 30    # 30 sec


@dataclass
class ProviderConfig:
    """RPC provider configuration."""
    name: str
    http_endpoint: str
    wss_endpoint: str | None = None
    rate_limit_per_second: float = 0.1
    priority: int = 1
    enabled: bool = True
    is_primary: bool = False
    
    # Runtime state
    last_request_time: float = field(default=0.0, repr=False)
    consecutive_errors: int = field(default=0, repr=False)
    total_requests: int = field(default=0, repr=False)
    total_errors: int = field(default=0, repr=False)
    backoff_until: float = field(default=0.0, repr=False)
    daily_requests: int = field(default=0, repr=False)
    daily_reset_time: float = field(default=0.0, repr=False)


class RPCManager:
    """Optimized RPC manager with caching and rate limiting."""
    
    _instance: "RPCManager | None" = None
    _lock = asyncio.Lock()
    
    HTTP_OK = 200
    HTTP_RATE_LIMITED = 429

    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {}
        self._session: aiohttp.ClientSession | None = None
        self._initialized = False
        
        # Smart cache with TTL
        self._cache: dict[str, tuple[Any, float, float]] = {}  # key -> (value, timestamp, ttl)
        self._cache_max_size = 2000
        
        # Metrics
        self._metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "rate_limited": 0,
            "fallback_used": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "helius_credits_used": 0,
            "chainstack_requests": 0,
        }
        
        # Request log for debugging
        self._request_log: list[dict] = []
        self._max_log_entries = 100

    @classmethod
    async def get_instance(cls) -> "RPCManager":
        """Get singleton instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = RPCManager()
                    await cls._instance._initialize()
        return cls._instance

    async def _initialize(self) -> None:
        """Initialize providers from .env."""
        if self._initialized:
            return
            
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=100, limit_per_host=20),
        )
        
        # =================================================================
        # HELIUS = PRIMARY HTTP (use key from .env!)
        # =================================================================
        helius_key = os.getenv("HELIUS_API_KEY")
        if helius_key:
            helius_http = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            self.providers["helius"] = ProviderConfig(
                name="Helius",
                http_endpoint=helius_http,
                wss_endpoint=None,  # Helius doesn't have WSS
                rate_limit_per_second=0.08,  # 4.8 req/min per bot
                priority=0,
                is_primary=True,
            )
            logger.info(f"[RPC] ✓ HELIUS PRIMARY: 0.08 req/s ({helius_key[:8]}...)")
            
            # Helius Enhanced API (50 credits per request)
            helius_enhanced = f"https://api.helius.xyz/v0/transactions/?api-key={helius_key}"
            self.providers["helius_enhanced"] = ProviderConfig(
                name="Helius Enhanced",
                http_endpoint=helius_enhanced,
                wss_endpoint=None,
                rate_limit_per_second=0.015,  # ~1 req/min - very expensive!
                priority=0,
                is_primary=True,
            )
            logger.info("[RPC] ✓ Helius Enhanced: 0.015 req/s (50 credits each)")
        else:
            logger.error("[RPC] ✗ HELIUS_API_KEY not found in .env!")

        # =================================================================
        # CHAINSTACK = CO-PRIMARY (HTTP + WSS)
        # =================================================================
        chainstack_http = os.getenv("CHAINSTACK_RPC_ENDPOINT")
        chainstack_wss = os.getenv("CHAINSTACK_WSS_ENDPOINT")
        if chainstack_http:
            self.providers["chainstack"] = ProviderConfig(
                name="Chainstack",
                http_endpoint=chainstack_http,
                wss_endpoint=chainstack_wss,
                rate_limit_per_second=0.10,  # 6 req/min per bot
                priority=1,
                is_primary=True,
            )
            logger.info("[RPC] ✓ CHAINSTACK CO-PRIMARY: 0.10 req/s + WSS")
        else:
            logger.warning("[RPC] ⚠ CHAINSTACK_RPC_ENDPOINT not set")

        # =================================================================
        # ALCHEMY = FALLBACK #1
        # =================================================================
        alchemy_http = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy_http:
            self.providers["alchemy"] = ProviderConfig(
                name="Alchemy",
                http_endpoint=alchemy_http,
                wss_endpoint=None,
                rate_limit_per_second=0.5,  # 30 req/min
                priority=5,
                is_primary=False,
            )
            logger.info("[RPC] ✓ Alchemy FALLBACK #1: 0.5 req/s")

        # =================================================================
        # PUBLIC SOLANA = FALLBACK #2 (last resort)
        # =================================================================
        self.providers["public_solana"] = ProviderConfig(
            name="Public Solana",
            http_endpoint="https://api.mainnet-beta.solana.com",
            wss_endpoint="wss://api.mainnet-beta.solana.com",
            rate_limit_per_second=0.3,
            priority=10,
            is_primary=False,
        )
        logger.info("[RPC] ✓ Public Solana FALLBACK #2: 0.3 req/s")
        
        self._initialized = True
        logger.info(f"[RPC] Initialized {len(self.providers)} providers")
        logger.info(f"[RPC] Daily budget: Helius {HELIUS_DAILY} + Chainstack {CHAINSTACK_DAILY}")

    def _get_cache(self, key: str) -> Any | None:
        """Get from cache if not expired."""
        if key in self._cache:
            value, timestamp, ttl = self._cache[key]
            if time.time() - timestamp < ttl:
                self._metrics["cache_hits"] += 1
                return value
            else:
                del self._cache[key]
        self._metrics["cache_misses"] += 1
        return None

    def _set_cache(self, key: str, value: Any, ttl: float) -> None:
        """Set cache with TTL."""
        self._cache[key] = (value, time.time(), ttl)
        
        # LRU eviction
        if len(self._cache) > self._cache_max_size:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]

    def _get_available_provider(self, exclude: set[str] | None = None) -> ProviderConfig | None:
        """Get best available provider."""
        exclude = exclude or set()
        now = time.time()
        
        available = []
        for name, provider in self.providers.items():
            if name in exclude or not provider.enabled:
                continue
            if "enhanced" in name.lower():  # Skip enhanced for regular requests
                continue
            if now < provider.backoff_until:
                continue
                
            # Check rate limit
            time_since_last = now - provider.last_request_time
            min_interval = 1.0 / provider.rate_limit_per_second
            if time_since_last < min_interval:
                continue
                
            # Reset daily counter
            if now - provider.daily_reset_time > 86400:
                provider.daily_requests = 0
                provider.daily_reset_time = now
                
            available.append((provider.priority, name, provider))
            
        if not available:
            return None
            
        available.sort(key=lambda x: x[0])
        return available[0][2]

    async def _wait_for_rate_limit(self, provider: ProviderConfig) -> None:
        """Wait for rate limit."""
        now = time.time()
        min_interval = 1.0 / provider.rate_limit_per_second
        time_since_last = now - provider.last_request_time
        
        if time_since_last < min_interval:
            await asyncio.sleep(min_interval - time_since_last)
            
        provider.last_request_time = time.time()

    def _handle_rate_limit(self, provider: ProviderConfig) -> None:
        """Handle 429 rate limit."""
        provider.consecutive_errors += 1
        provider.total_errors += 1
        self._metrics["rate_limited"] += 1
        
        backoff = min(2 ** provider.consecutive_errors, 60)
        provider.backoff_until = time.time() + backoff
        
        logger.warning(f"[RPC] {provider.name} rate limited (429), backoff {backoff}s")

    def _handle_success(self, provider: ProviderConfig) -> None:
        """Handle successful request."""
        provider.consecutive_errors = 0
        provider.total_requests += 1
        provider.daily_requests += 1
        self._metrics["total_requests"] += 1
        self._metrics["successful_requests"] += 1
        
        if "helius" in provider.name.lower():
            credits = 50 if "enhanced" in provider.name.lower() else 1
            self._metrics["helius_credits_used"] += credits
        elif "chainstack" in provider.name.lower():
            self._metrics["chainstack_requests"] += 1

    def _log_request(self, method: str, provider: str, cached: bool, duration: float) -> None:
        """Log request for debugging."""
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "method": method,
            "provider": provider,
            "cached": cached,
            "duration_ms": int(duration * 1000),
        }
        self._request_log.append(entry)
        if len(self._request_log) > self._max_log_entries:
            self._request_log.pop(0)

    async def post_rpc(
        self,
        body: dict[str, Any],
        cache_type: CacheType | None = None,
        cache_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Send RPC request with caching and fallback."""
        if not self._session:
            await self._initialize()
            
        start_time = time.time()
        method = body.get("method", "unknown")
        
        # Check cache
        if cache_key and cache_type:
            cached = self._get_cache(cache_key)
            if cached is not None:
                self._log_request(method, "CACHE", True, time.time() - start_time)
                return {"result": cached}
                
        tried_providers: set[str] = set()
        
        while True:
            provider = self._get_available_provider(exclude=tried_providers)
            if not provider:
                logger.warning(f"[RPC] No available providers for {method}")
                return None
                
            tried_providers.add(provider.name.lower().replace(" ", "_"))
            await self._wait_for_rate_limit(provider)
            
            try:
                async with self._session.post(
                    provider.http_endpoint,
                    json=body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status == self.HTTP_OK:
                        data = await resp.json()
                        self._handle_success(provider)
                        
                        # Cache result
                        if cache_key and cache_type and "result" in data:
                            self._set_cache(cache_key, data["result"], cache_type.value)
                            
                        self._log_request(method, provider.name, False, time.time() - start_time)
                        return data
                        
                    elif resp.status == self.HTTP_RATE_LIMITED:
                        self._handle_rate_limit(provider)
                        self._metrics["fallback_used"] += 1
                        continue
                    else:
                        logger.debug(f"[RPC] {provider.name} HTTP {resp.status}")
                        provider.consecutive_errors += 1
                        continue
                        
            except asyncio.TimeoutError:
                logger.debug(f"[RPC] {provider.name} timeout")
                provider.consecutive_errors += 1
                continue
            except Exception as e:
                logger.debug(f"[RPC] {provider.name} error: {e}")
                provider.consecutive_errors += 1
                continue

    async def get_transaction(self, signature: str, use_cache: bool = True) -> dict | None:
        """Get transaction with caching."""
        cache_key = f"tx:{signature}" if use_cache else None
        cache_type = CacheType.TRANSACTION if use_cache else None
        
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        
        result = await self.post_rpc(body, cache_type, cache_key)
        return result.get("result") if result else None

    async def get_account_info(self, pubkey: str, use_cache: bool = True) -> dict | None:
        """Get account info with caching."""
        cache_key = f"acc:{pubkey}" if use_cache else None
        cache_type = CacheType.ACCOUNT_INFO if use_cache else None
        
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [pubkey, {"encoding": "base64"}],
        }
        
        result = await self.post_rpc(body, cache_type, cache_key)
        return result.get("result") if result else None

    async def get_balance(self, pubkey: str) -> int | None:
        """Get SOL balance (short cache)."""
        cache_key = f"bal:{pubkey}"
        
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [pubkey],
        }
        
        result = await self.post_rpc(body, CacheType.BALANCE, cache_key)
        if result and "result" in result:
            return result["result"].get("value")
        return None

    async def get_health(self) -> str | None:
        """Get node health."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        result = await self.post_rpc(body, CacheType.HEALTH, "health")
        return result.get("result") if result else None

    def get_metrics(self) -> dict[str, Any]:
        """Get current metrics."""
        helius_used = self._metrics["helius_credits_used"]
        chainstack_used = self._metrics["chainstack_requests"]
        
        cache_total = self._metrics["cache_hits"] + self._metrics["cache_misses"]
        cache_rate = (self._metrics["cache_hits"] / cache_total * 100) if cache_total > 0 else 0
        
        return {
            **self._metrics,
            "helius_daily_remaining": HELIUS_DAILY - helius_used,
            "chainstack_daily_remaining": CHAINSTACK_DAILY - chainstack_used,
            "cache_hit_rate": f"{cache_rate:.1f}%",
            "cache_size": len(self._cache),
            "providers": {
                name: {
                    "requests": p.total_requests,
                    "errors": p.total_errors,
                    "daily": p.daily_requests,
                    "in_backoff": time.time() < p.backoff_until,
                }
                for name, p in self.providers.items()
            },
        }

    def log_metrics(self) -> None:
        """Log current metrics."""
        m = self._metrics
        helius_pct = (m["helius_credits_used"] / HELIUS_DAILY * 100) if HELIUS_DAILY > 0 else 0
        chainstack_pct = (m["chainstack_requests"] / CHAINSTACK_DAILY * 100) if CHAINSTACK_DAILY > 0 else 0
        
        cache_total = m["cache_hits"] + m["cache_misses"]
        cache_rate = (m["cache_hits"] / cache_total * 100) if cache_total > 0 else 0
        
        logger.info(
            f"[RPC STATS] Requests: {m['total_requests']}, "
            f"Rate limited: {m['rate_limited']}, Fallbacks: {m['fallback_used']}"
        )
        logger.info(
            f"[RPC BUDGET] Helius: {m['helius_credits_used']}/{HELIUS_DAILY} ({helius_pct:.1f}%), "
            f"Chainstack: {m['chainstack_requests']}/{CHAINSTACK_DAILY} ({chainstack_pct:.1f}%)"
        )
        logger.info(f"[RPC CACHE] Hits: {m['cache_hits']}, Rate: {cache_rate:.1f}%, Size: {len(self._cache)}")

    async def close(self) -> None:
        """Close session."""
        if self._session:
            await self._session.close()
            self._session = None


async def get_rpc_manager() -> RPCManager:
    """Get global RPC manager."""
    return await RPCManager.get_instance()

    async def get_transaction_helius_enhanced(
        self,
        signature: str,
        use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """Get parsed transaction from Helius Enhanced API.
        
        This uses more credits (50 per request) but returns parsed data.
        """
        cache_key = f"tx_enhanced:{signature}"
        
        # Check cache
        if use_cache:
            cached = self._get_cache(cache_key)
            if cached is not None:
                return cached
        
        provider = self.providers.get("helius_enhanced")
        if not provider:
            # Fallback to regular getTransaction
            return await self.get_transaction(signature, use_cache)
        
        await self._wait_for_rate_limit(provider)
        
        try:
            async with self._session.post(
                provider.http_endpoint,
                json={"transactions": [signature]},
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == self.HTTP_OK:
                    data = await resp.json()
                    self._handle_success(provider)
                    if data and len(data) > 0:
                        self._set_cache(cache_key, data[0], 300)  # 5 min cache
                        return data[0]
                elif resp.status == self.HTTP_RATE_LIMITED:
                    self._handle_rate_limit(provider)
                    # Fallback to regular getTransaction
                    return await self.get_transaction(signature, use_cache)
        except Exception as e:
            logger.debug(f"[RPC] Helius Enhanced error: {e}")
        
        # Fallback to regular getTransaction
        return await self.get_transaction(signature, use_cache)

