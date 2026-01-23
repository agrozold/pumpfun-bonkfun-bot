"""
Global RPC Manager - Optimized with Dynamic Latency Testing.

ENDPOINTS (from .env):
- Chainstack: PRIMARY (3M req/month, HTTP + WSS)
- dRPC: FALLBACK #1 (HTTP + WSS)
- Syndica: FALLBACK #2 (HTTP + WSS) - NEW!
- Alchemy: FALLBACK #3
- QuickNode: FALLBACK #4 (WSS only)
- Public Solana: LAST RESORT
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

try:
    from core.dynamic_rpc_tester import DynamicRPCTester
    DYNAMIC_TESTER_AVAILABLE = True
except ImportError:
    DYNAMIC_TESTER_AVAILABLE = False
    logger.warning("[RPC] DynamicRPCTester not available")

try:
    from core.redis_cache import cache_get as redis_get, cache_set as redis_set
    REDIS_ENABLED = True
except ImportError:
    REDIS_ENABLED = False
    redis_get = lambda k: None
    redis_set = lambda k, v, t=60: False


NUM_BOTS = 6
CHAINSTACK_MONTHLY_REQUESTS = 3_000_000  # Updated to 3M!
CHAINSTACK_DAILY = CHAINSTACK_MONTHLY_REQUESTS // 30  # ~100k/day

MAX_CONSECUTIVE_ERRORS = 5
PROVIDER_COOLDOWN_SECONDS = 300


class CacheType(Enum):
    TRANSACTION = 300
    ACCOUNT_INFO = 3600
    HEALTH = 60
    SIGNATURE = 120
    BALANCE = 10
    TOKEN_ACCOUNTS = 300


@dataclass
class ProviderConfig:
    name: str
    http_endpoint: str
    wss_endpoint: str | None = None
    rate_limit_per_second: float = 0.1
    priority: int = 1
    enabled: bool = True
    is_primary: bool = False
    last_request_time: float = field(default=0.0, repr=False)
    consecutive_errors: int = field(default=0, repr=False)
    total_requests: int = field(default=0, repr=False)
    total_errors: int = field(default=0, repr=False)
    backoff_until: float = field(default=0.0, repr=False)
    disabled_until: float = field(default=0.0, repr=False)
    daily_requests: int = field(default=0, repr=False)
    daily_reset_time: float = field(default=0.0, repr=False)


class RPCManager:
    _instance: "RPCManager | None" = None
    _lock = asyncio.Lock()
    HTTP_OK = 200
    HTTP_RATE_LIMITED = 429

    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {}
        self._session: aiohttp.ClientSession | None = None
        self._initialized = False
        self._cache: dict[str, tuple[Any, float, float]] = {}
        self._cache_max_size = 2000
        self._metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "rate_limited": 0,
            "fallback_used": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "chainstack_requests": 0,
        }
        self._request_log: list[dict] = []
        self._max_log_entries = 100
        self._dynamic_tester: "DynamicRPCTester | None" = None

    @classmethod
    async def get_instance(cls) -> "RPCManager":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = RPCManager()
                    await cls._instance._initialize()
        return cls._instance

    async def _initialize(self) -> None:
        if self._initialized:
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=100, limit_per_host=20),
        )

        # === CHAINSTACK = PRIMARY (3M requests!) ===
        chainstack_http = os.getenv("CHAINSTACK_RPC_ENDPOINT")
        chainstack_wss = os.getenv("CHAINSTACK_WSS_ENDPOINT")
        if chainstack_http:
            self.providers["chainstack"] = ProviderConfig(
                name="Chainstack",
                http_endpoint=chainstack_http,
                wss_endpoint=chainstack_wss,
                rate_limit_per_second=0.5,  # Increased! 3M/month = 1.15/s safe
                priority=1,
                is_primary=True,
            )
            logger.info("[RPC] ✓ CHAINSTACK PRIMARY: priority=1, 0.5 req/s + WSS (3M/month)")

        # === dRPC = FALLBACK #1 ===
        drpc_http = os.getenv("DRPC_RPC_ENDPOINT")
        drpc_wss = os.getenv("DRPC_WSS_ENDPOINT")
        if drpc_http:
            self.providers["drpc"] = ProviderConfig(
                name="dRPC",
                http_endpoint=drpc_http,
                wss_endpoint=drpc_wss,
                rate_limit_per_second=0.2,
                priority=2,
                is_primary=False,
            )
            logger.info("[RPC] ✓ dRPC: priority=2, 0.2 req/s + WSS")

        # === SYNDICA = FALLBACK #2 (NEW!) ===
        syndica_http = os.getenv("SYNDICA_RPC_ENDPOINT")
        syndica_wss = os.getenv("SYNDICA_WSS_ENDPOINT")
        if syndica_http:
            self.providers["syndica"] = ProviderConfig(
                name="Syndica",
                http_endpoint=syndica_http,
                wss_endpoint=syndica_wss,
                rate_limit_per_second=0.2,
                priority=3,
                is_primary=False,
            )
            logger.info("[RPC] ✓ SYNDICA: priority=3, 0.2 req/s + WSS")

        # === ALCHEMY = FALLBACK #3 ===
        alchemy_http = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy_http:
            self.providers["alchemy"] = ProviderConfig(
                name="Alchemy",
                http_endpoint=alchemy_http,
                rate_limit_per_second=0.1,
                priority=4,
                is_primary=False,
            )
            logger.info("[RPC] ✓ ALCHEMY: priority=4, 0.1 req/s")

        # === QUICKNODE = FALLBACK #4 (WSS) ===
        quicknode_wss = os.getenv("QUICKNODE_WSS_ENDPOINT")
        if quicknode_wss:
            quicknode_http = quicknode_wss.replace("wss://", "https://")
            self.providers["quicknode"] = ProviderConfig(
                name="QuickNode",
                http_endpoint=quicknode_http,
                wss_endpoint=quicknode_wss,
                rate_limit_per_second=0.15,
                priority=5,
                is_primary=False,
            )
            logger.info("[RPC] ✓ QUICKNODE: priority=5, 0.15 req/s + WSS")

        # === PUBLIC SOLANA = LAST RESORT ===
        self.providers["public_solana"] = ProviderConfig(
            name="Public Solana",
            http_endpoint="https://api.mainnet-beta.solana.com",
            wss_endpoint="wss://api.mainnet-beta.solana.com",
            rate_limit_per_second=0.05,
            priority=20,
            is_primary=False,
        )
        logger.info("[RPC] ✓ PUBLIC SOLANA: priority=20, 0.05 req/s (last resort)")

        self._initialized = True
        logger.info(f"[RPC] Initialized {len(self.providers)} providers")
        logger.info(f"[RPC] Daily budget: Chainstack {CHAINSTACK_DAILY:,} requests")

        # Start dynamic tester
        if DYNAMIC_TESTER_AVAILABLE:
            test_interval = int(os.getenv("RPC_TEST_INTERVAL", "30"))
            self._dynamic_tester = DynamicRPCTester(self, test_interval=test_interval)
            asyncio.create_task(self._dynamic_tester.start())
            logger.info(f"[RPC] Dynamic latency testing enabled (interval: {test_interval}s)")

    def _get_cache(self, key: str) -> Any | None:
        if key in self._cache:
            value, timestamp, ttl = self._cache[key]
            if time.time() - timestamp < ttl:
                self._metrics["cache_hits"] += 1
                return value
            else:
                del self._cache[key]
        if REDIS_ENABLED:
            cached = redis_get(f"rpc:{key}")
            if cached is not None:
                self._metrics["cache_hits"] += 1
                self._cache[key] = (cached, time.time(), 3600)
                return cached
        self._metrics["cache_misses"] += 1
        return None

    def _set_cache(self, key: str, value: Any, ttl: float) -> None:
        self._cache[key] = (value, time.time(), ttl)
        if REDIS_ENABLED:
            redis_set(f"rpc:{key}", value, int(ttl))
        if len(self._cache) > self._cache_max_size:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]

    def _get_available_provider(self, exclude: set[str] | None = None) -> ProviderConfig | None:
        """Get best available provider with latency-aware selection."""
        exclude = exclude or set()
        now = time.time()
        available = []

        for name, provider in self.providers.items():
            if name in exclude or not provider.enabled:
                continue
            if now < provider.disabled_until:
                continue
            if now < provider.backoff_until:
                continue

            time_since_last = now - provider.last_request_time
            min_interval = 1.0 / provider.rate_limit_per_second
            if time_since_last < min_interval:
                continue

            if now - provider.daily_reset_time > 86400:
                provider.daily_requests = 0
                provider.daily_reset_time = now

            base_score = float(provider.priority)

            if self._dynamic_tester:
                latency_score = self._dynamic_tester.get_latency_score(name)
                base_score += latency_score

            if provider.consecutive_errors > 0:
                base_score += provider.consecutive_errors * 2.0

            available.append((base_score, name, provider))

        if not available:
            return None

        available.sort(key=lambda x: x[0])
        return available[0][2]

    async def _wait_for_rate_limit(self, provider: ProviderConfig) -> None:
        now = time.time()
        min_interval = 1.0 / provider.rate_limit_per_second
        time_since_last = now - provider.last_request_time
        if time_since_last < min_interval:
            await asyncio.sleep(min_interval - time_since_last)
        provider.last_request_time = time.time()

    def _check_provider_health(self, provider: ProviderConfig) -> None:
        if provider.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            provider.disabled_until = time.time() + PROVIDER_COOLDOWN_SECONDS
            logger.warning(f"[RPC] ⚠️ {provider.name} DISABLED for {PROVIDER_COOLDOWN_SECONDS}s")

    def _try_recover_provider(self, provider: ProviderConfig) -> None:
        now = time.time()
        if provider.disabled_until > 0 and now >= provider.disabled_until:
            logger.info(f"[RPC] ✓ {provider.name} recovered")
            provider.disabled_until = 0
            provider.consecutive_errors = 0
            provider.backoff_until = 0

    def _handle_rate_limit(self, provider: ProviderConfig) -> None:
        provider.consecutive_errors += 1
        provider.total_errors += 1
        self._metrics["rate_limited"] += 1
        backoff = min(2 ** provider.consecutive_errors, 60)
        provider.backoff_until = time.time() + backoff
        logger.warning(f"[RPC] {provider.name} rate limited (429), backoff {backoff}s")

    def _handle_success(self, provider: ProviderConfig) -> None:
        provider.consecutive_errors = 0
        self._try_recover_provider(provider)
        provider.total_requests += 1
        provider.daily_requests += 1
        self._metrics["total_requests"] += 1
        self._metrics["successful_requests"] += 1
        if "chainstack" in provider.name.lower():
            self._metrics["chainstack_requests"] += 1

    async def post_rpc(
        self,
        body: dict[str, Any],
        cache_type: CacheType | None = None,
        cache_key: str | None = None,
    ) -> dict[str, Any] | None:
        if not self._session:
            await self._initialize()

        start_time = time.time()
        method = body.get("method", "unknown")

        if cache_key and cache_type:
            cached = self._get_cache(cache_key)
            if cached is not None:
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
                        if cache_key and cache_type and "result" in data:
                            self._set_cache(cache_key, data["result"], cache_type.value)
                        return data
                    elif resp.status == self.HTTP_RATE_LIMITED:
                        self._handle_rate_limit(provider)
                        self._metrics["fallback_used"] += 1
                        continue
                    else:
                        provider.consecutive_errors += 1
                        continue
            except asyncio.TimeoutError:
                provider.consecutive_errors += 1
                self._check_provider_health(provider)
                continue
            except Exception as e:
                logger.debug(f"[RPC] {provider.name} error: {e}")
                provider.consecutive_errors += 1
                self._check_provider_health(provider)
                continue

    async def get_transaction(self, signature: str, use_cache: bool = True) -> dict | None:
        cache_key = f"tx:{signature}" if use_cache else None
        cache_type = CacheType.TRANSACTION if use_cache else None
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        result = await self.post_rpc(body, cache_type, cache_key)
        return result.get("result") if result else None

    async def get_account_info(self, pubkey: str, use_cache: bool = True) -> dict | None:
        cache_key = f"acc:{pubkey}" if use_cache else None
        cache_type = CacheType.ACCOUNT_INFO if use_cache else None
        body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [pubkey, {"encoding": "base64"}]}
        result = await self.post_rpc(body, cache_type, cache_key)
        return result.get("result") if result else None

    async def get_balance(self, pubkey: str) -> int | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
        result = await self.post_rpc(body, CacheType.BALANCE, f"bal:{pubkey}")
        if result and "result" in result:
            return result["result"].get("value")
        return None

    def get_wss_provider(self) -> tuple[str, str] | None:
        """Get best WSS. Priority: Chainstack > Syndica > dRPC > QuickNode > Public."""
        now = time.time()
        wss_priority = ["chainstack", "syndica", "drpc", "quicknode", "public_solana"]
        
        for name in wss_priority:
            provider = self.providers.get(name)
            if provider and provider.wss_endpoint and provider.enabled:
                if now >= provider.backoff_until and now >= provider.disabled_until:
                    return (name, provider.wss_endpoint)

        logger.warning("[RPC] No WSS providers available!")
        return None

    def get_wss_endpoint(self) -> str | None:
        result = self.get_wss_provider()
        return result[1] if result else None

    def report_wss_error(self, provider_name: str) -> None:
        provider = self.providers.get(provider_name)
        if provider:
            provider.consecutive_errors += 1
            provider.total_errors += 1
            if provider.consecutive_errors >= 3:
                backoff = min(2 ** provider.consecutive_errors, 120)
                provider.backoff_until = time.time() + backoff
                logger.warning(f"[RPC] WSS {provider.name} backoff {backoff}s")

    def report_wss_success(self, provider_name: str) -> None:
        provider = self.providers.get(provider_name)
        if provider:
            provider.consecutive_errors = 0

    async def get_health(self) -> str | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        result = await self.post_rpc(body, CacheType.HEALTH, "health")
        return result.get("result") if result else None

    def get_metrics(self) -> dict[str, Any]:
        chainstack_used = self._metrics["chainstack_requests"]
        cache_total = self._metrics["cache_hits"] + self._metrics["cache_misses"]
        cache_rate = (self._metrics["cache_hits"] / cache_total * 100) if cache_total > 0 else 0
        return {
            **self._metrics,
            "chainstack_daily_remaining": CHAINSTACK_DAILY - chainstack_used,
            "cache_hit_rate": f"{cache_rate:.1f}%",
            "cache_size": len(self._cache),
            "providers": {
                name: {
                    "requests": p.total_requests,
                    "errors": p.total_errors,
                    "daily": p.daily_requests,
                    "in_backoff": time.time() < p.backoff_until,
                    "disabled": time.time() < p.disabled_until,
                }
                for name, p in self.providers.items()
            },
        }

    def get_latency_stats(self) -> dict:
        if self._dynamic_tester:
            return self._dynamic_tester.get_endpoint_stats()
        return {}

    def log_metrics(self) -> None:
        m = self._metrics
        chainstack_pct = (m["chainstack_requests"] / CHAINSTACK_DAILY * 100) if CHAINSTACK_DAILY > 0 else 0
        cache_total = m["cache_hits"] + m["cache_misses"]
        cache_rate = (m["cache_hits"] / cache_total * 100) if cache_total > 0 else 0
        logger.info(f"[RPC] Requests: {m['total_requests']}, Rate limited: {m['rate_limited']}")
        logger.info(f"[RPC] Chainstack: {m['chainstack_requests']}/{CHAINSTACK_DAILY} ({chainstack_pct:.1f}%)")
        logger.info(f"[RPC] Cache: {cache_rate:.1f}% hit rate")

    async def close(self) -> None:
        if self._dynamic_tester:
            await self._dynamic_tester.stop()
        if self._session:
            await self._session.close()
            self._session = None


async def get_rpc_manager() -> RPCManager:
    return await RPCManager.get_instance()
