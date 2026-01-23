"""
Holder Distribution Analysis Module
Analyzes token holder concentration using ONLY standard RPC.
NO external APIs required (Birdeye optional).
"""

import asyncio
import os
import time
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class HolderAnalyzer:
    """Analyze token holder distribution using standard Solana RPC."""

    def __init__(
        self,
        rpc_endpoint: str | None = None,
        birdeye_api_key: str | None = None,
        cache_ttl: int = 300,
    ):
        self.rpc_endpoint = rpc_endpoint or os.getenv("SOLANA_NODE_RPC_ENDPOINT")
        self.birdeye_api_key = birdeye_api_key or os.getenv("BIRDEYE_API_KEY")
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[Any, float]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    def _get_cached(self, key: str) -> Any | None:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.time() - timestamp < self.cache_ttl:
                return value
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        self._cache[key] = (value, time.time())

    async def get_top_holders_rpc(self, mint: str) -> list[dict[str, Any]]:
        """Get top 20 token holders using standard RPC."""
        cache_key = f"holders_rpc:{mint}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        if not self.rpc_endpoint:
            logger.error("[HOLDER] No RPC endpoint configured")
            return []

        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint],
        }

        try:
            async with session.post(
                self.rpc_endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "error" in data:
                        logger.warning(f"[HOLDER] RPC error: {data['error']}")
                        return []
                    accounts = data.get("result", {}).get("value", [])
                    holders = []
                    for acc in accounts:
                        amount_raw = int(acc.get("amount", 0))
                        decimals = acc.get("decimals", 0)
                        ui_amount = acc.get("uiAmount") or (amount_raw / (10 ** decimals) if decimals else amount_raw)
                        holders.append({
                            "address": acc.get("address"),
                            "amount": amount_raw,
                            "decimals": decimals,
                            "uiAmount": ui_amount,
                        })
                    self._set_cached(cache_key, holders)
                    return holders
                else:
                    logger.warning(f"[HOLDER] RPC HTTP error: {resp.status}")
                    return []
        except asyncio.TimeoutError:
            logger.warning("[HOLDER] RPC request timeout")
            return []
        except Exception as e:
            logger.error(f"[HOLDER] RPC request failed: {e}")
            return []

    async def get_token_supply(self, mint: str) -> tuple[float, int] | None:
        """Get token total supply and decimals."""
        cache_key = f"supply:{mint}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        if not self.rpc_endpoint:
            return None

        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenSupply",
            "params": [mint],
        }

        try:
            async with session.post(
                self.rpc_endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "error" in data:
                        return None
                    result = data.get("result", {}).get("value", {})
                    ui_amount = result.get("uiAmount", 0)
                    decimals = result.get("decimals", 0)
                    self._set_cached(cache_key, (ui_amount, decimals))
                    return (ui_amount, decimals)
                return None
        except Exception as e:
            logger.error(f"[HOLDER] Get supply failed: {e}")
            return None

    async def analyze_concentration(self, mint: str, total_supply: float | None = None) -> dict[str, Any]:
        """Analyze token holder concentration using RPC."""
        holders = await self.get_top_holders_rpc(mint)
        
        if not holders:
            return {
                "top_10_pct": 0,
                "top_20_pct": 0,
                "largest_holder_pct": 0,
                "total_holders_checked": 0,
                "risk_level": "unknown",
                "data_source": "none",
                "details": [],
                "error": "Could not fetch holders from RPC",
            }

        if total_supply is None:
            supply_data = await self.get_token_supply(mint)
            if supply_data:
                total_supply = supply_data[0]
                
        if not total_supply or total_supply <= 0:
            total_supply = sum(h.get("uiAmount", 0) or 0 for h in holders)

        if total_supply <= 0:
            return {
                "top_10_pct": 0,
                "top_20_pct": 0,
                "largest_holder_pct": 0,
                "total_holders_checked": len(holders),
                "risk_level": "unknown",
                "data_source": "rpc",
                "details": holders[:5],
                "error": "Could not determine total supply",
            }

        holder_amounts = sorted([h.get("uiAmount", 0) or 0 for h in holders], reverse=True)
        
        top_10_balance = sum(holder_amounts[:10])
        top_20_balance = sum(holder_amounts[:20])
        largest = holder_amounts[0] if holder_amounts else 0

        top_10_pct = (top_10_balance / total_supply * 100) if total_supply > 0 else 0
        top_20_pct = (top_20_balance / total_supply * 100) if total_supply > 0 else 0
        largest_pct = (largest / total_supply * 100) if total_supply > 0 else 0

        if largest_pct > 50 or top_10_pct > 90:
            risk_level = "critical"
        elif largest_pct > 30 or top_10_pct > 80:
            risk_level = "high"
        elif top_10_pct > 60:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "top_10_pct": round(top_10_pct, 2),
            "top_20_pct": round(top_20_pct, 2),
            "largest_holder_pct": round(largest_pct, 2),
            "total_holders_checked": len(holders),
            "risk_level": risk_level,
            "data_source": "rpc",
            "total_supply": total_supply,
            "details": holders[:5],
        }

    async def is_safe_distribution(self, mint: str, max_top10_concentration: float = 80.0, max_single_holder: float = 40.0) -> tuple[bool, dict]:
        """Check if token has safe holder distribution."""
        analysis = await self.analyze_concentration(mint)
        
        if analysis.get("error") and analysis.get("risk_level") == "unknown":
            return False, analysis
            
        is_safe = (
            analysis["top_10_pct"] <= max_top10_concentration
            and analysis["largest_holder_pct"] <= max_single_holder
        )
        
        return is_safe, analysis

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


_holder_analyzer: HolderAnalyzer | None = None


async def get_holder_analyzer() -> HolderAnalyzer:
    global _holder_analyzer
    if _holder_analyzer is None:
        _holder_analyzer = HolderAnalyzer()
    return _holder_analyzer
