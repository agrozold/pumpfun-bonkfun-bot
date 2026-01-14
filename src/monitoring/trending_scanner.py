"""
Trending Token Scanner - сканирует существующие токены на pump.fun.

Находит токены с резким ростом объёма/цены за последние часы.
Использует DexScreener API (бесплатный, без лимитов).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com"


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
        # 12 периодов по 5 минут в часе
        avg_5m = self.volume_1h / 12 if self.volume_1h > 0 else 0
        return self.volume_5m / avg_5m if avg_5m > 0 else 0


class TrendingScanner:
    """Сканер трендовых токенов pump.fun."""

    def __init__(
        self,
        # Фильтры
        min_volume_1h: float = 50000,        # Минимум $50k объёма за 1 час!
        min_market_cap: float = 10000,       # Минимум $10k маркеткап
        max_market_cap: float = 5000000,     # Максимум $5M (не слишком поздно)
        min_liquidity: float = 5000,         # Минимум $5k ликвидности
        max_token_age_hours: float = 24,     # Токены не старше 24 часов
        # Триггеры для покупки - РЕЗКИЙ ВСПЛЕСК
        min_price_change_5m: float = 5,      # Минимум +5% за 5 минут
        min_price_change_1h: float = 20,     # Минимум +20% за час
        min_buy_pressure: float = 0.65,      # 65% покупок (резкий buy)
        min_trade_velocity: int = 15,        # 15+ сделок за 5 мин
        min_volume_ratio: float = 3.0,       # Объём 5м должен быть 3x от среднего
        # Настройки
        scan_interval: float = 30,           # Сканировать каждые 30 сек
        max_concurrent_buys: int = 3,        # Макс покупок за цикл
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
        
        # State
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._scan_task: asyncio.Task | None = None
        self.on_trending_token: Callable | None = None
        
        # Track already processed tokens (avoid duplicates)
        self.processed_tokens: set[str] = set()
        self.processed_tokens_timestamps: dict[str, float] = {}
        
        logger.info(
            f"TrendingScanner initialized: "
            f"min_vol_1h=${min_volume_1h:,.0f}, "
            f"min_mc=${min_market_cap:,.0f}, "
            f"max_mc=${max_market_cap:,.0f}, "
            f"min_change_5m={min_price_change_5m}%, "
            f"min_buy_pressure={min_buy_pressure*100:.0f}%"
        )

    def set_callback(self, callback: Callable):
        """Установить callback для найденных токенов."""
        self.on_trending_token = callback

    async def start(self):
        """Запустить сканер."""
        if self._running:
            return
        
        self._running = True
        self._session = aiohttp.ClientSession()
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("🔍 Trending scanner started")

    async def stop(self):
        """Остановить сканер."""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
        if self._session:
            await self._session.close()
        logger.info("Trending scanner stopped")

    async def _scan_loop(self):
        """Основной цикл сканирования."""
        while self._running:
            try:
                await self._scan_trending()
                await asyncio.sleep(self.scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Scan error: {e}")
                await asyncio.sleep(10)

    async def _scan_trending(self):
        """Сканировать трендовые токены."""
        # Cleanup old processed tokens (older than 1 hour)
        self._cleanup_processed()
        
        # Get trending tokens from DexScreener
        tokens = await self._fetch_pump_tokens()
        if not tokens:
            logger.debug("No tokens fetched from DexScreener")
            return
        
        logger.info(f"🔍 Scanned {len(tokens)} pump.fun tokens from DexScreener")
        
        # Filter and score tokens
        candidates = []
        for token in tokens:
            if token.mint in self.processed_tokens:
                continue
            
            score, reasons = self._evaluate_token(token)
            if score > 0:
                candidates.append((token, score, reasons))
        
        if candidates:
            logger.info(f"📊 Found {len(candidates)} candidates passing filters")
        
        # Sort by score and take top candidates
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        for token, score, reasons in candidates[:self.max_concurrent_buys]:
            logger.warning(
                f"🔥 TRENDING: {token.symbol} - "
                f"MC: ${token.market_cap:,.0f}, "
                f"Vol24h: ${token.volume_24h:,.0f}, "
                f"Change1h: {token.price_change_1h:+.1f}%, "
                f"Score: {score}"
            )
            for reason in reasons:
                logger.info(f"   ✓ {reason}")
            
            # Mark as processed
            self.processed_tokens.add(token.mint)
            self.processed_tokens_timestamps[token.mint] = datetime.utcnow().timestamp()
            
            # Trigger callback
            if self.on_trending_token:
                await self.on_trending_token(token)

    async def _fetch_pump_tokens(self) -> list[TrendingToken]:
        """Получить токены pump.fun с DexScreener."""
        if not self._session:
            return []
        
        tokens = []
        
        try:
            # DexScreener search for pump.fun tokens
            # Using boosted tokens endpoint for trending
            url = f"{DEXSCREENER_API}/token-boosts/top/v1"
            
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.debug(f"DexScreener boosts error: {resp.status}")
                    # Fallback to search
                    return await self._fetch_pump_tokens_search()
                
                data = await resp.json()
                
                for item in data:
                    # Filter only pump.fun tokens (Solana + pump suffix)
                    token_addr = item.get("tokenAddress", "")
                    chain = item.get("chainId", "")
                    
                    if chain != "solana" or not token_addr.endswith("pump"):
                        continue
                    
                    # Get detailed info
                    detail = await self._fetch_token_detail(token_addr)
                    if detail:
                        tokens.append(detail)
                    
                    await asyncio.sleep(0.2)  # Rate limit
                    
                    if len(tokens) >= 20:  # Limit
                        break
        
        except Exception as e:
            logger.debug(f"Fetch boosts error: {e}")
            return await self._fetch_pump_tokens_search()
        
        return tokens

    async def _fetch_pump_tokens_search(self) -> list[TrendingToken]:
        """Fallback - поиск pump.fun токенов."""
        if not self._session:
            return []
        
        tokens = []
        
        try:
            # Search for recent pump.fun tokens
            url = f"{DEXSCREENER_API}/latest/dex/search?q=pump"
            
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                
                data = await resp.json()
                pairs = data.get("pairs", [])
                
                for pair in pairs:
                    # Filter pump.fun on Solana
                    if pair.get("chainId") != "solana":
                        continue
                    
                    base = pair.get("baseToken", {})
                    addr = base.get("address", "")
                    
                    if not addr.endswith("pump"):
                        continue
                    
                    token = self._parse_pair(pair)
                    if token:
                        tokens.append(token)
                    
                    if len(tokens) >= 30:
                        break
        
        except Exception as e:
            logger.debug(f"Search error: {e}")
        
        return tokens

    async def _fetch_token_detail(self, mint: str) -> TrendingToken | None:
        """Получить детали токена."""
        if not self._session:
            return None
        
        try:
            url = f"{DEXSCREENER_API}/latest/dex/tokens/{mint}"
            
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                pairs = data.get("pairs", [])
                
                if pairs:
                    return self._parse_pair(pairs[0])
        
        except Exception as e:
            logger.debug(f"Token detail error: {e}")
        
        return None

    def _parse_pair(self, pair: dict) -> TrendingToken | None:
        """Парсить пару в TrendingToken."""
        try:
            base = pair.get("baseToken", {})
            txns = pair.get("txns", {})
            m5 = txns.get("m5", {})
            h1 = txns.get("h1", {})
            volume = pair.get("volume", {})
            
            # Parse creation time
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
            )
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    def _evaluate_token(self, token: TrendingToken) -> tuple[int, list[str]]:
        """Оценить токен. Возвращает (score, reasons)."""
        score = 0
        reasons = []
        
        # Basic filters - ОБЯЗАТЕЛЬНЫЕ
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
        
        # === КРИТЕРИИ РЕЗКОГО ВСПЛЕСКА ===
        
        # 1. Резкий рост цены за 5 минут (ВАЖНО!)
        if token.price_change_5m >= self.min_price_change_5m:
            score += 35
            reasons.append(f"🚀 Price +{token.price_change_5m:.1f}% in 5min!")
        elif token.price_change_1h >= self.min_price_change_1h:
            # Или хороший рост за час
            score += 25
            reasons.append(f"📈 Price +{token.price_change_1h:.1f}% in 1h")
        else:
            # Без роста - не покупаем
            return 0, []
        
        # 2. Buy pressure за 5 минут (бонус, не обязательно)
        if token.buy_pressure_5m >= self.min_buy_pressure:
            score += 30
            reasons.append(f"💪 Buy pressure {token.buy_pressure_5m*100:.0f}% (5m)")
        elif token.buy_pressure_5m >= 0.5:
            score += 15
            reasons.append(f"👍 Buy pressure {token.buy_pressure_5m*100:.0f}% (5m)")
        
        # 3. Trade velocity (активность)
        if token.trade_velocity >= self.min_trade_velocity:
            score += 20
            reasons.append(f"⚡ {token.trade_velocity} trades in 5min")
        
        # 4. Volume ratio (всплеск объёма)
        if token.volume_ratio >= self.min_volume_ratio:
            score += 15
            reasons.append(f"📈 Volume {token.volume_ratio:.1f}x average")
        
        # 5. Бонус за рост за час
        if token.price_change_1h >= self.min_price_change_1h:
            score += 10
            reasons.append(f"📊 +{token.price_change_1h:.1f}% in 1h")
        
        # 6. Бонус за ранний маркеткап
        if 20000 <= token.market_cap <= 200000:
            score += 10
            reasons.append(f"🎯 Early MC: ${token.market_cap:,.0f}")
        
        return score, reasons

    def _cleanup_processed(self):
        """Очистить старые обработанные токены."""
        now = datetime.utcnow().timestamp()
        cutoff = now - 3600  # 1 hour
        
        to_remove = [
            mint for mint, ts in self.processed_tokens_timestamps.items()
            if ts < cutoff
        ]
        
        for mint in to_remove:
            self.processed_tokens.discard(mint)
            self.processed_tokens_timestamps.pop(mint, None)
