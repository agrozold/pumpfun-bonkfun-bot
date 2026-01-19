"""
Token Scorer - оценивает токены по паттернам для определения потенциальных гемов.
Использует Dexscreener API для получения данных в реальном времени.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class TokenScore:
    """Результат скоринга токена."""
    mint: str
    symbol: str
    total_score: int  # 0-100
    volume_score: int
    buy_pressure_score: int
    momentum_score: int
    liquidity_score: int
    details: dict
    timestamp: datetime
    recommendation: str  # "STRONG_BUY", "BUY", "HOLD", "SKIP"


class TokenScorer:
    """Скоринг токенов на основе паттернов."""

    def __init__(
        self,
        min_score: int = 70,
        volume_weight: int = 30,
        buy_pressure_weight: int = 30,
        momentum_weight: int = 25,
        liquidity_weight: int = 15,
        request_timeout: float = 2.0,
    ):
        self.min_score = min_score
        self.volume_weight = volume_weight
        self.buy_pressure_weight = buy_pressure_weight
        self.momentum_weight = momentum_weight
        self.liquidity_weight = liquidity_weight
        self.request_timeout = request_timeout
        
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, TokenScore] = {}
        self._cache_ttl = 30  # секунд
        
        logger.info(
            f"TokenScorer initialized: min_score={min_score}, "
            f"weights=[vol:{volume_weight}, bp:{buy_pressure_weight}, "
            f"mom:{momentum_weight}, liq:{liquidity_weight}]"
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать HTTP сессию."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        """Закрыть сессию."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def score_token(self, mint: str, symbol: str = "UNKNOWN", is_sniper_mode: bool = False) -> TokenScore:
        """Оценить токен по всем метрикам."""
        # Проверить кэш
        cache_key = mint
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            age = (datetime.utcnow() - cached.timestamp).seconds
            if age < self._cache_ttl:
                return cached
        
        session = await self._get_session()
        
        # Получить данные с Dexscreener
        dex_data = await self._fetch_dexscreener(session, mint)
        
        if not dex_data:
            # Нет данных на Dexscreener
            if is_sniper_mode:
                # СНАЙПЕР: токен слишком свежий для Dexscreener - ЭТО НОРМАЛЬНО!
                # Покупаем на основе того что токен новый и есть bonding curve
                logger.info(f"[SNIPER] {symbol} - No Dexscreener (too fresh), BUYING with sniper defaults")
                return TokenScore(
                    mint=mint,
                    symbol=symbol,
                    total_score=70,  # Достаточно для покупки
                    volume_score=50,
                    buy_pressure_score=70,
                    momentum_score=80,
                    liquidity_score=60,
                    details={"sniper_mode": True, "reason": "Fresh token, no DEX data yet"},
                    timestamp=datetime.utcnow(),
                    recommendation="BUY",
                )
            else:
                # Volume Analyzer: требуем данные
                logger.warning(f"[SKIP] {symbol} ({mint[:8]}...) - No Dexscreener data, refusing to buy")
                return TokenScore(
                    mint=mint,
                    symbol=symbol,
                    total_score=0,  # ZERO score = SKIP
                    volume_score=0,
                    buy_pressure_score=0,
                    momentum_score=0,
                    liquidity_score=0,
                    details={"error": "No Dexscreener data - SKIP"},
                    timestamp=datetime.utcnow(),
                    recommendation="SKIP",
                )
        
        # ============================================
        # МИНИМАЛЬНЫЕ ТРЕБОВАНИЯ - ЖЁСТКИЙ ФИЛЬТР!
        # Токены с минимальной активностью = SKIP
        # ============================================
        buys_5m = dex_data.get("buys_5m", 0)
        sells_5m = dex_data.get("sells_5m", 0)
        buys_1h = dex_data.get("buys_1h", 0)
        sells_1h = dex_data.get("sells_1h", 0)
        volume_5m = dex_data.get("volume_5m", 0)
        volume_1h = dex_data.get("volume_1h", 0)
        liquidity = dex_data.get("liquidity_usd", 0)
        
        total_trades_5m = buys_5m + sells_5m
        total_trades_1h = buys_1h + sells_1h
        
        # МИНИМУМ трейдов - РАЗНЫЙ для снайпера и Volume Analyzer
        # Снайпер (свежие токены на bonding curve): 15 trades
        # Volume Analyzer (мигрированные): 50 trades
        if is_sniper_mode:
            min_trades_threshold = 15
            logger.info(f"[SNIPER] {symbol} - using relaxed min_trades=15")
        else:
            min_trades_threshold = 50
        
        min_trades_5m_ok = total_trades_5m >= min_trades_threshold
        min_trades_1h_ok = total_trades_1h >= 200
        min_trades_ok = min_trades_5m_ok or min_trades_1h_ok
        
        # МИНИМУМ: $500 объём за 5 мин И $5000 за час
        # УЖЕСТОЧЕНО: Теперь требуем ОБА условия!
        min_volume_5m_ok = volume_5m >= 500
        min_volume_1h_ok = volume_1h >= 5000
        min_volume_ok = min_volume_5m_ok and min_volume_1h_ok
        
        # МИНИМУМ: $500 ликвидности
        min_liquidity_ok = liquidity >= 500
        
        # ============================================
        # НОВЫЙ ФИЛЬТР: BUY PRESSURE RATIO
        # Если покупок примерно столько же сколько продаж = МУСОР!
        # Нужно минимум 65% покупок
        # ============================================
        buy_pressure_5m = buys_5m / total_trades_5m if total_trades_5m > 0 else 0
        buy_pressure_1h = buys_1h / total_trades_1h if total_trades_1h > 0 else 0
        
        # Берём лучший из двух периодов
        best_buy_pressure = max(buy_pressure_5m, buy_pressure_1h)
        min_buy_pressure_ok = best_buy_pressure >= 0.65  # Минимум 65% покупок
        
        if not min_trades_ok:
            logger.warning(
                f"[SKIP] {symbol} - TOO FEW TRADES: "
                f"5m={total_trades_5m} (need 50), 1h={total_trades_1h} (need 200) - Either 5m OR 1h enough"
            )
            return TokenScore(
                mint=mint,
                symbol=symbol,
                total_score=0,
                volume_score=0,
                buy_pressure_score=0,
                momentum_score=0,
                liquidity_score=0,
                details={
                    "error": f"Too few trades: 5m={total_trades_5m} (need 50), 1h={total_trades_1h} (need 200)",
                    "buys_5m": buys_5m,
                    "sells_5m": sells_5m,
                },
                timestamp=datetime.utcnow(),
                recommendation="SKIP",
            )
        
        if not min_volume_ok:
            logger.warning(
                f"[SKIP] {symbol} - TOO LOW VOLUME: "
                f"5m=${volume_5m:.2f} (need $500), 1h=${volume_1h:.2f} (need $5000) - Either 5m OR 1h enough"
            )
            return TokenScore(
                mint=mint,
                symbol=symbol,
                total_score=0,
                volume_score=0,
                buy_pressure_score=0,
                momentum_score=0,
                liquidity_score=0,
                details={
                    "error": f"Too low volume: 5m=${volume_5m:.2f} (need $500), 1h=${volume_1h:.2f} (need $5000)",
                    "volume_5m": volume_5m,
                    "volume_1h": volume_1h,
                    "min_volume_5m": 500,
                    "min_volume_1h": 5000,
                },
                timestamp=datetime.utcnow(),
                recommendation="SKIP",
            )
        
        if not min_liquidity_ok:
            logger.warning(
                f"[SKIP] {symbol} - TOO LOW LIQUIDITY: ${liquidity:.2f} (need $500)"
            )
            return TokenScore(
                mint=mint,
                symbol=symbol,
                total_score=0,
                volume_score=0,
                buy_pressure_score=0,
                momentum_score=0,
                liquidity_score=0,
                details={
                    "error": f"Too low liquidity: ${liquidity:.2f}",
                    "liquidity_usd": liquidity,
                },
                timestamp=datetime.utcnow(),
                recommendation="SKIP",
            )
        
        # ============================================
        # НОВЫЙ ФИЛЬТР: BUY PRESSURE CHECK
        # Токен с 55 покупок / 43 продажи = 56% = МУСОР!
        # ============================================
        if not min_buy_pressure_ok:
            logger.warning(
                f"[SKIP] {symbol} - TOO LOW BUY PRESSURE: "
                f"5m={buy_pressure_5m:.1%} ({buys_5m}b/{sells_5m}s), "
                f"1h={buy_pressure_1h:.1%} ({buys_1h}b/{sells_1h}s) - need 65%+"
            )
            return TokenScore(
                mint=mint,
                symbol=symbol,
                total_score=0,
                volume_score=0,
                buy_pressure_score=0,
                momentum_score=0,
                liquidity_score=0,
                details={
                    "error": f"Too low buy pressure: best={best_buy_pressure:.1%} (need 65%)",
                    "buys_5m": buys_5m,
                    "sells_5m": sells_5m,
                    "buys_1h": buys_1h,
                    "sells_1h": sells_1h,
                    "buy_pressure_5m": buy_pressure_5m,
                    "buy_pressure_1h": buy_pressure_1h,
                },
                timestamp=datetime.utcnow(),
                recommendation="SKIP",
            )
        
        # Рассчитать отдельные scores
        volume_score = self._calc_volume_score(dex_data)
        buy_pressure_score = self._calc_buy_pressure_score(dex_data)
        momentum_score = self._calc_momentum_score(dex_data)
        liquidity_score = self._calc_liquidity_score(dex_data)
        
        # Взвешенный total score
        total_score = (
            volume_score * self.volume_weight +
            buy_pressure_score * self.buy_pressure_weight +
            momentum_score * self.momentum_weight +
            liquidity_score * self.liquidity_weight
        ) // 100
        
        # Определить рекомендацию
        if total_score >= 85:
            recommendation = "STRONG_BUY"
        elif total_score >= self.min_score:
            recommendation = "BUY"
        elif total_score >= 50:
            recommendation = "HOLD"
        else:
            recommendation = "SKIP"
        
        score = TokenScore(
            mint=mint,
            symbol=dex_data.get("symbol", symbol),
            total_score=total_score,
            volume_score=volume_score,
            buy_pressure_score=buy_pressure_score,
            momentum_score=momentum_score,
            liquidity_score=liquidity_score,
            details={
                "price_usd": dex_data.get("price_usd"),
                "volume_5m": dex_data.get("volume_5m"),
                "volume_1h": dex_data.get("volume_1h"),
                "buys_5m": dex_data.get("buys_5m"),
                "sells_5m": dex_data.get("sells_5m"),
                "price_change_5m": dex_data.get("price_change_5m"),
                "price_change_1h": dex_data.get("price_change_1h"),
                "liquidity_usd": dex_data.get("liquidity_usd"),
            },
            timestamp=datetime.utcnow(),
            recommendation=recommendation,
        )
        
        # Кэшировать
        self._cache[cache_key] = score
        
        logger.info(
            f"Token score for {score.symbol}: {total_score}/100 "
            f"[vol:{volume_score}, bp:{buy_pressure_score}, "
            f"mom:{momentum_score}, liq:{liquidity_score}] → {recommendation}"
        )
        
        return score

    async def _fetch_dexscreener(self, session: aiohttp.ClientSession, mint: str) -> dict | None:
        """Получить данные токена с Dexscreener."""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"Dexscreener returned {resp.status} for {mint[:8]}...")
                    return None
                
                data = await resp.json()
                pairs = data.get("pairs", [])
                
                if not pairs:
                    return None
                
                # Взять пару с наибольшей ликвидностью
                pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
                
                return {
                    "symbol": pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                    "price_usd": float(pair.get("priceUsd", 0) or 0),
                    "volume_5m": float(pair.get("volume", {}).get("m5", 0) or 0),
                    "volume_1h": float(pair.get("volume", {}).get("h1", 0) or 0),
                    "volume_24h": float(pair.get("volume", {}).get("h24", 0) or 0),
                    "buys_5m": int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0),
                    "sells_5m": int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0),
                    "buys_1h": int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0),
                    "sells_1h": int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0),
                    "price_change_5m": float(pair.get("priceChange", {}).get("m5", 0) or 0),
                    "price_change_1h": float(pair.get("priceChange", {}).get("h1", 0) or 0),
                    "price_change_24h": float(pair.get("priceChange", {}).get("h24", 0) or 0),
                    "liquidity_usd": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                    "fdv": float(pair.get("fdv", 0) or 0),
                    "pair_created": pair.get("pairCreatedAt"),
                }
                
        except asyncio.TimeoutError:
            logger.debug(f"Dexscreener timeout for {mint[:8]}...")
            return None
        except Exception as e:
            logger.debug(f"Dexscreener error for {mint[:8]}...: {e}")
            return None

    def _calc_volume_score(self, data: dict) -> int:
        """Рассчитать score по объёму (0-100)."""
        vol_5m = data.get("volume_5m", 0)
        vol_1h = data.get("volume_1h", 0)
        
        # Средний 5-минутный объём за час
        avg_5m = vol_1h / 12 if vol_1h > 0 else 0
        
        if avg_5m == 0:
            # Новый токен без истории - нейтральный score
            if vol_5m > 1000:
                return 70
            elif vol_5m > 100:
                return 50
            return 30
        
        # Volume spike ratio
        spike_ratio = vol_5m / avg_5m if avg_5m > 0 else 1
        
        if spike_ratio >= 5:
            return 100
        elif spike_ratio >= 3:
            return 85
        elif spike_ratio >= 2:
            return 70
        elif spike_ratio >= 1.5:
            return 60
        elif spike_ratio >= 1:
            return 50
        else:
            return 30

    def _calc_buy_pressure_score(self, data: dict) -> int:
        """Рассчитать score по давлению покупок (0-100)."""
        buys = data.get("buys_5m", 0)
        sells = data.get("sells_5m", 0)
        total = buys + sells
        
        if total == 0:
            # Нет транзакций - проверить 1h
            buys = data.get("buys_1h", 0)
            sells = data.get("sells_1h", 0)
            total = buys + sells
        
        if total == 0:
            return 50  # Нейтральный
        
        buy_ratio = buys / total
        
        if buy_ratio >= 0.9:
            return 100
        elif buy_ratio >= 0.8:
            return 90
        elif buy_ratio >= 0.7:
            return 80
        elif buy_ratio >= 0.6:
            return 65
        elif buy_ratio >= 0.5:
            return 50
        elif buy_ratio >= 0.4:
            return 35
        else:
            return 20  # Много продаж - плохо

    def _calc_momentum_score(self, data: dict) -> int:
        """Рассчитать score по моментуму цены (0-100)."""
        change_5m = data.get("price_change_5m", 0)
        change_1h = data.get("price_change_1h", 0)
        
        # Комбинированный momentum
        # Положительный 5m важнее чем 1h
        
        if change_5m >= 20:
            base_score = 95
        elif change_5m >= 10:
            base_score = 85
        elif change_5m >= 5:
            base_score = 75
        elif change_5m >= 0:
            base_score = 60
        elif change_5m >= -5:
            base_score = 45
        elif change_5m >= -10:
            base_score = 30
        else:
            base_score = 15
        
        # Бонус за положительный 1h тренд
        if change_1h > 0:
            base_score = min(100, base_score + 10)
        elif change_1h < -20:
            base_score = max(0, base_score - 15)
        
        return base_score

    def _calc_liquidity_score(self, data: dict) -> int:
        """Рассчитать score по ликвидности (0-100)."""
        liquidity = data.get("liquidity_usd", 0)
        
        # Для pump.fun токенов ликвидность обычно низкая
        # Слишком высокая = уже поздно входить
        # Слишком низкая = рискованно
        
        if liquidity >= 100000:
            return 60  # Уже большой - меньше потенциал
        elif liquidity >= 50000:
            return 80
        elif liquidity >= 20000:
            return 90
        elif liquidity >= 10000:
            return 100  # Sweet spot
        elif liquidity >= 5000:
            return 85
        elif liquidity >= 1000:
            return 60
        else:
            return 30  # Слишком мало - рискованно

    async def should_buy(self, mint: str, symbol: str = "UNKNOWN", is_sniper_mode: bool = False) -> tuple[bool, TokenScore]:
        """Проверить стоит ли покупать токен."""
        score = await self.score_token(mint, symbol, is_sniper_mode)
        should = score.total_score >= self.min_score
        return should, score

    def clear_cache(self):
        """Очистить кэш."""
        self._cache.clear()
