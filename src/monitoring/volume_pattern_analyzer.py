"""Volume Pattern Analyzer - анализирует паттерны объёмов для всех токенов."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com"


class PatternType(Enum):
    """Типы паттернов объёмов."""

    VOLUME_SPIKE = "volume_spike"
    WHALE_ACCUMULATION = "whale_accumulation"
    ORGANIC_GROWTH = "organic_growth"
    COORDINATED_BUYS = "coordinated_buys"
    SMART_MONEY_ENTRY = "smart_money_entry"
    BREAKOUT_PATTERN = "breakout_pattern"


class RiskLevel(Enum):
    """Уровни риска токена."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class VolumePattern:
    """Обнаруженный паттерн объёма."""

    pattern_type: PatternType
    strength: float
    confidence: float
    details: dict = field(default_factory=dict)


@dataclass
class TokenVolumeAnalysis:
    """Результат анализа объёмов токена."""

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
    patterns: list[VolumePattern] = field(default_factory=list)
    health_score: int = 50
    opportunity_score: int = 30
    recommendation: str = "WATCH"

    @property
    def is_healthy(self) -> bool:
        """Токен здоровый для торговли."""
        return self.health_score >= 60 and self.risk_level != RiskLevel.EXTREME

    @property
    def is_opportunity(self) -> bool:
        """Есть возможность для входа."""
        return self.opportunity_score >= 70 and self.is_healthy


class VolumePatternAnalyzer:
    """Анализатор паттернов объёмов для Volume Pattern Sniping."""

    def __init__(
        self,
        min_volume_1h: float = 10000,
        volume_spike_threshold: float = 3.0,
        min_trades_5m: int = 20,
        scan_interval: float = 30.0,
        max_tokens_per_scan: int = 50,
    ):
        """Initialize analyzer."""
        self.min_volume_1h = min_volume_1h
        self.volume_spike_threshold = volume_spike_threshold
        self.min_trades_5m = min_trades_5m
        self.scan_interval = scan_interval
        self.max_tokens_per_scan = max_tokens_per_scan
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self.on_opportunity: Callable | None = None
        self._processed_signals: set[str] = set()

    def set_callbacks(self, on_opportunity: Callable | None = None) -> None:
        """Set callback for opportunities."""
        self.on_opportunity = on_opportunity

    async def start(self) -> None:
        """Start analyzer."""
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("[VOLUME] Started")

    async def stop(self) -> None:
        """Stop analyzer."""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
        if self._session:
            await self._session.close()

    async def _scan_loop(self) -> None:
        """Main scan loop."""
        while self._running:
            try:
                await self._scan_all_tokens()
                await asyncio.sleep(self.scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[VOLUME] Scan error: {e}")
                await asyncio.sleep(10)

    async def _scan_all_tokens(self) -> None:
        """Scan tokens for patterns."""
        tokens = await self._fetch_trending_tokens()
        for token_data in tokens[: self.max_tokens_per_scan]:
            analysis = await self.analyze_token(token_data)
            if analysis and analysis.is_opportunity:
                await self._emit_opportunity(analysis)

    async def _fetch_trending_tokens(self) -> list[dict]:
        """Fetch tokens from DexScreener."""
        if not self._session:
            return []
        try:
            url = f"{DEXSCREENER_API}/latest/dex/search?q=pump"
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    return [p for p in pairs if p.get("chainId") == "solana"]
        except Exception as e:
            logger.warning(f"[VOLUME] Fetch error: {e}")
        return []

    async def analyze_token(self, pair_data: dict) -> TokenVolumeAnalysis | None:
        """Analyze token for volume patterns."""
        try:
            base = pair_data.get("baseToken", {})
            mint = base.get("address", "")
            symbol = base.get("symbol", "")
            if not mint:
                return None

            volume = pair_data.get("volume", {})
            v5m = float(volume.get("m5", 0) or 0)
            v1h = float(volume.get("h1", 0) or 0)
            v24h = float(volume.get("h24", 0) or 0)

            if v1h < self.min_volume_1h:
                return None

            spike = v5m / (v1h / 12) if v1h > 0 else 0

            txns = pair_data.get("txns", {})
            m5 = txns.get("m5", {})
            h1 = txns.get("h1", {})
            b5 = m5.get("buys", 0)
            s5 = m5.get("sells", 0)
            b1 = h1.get("buys", 0)
            s1 = h1.get("sells", 0)

            t5 = b5 + s5
            t1 = b1 + s1
            bp5 = b5 / t5 if t5 > 0 else 0.5
            bp1 = b1 / t1 if t1 > 0 else 0.5

            conc = self._estimate_concentration(t5)
            risk = self._calc_risk(conc)
            patterns = self._detect_patterns(spike, bp5, bp1, t5)
            health = self._calc_health(bp5, conc, t5)
            opp = self._calc_opportunity(spike, bp5, patterns, health)
            rec = self._get_recommendation(health, opp, risk)

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
                patterns=patterns,
                health_score=health,
                opportunity_score=opp,
                recommendation=rec,
            )
        except Exception:
            return None

    def _estimate_concentration(self, total_trades: int) -> float:
        """Estimate holder concentration."""
        if total_trades < 10:
            return 0.8
        if total_trades < 30:
            return 0.5
        if total_trades < 100:
            return 0.3
        return 0.2

    def _calc_risk(self, conc: float) -> RiskLevel:
        """Calculate risk level."""
        if conc >= 0.7:
            return RiskLevel.EXTREME
        if conc >= 0.5:
            return RiskLevel.HIGH
        if conc >= 0.3:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _detect_patterns(
        self, spike: float, bp5: float, bp1: float, t5: int
    ) -> list[VolumePattern]:
        """Detect volume patterns."""
        patterns = []
        if spike >= self.volume_spike_threshold:
            patterns.append(
                VolumePattern(PatternType.VOLUME_SPIKE, min(1.0, spike / 10), 0.8, {})
            )
        if t5 >= self.min_trades_5m and 0.55 <= bp5 <= 0.75:
            patterns.append(
                VolumePattern(PatternType.ORGANIC_GROWTH, 0.7, 0.8, {})
            )
        if 2.0 <= spike <= 5.0 and bp5 > 0.6 and bp1 > 0.55:
            patterns.append(
                VolumePattern(PatternType.SMART_MONEY_ENTRY, 0.75, 0.7, {})
            )
        return patterns

    def _calc_health(self, bp: float, conc: float, trades: int) -> int:
        """Calculate health score (0-100)."""
        s = 50
        if bp >= 0.6:
            s += int((bp - 0.5) * 40)
        elif bp < 0.4:
            s -= int((0.5 - bp) * 40)
        if conc > 0.7:
            s -= 30
        elif conc > 0.5:
            s -= 15
        elif conc > 0.3:
            s -= 5
        if trades >= 100:
            s += 20
        elif trades >= 50:
            s += 15
        elif trades >= 20:
            s += 10
        return max(0, min(100, s))

    def _calc_opportunity(
        self, spike: float, bp: float, patterns: list, health: int
    ) -> int:
        """Calculate opportunity score (0-100)."""
        s = 30
        if spike >= 5.0:
            s += 30
        elif spike >= 3.0:
            s += 20
        elif spike >= 2.0:
            s += 10
        if bp >= 0.7:
            s += 20
        elif bp >= 0.6:
            s += 10
        for p in patterns:
            if p.pattern_type == PatternType.SMART_MONEY_ENTRY:
                s += 15
            elif p.pattern_type == PatternType.ORGANIC_GROWTH:
                s += 10
        if health < 40:
            s = int(s * 0.5)
        elif health >= 70:
            s = int(s * 1.2)
        return max(0, min(100, s))

    def _get_recommendation(self, health: int, opp: int, risk: RiskLevel) -> str:
        """Get recommendation."""
        if risk == RiskLevel.EXTREME:
            return "DANGER"
        if health < 40:
            return "SKIP"
        if opp >= 80 and health >= 70:
            return "STRONG_BUY"
        if opp >= 70 and health >= 60:
            return "BUY"
        if opp >= 50:
            return "WATCH"
        return "SKIP"

    async def _emit_opportunity(self, a: TokenVolumeAnalysis) -> None:
        """Emit opportunity signal."""
        key = f"{a.mint}_{a.timestamp.strftime('%H%M')}"
        if key in self._processed_signals:
            return
        self._processed_signals.add(key)
        pstr = ", ".join([p.pattern_type.value for p in a.patterns])
        logger.warning(
            f"[VOLUME] 🎯 {a.symbol} | H:{a.health_score} O:{a.opportunity_score} "
            f"| ${a.volume_5m:,.0f} {a.volume_spike_ratio:.1f}x | [{pstr}]"
        )
        if self.on_opportunity:
            await self.on_opportunity(a)

    def get_stats(self) -> dict:
        """Get stats."""
        return {"signals": len(self._processed_signals), "running": self._running}
