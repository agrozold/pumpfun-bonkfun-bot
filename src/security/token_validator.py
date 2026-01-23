"""
Combined token validation using multiple security sources.
Updated: Uses new holder provider system (Birdeye/RPC with fallback).
"""

import asyncio
import time
import logging
from typing import Optional
from dataclasses import dataclass, field

from .rugcheck_client import RugcheckClient, RugcheckResult, RiskLevel
from ..data_providers import get_holder_provider, HolderAnalysis

logger = logging.getLogger(__name__)


@dataclass
class TokenValidation:
    mint: str
    is_safe: bool
    rugcheck: Optional[RugcheckResult] = None
    holder_analysis: Optional[HolderAnalysis] = None
    rejection_reasons: list = field(default_factory=list)
    validation_time_ms: float = 0.0
    data_source: str = ""  # Which provider was used


class TokenValidator:
    def __init__(
        self,
        enable_rugcheck: bool = True,
        enable_holder_check: bool = True,
        min_liquidity_usd: float = 1000,
        max_holder_concentration: float = 70,
        max_rugcheck_score: int = 5000,
    ):
        self.enable_rugcheck = enable_rugcheck
        self.enable_holder_check = enable_holder_check
        self.min_liquidity_usd = min_liquidity_usd
        self.max_holder_concentration = max_holder_concentration
        self.max_rugcheck_score = max_rugcheck_score
        self._rugcheck = RugcheckClient() if enable_rugcheck else None
        self._holder_provider = None  # Lazy init

    async def _get_holder_provider(self):
        """Lazy initialization of holder provider."""
        if self._holder_provider is None and self.enable_holder_check:
            self._holder_provider = await get_holder_provider()
        return self._holder_provider

    async def validate(self, mint: str) -> TokenValidation:
        start = time.perf_counter()
        rejection_reasons = []
        rugcheck_result = None
        holder_result = None
        data_source = ""

        tasks = []
        
        if self._rugcheck:
            tasks.append(("rugcheck", self._rugcheck.check_token(mint)))
        
        if self.enable_holder_check:
            holder_provider = await self._get_holder_provider()
            if holder_provider:
                tasks.append(("holders", holder_provider.get_holders(mint)))

        if tasks:
            results = await asyncio.gather(
                *[t[1] for t in tasks],
                return_exceptions=True
            )
            for (name, _), result in zip(tasks, results):
                if isinstance(result, Exception):
                    logger.error(f"{name} check failed: {result}")
                    continue
                if name == "rugcheck":
                    rugcheck_result = result
                elif name == "holders":
                    holder_result = result
                    if holder_result:
                        data_source = holder_result.source

        # Evaluate rugcheck results
        if rugcheck_result:
            if rugcheck_result.rugged:
                rejection_reasons.append("Token marked as RUGGED")
            if rugcheck_result.risk_level == RiskLevel.DANGER:
                rejection_reasons.append("Rugcheck Danger level")
            if rugcheck_result.score > self.max_rugcheck_score:
                rejection_reasons.append(f"High risk score: {rugcheck_result.score}")
            if rugcheck_result.has_mint_authority:
                rejection_reasons.append("Has mint authority")
            if rugcheck_result.has_freeze_authority:
                rejection_reasons.append("Has freeze authority")
            if rugcheck_result.liquidity_usd < self.min_liquidity_usd:
                rejection_reasons.append(f"Low liquidity: ${rugcheck_result.liquidity_usd:.0f}")
            if rugcheck_result.top_holders_concentration > self.max_holder_concentration:
                rejection_reasons.append(f"Top10 concentration: {rugcheck_result.top_holders_concentration:.1f}%")

        # Evaluate holder analysis results
        if holder_result:
            if holder_result.top_10_concentration > self.max_holder_concentration:
                rejection_reasons.append(f"Holder top10 ({holder_result.source}): {holder_result.top_10_concentration:.1f}%")
            if holder_result.top_holder_pct > 30:
                rejection_reasons.append(f"Top holder ({holder_result.source}): {holder_result.top_holder_pct:.1f}%")

        elapsed_ms = (time.perf_counter() - start) * 1000

        return TokenValidation(
            mint=mint,
            is_safe=len(rejection_reasons) == 0,
            rugcheck=rugcheck_result,
            holder_analysis=holder_result,
            rejection_reasons=rejection_reasons,
            validation_time_ms=elapsed_ms,
            data_source=data_source
        )

    async def close(self):
        if self._rugcheck:
            await self._rugcheck.close()
        if self._holder_provider:
            await self._holder_provider.close()
