"""
Pump Pattern Detector - отслеживает паттерны перед пампами токенов.

Паттерны:
1. Volume Spike - резкий рост объёма торговли (3x+)
2. Holder Growth - быстрый рост количества холдеров (50%+ за минуту)
3. Price Momentum - рост цены на малом объёме (накопление)
4. Multiple Whale Buys - несколько whale покупок за короткий период
5. Bonding Curve Progress - скорость заполнения кривой
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class TokenMetrics:
    """Метрики токена для отслеживания паттернов."""

    mint: str
    symbol: str
    first_seen: datetime = field(default_factory=datetime.utcnow)

    # Price history (timestamp, price)
    price_history: list = field(default_factory=list)

    # Volume history (timestamp, volume_sol)
    volume_history: list = field(default_factory=list)

    # Holder count history (timestamp, count)
    holder_history: list = field(default_factory=list)

    # Whale buys (timestamp, wallet, amount)
    whale_buys: list = field(default_factory=list)

    # Bonding curve progress (0-100%)
    curve_progress: float = 0.0

    # Detected patterns
    patterns_detected: list = field(default_factory=list)


@dataclass
class PatternSignal:
    """Сигнал о обнаруженном паттерне."""

    pattern_type: str  # VOLUME_SPIKE, HOLDER_GROWTH, MOMENTUM, WHALE_CLUSTER, CURVE_ACCELERATION
    strength: float  # 0.0 - 1.0
    description: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


class PumpPatternDetector:
    """Детектор паттернов перед пампами."""

    def __init__(
        self,
        # Volume spike settings
        volume_spike_threshold: float = 3.0,  # 3x от среднего
        volume_window_seconds: int = 60,
        # Holder growth settings
        holder_growth_threshold: float = 0.5,  # 50% рост
        holder_window_seconds: int = 60,
        # Price momentum settings
        momentum_threshold: float = 0.2,  # 20% рост цены
        low_volume_threshold: float = 0.5,  # Объём ниже 50% от среднего
        # Whale cluster settings
        min_whale_buys: int = 2,  # Минимум 2 whale покупки
        whale_window_seconds: int = 30,
        min_whale_amount: float = 0.5,  # Минимум 0.5 SOL
        # Curve acceleration settings
        curve_acceleration_threshold: float = 5.0,  # 5% за минуту
        # General settings
        min_patterns_to_signal: int = 2,  # Минимум паттернов для сигнала
    ):
        self.volume_spike_threshold = volume_spike_threshold
        self.volume_window = timedelta(seconds=volume_window_seconds)
        self.holder_growth_threshold = holder_growth_threshold
        self.holder_window = timedelta(seconds=holder_window_seconds)
        self.momentum_threshold = momentum_threshold
        self.low_volume_threshold = low_volume_threshold
        self.min_whale_buys = min_whale_buys
        self.whale_window = timedelta(seconds=whale_window_seconds)
        self.min_whale_amount = min_whale_amount
        self.curve_acceleration_threshold = curve_acceleration_threshold
        self.min_patterns_to_signal = min_patterns_to_signal

        # Active token tracking
        self.tokens: dict[str, TokenMetrics] = {}

        # Callback for pump signals
        self.on_pump_signal: Callable | None = None

        logger.info("PumpPatternDetector initialized")

    def set_pump_signal_callback(self, callback: Callable):
        """Установить callback для сигналов о пампе."""
        self.on_pump_signal = callback

    def start_tracking(self, mint: str, symbol: str) -> TokenMetrics:
        """Начать отслеживание токена."""
        if mint not in self.tokens:
            self.tokens[mint] = TokenMetrics(mint=mint, symbol=symbol)
            logger.info(f"Started tracking patterns for {symbol} ({mint[:8]}...)")
        return self.tokens[mint]

    def stop_tracking(self, mint: str):
        """Остановить отслеживание токена."""
        if mint in self.tokens:
            del self.tokens[mint]
            logger.debug(f"Stopped tracking {mint[:8]}...")

    async def record_price(self, mint: str, price: float, volume: float = 0.0):
        """Записать цену и объём."""
        if mint not in self.tokens:
            return

        now = datetime.utcnow()
        metrics = self.tokens[mint]
        metrics.price_history.append((now, price))
        metrics.volume_history.append((now, volume))

        # Cleanup old data (keep last 5 minutes)
        cutoff = now - timedelta(minutes=5)
        metrics.price_history = [(t, p) for t, p in metrics.price_history if t > cutoff]
        metrics.volume_history = [(t, v) for t, v in metrics.volume_history if t > cutoff]

        # Check patterns
        await self._check_patterns(mint)

    async def record_holder_count(self, mint: str, count: int):
        """Записать количество холдеров."""
        if mint not in self.tokens:
            return

        now = datetime.utcnow()
        metrics = self.tokens[mint]
        metrics.holder_history.append((now, count))

        # Cleanup old data
        cutoff = now - timedelta(minutes=5)
        metrics.holder_history = [(t, c) for t, c in metrics.holder_history if t > cutoff]

        await self._check_patterns(mint)

    async def record_whale_buy(self, mint: str, wallet: str, amount_sol: float):
        """Записать покупку whale."""
        if mint not in self.tokens:
            return

        if amount_sol < self.min_whale_amount:
            return

        now = datetime.utcnow()
        metrics = self.tokens[mint]
        metrics.whale_buys.append((now, wallet, amount_sol))

        # Cleanup old data
        cutoff = now - timedelta(minutes=5)
        metrics.whale_buys = [(t, w, a) for t, w, a in metrics.whale_buys if t > cutoff]

        logger.info(
            f"[WHALE BUY] {metrics.symbol}: {wallet[:8]}... bought {amount_sol:.2f} SOL"
        )

        await self._check_patterns(mint)

    async def record_curve_progress(self, mint: str, progress: float):
        """Записать прогресс bonding curve (0-100%)."""
        if mint not in self.tokens:
            return

        metrics = self.tokens[mint]
        old_progress = metrics.curve_progress
        metrics.curve_progress = progress

        # Check for acceleration
        if old_progress > 0:
            acceleration = progress - old_progress
            if acceleration >= self.curve_acceleration_threshold:
                signal = PatternSignal(
                    pattern_type="CURVE_ACCELERATION",
                    strength=min(acceleration / 10.0, 1.0),
                    description=f"Curve jumped {acceleration:.1f}% (from {old_progress:.1f}% to {progress:.1f}%)",
                )
                await self._add_pattern(mint, signal)

    async def _check_patterns(self, mint: str):
        """Проверить все паттерны для токена."""
        if mint not in self.tokens:
            return

        metrics = self.tokens[mint]
        now = datetime.utcnow()

        # 1. Check Volume Spike
        await self._check_volume_spike(mint, metrics, now)

        # 2. Check Holder Growth
        await self._check_holder_growth(mint, metrics, now)

        # 3. Check Price Momentum
        await self._check_price_momentum(mint, metrics, now)

        # 4. Check Whale Cluster
        await self._check_whale_cluster(mint, metrics, now)

        # Check if we should signal
        await self._evaluate_signal(mint)

    async def _check_volume_spike(
        self, mint: str, metrics: TokenMetrics, now: datetime
    ):
        """Проверить резкий рост объёма."""
        if len(metrics.volume_history) < 5:
            return

        cutoff = now - self.volume_window
        recent = [v for t, v in metrics.volume_history if t > cutoff]
        older = [v for t, v in metrics.volume_history if t <= cutoff]

        if not recent or not older:
            return

        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older) if older else recent_avg

        if older_avg > 0 and recent_avg > older_avg * self.volume_spike_threshold:
            spike_ratio = recent_avg / older_avg
            signal = PatternSignal(
                pattern_type="VOLUME_SPIKE",
                strength=min(spike_ratio / 10.0, 1.0),
                description=f"Volume {spike_ratio:.1f}x higher than average",
            )
            await self._add_pattern(mint, signal)

    async def _check_holder_growth(
        self, mint: str, metrics: TokenMetrics, now: datetime
    ):
        """Проверить рост количества холдеров."""
        if len(metrics.holder_history) < 2:
            return

        cutoff = now - self.holder_window
        recent = [c for t, c in metrics.holder_history if t > cutoff]
        older = [c for t, c in metrics.holder_history if t <= cutoff]

        if not recent or not older:
            return

        recent_count = recent[-1] if recent else 0
        older_count = older[-1] if older else recent_count

        if older_count > 0:
            growth = (recent_count - older_count) / older_count
            if growth >= self.holder_growth_threshold:
                signal = PatternSignal(
                    pattern_type="HOLDER_GROWTH",
                    strength=min(growth, 1.0),
                    description=f"Holders grew {growth * 100:.0f}% ({older_count} → {recent_count})",
                )
                await self._add_pattern(mint, signal)

    async def _check_price_momentum(
        self, mint: str, metrics: TokenMetrics, now: datetime
    ):
        """Проверить рост цены на малом объёме (накопление)."""
        if len(metrics.price_history) < 5 or len(metrics.volume_history) < 5:
            return

        # Get recent price change
        prices = [p for _, p in metrics.price_history[-10:]]
        if len(prices) < 2 or prices[0] == 0:
            return

        price_change = (prices[-1] - prices[0]) / prices[0]

        # Get volume level
        volumes = [v for _, v in metrics.volume_history]
        if not volumes:
            return

        avg_volume = sum(volumes) / len(volumes)
        recent_volume = volumes[-1] if volumes else 0

        # Check for momentum on low volume
        is_low_volume = recent_volume < avg_volume * self.low_volume_threshold
        is_price_up = price_change >= self.momentum_threshold

        if is_price_up and is_low_volume:
            signal = PatternSignal(
                pattern_type="ACCUMULATION",
                strength=min(price_change * 2, 1.0),
                description=f"Price +{price_change * 100:.1f}% on low volume (accumulation pattern)",
            )
            await self._add_pattern(mint, signal)

    async def _check_whale_cluster(
        self, mint: str, metrics: TokenMetrics, now: datetime
    ):
        """Проверить кластер whale покупок."""
        cutoff = now - self.whale_window
        recent_whales = [(t, w, a) for t, w, a in metrics.whale_buys if t > cutoff]

        if len(recent_whales) >= self.min_whale_buys:
            total_amount = sum(a for _, _, a in recent_whales)
            unique_wallets = len(set(w for _, w, _ in recent_whales))

            signal = PatternSignal(
                pattern_type="WHALE_CLUSTER",
                strength=min(len(recent_whales) / 5.0, 1.0),
                description=f"{len(recent_whales)} whale buys ({unique_wallets} wallets) totaling {total_amount:.2f} SOL in {self.whale_window.seconds}s",
            )
            await self._add_pattern(mint, signal)

    async def _add_pattern(self, mint: str, signal: PatternSignal):
        """Добавить обнаруженный паттерн."""
        if mint not in self.tokens:
            return

        metrics = self.tokens[mint]

        # Avoid duplicate patterns in short time
        for existing in metrics.patterns_detected:
            if (
                existing.pattern_type == signal.pattern_type
                and (signal.timestamp - existing.timestamp).seconds < 30
            ):
                return

        metrics.patterns_detected.append(signal)
        logger.info(
            f"[PATTERN] {metrics.symbol}: {signal.pattern_type} (strength: {signal.strength:.2f}) - {signal.description}"
        )

        # Cleanup old patterns (keep last 2 minutes)
        cutoff = datetime.utcnow() - timedelta(minutes=2)
        metrics.patterns_detected = [
            p for p in metrics.patterns_detected if p.timestamp > cutoff
        ]

    async def _evaluate_signal(self, mint: str):
        """Оценить нужно ли подавать сигнал о пампе."""
        if mint not in self.tokens:
            return

        metrics = self.tokens[mint]

        # Count recent patterns
        cutoff = datetime.utcnow() - timedelta(seconds=60)
        recent_patterns = [p for p in metrics.patterns_detected if p.timestamp > cutoff]

        if len(recent_patterns) >= self.min_patterns_to_signal:
            # Calculate combined strength
            total_strength = sum(p.strength for p in recent_patterns) / len(
                recent_patterns
            )
            pattern_types = [p.pattern_type for p in recent_patterns]

            logger.warning(
                f"🚀 [PUMP SIGNAL] {metrics.symbol}: {len(recent_patterns)} patterns detected! "
                f"Types: {pattern_types}, Strength: {total_strength:.2f}"
            )

            if self.on_pump_signal:
                await self.on_pump_signal(
                    mint=mint,
                    symbol=metrics.symbol,
                    patterns=recent_patterns,
                    strength=total_strength,
                )

    def get_token_status(self, mint: str) -> dict | None:
        """Получить статус отслеживания токена."""
        if mint not in self.tokens:
            return None

        metrics = self.tokens[mint]
        return {
            "mint": mint,
            "symbol": metrics.symbol,
            "tracking_since": metrics.first_seen.isoformat(),
            "price_points": len(metrics.price_history),
            "volume_points": len(metrics.volume_history),
            "holder_points": len(metrics.holder_history),
            "whale_buys": len(metrics.whale_buys),
            "curve_progress": metrics.curve_progress,
            "patterns_detected": [
                {
                    "type": p.pattern_type,
                    "strength": p.strength,
                    "description": p.description,
                    "timestamp": p.timestamp.isoformat(),
                }
                for p in metrics.patterns_detected
            ],
        }

    def get_all_active_tokens(self) -> list[str]:
        """Получить список всех отслеживаемых токенов."""
        return list(self.tokens.keys())
