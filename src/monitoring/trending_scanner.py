"""
Multi-Source Trending Token Scanner.

Сканирует токены из нескольких источников:
- DexScreener (без лимитов)
- Jupiter Lite API (60 req/min)
- Birdeye (fallback)

Распределяет запросы чтобы не превышать лимиты API.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)


class DataSource(Enum):
    """Источники данных для мониторинга."""
    DEXSCREENER = "dexscreener"
    JUPITER = "jupiter"
    BIRDEYE = "birdeye"


@dataclass
class TrendingToken:
    """Трендовый токен."""
    mint: str
    symbol: str
    name: str
    price_usd: float
    volume_24h: float
    volume_1h: float
    volume_5m: float
    market_cap: float
    price_change_5m: float
    price_change_1h: float
    price_change_24h: float
    buys_5m: int
    sells_5m: int
    buys_1h: int
    sells_1h: int
    liquidity: float
    created_at: datetime | None
    pair_address: str | None = None
    dex_id: str | None = None
    source: DataSource = DataSource.DEXSCREENER

    @property
    def buy_pressure_5m(self) -> float:
        """Процент покупок за 5 минут."""
        total = self.buys_5m + self.sells_5m
        return self.buys_5m / total if total > 0 else 0

    @property
    def buy_pressure_1h(self) -> float:
        """Процент покупок за 1 час."""
        total = self.buys_1h + self.sells_1h
        return self.buys_1h / total if total > 0 else 0

    @property
    def trade_velocity(self) -> int:
        """Количество сделок за 5 минут."""
        return self.buys_5m + self.sells_5m

    @property
    def volume_ratio(self) -> float:
        """Отношение объёма 5м к среднему 5м за час."""
        avg_5m = self.volume_1h / 12 if self.volume_1h > 0 else 0
        return self.volume_5m / avg_5m if avg_5m > 0 else 0


@dataclass
class RateLimiter:
    """Rate limiter с дневным/месячным бюджетом."""
    # Per-window limits
    max_requests: int
    window_seconds: float
    # Daily budget (для экономии free tier)
    daily_budget: int = 0  # 0 = unlimited
    # State
    requests: list = field(default_factory=list)
    daily_requests: int = field(default=0)
    last_reset_day: int = field(default=0)

    def _reset_daily_if_needed(self) -> None:
        """Reset daily counter at midnight."""
        today = datetime.utcnow().timetuple().tm_yday
        if today != self.last_reset_day:
            self.daily_requests = 0
            self.last_reset_day = today

    def can_request(self) -> bool:
        """Проверить можно ли делать запрос."""
        self._reset_daily_if_needed()
        now = datetime.utcnow().timestamp()
        # Очистить старые запросы
        self.requests = [t for t in self.requests if now - t < self.window_seconds]
        
        # Check window limit
        if len(self.requests) >= self.max_requests:
            return False
        
        # Check daily budget
        if self.daily_budget > 0 and self.daily_requests >= self.daily_budget:
            return False
        
        return True

    def record_request(self) -> None:
        """Записать запрос."""
        self._reset_daily_if_needed()
        self.requests.append(datetime.utcnow().timestamp())
        self.daily_requests += 1

    def time_until_available(self) -> float:
        """Время до следующего доступного слота."""
        if self.can_request():
            return 0
        now = datetime.utcnow().timestamp()
        if self.requests:
            oldest = min(self.requests)
            return max(0, self.window_seconds - (now - oldest))
        return 0

    def get_daily_remaining(self) -> int:
        """Сколько запросов осталось на сегодня."""
        self._reset_daily_if_needed()
        if self.daily_budget <= 0:
            return 999999
        return max(0, self.daily_budget - self.daily_requests)

    def get_usage_percent(self) -> float:
        """Процент использования дневного бюджета."""
        self._reset_daily_if_needed()
        if self.daily_budget <= 0:
            return 0
        return (self.daily_requests / self.daily_budget) * 100


# API endpoints
DEXSCREENER_API = "https://api.dexscreener.com"
JUPITER_LITE_API = "https://lite-api.jup.ag"
JUPITER_PRICE_API = "https://price.jup.ag"
BIRDEYE_API = "https://public-api.birdeye.so"


class TrendingScanner:
    """Мульти-источник сканер трендовых токенов."""

    def __init__(
        self,
        # Фильтры
        min_volume_1h: float = 50000,
        min_market_cap: float = 10000,
        max_market_cap: float = 5000000,
        min_liquidity: float = 5000,
        max_token_age_hours: float = 24,
        # Триггеры
        min_price_change_5m: float = 5,
        min_price_change_1h: float = 20,
        min_buy_pressure: float = 0.65,
        min_trade_velocity: int = 15,
        min_volume_ratio: float = 3.0,
        # Настройки
        scan_interval: float = 30,
        max_concurrent_buys: int = 3,
        token_monitor_ttl: float = 120,
        # API keys (optional)
        jupiter_api_key: str | None = None,
        birdeye_api_key: str | None = None,
    ):
        self.min_volume_1h = min_volume_1h
        self.min_market_cap = min_market_cap
        self.max_market_cap = max_market_cap
        self.min_liquidity = min_liquidity
        self.max_token_age_hours = max_token_age_hours

        self.min_price_change_5m = min_price_change_5m
        self.min_price_change_1h = min_price_change_1h
        self.min_buy_pressure = min_buy_pressure
        self.min_trade_velocity = min_trade_velocity
        self.min_volume_ratio = min_volume_ratio

        self.scan_interval = scan_interval
        self.max_concurrent_buys = max_concurrent_buys
        self.token_monitor_ttl = token_monitor_ttl

        # API keys from params or env
        self.jupiter_api_key = jupiter_api_key or os.getenv("JUPITER_API_KEY")
        self.birdeye_api_key = birdeye_api_key or os.getenv("BIRDEYE_API_KEY")

        # Rate limiters с дневными бюджетами
        # 
        # Jupiter Free Tier (Basic):
        #   - Price API: 1 RPS = 60 req/min = 86,400/day
        #   - Tokens API: ~1 RPS
        #   - При scan каждые 30s = 2 req/min = 2,880/day (3.3% от лимита)
        #
        # DexScreener: без строгих лимитов, используем как основной
        # Birdeye: зависит от tier
        #
        self.rate_limiters = {
            DataSource.DEXSCREENER: RateLimiter(
                max_requests=20, window_seconds=10, daily_budget=0  # unlimited
            ),
            DataSource.JUPITER: RateLimiter(
                max_requests=55,      # 55 req/min (оставляем запас от 60)
                window_seconds=60,
                daily_budget=10000    # ~12% от 86,400 - консервативно
            ),
            DataSource.BIRDEYE: RateLimiter(
                max_requests=5, window_seconds=60, daily_budget=1000
            ),
        }

        # Source priority (higher = more preferred when available)
        self._source_priority = {
            DataSource.DEXSCREENER: 100,  # Primary - no limits
            DataSource.JUPITER: 50,       # Secondary - has free tier
            DataSource.BIRDEYE: 25,       # Tertiary - needs API key
        }

        # State
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self.on_trending_token: Callable | None = None

        # Track tokens
        self.processed_tokens: set[str] = set()
        self.processed_tokens_timestamps: dict[str, float] = {}
        self.monitored_tokens: dict[str, float] = {}

        # Enabled sources with smart rotation
        self._enabled_sources: list[DataSource] = [DataSource.DEXSCREENER]
        self._enabled_sources.append(DataSource.JUPITER)
        if self.birdeye_api_key:
            self._enabled_sources.append(DataSource.BIRDEYE)

        # Rotation state - tracks which source to use next
        self._last_source_used: DataSource | None = None
        self._scan_count = 0

        logger.info(
            f"TrendingScanner initialized: sources={[s.value for s in self._enabled_sources]}, "
            f"scan_interval={scan_interval}s, token_ttl={token_monitor_ttl}s"
        )

    def set_callback(self, callback: Callable) -> None:
        """Установить callback для найденных токенов."""
        self.on_trending_token = callback

    async def start(self) -> None:
        """Запустить сканер."""
        if self._running:
            return

        self._running = True
        self._session = aiohttp.ClientSession()
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info(f"🔍 Multi-source scanner started: {[s.value for s in self._enabled_sources]}")

    async def stop(self) -> None:
        """Остановить сканер."""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
        if self._session:
            await self._session.close()
        logger.info("Trending scanner stopped")

    async def _scan_loop(self) -> None:
        """Основной цикл сканирования с умной ротацией источников."""
        while self._running:
            try:
                await self._scan_trending()
                self._scan_count += 1
                
                # Log stats every 10 scans
                if self._scan_count % 10 == 0:
                    self._log_budget_stats()
                
                await asyncio.sleep(self.scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Scan error: {e}")
                await asyncio.sleep(10)

    def _log_budget_stats(self) -> None:
        """Log daily budget usage stats."""
        stats = []
        for source in self._enabled_sources:
            limiter = self.rate_limiters[source]
            remaining = limiter.get_daily_remaining()
            usage = limiter.get_usage_percent()
            if limiter.daily_budget > 0:
                stats.append(f"{source.value}: {remaining} left ({usage:.1f}% used)")
        if stats:
            logger.info(f"📊 Daily budget: {', '.join(stats)}")

    def _select_sources_for_scan(self) -> list[DataSource]:
        """Выбрать источники для текущего скана с учётом бюджета и ротации."""
        available = []
        
        for source in self._enabled_sources:
            limiter = self.rate_limiters[source]
            
            # Skip if rate limited
            if not limiter.can_request():
                continue
            
            # Skip if daily budget exhausted (except unlimited sources)
            if limiter.daily_budget > 0:
                remaining = limiter.get_daily_remaining()
                usage_pct = limiter.get_usage_percent()
                
                # Экономим бюджет: если использовано >80%, пропускаем каждый 2й запрос
                if usage_pct > 80 and self._scan_count % 2 == 0:
                    continue
                # Если использовано >90%, пропускаем каждые 3 из 4 запросов
                if usage_pct > 90 and self._scan_count % 4 != 0:
                    continue
            
            available.append(source)
        
        # Sort by priority
        available.sort(key=lambda s: self._source_priority[s], reverse=True)
        
        # Smart rotation: DexScreener always, others rotate
        result = []
        
        # Always include DexScreener if available (no limits)
        if DataSource.DEXSCREENER in available:
            result.append(DataSource.DEXSCREENER)
        
        # Add one secondary source (rotate between Jupiter/Birdeye)
        secondary = [s for s in available if s != DataSource.DEXSCREENER]
        if secondary:
            # Rotate: use different source than last time
            if self._last_source_used in secondary and len(secondary) > 1:
                secondary.remove(self._last_source_used)
            result.append(secondary[0])
            self._last_source_used = secondary[0]
        
        return result

    def _get_next_source(self) -> DataSource:
        """Deprecated - use _select_sources_for_scan instead."""
        sources = self._select_sources_for_scan()
        return sources[0] if sources else DataSource.DEXSCREENER

    async def _scan_trending(self) -> None:
        """Сканировать токены с умной ротацией источников."""
        self._cleanup_processed()

        all_tokens: list[TrendingToken] = []
        seen_mints: set[str] = set()

        # Select sources for this scan (budget-aware)
        sources_to_use = self._select_sources_for_scan()
        
        if not sources_to_use:
            logger.warning("⚠️ No sources available (all rate limited or budget exhausted)")
            return

        # Fetch from selected sources
        for source in sources_to_use:
            limiter = self.rate_limiters[source]

            try:
                tokens = await self._fetch_from_source(source)
                limiter.record_request()

                for token in tokens:
                    if token.mint not in seen_mints:
                        seen_mints.add(token.mint)
                        all_tokens.append(token)

                logger.debug(
                    f"Fetched {len(tokens)} from {source.value} "
                    f"(daily: {limiter.daily_requests}/{limiter.daily_budget or '∞'})"
                )
            except Exception as e:
                logger.debug(f"Error fetching from {source.value}: {e}")

            await asyncio.sleep(0.2)

        if not all_tokens:
            logger.debug("No tokens fetched from any source")
            return

        logger.info(f"🔍 Scanned {len(all_tokens)} tokens from {len(self._enabled_sources)} sources")

        # Filter and score
        candidates = []
        for token in all_tokens:
            if token.mint in self.processed_tokens:
                continue
            if self.is_being_monitored(token.mint):
                continue

            score, reasons = self._evaluate_token(token)
            if score > 0:
                candidates.append((token, score, reasons))

        stats = self.get_monitoring_stats()
        if candidates:
            logger.info(
                f"📊 Found {len(candidates)} candidates "
                f"(monitoring: {stats['active_monitored']}, processed: {stats['total_processed']})"
            )

        candidates.sort(key=lambda x: x[1], reverse=True)

        for token, score, reasons in candidates[: self.max_concurrent_buys]:
            logger.warning(
                f"🔥 TRENDING [{token.source.value}]: {token.symbol} - "
                f"MC: ${token.market_cap:,.0f}, Vol1h: ${token.volume_1h:,.0f}, "
                f"Change5m: {token.price_change_5m:+.1f}%, Score: {score}"
            )
            for reason in reasons:
                logger.info(f"   ✓ {reason}")

            self.add_to_monitoring(token.mint)
            self.processed_tokens.add(token.mint)
            self.processed_tokens_timestamps[token.mint] = datetime.utcnow().timestamp()

            if self.on_trending_token:
                await self.on_trending_token(token)

    async def _fetch_from_source(self, source: DataSource) -> list[TrendingToken]:
        """Fetch токенов из конкретного источника."""
        if source == DataSource.DEXSCREENER:
            return await self._fetch_dexscreener()
        elif source == DataSource.JUPITER:
            return await self._fetch_jupiter()
        elif source == DataSource.BIRDEYE:
            return await self._fetch_birdeye()
        return []

    # ==================== DEXSCREENER ====================

    async def _fetch_dexscreener(self) -> list[TrendingToken]:
        """Fetch токенов с DexScreener по объёму."""
        if not self._session:
            return []

        tokens = []
        seen_mints: set[str] = set()

        try:
            # Fetch from pumpswap and raydium
            for dex in ["pumpswap", "raydium"]:
                url = f"{DEXSCREENER_API}/latest/dex/pairs/solana/{dex}"

                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    pairs = data.get("pairs", [])

                    # Sort by volume spike
                    pairs_sorted = self._sort_by_volume_spike(pairs)

                    for pair, _, _ in pairs_sorted[:30]:
                        base = pair.get("baseToken", {})
                        mint = base.get("address", "")

                        # Support both pump.fun and bonk.fun tokens
                        if mint in seen_mints:
                            continue
                        if not (mint.endswith("pump") or mint.endswith("bonk")):
                            continue

                        seen_mints.add(mint)
                        token = self._parse_dexscreener_pair(pair)
                        if token:
                            tokens.append(token)

                await asyncio.sleep(0.3)

            # Also search for newer tokens
            search_tokens = await self._fetch_dexscreener_search()
            for token in search_tokens:
                if token.mint not in seen_mints:
                    seen_mints.add(token.mint)
                    tokens.append(token)

        except Exception as e:
            logger.debug(f"DexScreener fetch error: {e}")

        tokens.sort(key=lambda t: t.volume_ratio, reverse=True)
        return tokens[:40]

    async def _fetch_dexscreener_search(self) -> list[TrendingToken]:
        """Search pump.fun tokens on DexScreener."""
        if not self._session:
            return []

        tokens = []
        try:
            url = f"{DEXSCREENER_API}/latest/dex/search?q=pump"

            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                for pair in data.get("pairs", [])[:30]:
                    if pair.get("chainId") != "solana":
                        continue

                    base = pair.get("baseToken", {})
                    mint_addr = base.get("address", "")
                    # Support both pump.fun and bonk.fun tokens
                    if not (mint_addr.endswith("pump") or mint_addr.endswith("bonk")):
                        continue

                    token = self._parse_dexscreener_pair(pair)
                    if token:
                        tokens.append(token)

        except Exception as e:
            logger.debug(f"DexScreener search error: {e}")

        return tokens

    def _sort_by_volume_spike(self, pairs: list) -> list[tuple]:
        """Sort pairs by volume spike ratio."""
        result = []
        for pair in pairs:
            vol_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
            vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
            avg_5m = vol_1h / 12 if vol_1h > 0 else 0
            ratio = vol_5m / avg_5m if avg_5m > 0 else 0
            result.append((pair, ratio, vol_5m))
        result.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return result

    def _parse_dexscreener_pair(self, pair: dict) -> TrendingToken | None:
        """Parse DexScreener pair to TrendingToken."""
        try:
            base = pair.get("baseToken", {})
            txns = pair.get("txns", {})
            m5 = txns.get("m5", {})
            h1 = txns.get("h1", {})
            volume = pair.get("volume", {})

            created_at = None
            if pair.get("pairCreatedAt"):
                created_at = datetime.fromtimestamp(pair["pairCreatedAt"] / 1000)

            return TrendingToken(
                mint=base.get("address", ""),
                symbol=base.get("symbol", ""),
                name=base.get("name", ""),
                price_usd=float(pair.get("priceUsd", 0) or 0),
                volume_24h=float(volume.get("h24", 0) or 0),
                volume_1h=float(volume.get("h1", 0) or 0),
                volume_5m=float(volume.get("m5", 0) or 0),
                market_cap=float(pair.get("marketCap", 0) or 0),
                price_change_5m=float(pair.get("priceChange", {}).get("m5", 0) or 0),
                price_change_1h=float(pair.get("priceChange", {}).get("h1", 0) or 0),
                price_change_24h=float(pair.get("priceChange", {}).get("h24", 0) or 0),
                buys_5m=m5.get("buys", 0),
                sells_5m=m5.get("sells", 0),
                buys_1h=h1.get("buys", 0),
                sells_1h=h1.get("sells", 0),
                liquidity=float(pair.get("liquidity", {}).get("usd", 0) or 0),
                created_at=created_at,
                pair_address=pair.get("pairAddress"),
                dex_id=pair.get("dexId"),
                source=DataSource.DEXSCREENER,
            )
        except Exception as e:
            logger.debug(f"Parse DexScreener error: {e}")
            return None

    # ==================== JUPITER ====================

    async def _fetch_jupiter(self) -> list[TrendingToken]:
        """Fetch trending tokens from Jupiter (free tier - 1 RPS).
        
        Uses:
        - /tokens/v1/tagged/pump-fun - list of pump.fun tokens
        - /v6/price - batch price data
        """
        if not self._session:
            return []

        tokens = []

        try:
            # Get pump.fun tagged tokens from Jupiter
            # This endpoint lists tokens tagged as pump-fun
            url = f"{JUPITER_LITE_API}/tokens/v1/tagged/pump-fun"

            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Jupiter tokens error: {resp.status}")
                    # Try alternative endpoint
                    return await self._fetch_jupiter_all_tokens()

                data = await resp.json()

                if not data:
                    return await self._fetch_jupiter_all_tokens()

                # Get price data for tokens (batch request - counts as 1 req)
                mints = [t.get("address") for t in data[:25] if t.get("address")]

                if mints:
                    prices = await self._fetch_jupiter_prices(mints)

                    for token_data in data[:25]:
                        mint = token_data.get("address", "")
                        if not mint:
                            continue

                        price_info = prices.get(mint, {})
                        token = self._parse_jupiter_token(token_data, price_info)
                        if token:
                            tokens.append(token)

        except Exception as e:
            logger.debug(f"Jupiter fetch error: {e}")

        return tokens

    async def _fetch_jupiter_all_tokens(self) -> list[TrendingToken]:
        """Fallback: fetch from all tokens and filter pump.fun."""
        if not self._session:
            return []

        tokens = []
        try:
            # Get all tradeable tokens
            url = f"{JUPITER_LITE_API}/tokens/v1/mints/tradable"

            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return []

                mints = await resp.json()

                # Filter pump.fun and bonk.fun tokens (end with "pump" or "bonk")
                pump_mints = [m for m in mints if m.endswith("pump") or m.endswith("bonk")][:25]

                if pump_mints:
                    prices = await self._fetch_jupiter_prices(pump_mints)

                    for mint in pump_mints:
                        price_info = prices.get(mint, {})
                        token = TrendingToken(
                            mint=mint,
                            symbol=mint[:6],
                            name=mint[:6],
                            price_usd=float(price_info.get("price", 0) or 0),
                            volume_24h=0,
                            volume_1h=0,
                            volume_5m=0,
                            market_cap=0,
                            price_change_5m=0,
                            price_change_1h=0,
                            price_change_24h=0,
                            buys_5m=0,
                            sells_5m=0,
                            buys_1h=0,
                            sells_1h=0,
                            liquidity=0,
                            created_at=None,
                            source=DataSource.JUPITER,
                        )
                        tokens.append(token)

        except Exception as e:
            logger.debug(f"Jupiter all tokens error: {e}")

        return tokens

    async def _fetch_jupiter_prices(self, mints: list[str]) -> dict:
        """Fetch prices from Jupiter Price API (batched)."""
        if not self._session or not mints:
            return {}

        try:
            # Batch up to 100 tokens per request
            ids = ",".join(mints[:100])
            url = f"{JUPITER_PRICE_API}/v6/price?ids={ids}"

            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return {}

                data = await resp.json()
                return data.get("data", {})

        except Exception as e:
            logger.debug(f"Jupiter price error: {e}")
            return {}

    def _parse_jupiter_token(
        self, token_data: dict, price_info: dict
    ) -> TrendingToken | None:
        """Parse Jupiter token data to TrendingToken."""
        try:
            mint = token_data.get("address", "")
            price = float(price_info.get("price", 0) or 0)

            return TrendingToken(
                mint=mint,
                symbol=token_data.get("symbol", ""),
                name=token_data.get("name", ""),
                price_usd=price,
                volume_24h=0,  # Jupiter doesn't provide volume in token list
                volume_1h=0,
                volume_5m=0,
                market_cap=0,
                price_change_5m=0,
                price_change_1h=0,
                price_change_24h=0,
                buys_5m=0,
                sells_5m=0,
                buys_1h=0,
                sells_1h=0,
                liquidity=0,
                created_at=None,
                source=DataSource.JUPITER,
            )
        except Exception as e:
            logger.debug(f"Parse Jupiter error: {e}")
            return None

    # ==================== BIRDEYE ====================

    async def _fetch_birdeye(self) -> list[TrendingToken]:
        """Fetch trending tokens from Birdeye (requires API key)."""
        if not self._session or not self.birdeye_api_key:
            return []

        tokens = []

        try:
            # Birdeye trending tokens endpoint
            url = f"{BIRDEYE_API}/defi/token_trending"
            headers = {
                "X-API-KEY": self.birdeye_api_key,
                "x-chain": "solana",
            }

            async with self._session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Birdeye error: {resp.status}")
                    return []

                data = await resp.json()

                for item in data.get("data", {}).get("items", [])[:30]:
                    token = self._parse_birdeye_token(item)
                    # Support both pump.fun and bonk.fun tokens
                    if token and (token.mint.endswith("pump") or token.mint.endswith("bonk")):
                        tokens.append(token)

        except Exception as e:
            logger.debug(f"Birdeye fetch error: {e}")

        return tokens

    def _parse_birdeye_token(self, item: dict) -> TrendingToken | None:
        """Parse Birdeye token data to TrendingToken."""
        try:
            return TrendingToken(
                mint=item.get("address", ""),
                symbol=item.get("symbol", ""),
                name=item.get("name", ""),
                price_usd=float(item.get("price", 0) or 0),
                volume_24h=float(item.get("volume24h", 0) or 0),
                volume_1h=float(item.get("volume24h", 0) or 0) / 24,
                volume_5m=0,
                market_cap=float(item.get("mc", 0) or 0),
                price_change_5m=0,
                price_change_1h=float(item.get("priceChange1h", 0) or 0),
                price_change_24h=float(item.get("priceChange24h", 0) or 0),
                buys_5m=0,
                sells_5m=0,
                buys_1h=0,
                sells_1h=0,
                liquidity=float(item.get("liquidity", 0) or 0),
                created_at=None,
                source=DataSource.BIRDEYE,
            )
        except Exception as e:
            logger.debug(f"Parse Birdeye error: {e}")
            return None

    # ==================== EVALUATION ====================

    def _evaluate_token(self, token: TrendingToken) -> tuple[int, list[str]]:
        """Оценить токен. Возвращает (score, reasons)."""
        score = 0
        reasons = []

        # Basic filters
        if token.volume_1h < self.min_volume_1h:
            return 0, []
        if token.market_cap < self.min_market_cap:
            return 0, []
        if token.market_cap > self.max_market_cap:
            return 0, []
        if token.liquidity < self.min_liquidity:
            return 0, []

        # Age filter
        if token.created_at:
            age_hours = (datetime.utcnow() - token.created_at).total_seconds() / 3600
            if age_hours > self.max_token_age_hours:
                return 0, []

        # Price change criteria
        if token.price_change_5m >= self.min_price_change_5m:
            score += 35
            reasons.append(f"🚀 Price +{token.price_change_5m:.1f}% in 5min!")
        elif token.price_change_1h >= self.min_price_change_1h:
            score += 25
            reasons.append(f"📈 Price +{token.price_change_1h:.1f}% in 1h")
        else:
            return 0, []

        # Buy pressure
        if token.buy_pressure_5m >= self.min_buy_pressure:
            score += 30
            reasons.append(f"💪 Buy pressure {token.buy_pressure_5m*100:.0f}%")
        elif token.buy_pressure_5m >= 0.5:
            score += 15
            reasons.append(f"👍 Buy pressure {token.buy_pressure_5m*100:.0f}%")

        # Trade velocity
        if token.trade_velocity >= self.min_trade_velocity:
            score += 20
            reasons.append(f"⚡ {token.trade_velocity} trades in 5min")

        # Volume ratio
        if token.volume_ratio >= self.min_volume_ratio:
            score += 15
            reasons.append(f"📈 Volume {token.volume_ratio:.1f}x average")

        # Bonus for 1h growth
        if token.price_change_1h >= self.min_price_change_1h:
            score += 10

        # Bonus for early market cap
        if 20000 <= token.market_cap <= 200000:
            score += 10
            reasons.append(f"🎯 Early MC: ${token.market_cap:,.0f}")

        return score, reasons

    # ==================== MONITORING STATE ====================

    def _cleanup_processed(self) -> None:
        """Очистить старые токены и ротировать мониторинг."""
        now = datetime.utcnow().timestamp()

        # Cleanup processed (1 hour TTL)
        cutoff_processed = now - 3600
        to_remove = [
            mint
            for mint, ts in self.processed_tokens_timestamps.items()
            if ts < cutoff_processed
        ]
        for mint in to_remove:
            self.processed_tokens.discard(mint)
            self.processed_tokens_timestamps.pop(mint, None)

        # Rotate monitored tokens (TTL based)
        cutoff_monitor = now - self.token_monitor_ttl
        expired = [
            mint
            for mint, start_time in self.monitored_tokens.items()
            if start_time < cutoff_monitor
        ]

        if expired:
            for mint in expired:
                self.monitored_tokens.pop(mint, None)
            logger.info(f"🔄 Rotated {len(expired)} tokens (TTL={self.token_monitor_ttl}s)")

    def is_being_monitored(self, mint: str) -> bool:
        """Check if token is being monitored."""
        if mint not in self.monitored_tokens:
            return False
        now = datetime.utcnow().timestamp()
        return (now - self.monitored_tokens[mint]) < self.token_monitor_ttl

    def add_to_monitoring(self, mint: str) -> None:
        """Add token to monitoring."""
        self.monitored_tokens[mint] = datetime.utcnow().timestamp()

    def get_monitoring_stats(self) -> dict:
        """Get monitoring statistics."""
        now = datetime.utcnow().timestamp()
        active = sum(
            1
            for start_time in self.monitored_tokens.values()
            if (now - start_time) < self.token_monitor_ttl
        )
        return {
            "active_monitored": active,
            "total_processed": len(self.processed_tokens),
            "ttl_seconds": self.token_monitor_ttl,
            "sources": [s.value for s in self._enabled_sources],
        }

    def get_rate_limit_stats(self) -> dict:
        """Get rate limiter statistics for all sources."""
        stats = {}
        for source, limiter in self.rate_limiters.items():
            stats[source.value] = {
                "can_request": limiter.can_request(),
                "requests_in_window": len(limiter.requests),
                "max_requests": limiter.max_requests,
                "wait_time": limiter.time_until_available(),
                "daily_used": limiter.daily_requests,
                "daily_budget": limiter.daily_budget or "unlimited",
                "daily_remaining": limiter.get_daily_remaining(),
                "usage_percent": limiter.get_usage_percent(),
            }
        return stats

    def get_budget_summary(self) -> str:
        """Get human-readable budget summary."""
        lines = ["📊 API Budget Status:"]
        for source in self._enabled_sources:
            limiter = self.rate_limiters[source]
            if limiter.daily_budget > 0:
                remaining = limiter.get_daily_remaining()
                usage = limiter.get_usage_percent()
                lines.append(
                    f"  {source.value}: {limiter.daily_requests}/{limiter.daily_budget} "
                    f"({usage:.1f}% used, {remaining} remaining)"
                )
            else:
                lines.append(f"  {source.value}: unlimited (used {limiter.daily_requests} today)")
        return "\n".join(lines)
