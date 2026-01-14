"""Dev Reputation Checker - проверка истории создателя токена.

Использует Helius API для анализа истории кошелька дева:
- Сколько токенов создал
- Как давно активен
- Паттерны скамера (много токенов, все мёртвые)
"""

import os
from datetime import datetime, timezone

import aiohttp

from utils.logger import get_logger

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
        """Анализ истории дева через Helius API с пагинацией."""
        base_url = f"https://api.helius.xyz/v0/addresses/{creator_address}/transactions"
        
        tokens_created = 0
        oldest_tx_time = None
        newest_tx_time = None
        before_signature = None
        max_pages = 10  # До 10000 транзакций (10 * 1000)
        
        async with aiohttp.ClientSession() as session:
            for page in range(max_pages):
                params = {"api-key": self.api_key, "limit": 1000}
                if before_signature:
                    params["before"] = before_signature
                
                async with session.get(base_url, params=params) as resp:
                    if resp.status != 200:
                        raise ValueError(f"Helius API error: {resp.status}")
                    transactions = await resp.json()
                
                if not transactions:
                    break  # Нет больше транзакций
                
                # Обрабатываем транзакции
                for tx in transactions:
                    tx_time = tx.get("timestamp")
                    if tx_time:
                        if oldest_tx_time is None or tx_time < oldest_tx_time:
                            oldest_tx_time = tx_time
                        if newest_tx_time is None or tx_time > newest_tx_time:
                            newest_tx_time = tx_time

                    instructions = tx.get("instructions", [])
                    for ix in instructions:
                        program_id = ix.get("programId", "")
                        if program_id == PUMP_PROGRAM:
                            ix_type = ix.get("type", "").lower()
                            if "create" in ix_type:
                                tokens_created += 1
                
                # Если уже нашли много токенов - сразу скипаем, не тратим время
                if tokens_created > self.max_tokens_created:
                    logger.info(f"Dev {creator_address[:8]}... already has {tokens_created} tokens, skipping further pages")
                    break
                
                # Если меньше 1000 транзакций - это последняя страница
                if len(transactions) < 1000:
                    break
                
                # Берём signature последней транзакции для пагинации
                before_signature = transactions[-1].get("signature")
                if not before_signature:
                    break

        if tokens_created == 0 and oldest_tx_time is None:
            return {
                "is_safe": True,
                "reason": "New wallet, no history",
                "tokens_created": 0,
                "risk_score": 30,
            }

        # Вычисляем возраст аккаунта
        account_age_days = 0
        if oldest_tx_time:
            oldest_dt = datetime.fromtimestamp(oldest_tx_time, tz=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            account_age_days = (now - oldest_dt).days

        # Вычисляем risk score
        risk_score = self._calculate_risk_score(tokens_created, account_age_days)

        # Определяем безопасность
        # ВАЖНО: Новые аккаунты (0 дней, 0 токенов) = ХОРОШО, возможно гем!
        # Много токенов создано = ПЛОХО, серийный скамер
        is_safe = True
        reason = "Dev looks OK"

        if tokens_created > self.max_tokens_created:
            is_safe = False
            reason = f"Serial token creator: {tokens_created} tokens"
        elif risk_score > 80:
            # Только очень высокий риск (много токенов за короткое время)
            is_safe = False
            reason = f"High risk score: {risk_score}"
        elif tokens_created == 0 and account_age_days < 1:
            # Новый аккаунт с первым токеном = потенциальный гем!
            is_safe = True
            reason = "Fresh dev, first token - potential gem!"

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
        """Вычислить оценку риска 0-100.
        
        Новый аккаунт с первым токеном = низкий риск (потенциальный гем).
        Много токенов = высокий риск (серийный скамер).
        """
        score = 0

        # Много токенов = высокий риск (серийный скамер)
        if tokens_created > 100:
            score += 60
        elif tokens_created > 50:
            score += 50
        elif tokens_created > 20:
            score += 40
        elif tokens_created > 10:
            score += 30
        elif tokens_created > 5:
            score += 20
        elif tokens_created > 2:
            score += 10

        # Новый аккаунт с первым токеном = НЕ штрафуем (потенциальный гем)
        # Штрафуем только если много токенов за короткое время
        if account_age_days > 0 and tokens_created > 0:
            tokens_per_day = tokens_created / account_age_days
            if tokens_per_day > 10:
                score += 40  # Очень много токенов в день = скамер
            elif tokens_per_day > 5:
                score += 30
            elif tokens_per_day > 2:
                score += 20

        return min(score, 100)

    def clear_cache(self):
        """Очистить кэш."""
        self._cache.clear()
