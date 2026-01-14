"""
Pump Pattern Detector - отслеживает паттерны перед пампами токенов.

Использует несколько API для получения реальных данных:
- Birdeye API (основной)
- DexCheck API (fallback)
- Codex API (fallback)
- GoldRush/Covalent API (fallback)

Паттерны:
1. Volume Spike - резкий рост объёма (3x+)
2. Buy Pressure - много покупок vs продаж (>70% buys)
3. Trade Velocity - много трейдов за короткий период
4. Whale Accumulation - крупные покупки
5. Price Momentum - рост цены
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

# API URLs
BIRDEYE_API_URL = "https://public-api.birdeye.so"
DEXCHECK_API_URL = "https://api.dexcheck.ai"
CODEX_API_URL = "https://graph.codex.io/graphql"
GOLDRUSH_API_URL = "https://api.covalenthq.com/v1"


@dataclass
class TokenMetrics:
    """Метрики токена."""
    mint: str
    symbol: str
    first_seen: datetime = field(default_factory=datetime.utcnow)
    
    # Current data
    price: float = 0.0
    volume_24h: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    
    # Trade data
    buys_5m: int = 0
    sells_5m: int = 0
    buy_volume_5m: float = 0.0
    sell_volume_5m: float = 0.0
    
    # History for pattern detection
    price_history: list = field(default_factory=list)
    volume_history: list = field(default_factory=list)
    
    # Whale buys from whale tracker
    whale_buys: list = field(default_factory=list)
    
    # Detected patterns
    patterns: list = field(default_factory=list)
    
    # Last update
    last_update: datetime | None = None


@dataclass 
class PatternSignal:
    """Сигнал о паттерне."""
    pattern_type: str
    strength: float  # 0.0 - 1.0
    description: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


class PumpPatternDetector:
    """Детектор паттернов с несколькими API источниками."""

    def __init__(
        self,
        birdeye_api_key: str | None = None,
        dexcheck_api_key: str | None = None,
        codex_api_key: str | None = None,
        goldrush_api_key: str | None = None,
        # Thresholds
        volume_spike_threshold: float = 3.0,
        buy_pressure_threshold: float = 0.7,  # 70% buys
        trade_velocity_threshold: int = 20,  # 20+ trades per 5min
        price_momentum_threshold: float = 0.1,  # 10% price increase
        min_whale_buys: int = 2,
        whale_window_seconds: int = 60,
        min_whale_amount: float = 0.5,
        # Signal settings
        min_patterns_to_signal: int = 1,
        update_interval: float = 5.0,  # seconds between updates
        # Ignored params for compatibility
        volume_window_seconds: int = 60,
        holder_growth_threshold: float = 0.5,
        holder_window_seconds: int = 60,
    ):
        # API keys - try env vars as fallback
        self.birdeye_api_key = birdeye_api_key or os.getenv("BIRDEYE_API_KEY")
        self.dexcheck_api_key = dexcheck_api_key or os.getenv("DEXCHECK_API_KEY")
        self.codex_api_key = codex_api_key or os.getenv("CODEX_API_KEY")
        self.goldrush_api_key = goldrush_api_key or os.getenv("GOLDRUSH_API_KEY")
        
        # Track API failures for fallback logic
        self._birdeye_failures = 0
        self._current_api = "birdeye"  # birdeye, dexcheck, codex
        
        self.volume_spike_threshold = volume_spike_threshold
        self.buy_pressure_threshold = buy_pressure_threshold
        self.trade_velocity_threshold = trade_velocity_threshold
        self.price_momentum_threshold = price_momentum_threshold
        self.min_whale_buys = min_whale_buys
        self.whale_window = timedelta(seconds=whale_window_seconds)
        self.min_whale_amount = min_whale_amount
        self.min_patterns_to_signal = min_patterns_to_signal
        self.update_interval = update_interval

        self.tokens: dict[str, TokenMetrics] = {}
        self.on_pump_signal: Callable | None = None
        self._session: aiohttp.ClientSession | None = None
        self._update_task: asyncio.Task | None = None
        self._running = False

        # Log available APIs
        apis = []
        if self.birdeye_api_key:
            apis.append("Birdeye")
        if self.dexcheck_api_key:
            apis.append("DexCheck")
        if self.codex_api_key:
            apis.append("Codex")
        if self.goldrush_api_key:
            apis.append("GoldRush")
        
        if apis:
            logger.info(f"PumpPatternDetector initialized with APIs: {', '.join(apis)}")
        else:
            logger.warning("PumpPatternDetector: No API keys - limited functionality")

    def set_pump_signal_callback(self, callback: Callable):
        """Установить callback для сигналов."""
        self.on_pump_signal = callback

    def start_tracking(self, mint: str, symbol: str) -> TokenMetrics:
        """Начать отслеживание токена."""
        if mint not in self.tokens:
            self.tokens[mint] = TokenMetrics(mint=mint, symbol=symbol)
            logger.info(f"Started tracking patterns for {symbol} ({mint[:8]}...)")
        
        # Start background updater if not running
        if self.birdeye_api_key and not self._running:
            self._running = True
            self._update_task = asyncio.create_task(self._background_updater())
        
        return self.tokens[mint]

    def stop_tracking(self, mint: str):
        """Остановить отслеживание."""
        if mint in self.tokens:
            del self.tokens[mint]

    async def stop(self):
        """Остановить детектор."""
        self._running = False
        if self._update_task:
            self._update_task.cancel()
        if self._session:
            await self._session.close()

    async def record_whale_buy(self, mint: str, wallet: str, amount_sol: float):
        """Записать покупку кита (вызывается из whale tracker)."""
        if mint not in self.tokens:
            return
        
        if amount_sol < self.min_whale_amount:
            return
        
        now = datetime.utcnow()
        metrics = self.tokens[mint]
        metrics.whale_buys.append((now, wallet, amount_sol))
        
        # Cleanup old
        cutoff = now - timedelta(minutes=5)
        metrics.whale_buys = [(t, w, a) for t, w, a in metrics.whale_buys if t > cutoff]
        
        logger.info(f"🐋 Whale buy recorded: {metrics.symbol} - {amount_sol:.2f} SOL")
        
        await self._check_patterns(mint)

    async def _background_updater(self):
        """Фоновое обновление данных из Birdeye."""
        self._session = aiohttp.ClientSession()
        
        while self._running:
            try:
                # Update all tracked tokens
                for mint in list(self.tokens.keys()):
                    if not self._running:
                        break
                    await self._update_token_data(mint)
                    await asyncio.sleep(0.5)  # Rate limit
                
                await asyncio.sleep(self.update_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Background updater error: {e}")
                await asyncio.sleep(5)
        
        if self._session:
            await self._session.close()

    async def _update_token_data(self, mint: str):
        """Обновить данные токена из доступных API."""
        if mint not in self.tokens:
            return
        
        metrics = self.tokens[mint]
        
        # Try APIs in order of preference
        data = None
        
        # 1. Try Birdeye (if not too many failures)
        if self.birdeye_api_key and self._birdeye_failures < 5:
            data = await self._fetch_from_birdeye(mint)
            if data:
                self._birdeye_failures = 0
            else:
                self._birdeye_failures += 1
                if self._birdeye_failures >= 5:
                    logger.warning("Birdeye API failing, switching to fallback")
        
        # 2. Try DexCheck as fallback
        if not data and self.dexcheck_api_key:
            data = await self._fetch_from_dexcheck(mint)
        
        # 3. Try Codex as fallback
        if not data and self.codex_api_key:
            data = await self._fetch_from_codex(mint)
        
        if not data:
            return
        
        # Update metrics from data
        old_price = metrics.price
        metrics.price = data.get("price", 0) or 0
        metrics.volume_24h = data.get("volume_24h", 0) or 0
        metrics.price_change_5m = data.get("price_change_5m", 0) or 0
        metrics.price_change_1h = data.get("price_change_1h", 0) or 0
        
        # Buy/sell data
        metrics.buys_5m = data.get("buys_5m", 0) or 0
        metrics.sells_5m = data.get("sells_5m", 0) or 0
        metrics.buy_volume_5m = data.get("buy_volume_5m", 0) or 0
        metrics.sell_volume_5m = data.get("sell_volume_5m", 0) or 0
        
        # Record history
        now = datetime.utcnow()
        if metrics.price > 0:
            metrics.price_history.append((now, metrics.price))
        if metrics.volume_24h > 0:
            metrics.volume_history.append((now, metrics.buy_volume_5m + metrics.sell_volume_5m))
        
        # Cleanup old history
        cutoff = now - timedelta(minutes=10)
        metrics.price_history = [(t, p) for t, p in metrics.price_history if t > cutoff][-50:]
        metrics.volume_history = [(t, v) for t, v in metrics.volume_history if t > cutoff][-50:]
        
        metrics.last_update = now
        
        # Check patterns
        await self._check_patterns(mint)

    async def _fetch_from_birdeye(self, mint: str) -> dict | None:
        """Получить данные из Birdeye API."""
        try:
            data = await self._birdeye_request(f"/defi/token_overview?address={mint}")
            if data and data.get("success"):
                info = data.get("data", {})
                return {
                    "price": info.get("price", 0),
                    "volume_24h": info.get("v24hUSD", 0),
                    "price_change_5m": info.get("priceChange5mPercent", 0),
                    "price_change_1h": info.get("priceChange1hPercent", 0),
                    "buys_5m": info.get("buy5m", 0),
                    "sells_5m": info.get("sell5m", 0),
                    "buy_volume_5m": info.get("vBuy5mUSD", 0),
                    "sell_volume_5m": info.get("vSell5mUSD", 0),
                }
        except Exception as e:
            logger.debug(f"Birdeye error for {mint[:8]}...: {e}")
        return None

    async def _fetch_from_dexcheck(self, mint: str) -> dict | None:
        """Получить данные из DexCheck API."""
        if not self._session or not self.dexcheck_api_key:
            return None
        
        try:
            url = f"{DEXCHECK_API_URL}/v1/tokens/solana/{mint}"
            headers = {"X-API-KEY": self.dexcheck_api_key}
            
            async with self._session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Map DexCheck response to our format
                    return {
                        "price": data.get("price", 0),
                        "volume_24h": data.get("volume24h", 0),
                        "price_change_5m": data.get("priceChange5m", 0),
                        "price_change_1h": data.get("priceChange1h", 0),
                        "buys_5m": data.get("buys5m", 0),
                        "sells_5m": data.get("sells5m", 0),
                        "buy_volume_5m": data.get("buyVolume5m", 0),
                        "sell_volume_5m": data.get("sellVolume5m", 0),
                    }
        except Exception as e:
            logger.debug(f"DexCheck error for {mint[:8]}...: {e}")
        return None

    async def _fetch_from_codex(self, mint: str) -> dict | None:
        """Получить данные из Codex API."""
        if not self._session or not self.codex_api_key:
            return None
        
        try:
            # Codex uses GraphQL
            query = """
            query GetToken($address: String!) {
                token(input: {address: $address, networkId: 1399811149}) {
                    price
                    volume24h
                    priceChange5m
                    priceChange1h
                    txnCount5m
                    buyCount5m
                    sellCount5m
                }
            }
            """
            
            headers = {
                "Authorization": self.codex_api_key,
                "Content-Type": "application/json",
            }
            
            async with self._session.post(
                CODEX_API_URL,
                headers=headers,
                json={"query": query, "variables": {"address": mint}},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    data = result.get("data", {}).get("token", {})
                    if data:
                        return {
                            "price": data.get("price", 0),
                            "volume_24h": data.get("volume24h", 0),
                            "price_change_5m": data.get("priceChange5m", 0),
                            "price_change_1h": data.get("priceChange1h", 0),
                            "buys_5m": data.get("buyCount5m", 0),
                            "sells_5m": data.get("sellCount5m", 0),
                            "buy_volume_5m": 0,  # Not available in Codex
                            "sell_volume_5m": 0,
                        }
        except Exception as e:
            logger.debug(f"Codex error for {mint[:8]}...: {e}")
        return None

    async def _birdeye_request(self, endpoint: str) -> dict | None:
        """Сделать запрос к Birdeye API."""
        if not self._session or not self.birdeye_api_key:
            return None
        
        url = f"{BIRDEYE_API_URL}{endpoint}"
        headers = {
            "X-API-KEY": self.birdeye_api_key,
            "x-chain": "solana",
        }
        
        try:
            async with self._session.get(
                url, headers=headers, 
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    logger.debug("Birdeye rate limit")
                    await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"Birdeye request error: {e}")
        
        return None

    async def _check_patterns(self, mint: str):
        """Проверить все паттерны."""
        if mint not in self.tokens:
            return
        
        metrics = self.tokens[mint]
        now = datetime.utcnow()
        
        # Clear old patterns
        cutoff = now - timedelta(seconds=60)
        metrics.patterns = [p for p in metrics.patterns if p.timestamp > cutoff]
        
        # 1. Buy Pressure
        await self._check_buy_pressure(mint, metrics)
        
        # 2. Trade Velocity
        await self._check_trade_velocity(mint, metrics)
        
        # 3. Price Momentum
        await self._check_price_momentum(mint, metrics)
        
        # 4. Volume Spike
        await self._check_volume_spike(mint, metrics)
        
        # 5. Whale Cluster
        await self._check_whale_cluster(mint, metrics, now)
        
        # Evaluate signal
        await self._evaluate_signal(mint)

    async def _check_buy_pressure(self, mint: str, metrics: TokenMetrics):
        """Проверить давление покупок."""
        total_trades = metrics.buys_5m + metrics.sells_5m
        if total_trades < 5:
            return
        
        buy_ratio = metrics.buys_5m / total_trades
        
        if buy_ratio >= self.buy_pressure_threshold:
            await self._add_pattern(mint, PatternSignal(
                pattern_type="BUY_PRESSURE",
                strength=min(buy_ratio, 1.0),
                description=f"{buy_ratio*100:.0f}% buys ({metrics.buys_5m}/{total_trades} trades)",
            ))

    async def _check_trade_velocity(self, mint: str, metrics: TokenMetrics):
        """Проверить скорость трейдов."""
        total_trades = metrics.buys_5m + metrics.sells_5m
        
        if total_trades >= self.trade_velocity_threshold:
            strength = min(total_trades / (self.trade_velocity_threshold * 2), 1.0)
            await self._add_pattern(mint, PatternSignal(
                pattern_type="TRADE_VELOCITY",
                strength=strength,
                description=f"{total_trades} trades in 5min",
            ))

    async def _check_price_momentum(self, mint: str, metrics: TokenMetrics):
        """Проверить рост цены."""
        if metrics.price_change_5m >= self.price_momentum_threshold * 100:
            strength = min(metrics.price_change_5m / 50, 1.0)
            await self._add_pattern(mint, PatternSignal(
                pattern_type="PRICE_MOMENTUM",
                strength=strength,
                description=f"+{metrics.price_change_5m:.1f}% in 5min",
            ))

    async def _check_volume_spike(self, mint: str, metrics: TokenMetrics):
        """Проверить всплеск объёма."""
        if len(metrics.volume_history) < 3:
            return
        
        volumes = [v for _, v in metrics.volume_history]
        if not volumes:
            return
        
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else volumes[0]
        current_volume = volumes[-1]
        
        if avg_volume > 0 and current_volume > avg_volume * self.volume_spike_threshold:
            spike = current_volume / avg_volume
            await self._add_pattern(mint, PatternSignal(
                pattern_type="VOLUME_SPIKE",
                strength=min(spike / 10, 1.0),
                description=f"Volume {spike:.1f}x average",
            ))

    async def _check_whale_cluster(self, mint: str, metrics: TokenMetrics, now: datetime):
        """Проверить кластер whale покупок."""
        cutoff = now - self.whale_window
        recent = [(t, w, a) for t, w, a in metrics.whale_buys if t > cutoff]
        
        if len(recent) >= self.min_whale_buys:
            total = sum(a for _, _, a in recent)
            await self._add_pattern(mint, PatternSignal(
                pattern_type="WHALE_CLUSTER",
                strength=min(len(recent) / 5, 1.0),
                description=f"{len(recent)} whale buys ({total:.1f} SOL) in {self.whale_window.seconds}s",
            ))

    async def _add_pattern(self, mint: str, signal: PatternSignal):
        """Добавить паттерн."""
        if mint not in self.tokens:
            return
        
        metrics = self.tokens[mint]
        
        # Avoid duplicates
        for p in metrics.patterns:
            if p.pattern_type == signal.pattern_type:
                return
        
        metrics.patterns.append(signal)
        logger.info(f"📊 [{signal.pattern_type}] {metrics.symbol}: {signal.description}")

    async def _evaluate_signal(self, mint: str):
        """Оценить нужно ли сигналить."""
        if mint not in self.tokens:
            return
        
        metrics = self.tokens[mint]
        
        if len(metrics.patterns) >= self.min_patterns_to_signal:
            avg_strength = sum(p.strength for p in metrics.patterns) / len(metrics.patterns)
            pattern_types = [p.pattern_type for p in metrics.patterns]
            
            logger.warning(
                f"🚀 PUMP SIGNAL: {metrics.symbol} - {len(metrics.patterns)} patterns: "
                f"{pattern_types}, strength: {avg_strength:.2f}"
            )
            
            if self.on_pump_signal:
                await self.on_pump_signal(
                    mint=mint,
                    symbol=metrics.symbol,
                    patterns=metrics.patterns,
                    strength=avg_strength,
                )
            
            # Clear patterns after signal
            metrics.patterns = []

    def get_token_status(self, mint: str) -> dict | None:
        """Получить статус токена."""
        if mint not in self.tokens:
            return None
        
        m = self.tokens[mint]
        return {
            "mint": mint,
            "symbol": m.symbol,
            "price": m.price,
            "price_change_5m": m.price_change_5m,
            "buys_5m": m.buys_5m,
            "sells_5m": m.sells_5m,
            "whale_buys": len(m.whale_buys),
            "patterns": [p.pattern_type for p in m.patterns],
        }

    def get_all_active_tokens(self) -> list[str]:
        """Получить все отслеживаемые токены."""
        return list(self.tokens.keys())

    # Compatibility methods (not used with Birdeye)
    async def record_price(self, mint: str, price: float, volume: float = 0.0):
        pass
    
    async def record_holder_count(self, mint: str, count: int):
        pass
    
    async def record_curve_progress(self, mint: str, progress: float):
        pass
