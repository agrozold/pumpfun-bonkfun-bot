"""
Birdeye API provider - TEMPORARY until custom indexer is ready.
Provides richer data than RPC but requires API key.
"""

import os
import aiohttp
import asyncio
import time
import logging
from typing import Optional

from .holder_provider import (
    HolderProvider, HolderAnalysis, TokenSecurityInfo,
    ProviderType, calculate_risk_level
)

logger = logging.getLogger(__name__)


class BirdeyeProvider(HolderProvider):
    """
    Birdeye API provider for holder and security analysis.
    TEMPORARY - will be replaced by custom indexer.
    """
    
    BASE_URL = "https://public-api.birdeye.so"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("BIRDEYE_API_KEY")
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_ttl = 120
    
    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.BIRDEYE
    
    @property
    def is_available(self) -> bool:
        return bool(self.api_key)
    
    def _get_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-chain": "solana",
            "X-API-KEY": self.api_key or "",
        }
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session
    
    async def get_holders(self, mint: str, limit: int = 20) -> Optional[HolderAnalysis]:
        """Get holder distribution from Birdeye API."""
        if not self.api_key:
            return None
        
        cache_key = f"holders:{mint}"
        if cache_key in self._cache:
            result, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return result
        
        try:
            session = await self._get_session()
            url = f"{self.BASE_URL}/defi/v3/token/holder"
            params = {"address": mint, "offset": 0, "limit": limit}
            
            async with session.get(url, params=params, headers=self._get_headers()) as resp:
                if resp.status == 401:
                    logger.warning("Birdeye unauthorized")
                    return None
                if resp.status == 429:
                    logger.warning("Birdeye rate limited")
                    return None
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                
                if not data.get("success"):
                    return None
                
                holder_data = data.get("data", {})
                items = holder_data.get("items", [])
                total = holder_data.get("total", len(items))
                
                if not items:
                    return None
                
                total_amount = sum(float(h.get("uiAmount", 0)) for h in items)
                
                holders = []
                for h in items:
                    amount = float(h.get("uiAmount", 0))
                    pct = (amount / total_amount * 100) if total_amount > 0 else 0
                    holders.append({
                        "owner": h.get("owner", ""),
                        "amount": amount,
                        "percentage": pct
                    })
                
                holders.sort(key=lambda x: x["amount"], reverse=True)
                
                top_10_pct = sum(h["percentage"] for h in holders[:10])
                top_holder_pct = holders[0]["percentage"] if holders else 0
                
                result = HolderAnalysis(
                    mint=mint,
                    total_holders=total,
                    top_10_concentration=top_10_pct,
                    top_holder_pct=top_holder_pct,
                    risk_level=calculate_risk_level(top_10_pct, top_holder_pct),
                    is_concentrated=top_10_pct > 50,
                    holders=holders,
                    source="birdeye"
                )
                
                self._cache[cache_key] = (result, time.time())
                return result
                
        except asyncio.TimeoutError:
            logger.warning(f"Birdeye timeout for {mint[:8]}...")
            return None
        except Exception as e:
            logger.error(f"Birdeye holder error: {e}")
            return None
    
    async def get_security(self, mint: str) -> Optional[TokenSecurityInfo]:
        """Get token security from Birdeye API."""
        if not self.api_key:
            return None
        
        cache_key = f"security:{mint}"
        if cache_key in self._cache:
            result, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return result
        
        try:
            session = await self._get_session()
            url = f"{self.BASE_URL}/defi/token_security"
            params = {"address": mint}
            
            async with session.get(url, params=params, headers=self._get_headers()) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                
                if not data.get("success"):
                    return None
                
                sec = data.get("data", {})
                warnings = []
                risk_score = 0
                
                if sec.get("isHoneypot"):
                    warnings.append("HONEYPOT")
                    risk_score += 50
                if sec.get("isFakeToken"):
                    warnings.append("FAKE TOKEN")
                    risk_score += 50
                if sec.get("freezable"):
                    warnings.append("Freezable")
                    risk_score += 20
                if not sec.get("ownershipRenounced", True):
                    warnings.append("Ownership not renounced")
                    risk_score += 15
                if sec.get("mintable"):
                    warnings.append("Mintable")
                    risk_score += 10
                
                top_10 = sec.get("top10HolderPercent", 0)
                if top_10 > 80:
                    warnings.append(f"High concentration: {top_10:.0f}%")
                    risk_score += 15
                
                fee = sec.get("transferFee", 0)
                if fee > 0:
                    warnings.append(f"Transfer fee: {fee}%")
                    risk_score += 10
                
                result = TokenSecurityInfo(
                    mint=mint,
                    is_safe=risk_score < 30 and not sec.get("isHoneypot"),
                    risk_score=min(risk_score, 100),
                    warnings=warnings,
                    details=sec,
                    source="birdeye"
                )
                
                self._cache[cache_key] = (result, time.time())
                return result
                
        except Exception as e:
            logger.error(f"Birdeye security error: {e}")
            return None
    
    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
