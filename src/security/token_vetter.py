"""
Token Vetter - Проверка безопасности токена перед покупкой.

Проверки:
1. Freeze Authority - должен быть отозван (КРИТИЧНО!)
2. Mint Authority - желательно отозван
3. Rugcheck.xyz API - комплексная проверка

Для токенов на bonding curve (pump.fun) freeze/mint контролируются
программой платформы, поэтому проверки опциональны для свежих токенов.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import aiohttp
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

MINT_LAYOUT_SIZE = 82


class VetResult(Enum):
    """Результат проверки токена."""
    SAFE = "safe"
    RISKY = "risky"
    DANGEROUS = "dangerous"
    SKIP = "skip"  # Skip check (fresh token)
    ERROR = "error"


@dataclass
class TokenVetReport:
    """Отчет о проверке токена."""
    mint: str
    symbol: str
    result: VetResult
    
    mint_authority_revoked: bool = False
    freeze_authority_revoked: bool = False
    
    rugcheck_score: int = 0
    rugcheck_risks: list = field(default_factory=list)
    
    reason: str = ""
    check_time_ms: float = 0


class TokenVetter:
    """Быстрая проверка безопасности токенов."""
    
    def __init__(
        self,
        rpc_endpoint: str,
        require_freeze_revoked: bool = True,
        require_mint_revoked: bool = False,
        min_rugcheck_score: int = 30,
        check_timeout: float = 2.0,
        skip_for_bonding_curve: bool = True,
    ):
        self.rpc_endpoint = rpc_endpoint
        self.require_freeze_revoked = require_freeze_revoked
        self.require_mint_revoked = require_mint_revoked
        self.min_rugcheck_score = min_rugcheck_score
        self.check_timeout = check_timeout
        self.skip_for_bonding_curve = skip_for_bonding_curve
        
        self._rpc: AsyncClient | None = None
        self._http: aiohttp.ClientSession | None = None
        self._cache: dict[str, TokenVetReport] = {}
        
        logger.info(
            f"TokenVetter: freeze_required={require_freeze_revoked}, "
            f"mint_required={require_mint_revoked}, timeout={check_timeout}s"
        )
    
    async def _get_rpc(self) -> AsyncClient:
        if not self._rpc:
            self._rpc = AsyncClient(self.rpc_endpoint)
        return self._rpc
    
    async def _get_http(self) -> aiohttp.ClientSession:
        if not self._http or self._http.closed:
            timeout = aiohttp.ClientTimeout(total=self.check_timeout)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http
    
    async def close(self):
        if self._rpc:
            await self._rpc.close()
        if self._http and not self._http.closed:
            await self._http.close()
    
    async def vet_token(
        self,
        mint_address: str,
        symbol: str = "UNKNOWN",
        is_bonding_curve: bool = False,
    ) -> TokenVetReport:
        """
        Проверить токен перед покупкой.
        
        Args:
            mint_address: Адрес mint
            symbol: Символ для логов
            is_bonding_curve: True если токен на bonding curve
        
        Returns:
            TokenVetReport
        """
        import time
        start = time.time()
        
        # Check cache
        if mint_address in self._cache:
            return self._cache[mint_address]
        
        report = TokenVetReport(
            mint=mint_address,
            symbol=symbol,
            result=VetResult.SAFE,
        )
        
        # Skip detailed checks for bonding curve tokens
        if is_bonding_curve and self.skip_for_bonding_curve:
            report.result = VetResult.SKIP
            report.reason = "Bonding curve token - platform controls authorities"
            logger.debug(f"[VET] {symbol}: Skipped (bonding curve)")
            self._cache[mint_address] = report
            return report
        
        try:
            # Check authorities via RPC
            auth_result = await self._check_authorities(mint_address)
            report.mint_authority_revoked = auth_result.get("mint_revoked", False)
            report.freeze_authority_revoked = auth_result.get("freeze_revoked", False)
            
            # Determine result
            reasons = []
            
            if not report.freeze_authority_revoked and self.require_freeze_revoked:
                reasons.append("Freeze authority ACTIVE - can freeze your tokens!")
                report.result = VetResult.DANGEROUS
            
            if not report.mint_authority_revoked and self.require_mint_revoked:
                reasons.append("Mint authority ACTIVE - can inflate supply")
                if report.result != VetResult.DANGEROUS:
                    report.result = VetResult.RISKY
            
            if reasons:
                report.reason = "; ".join(reasons)
            else:
                report.reason = "Authorities OK"
            
            # Optional: Quick rugcheck (async, don't wait too long)
            try:
                rugcheck = await asyncio.wait_for(
                    self._check_rugcheck(mint_address),
                    timeout=1.0
                )
                if rugcheck:
                    report.rugcheck_score = rugcheck.get("score", 0)
                    report.rugcheck_risks = rugcheck.get("risks", [])
                    
                    if report.rugcheck_score < self.min_rugcheck_score:
                        report.reason += f"; Low rugcheck score: {report.rugcheck_score}"
                        if report.result == VetResult.SAFE:
                            report.result = VetResult.RISKY
            except asyncio.TimeoutError:
                logger.debug(f"[VET] Rugcheck timeout for {symbol}")
            
        except Exception as e:
            logger.warning(f"[VET] Check failed for {symbol}: {e}")
            report.result = VetResult.ERROR
            report.reason = f"Check error: {e}"
        
        report.check_time_ms = (time.time() - start) * 1000
        
        # Log result
        if report.result == VetResult.DANGEROUS:
            logger.warning(f"[VET] ⛔ DANGEROUS: {symbol} - {report.reason}")
        elif report.result == VetResult.RISKY:
            logger.warning(f"[VET] ⚠️ RISKY: {symbol} - {report.reason}")
        else:
            logger.info(f"[VET] ✅ {report.result.value}: {symbol} ({report.check_time_ms:.0f}ms)")
        
        self._cache[mint_address] = report
        return report
    
    async def _check_authorities(self, mint_address: str) -> dict:
        """Check mint and freeze authorities via RPC."""
        try:
            client = await self._get_rpc()
            mint_pubkey = Pubkey.from_string(mint_address)
            
            response = await client.get_account_info(mint_pubkey)
            if not response.value:
                return {"error": "Mint not found"}
            
            data = response.value.data
            if len(data) < MINT_LAYOUT_SIZE:
                return {"error": "Invalid mint data"}
            
            # Parse mint layout
            # Offset 0: mint_authority_option (4 bytes)
            # Offset 46: freeze_authority_option (4 bytes)
            mint_auth_option = int.from_bytes(data[0:4], 'little')
            freeze_auth_option = int.from_bytes(data[46:50], 'little')
            
            return {
                "mint_revoked": mint_auth_option == 0,
                "freeze_revoked": freeze_auth_option == 0,
            }
        except Exception as e:
            logger.debug(f"Authority check error: {e}")
            return {"error": str(e)}
    
    async def _check_rugcheck(self, mint_address: str) -> dict | None:
        """Check via rugcheck.xyz API."""
        try:
            session = await self._get_http()
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report"
            
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                score = data.get("score", data.get("riskScore", 50))
                
                risks = []
                for risk in data.get("risks", []):
                    if isinstance(risk, dict):
                        risks.append(risk.get("name", str(risk)))
                    else:
                        risks.append(str(risk))
                
                return {"score": score, "risks": risks}
        except Exception as e:
            logger.debug(f"Rugcheck error: {e}")
            return None
    
    def should_buy(self, report: TokenVetReport) -> bool:
        """Определить можно ли покупать."""
        if report.result == VetResult.DANGEROUS:
            return False
        # SKIP, SAFE, RISKY - можно покупать
        # ERROR - на усмотрение (пропускаем для безопасности)
        if report.result == VetResult.ERROR:
            return False
        return True
    
    def clear_cache(self):
        self._cache.clear()
