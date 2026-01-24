"""
Dynamic Decimals - автоматическое определение decimals токена из on-chain данных.
Устраняет хардкод decimals=6 и поддерживает токены с разными decimals.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

logger = logging.getLogger(__name__)

# Кеш decimals для токенов
_decimals_cache: Dict[str, int] = {}

# Известные decimals для популярных токенов
KNOWN_DECIMALS: Dict[str, int] = {
    'So11111111111111111111111111111111111111112': 9,   # SOL (wrapped)
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 6,  # USDC
    'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB': 6,  # USDT
}

# Дефолтные decimals по платформе
PLATFORM_DEFAULT_DECIMALS: Dict[str, int] = {
    'pump_fun': 6,
    'lets_bonk': 6,
    'bags': 9,
    'raydium': 6,
    'meteora': 6,
}


@dataclass
class TokenInfo:
    """Информация о токене"""
    mint: str
    decimals: int
    supply: Optional[int] = None
    name: Optional[str] = None
    symbol: Optional[str] = None

    @property
    def decimal_factor(self) -> int:
        """Множитель для конвертации (10^decimals)"""
        return 10 ** self.decimals

    def to_ui_amount(self, raw_amount: int) -> Decimal:
        """Конвертировать raw amount в UI amount"""
        return Decimal(raw_amount) / Decimal(self.decimal_factor)

    def to_raw_amount(self, ui_amount: Decimal) -> int:
        """Конвертировать UI amount в raw amount"""
        return int((ui_amount * Decimal(self.decimal_factor)).quantize(Decimal('1'), rounding=ROUND_DOWN))


class DecimalsResolver:
    """
    Resolver для определения decimals токена.

    Использование:
        resolver = DecimalsResolver(rpc_client)
        decimals = await resolver.get_decimals(mint)
        token_info = await resolver.get_token_info(mint)
    """

    def __init__(self, rpc_client=None):
        self.rpc_client = rpc_client
        self._cache = _decimals_cache

    async def get_decimals(self, mint: str, platform: str = None) -> int:
        """
        Получить decimals для токена.

        Приоритет:
        1. Кеш
        2. Известные токены
        3. On-chain запрос
        4. Дефолт по платформе
        """
        # 1. Проверяем кеш
        if mint in self._cache:
            return self._cache[mint]

        # 2. Проверяем известные токены
        if mint in KNOWN_DECIMALS:
            decimals = KNOWN_DECIMALS[mint]
            self._cache[mint] = decimals
            return decimals

        # 3. Запрашиваем on-chain
        if self.rpc_client:
            try:
                decimals = await self._fetch_decimals_onchain(mint)
                if decimals is not None:
                    self._cache[mint] = decimals
                    logger.debug(f"Fetched decimals for {mint[:16]}...: {decimals}")
                    return decimals
            except Exception as e:
                logger.warning(f"Failed to fetch decimals for {mint}: {e}")

        # 4. Возвращаем дефолт по платформе
        default = PLATFORM_DEFAULT_DECIMALS.get(platform, 6)
        logger.debug(f"Using default decimals for {mint[:16]}...: {default}")
        return default

    async def _fetch_decimals_onchain(self, mint: str) -> Optional[int]:
        """Получить decimals из on-chain данных"""
        try:
            from solders.pubkey import Pubkey

            pubkey = Pubkey.from_string(mint)

            # Используем getAccountInfo для получения данных Mint аккаунта
            response = await self.rpc_client.get_account_info(pubkey)

            if response.value is None:
                return None

            data = response.value.data

            # SPL Token Mint layout: decimals находится на позиции 44 (1 byte)
            # https://github.com/solana-labs/solana-program-library/blob/master/token/program/src/state.rs
            if len(data) >= 45:
                decimals = data[44]
                return decimals

            return None

        except Exception as e:
            logger.error(f"Error fetching decimals on-chain: {e}")
            return None

    async def get_token_info(self, mint: str, platform: str = None) -> TokenInfo:
        """Получить полную информацию о токене"""
        decimals = await self.get_decimals(mint, platform)

        return TokenInfo(
            mint=mint,
            decimals=decimals
        )

    def clear_cache(self) -> None:
        """Очистить кеш"""
        self._cache.clear()

    def set_decimals(self, mint: str, decimals: int) -> None:
        """Установить decimals вручную (для тестов или известных токенов)"""
        self._cache[mint] = decimals


# Глобальный resolver
_resolver: Optional[DecimalsResolver] = None


def get_decimals_resolver(rpc_client=None) -> DecimalsResolver:
    """Получить глобальный resolver"""
    global _resolver
    if _resolver is None or (rpc_client and _resolver.rpc_client is None):
        _resolver = DecimalsResolver(rpc_client)
    return _resolver


async def get_token_decimals(mint: str, platform: str = None, rpc_client=None) -> int:
    """Удобная функция для получения decimals"""
    resolver = get_decimals_resolver(rpc_client)
    return await resolver.get_decimals(mint, platform)


def convert_to_ui_amount(raw_amount: int, decimals: int) -> Decimal:
    """Конвертировать raw amount в UI amount"""
    return Decimal(raw_amount) / Decimal(10 ** decimals)


def convert_to_raw_amount(ui_amount: Decimal, decimals: int) -> int:
    """Конвертировать UI amount в raw amount"""
    return int((ui_amount * Decimal(10 ** decimals)).quantize(Decimal('1'), rounding=ROUND_DOWN))


def format_token_amount(amount: Decimal, decimals: int = 6, max_decimals: int = 4) -> str:
    """Форматировать количество токенов для отображения"""
    if amount == 0:
        return "0"

    # Определяем количество значащих цифр после запятой
    display_decimals = min(decimals, max_decimals)

    # Форматируем
    formatted = f"{amount:.{display_decimals}f}".rstrip('0').rstrip('.')

    return formatted
