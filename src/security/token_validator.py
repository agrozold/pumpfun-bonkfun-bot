"""
Combined token validation using multiple security sources.
Updated: Uses new holder provider system (Birdeye/RPC with fallback).
"""

import asyncio
import time
import logging
from typing import Optional
from dataclasses import dataclass, field

from security.rugcheck_client import RugcheckClient, RugcheckResult, RiskLevel
from data_providers import get_holder_provider, HolderAnalysis

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
        
        self.rugcheck = RugcheckClient() if enable_rugcheck else None
        
    async def validate_token(self, mint: str, timeout: float = 5.0) -> TokenValidation:
        """Validate token using multiple sources."""
        start = time.time()
        validation = TokenValidation(mint=mint, is_safe=True)
        
        tasks = []
        
        # Rugcheck validation
        if self.enable_rugcheck and self.rugcheck:
            tasks.append(self._check_rugcheck(mint, validation))
        
        # Holder analysis
        if self.enable_holder_check:
            tasks.append(self._check_holders(mint, validation))
        
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"[TokenValidator] Timeout for {mint[:8]}...")
        
        validation.validation_time_ms = (time.time() - start) * 1000
        return validation
    
    async def _check_rugcheck(self, mint: str, validation: TokenValidation):
        """Check token with Rugcheck."""
        try:
            result = await self.rugcheck.check_token(mint)
            validation.rugcheck = result
            
            if result and result.score > self.max_rugcheck_score:
                validation.is_safe = False
                validation.rejection_reasons.append(
                    f"Rugcheck score {result.score} > {self.max_rugcheck_score}"
                )
        except Exception as e:
            logger.debug(f"[TokenValidator] Rugcheck error: {e}")
    
    async def _check_holders(self, mint: str, validation: TokenValidation):
        """Check holder distribution."""
        try:
            provider = get_holder_provider()
            analysis = await provider.get_holders(mint, limit=10)
            
            if analysis:
                validation.holder_analysis = analysis
                validation.data_source = analysis.source
                
                if analysis.top_holder_pct > self.max_holder_concentration:
                    validation.is_safe = False
                    validation.rejection_reasons.append(
                        f"Top holder {analysis.top_holder_pct:.1f}% > {self.max_holder_concentration}%"
                    )
        except Exception as e:
            logger.debug(f"[TokenValidator] Holder check error: {e}")
