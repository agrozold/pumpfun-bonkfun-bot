"""
Rugcheck.xyz API client for Solana token safety verification.
"""

import asyncio
import aiohttp
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    GOOD = "Good"
    WARNING = "Warn"
    DANGER = "Danger"
    UNKNOWN = "Unknown"


@dataclass
class RugcheckResult:
    mint: str
    score: int
    risks: list = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    is_safe: bool = False
    rugged: bool = False
    top_holders_concentration: float = 0.0
    has_mint_authority: bool = False
    has_freeze_authority: bool = False
    liquidity_usd: float = 0.0
    details: dict = field(default_factory=dict)


class RugcheckClient:
    BASE_URL = "https://api.rugcheck.xyz/v1"
    MAX_SAFE_SCORE = 1000
    MIN_LIQUIDITY_USD = 1000

    def __init__(self, cache_ttl=300, max_retries=2, timeout=10.0):
        self._session = None
        self._cache = {}
        self._cache_ttl = cache_ttl
        self._max_retries = max_retries
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(5)

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"Accept": "application/json"}
            )
        return self._session

    async def check_token(self, mint):
        if mint in self._cache:
            result, ts = self._cache[mint]
            if time.time() - ts < self._cache_ttl:
                return result

        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                try:
                    session = await self._get_session()
                    url = f"{self.BASE_URL}/tokens/{mint}/report"

                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            result = self._parse_response(mint, data)
                            self._cache[mint] = (result, time.time())
                            return result
                        elif resp.status == 404:
                            return None
                        elif resp.status == 429:
                            await asyncio.sleep(2 ** attempt)
                            continue

                except asyncio.TimeoutError:
                    logger.warning(f"Rugcheck timeout attempt {attempt + 1}")
                except Exception as e:
                    logger.error(f"Rugcheck error: {e}")

                if attempt < self._max_retries:
                    await asyncio.sleep(1)

        return None

    def _parse_response(self, mint, data):
        risks = data.get("risks", [])
        score = data.get("score", 0)

        result_str = data.get("result", "Unknown")
        try:
            risk_level = RiskLevel(result_str)
        except ValueError:
            risk_level = RiskLevel.UNKNOWN

        top_holders = data.get("topHolders", [])
        concentration = sum(h.get("pct", 0) for h in top_holders[:10])

        has_mint = data.get("mintAuthority") is not None
        has_freeze = data.get("freezeAuthority") is not None
        liquidity = data.get("totalMarketLiquidity", 0)

        danger_count = len([r for r in risks if r.get("level") == "danger"])

        is_safe = (
            risk_level == RiskLevel.GOOD
            and score < self.MAX_SAFE_SCORE
            and danger_count == 0
            and not data.get("rugged", False)
            and liquidity >= self.MIN_LIQUIDITY_USD
        )

        return RugcheckResult(
            mint=mint,
            score=score,
            risks=risks,
            risk_level=risk_level,
            is_safe=is_safe,
            rugged=data.get("rugged", False),
            top_holders_concentration=concentration,
            has_mint_authority=has_mint,
            has_freeze_authority=has_freeze,
            liquidity_usd=liquidity,
            details=data,
        )

    async def is_token_safe(self, mint):
        result = await self.check_token(mint)
        return result.is_safe if result else False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
