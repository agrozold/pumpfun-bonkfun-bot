"""Dev Reputation Checker - проверка истории создателя токена.

Использует Helius API для анализа истории кошелька дева:
- Сколько токенов создал
- Как давно активен
- Паттерны скамера (много токенов, все мёртвые)
"""

import os
from datetime import datetime, timezone

import aiohttp

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Pump.fun program ID
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


class DevReputationChecker:
    """Проверяет репутацию создателя токена."""

    def __init__(
        self,
        helius_api_key: str | None = None,
        max_tokens_created: int = 50,
        min_account_age_days: int = 1,
        enabled: bool = True,
    ):
        """Инициализация чекера.

        Args:
            helius_api_key: API ключ Helius
            max_tokens_created: Максимум токенов от одного дева (больше = скамер)
            min_account_age_days: Минимальный возраст аккаунта в днях
            enabled: Включен ли чекер
        """
        self.api_key = helius_api_key or os.getenv("HELIUS_API_KEY")
        self.max_tokens_created = max_tokens_created
        self.min_account_age_days = min_account_age_days
        self.enabled = enabled
        self._cache: dict[str, dict] = {}  # Кэш результатов

        if not self.api_key:
            logger.warning("HELIUS_API_KEY not set, dev reputation check disabled")
            self.enabled = False

    async def check_dev(self, creator_address: str) -> dict:
        """Проверить репутацию создателя.

        Args:
            creator_address: Адрес кошелька создателя

        Returns:
            dict с полями:
                - is_safe: bool - безопасно ли покупать
                - reason: str - причина решения
                - tokens_created: int - сколько токенов создал
                - account_age_days: int - возраст аккаунта
                - risk_score: int - оценка риска 0-100
        """
        if not self.enabled:
            return {"is_safe": True, "reason": "Dev check disabled", "risk_score": 0}

        # Проверяем кэш
        if creator_address in self._cache:
            logger.debug(f"Using cached result for {creator_address[:8]}...")
            return self._cache[creator_address]

        try:
            result = await self._analyze_dev(creator_address)
            self._cache[creator_address] = result
            return result
        except Exception as e:
            logger.exception(f"Failed to check dev {creator_address[:8]}: {e}")
            # При ошибке разрешаем покупку, но с предупреждением
            return {
                "is_safe": True,
                "reason": f"Check failed: {e}",
                "risk_score": 50,
            }

    async def _analyze_dev(self, creator_address: str) -> dict:
        """Анализ истории дева через Helius API."""
        url = f"https://api.helius.xyz/v0/addresses/{creator_address}/transactions"
        params = {"api-key": self.api_key, "limit": 100}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise ValueError(f"Helius API error: {resp.status}")
                transactions = await resp.json()

        if not transactions:
            return {
                "is_safe": True,
                "reason": "New wallet, no history",
                "tokens_created": 0,
                "risk_score": 30,
            }

        # Считаем создания токенов на pump.fun
        tokens_created = 0
        oldest_tx_time = None
        newest_tx_time = None

        for tx in transactions:
            # Проверяем время транзакции
            tx_time = tx.get("timestamp")
            if tx_time:
                if oldest_tx_time is None or tx_time < oldest_tx_time:
                    oldest_tx_time = tx_time
                if newest_tx_time is None or tx_time > newest_tx_time:
                    newest_tx_time = tx_time

            # Проверяем инструкции на создание токенов
            instructions = tx.get("instructions", [])
            for ix in instructions:
                program_id = ix.get("programId", "")
                if program_id == PUMP_PROGRAM:
                    # Проверяем тип инструкции (create/create_v2)
                    ix_type = ix.get("type", "").lower()
                    if "create" in ix_type:
                        tokens_created += 1

        # Вычисляем возраст аккаунта
        account_age_days = 0
        if oldest_tx_time:
            oldest_dt = datetime.fromtimestamp(oldest_tx_time, tz=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            account_age_days = (now - oldest_dt).days

        # Вычисляем risk score
        risk_score = self._calculate_risk_score(tokens_created, account_age_days)

        # Определяем безопасность
        is_safe = True
        reason = "Dev looks OK"

        if tokens_created > self.max_tokens_created:
            is_safe = False
            reason = f"Serial token creator: {tokens_created} tokens"
        elif account_age_days < self.min_account_age_days:
            is_safe = False
            reason = f"New account: {account_age_days} days old"
        elif risk_score > 70:
            is_safe = False
            reason = f"High risk score: {risk_score}"

        result = {
            "is_safe": is_safe,
            "reason": reason,
            "tokens_created": tokens_created,
            "account_age_days": account_age_days,
            "risk_score": risk_score,
        }

        logger.info(
            f"Dev {creator_address[:8]}... - tokens: {tokens_created}, "
            f"age: {account_age_days}d, risk: {risk_score}, safe: {is_safe}"
        )

        return result

    def _calculate_risk_score(self, tokens_created: int, account_age_days: int) -> int:
        """Вычислить оценку риска 0-100."""
        score = 0

        # Много токенов = высокий риск
        if tokens_created > 100:
            score += 50
        elif tokens_created > 50:
            score += 40
        elif tokens_created > 20:
            score += 30
        elif tokens_created > 10:
            score += 20
        elif tokens_created > 5:
            score += 10

        # Новый аккаунт = риск
        if account_age_days < 1:
            score += 30
        elif account_age_days < 7:
            score += 20
        elif account_age_days < 30:
            score += 10

        # Много токенов за короткое время = очень плохо
        if account_age_days > 0:
            tokens_per_day = tokens_created / account_age_days
            if tokens_per_day > 10:
                score += 30
            elif tokens_per_day > 5:
                score += 20
            elif tokens_per_day > 1:
                score += 10

        return min(score, 100)

    def clear_cache(self):
        """Очистить кэш."""
        self._cache.clear()
