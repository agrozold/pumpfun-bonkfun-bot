import statistics
import time
from enum import Enum
from typing import Optional, Tuple

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee import PriorityFeePlugin
from utils.logger import get_logger

logger = get_logger(__name__)


class FeeStrategy(Enum):
    """Стратегии расчёта приоритетной комиссии."""
    CONSERVATIVE = "conservative"  # 50-й перцентиль (медиана) - экономия
    AGGRESSIVE = "aggressive"      # 75-й перцентиль - баланс скорость/цена
    SNIPER = "sniper"              # 90-й перцентиль - максимальная скорость


# Дефолтные аккаунты DEX для получения релевантных комиссий
DEFAULT_DEX_ACCOUNTS = [
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # Pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # Raydium LaunchLab (Bonk)
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",  # Meteora DBC (Bags)
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
]


class DynamicPriorityFee(PriorityFeePlugin):
    """Dynamic priority fee plugin using getRecentPrioritizationFees."""

    # Конфигурация по умолчанию
    MIN_FEE = 50_000           # Минимум 50k микролампортов
    MAX_FEE = 10_000_000       # Максимум 10M микролампортов
    FALLBACK_FEE = 500_000     # Запасное значение при ошибке
    CACHE_TTL = 2.0            # Время жизни кэша в секундах

    # Множители для стратегий
    STRATEGY_MULTIPLIERS = {
        FeeStrategy.CONSERVATIVE: 1.0,
        FeeStrategy.AGGRESSIVE: 1.5,  # Увеличен с 1.3
        FeeStrategy.SNIPER: 1.2,      # Уменьшен - 90-й перцентиль уже высокий
    }

    # Перцентили для стратегий
    STRATEGY_PERCENTILES = {
        FeeStrategy.CONSERVATIVE: 0.50,  # Медиана
        FeeStrategy.AGGRESSIVE: 0.75,    # 75-й перцентиль
        FeeStrategy.SNIPER: 0.90,        # 90-й перцентиль
    }

    def __init__(
        self,
        client: SolanaClient,
        strategy: FeeStrategy = FeeStrategy.AGGRESSIVE,
        min_fee: int = None,
        max_fee: int = None,
        fallback_fee: int = None,
        use_default_accounts: bool = True,
    ):
        """
        Initialize the dynamic fee plugin.

        Args:
            client: Solana RPC client for network requests.
            strategy: Fee calculation strategy (conservative/aggressive/sniper).
            min_fee: Minimum fee in microlamports.
            max_fee: Maximum fee in microlamports.
            fallback_fee: Fallback fee when RPC fails.
            use_default_accounts: Use DEX program accounts for relevant fees.
        """
        self.client = client
        self.strategy = strategy
        self.min_fee = min_fee or self.MIN_FEE
        self.max_fee = max_fee or self.MAX_FEE
        self.fallback_fee = fallback_fee or self.FALLBACK_FEE
        self.use_default_accounts = use_default_accounts

        # Кэш: (timestamp, fee_value)
        self._cache: Optional[Tuple[float, int]] = None

    def set_strategy(self, strategy: FeeStrategy | str):
        """Change the fee calculation strategy."""
        if isinstance(strategy, str):
            strategy = FeeStrategy(strategy.lower())
        self.strategy = strategy
        # Сбрасываем кэш при смене стратегии
        self._cache = None
        logger.info(f"Priority fee strategy changed to: {strategy.value}")

    async def get_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """
        Fetch and calculate the priority fee based on current strategy.

        Args:
            accounts: List of accounts to consider for the fee calculation.
                     If None and use_default_accounts=True, uses DEX program accounts.

        Returns:
            Optional[int]: Calculated priority fee in microlamports.
        """
        # Проверяем кэш
        if self._cache:
            cache_time, cached_fee = self._cache
            if time.time() - cache_time < self.CACHE_TTL:
                logger.debug(f"Using cached priority fee: {cached_fee:,}")
                return cached_fee

        try:
            # Используем дефолтные аккаунты если не переданы и включена опция
            if accounts is None and self.use_default_accounts:
                accounts = [Pubkey.from_string(acc) for acc in DEFAULT_DEX_ACCOUNTS]

            fees = await self._fetch_recent_fees(accounts)
            if not fees:
                logger.warning(f"No fees data, using fallback: {self.fallback_fee:,}")
                return self.fallback_fee

            calculated_fee = self._calculate_fee(fees)

            # Обновляем кэш
            self._cache = (time.time(), calculated_fee)

            logger.info(
                f"Priority fee calculated: {calculated_fee:,} µL "
                f"(strategy={self.strategy.value}, samples={len(fees)})"
            )
            return calculated_fee

        except Exception:
            logger.exception("Failed to fetch priority fee, using fallback")
            return self.fallback_fee

    async def _fetch_recent_fees(
        self, accounts: list[Pubkey] | None = None
    ) -> list[int]:
        """Fetch recent prioritization fees from RPC."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getRecentPrioritizationFees",
            "params": [[str(account) for account in accounts]] if accounts else [],
        }

        response = await self.client.post_rpc(body)
        if not response or "result" not in response:
            logger.error("Invalid response from getRecentPrioritizationFees")
            return []

        fees = [
            fee["prioritizationFee"]
            for fee in response["result"]
            if fee.get("prioritizationFee", 0) > 0
        ]
        return fees

    def _calculate_fee(self, fees: list[int]) -> int:
        """Calculate fee based on strategy with bounds."""
        if not fees:
            return self.fallback_fee

        sorted_fees = sorted(fees)

        # Получаем перцентиль для текущей стратегии
        percentile = self.STRATEGY_PERCENTILES.get(
            self.strategy, 0.75
        )

        # Вычисляем базовую комиссию
        if len(sorted_fees) >= 10:
            # Используем quantiles для точного расчёта
            quantile_idx = int(percentile * 10) - 1  # 0-9
            quantile_idx = max(0, min(quantile_idx, 9))
            base_fee = statistics.quantiles(sorted_fees, n=10)[quantile_idx]
        else:
            # Для малых выборок - простой индекс
            idx = int(len(sorted_fees) * percentile)
            idx = min(idx, len(sorted_fees) - 1)
            base_fee = sorted_fees[idx]

        # Применяем множитель стратегии
        multiplier = self.STRATEGY_MULTIPLIERS.get(self.strategy, 1.0)
        calculated_fee = int(base_fee * multiplier)

        # Применяем границы
        final_fee = max(self.min_fee, min(calculated_fee, self.max_fee))

        logger.debug(
            f"Fee calculation: base={base_fee:,}, "
            f"multiplier={multiplier}, "
            f"calculated={calculated_fee:,}, "
            f"final={final_fee:,}"
        )

        return final_fee


# Удобная функция для standalone использования
async def get_dynamic_fee_standalone(
    rpc_endpoint: str,
    strategy: str = "aggressive",
    accounts: list[str] | None = None,
) -> int:
    """
    Standalone function to get dynamic priority fee.
    
    For use in buy.py/sell.py without full PriorityFeeManager.
    
    Args:
        rpc_endpoint: Solana RPC endpoint URL.
        strategy: Fee strategy - "conservative", "aggressive", or "sniper".
        accounts: Optional list of account addresses (strings).
    
    Returns:
        int: Priority fee in microlamports.
    """
    from core.client import SolanaClient

    client = SolanaClient(rpc_endpoint)

    try:
        fee_strategy = FeeStrategy(strategy.lower())
    except ValueError:
        fee_strategy = FeeStrategy.AGGRESSIVE
        logger.warning(f"Unknown strategy '{strategy}', using aggressive")

    plugin = DynamicPriorityFee(client, strategy=fee_strategy)

    pubkey_accounts = None
    if accounts:
        pubkey_accounts = [Pubkey.from_string(acc) for acc in accounts]

    fee = await plugin.get_priority_fee(pubkey_accounts)
    return fee or DynamicPriorityFee.FALLBACK_FEE
