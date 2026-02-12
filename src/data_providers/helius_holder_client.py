"""
Helius API for Solana token holder analysis.
"""

import os
import aiohttp
import asyncio
import time
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HolderAnalysis:
    mint: str
    total_holders: int
    top_10_concentration: float
    top_holder_pct: float
    risk_level: str
    is_concentrated: bool
    holders: list


class HeliusHolderClient:
    BASE_URL = os.environ.get("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com")

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("HELIUS_API_KEY")
        if not self.api_key:
            logger.warning("HELIUS_API_KEY not set - holder analysis disabled")
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_ttl = 120

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def get_token_holders(self, mint: str, limit: int = 20) -> Optional[HolderAnalysis]:
        if not self.api_key:
            return None

        cache_key = f"{mint}:{limit}"
        if cache_key in self._cache:
            result, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return result

        try:
            session = await self._get_session()
            url = f"{self.BASE_URL}/?api-key={self.api_key}"

            payload = {
                "jsonrpc": "2.0",
                "id": "helius-holders",
                "method": "getTokenAccounts",
                "params": {
                    "mint": mint,
                    "limit": limit,
                    "options": {"showZeroBalance": False}
                }
            }

            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"Helius API error: {resp.status}")
                    return None

                data = await resp.json()

                if "error" in data:
                    logger.error(f"Helius RPC error: {data['error']}")
                    return None

                result = data.get("result", {})
                accounts = result.get("token_accounts", [])

                if not accounts:
                    return None

                total_amount = sum(int(acc.get("amount", 0)) for acc in accounts)

                holders = []
                for acc in accounts:
                    amount = int(acc.get("amount", 0))
                    pct = (amount / total_amount * 100) if total_amount > 0 else 0
                    holders.append({
                        "owner": acc.get("owner"),
                        "amount": amount,
                        "percentage": pct
                    })

                holders.sort(key=lambda x: x["amount"], reverse=True)

                top_10_pct = sum(h["percentage"] for h in holders[:10])
                top_holder_pct = holders[0]["percentage"] if holders else 0

                if top_10_pct > 70 or top_holder_pct > 30:
                    risk_level = "HIGH"
                elif top_10_pct > 50 or top_holder_pct > 20:
                    risk_level = "MEDIUM"
                else:
                    risk_level = "LOW"

                analysis = HolderAnalysis(
                    mint=mint,
                    total_holders=len(accounts),
                    top_10_concentration=top_10_pct,
                    top_holder_pct=top_holder_pct,
                    risk_level=risk_level,
                    is_concentrated=top_10_pct > 50,
                    holders=holders[:limit]
                )

                self._cache[cache_key] = (analysis, time.time())
                return analysis

        except Exception as e:
            logger.error(f"Helius holder analysis error: {e}")
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
