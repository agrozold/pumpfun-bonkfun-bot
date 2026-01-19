"""
Volume Pattern Analyzer v3 - Multi-source volume spike detection.

FIXED: Now fetches full token data with volume/txns for each token.

Sources:
- DexScreener Token Boosts (top tokens) -> then fetch full data
- DexScreener Search (pump.fun tokens)
- Birdeye Trending (if API key available)
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

DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"


class PatternType(Enum):
    """Volume pattern types."""
    VOLUME_SPIKE = "volume_spike"
    ORGANIC_GROWTH = "organic_growth"
    SMART_MONEY_ENTRY = "smart_money_entry"
    WHALE_ACCUMULATION = "whale_accumulation"
    BREAKOUT = "breakout"


class RiskLevel(Enum):
    """Token risk levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class VolumePattern:
    """Detected volume pattern."""
    pattern_type: PatternType
    strength: float
    confidence: float
    details: dict = field(default_factory=dict)


@dataclass
class TokenVolumeAnalysis:
    """Token volume analysis result."""
    mint: str
    symbol: str
    timestamp: datetime
    volume_5m: float
    volume_1h: float
    volume_24h: float
    volume_spike_ratio: float
    buys_5m: int
    sells_5m: int
    buys_1h: int
    sells_1h: int
    buy_pressure_5m: float
    buy_pressure_1h: float
    avg_trade_size: float
    large_trades_count: int
    small_trades_count: int
    trade_size_ratio: float
    unique_buyers_5m: int
    unique_sellers_5m: int
    wallet_concentration: float
    risk_level: RiskLevel
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    liquidity_usd: float = 0.0
    market_cap: float = 0.0
    patterns: list[VolumePattern] = field(default_factory=list)
    health_score: int = 50
    opportunity_score: int = 30
    recommendation: str = "WATCH"
    source: str = "dexscreener"

    @property
    def is_healthy(self) -> bool:
        return self.health_score >= 70 and self.risk_level != RiskLevel.EXTREME

    @property
    def is_opportunity(self) -> bool:
        return self.opportunity_score >= 70 and self.is_healthy


class VolumePatternAnalyzer:
    """Multi-source volume pattern analyzer with full data fetch."""

    def __init__(
        self,
        min_volume_1h: float = 5000,
        volume_spike_threshold: float = 2.5,
        min_trades_5m: int = 30,
        min_buy_pressure: float = 0.55,
        scan_interval: float = 45.0,
        max_tokens_per_scan: int = 50,
        min_health_score: int = 65,
        min_opportunity_score: int = 65,
    ):
        self.min_volume_1h = min_volume_1h
        self.volume_spike_threshold = volume_spike_threshold
        self.min_trades_5m = min_trades_5m
        self.min_buy_pressure = min_buy_pressure
        self.scan_interval = scan_interval
        self.max_tokens_per_scan = max_tokens_per_scan
        self.min_health_score = min_health_score
        self.min_opportunity_score = min_opportunity_score
        
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self.on_opportunity: Callable | None = None
        
        # Anti-duplicate
        self._signal_cooldown: dict[str, float] = {}
        self._cooldown_seconds = 300  # 5 min cooldown per token
        
        # Cache for token data
        self._token_cache: dict[str, tuple[dict, float]] = {}
        self._cache_ttl = 30  # 30 sec
        
        # Stats
        self._stats = {
            "scans": 0,
            "tokens_checked": 0,
            "tokens_analyzed": 0,
            "opportunities_found": 0,
            "api_calls": 0,
        }
        
        # API keys
        self._birdeye_key = os.getenv("BIRDEYE_API_KEY")
        
        logger.info(
            f"[VOLUME] Initialized: min_vol=${min_volume_1h:,.0f}, "
            f"spike={volume_spike_threshold}x, interval={scan_interval}s"
        )

    def set_callbacks(self, on_opportunity: Callable | None = None) -> None:
        self.on_opportunity = on_opportunity

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("[VOLUME] Started multi-source scanner")

    async def stop(self) -> None:
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
        if self._session:
            await self._session.close()
        logger.info("[VOLUME] Stopped")

    async def _scan_loop(self) -> None:
        """Main scan loop."""
        while self._running:
            try:
                await self._scan_all_sources()
                self._stats["scans"] += 1
                
                # Log stats every 5 scans
                if self._stats["scans"] % 5 == 0:
                    self._log_stats()
                    
                await asyncio.sleep(self.scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[VOLUME] Scan error: {e}")
                await asyncio.sleep(10)

    async def _scan_all_sources(self) -> None:
        """Scan all sources for volume patterns."""
        # Get token addresses from multiple sources
        token_addresses: set[str] = set()
        
        # Source 1: DexScreener Token Boosts (Solana only)
        boosts = await self._fetch_token_boosts()
        for t in boosts:
            if t.get("chainId") == "solana":
                addr = t.get("tokenAddress")
                if addr:
                    token_addresses.add(addr)
        
        # Source 2: DexScreener Search (pump.fun tokens)
        search_pairs = await self._fetch_dexscreener_search("pump")
        for p in search_pairs:
            addr = p.get("baseToken", {}).get("address")
            if addr:
                token_addresses.add(addr)
        
        logger.info(f"[VOLUME] Found {len(token_addresses)} unique tokens to analyze")
        self._stats["tokens_checked"] += len(token_addresses)
        
        # Fetch full data and analyze each token (limit to max_tokens_per_scan)
        analyzed = 0
        for mint in list(token_addresses)[:self.max_tokens_per_scan]:
            # Check cooldown
            now = datetime.utcnow().timestamp()
            if mint in self._signal_cooldown:
                if now - self._signal_cooldown[mint] < self._cooldown_seconds:
                    continue
            
            # Fetch full token data
            pair_data = await self._fetch_token_data(mint)
            if not pair_data:
                continue
            
            # Analyze
            analysis = await self.analyze_token(pair_data)
            if analysis:
                analyzed += 1
                if analysis.is_opportunity:
                    await self._emit_opportunity(analysis)
            
            # Small delay to avoid rate limits
            await asyncio.sleep(0.1)
        
        logger.info(f"[VOLUME] Analyzed {analyzed} tokens this scan")

    async def _fetch_token_boosts(self) -> list[dict]:
        """Fetch top boosted tokens from DexScreener."""
        if not self._session:
            return []
        try:
            url = f"{DEXSCREENER_API}/token-boosts/top/v1"
            self._stats["api_calls"] += 1
            
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug(f"[VOLUME] Token boosts: {len(data)} tokens")
                    return data
        except Exception as e:
            logger.debug(f"[VOLUME] Token boosts error: {e}")
        return []

    async def _fetch_dexscreener_search(self, query: str) -> list[dict]:
        """Fetch tokens from DexScreener search."""
        if not self._session:
            return []
        try:
            url = f"{DEXSCREENER_API}/latest/dex/search?q={query}"
            self._stats["api_calls"] += 1
            
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    logger.debug(f"[VOLUME] Search '{query}': {len(solana_pairs)} Solana pairs")
                    return solana_pairs
        except Exception as e:
            logger.debug(f"[VOLUME] Search error: {e}")
        return []

    async def _fetch_token_data(self, mint: str) -> dict | None:
        """Fetch full token data from DexScreener."""
        # Check cache
        now = datetime.utcnow().timestamp()
        if mint in self._token_cache:
            data, ts = self._token_cache[mint]
            if now - ts < self._cache_ttl:
                return data
        
        if not self._session:
            return None
        
        try:
            url = f"{DEXSCREENER_API}/latest/dex/tokens/{mint}"
            self._stats["api_calls"] += 1
            
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        # Get pair with highest liquidity
                        best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                        self._token_cache[mint] = (best_pair, now)
                        return best_pair
        except Exception as e:
            logger.debug(f"[VOLUME] Fetch token error for {mint[:8]}...: {e}")
        return None

    async def analyze_token(self, pair_data: dict) -> TokenVolumeAnalysis | None:
        """Analyze token for volume patterns."""
        try:
            base = pair_data.get("baseToken", {})
            mint = base.get("address", "")
            symbol = base.get("symbol", "UNKNOWN")
            
            if not mint:
                return None
            
            # Get volume data
            volume = pair_data.get("volume", {})
            v5m = float(volume.get("m5", 0) or 0)
            v1h = float(volume.get("h1", 0) or 0)
            v24h = float(volume.get("h24", 0) or 0)
            
            # Filter by minimum volume
            if v1h < self.min_volume_1h:
                return None
            
            # Calculate spike ratio
            avg_5m = v1h / 12 if v1h > 0 else 1
            spike = v5m / avg_5m if avg_5m > 0 else 0
            
            # Get transaction data
            txns = pair_data.get("txns", {})
            m5 = txns.get("m5", {})
            h1 = txns.get("h1", {})
            b5 = int(m5.get("buys", 0) or 0)
            s5 = int(m5.get("sells", 0) or 0)
            b1 = int(h1.get("buys", 0) or 0)
            s1 = int(h1.get("sells", 0) or 0)
            
            t5 = b5 + s5
            t1 = b1 + s1
            
            # Filter by minimum trades
            if t5 < self.min_trades_5m:
                return None
            
            bp5 = b5 / t5 if t5 > 0 else 0.5
            bp1 = b1 / t1 if t1 > 0 else 0.5
            
            # Filter by buy pressure
            if bp5 < self.min_buy_pressure:
                return None
            
            # Get price changes
            price_change = pair_data.get("priceChange", {})
            pc5m = float(price_change.get("m5", 0) or 0)
            pc1h = float(price_change.get("h1", 0) or 0)
            
            # Get liquidity and market cap
            liquidity = float(pair_data.get("liquidity", {}).get("usd", 0) or 0)
            fdv = float(pair_data.get("fdv", 0) or 0)
            
            # Calculate scores
            conc = self._estimate_concentration(t5)
            risk = self._calc_risk(conc, liquidity)
            patterns = self._detect_patterns(spike, bp5, bp1, t5, pc5m, pc1h)
            health = self._calc_health(bp5, conc, t5, liquidity)
            opp = self._calc_opportunity(spike, bp5, patterns, health, pc5m)
            rec = self._get_recommendation(health, opp, risk, fdv)
            
            self._stats["tokens_analyzed"] += 1
            
            return TokenVolumeAnalysis(
                mint=mint,
                symbol=symbol,
                timestamp=datetime.utcnow(),
                volume_5m=v5m,
                volume_1h=v1h,
                volume_24h=v24h,
                volume_spike_ratio=spike,
                buys_5m=b5,
                sells_5m=s5,
                buys_1h=b1,
                sells_1h=s1,
                buy_pressure_5m=bp5,
                buy_pressure_1h=bp1,
                avg_trade_size=v5m / t5 if t5 else 0,
                large_trades_count=0,
                small_trades_count=t5,
                trade_size_ratio=0,
                unique_buyers_5m=b5,
                unique_sellers_5m=s5,
                wallet_concentration=conc,
                risk_level=risk,
                price_change_5m=pc5m,
                price_change_1h=pc1h,
                liquidity_usd=liquidity,
                market_cap=fdv,
                patterns=patterns,
                health_score=health,
                opportunity_score=opp,
                recommendation=rec,
                source="dexscreener",
            )
        except Exception as e:
            logger.debug(f"[VOLUME] Analyze error: {e}")
            return None

    def _estimate_concentration(self, total_trades: int) -> float:
        if total_trades < 20:
            return 0.7
        if total_trades < 50:
            return 0.5
        if total_trades < 100:
            return 0.3
        return 0.2

    def _calc_risk(self, conc: float, liquidity: float) -> RiskLevel:
        if conc >= 0.7 or liquidity < 1000:
            return RiskLevel.EXTREME
        if conc >= 0.5 or liquidity < 5000:
            return RiskLevel.HIGH
        if conc >= 0.3 or liquidity < 10000:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _detect_patterns(
        self, spike: float, bp5: float, bp1: float, t5: int, pc5m: float, pc1h: float
    ) -> list[VolumePattern]:
        patterns = []
        
        # Volume spike
        if spike >= self.volume_spike_threshold:
            strength = min(1.0, spike / 10)
            patterns.append(VolumePattern(PatternType.VOLUME_SPIKE, strength, 0.85))
        
        # Organic growth (high volume + balanced buy pressure)
        if t5 >= 100 and 0.55 <= bp5 <= 0.75:
            patterns.append(VolumePattern(PatternType.ORGANIC_GROWTH, 0.7, 0.8))
        
        # Smart money entry (moderate spike + good momentum)
        if 2.0 <= spike <= 6.0 and bp5 >= 0.6 and pc5m > 5:
            patterns.append(VolumePattern(PatternType.SMART_MONEY_ENTRY, 0.75, 0.75))
        
        # Breakout (price up + volume spike)
        if spike >= 2.0 and pc5m >= 10 and bp5 >= 0.65:
            patterns.append(VolumePattern(PatternType.BREAKOUT, 0.8, 0.8))
        
        # Whale accumulation (very high buy pressure)
        if bp5 >= 0.8 and t5 >= 50:
            patterns.append(VolumePattern(PatternType.WHALE_ACCUMULATION, 0.85, 0.7))
        
        return patterns

    def _calc_health(self, bp: float, conc: float, trades: int, liquidity: float) -> int:
        score = 50
        
        # Buy pressure (+/- 20)
        if bp >= 0.7:
            score += 20
        elif bp >= 0.6:
            score += 10
        elif bp < 0.4:
            score -= 20
        
        # Concentration (-30 to 0)
        if conc >= 0.7:
            score -= 30
        elif conc >= 0.5:
            score -= 15
        
        # Trade count (+20 max)
        if trades >= 100:
            score += 20
        elif trades >= 50:
            score += 10
        
        # Liquidity (+10 max)
        if liquidity >= 20000:
            score += 10
        elif liquidity >= 10000:
            score += 5
        elif liquidity < 5000:
            score -= 10
        
        return max(0, min(100, score))

    def _calc_opportunity(
        self, spike: float, bp: float, patterns: list, health: int, pc5m: float
    ) -> int:
        score = 30
        
        # Volume spike (+30 max)
        if spike >= 5.0:
            score += 30
        elif spike >= 3.0:
            score += 20
        elif spike >= 2.0:
            score += 10
        
        # Buy pressure (+20 max)
        if bp >= 0.75:
            score += 20
        elif bp >= 0.65:
            score += 10
        
        # Price momentum (+15 max)
        if pc5m >= 15:
            score += 15
        elif pc5m >= 10:
            score += 10
        elif pc5m >= 5:
            score += 5
        
        # Pattern bonuses
        for p in patterns:
            if p.pattern_type == PatternType.SMART_MONEY_ENTRY:
                score += 15
            elif p.pattern_type == PatternType.BREAKOUT:
                score += 12
            elif p.pattern_type == PatternType.WHALE_ACCUMULATION:
                score += 10
            elif p.pattern_type == PatternType.ORGANIC_GROWTH:
                score += 8
        
        # Health multiplier
        if health < 60:
            score = int(score * 0.5)
        elif health >= 80:
            score = int(score * 1.15)
        
        return max(0, min(100, score))

    def _get_recommendation(self, health: int, opp: int, risk: RiskLevel, market_cap: float = 0) -> str:
        if risk == RiskLevel.EXTREME:
            return "DANGER"
        if health < 60:
            return "SKIP"
        # Skip tokens with market cap > 100k (too late to enter)
        if market_cap > 100_000:
            logger.info(f"[VOLUME] SKIP - market cap ${market_cap:,.0f} > $100k (too late)")
            return "SKIP"
        if opp >= 85 and health >= 80:
            return "STRONG_BUY"
        if opp >= 70 and health >= 70:
            return "BUY"
        if opp >= 50:
            return "WATCH"
        return "SKIP"

    async def _emit_opportunity(self, a: TokenVolumeAnalysis) -> None:
        """Emit opportunity signal."""
        now = datetime.utcnow().timestamp()
        self._signal_cooldown[a.mint] = now
        self._stats["opportunities_found"] += 1
        
        patterns_str = ", ".join([p.pattern_type.value for p in a.patterns]) or "none"
        
        logger.warning(
            f"[VOLUME] 🎯 OPPORTUNITY: {a.symbol}\n"
            f"    Mint: {a.mint}\n"
            f"    Health: {a.health_score}/100, Opportunity: {a.opportunity_score}/100\n"
            f"    Volume 5m: ${a.volume_5m:,.0f} ({a.volume_spike_ratio:.1f}x spike)\n"
            f"    Trades 5m: {a.buys_5m}B/{a.sells_5m}S ({a.buy_pressure_5m:.0%} buy pressure)\n"
            f"    Price 5m: {a.price_change_5m:+.1f}%, Liquidity: ${a.liquidity_usd:,.0f}\n"
            f"    Patterns: [{patterns_str}]\n"
            f"    Recommendation: {a.recommendation}"
        )
        
        if self.on_opportunity:
            await self.on_opportunity(a)

    def _log_stats(self) -> None:
        """Log analyzer statistics."""
        s = self._stats
        logger.info(
            f"[VOLUME STATS] Scans: {s['scans']}, Checked: {s['tokens_checked']}, "
            f"Analyzed: {s['tokens_analyzed']}, Opportunities: {s['opportunities_found']}, "
            f"API calls: {s['api_calls']}"
        )

    def get_stats(self) -> dict:
        return {**self._stats, "running": self._running}
