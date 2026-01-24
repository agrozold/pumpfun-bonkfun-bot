"""
RPC-based holder provider using getTokenLargestAccounts.
Works without any external API - uses your configured RPC endpoints.
This is the FALLBACK that always works.
"""

import asyncio
import logging
from typing import Optional

from .holder_provider import (
    HolderProvider, HolderAnalysis, TokenSecurityInfo,
    ProviderType, calculate_risk_level
)

logger = logging.getLogger(__name__)


class RPCHolderProvider(HolderProvider):
    """
    Holder analysis using native Solana RPC.
    Uses getTokenLargestAccounts - returns top 20 holders.
    No external API needed!
    """

    def __init__(self):
        self._rpc_manager = None
        self._initialized = False

    async def _get_rpc(self):
        if not self._initialized:
            try:
                from core.rpc_manager import get_rpc_manager
                self._rpc_manager = await get_rpc_manager()
                self._initialized = True
            except Exception as e:
                logger.error(f"Failed to get RPC manager: {e}")
        return self._rpc_manager

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.RPC

    @property
    def is_available(self) -> bool:
        return True  # RPC is always available

    async def get_holders(self, mint: str, limit: int = 20) -> Optional[HolderAnalysis]:
        """Get top holders using getTokenLargestAccounts RPC method."""
        rpc = await self._get_rpc()
        if not rpc:
            return None

        try:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [mint]
            }

            result = await rpc.post_rpc(body)

            if not result or "result" not in result:
                logger.debug(f"No holder data for {mint[:8]}...")
                return None

            accounts = result["result"].get("value", [])

            if not accounts:
                return HolderAnalysis(
                    mint=mint,
                    total_holders=0,
                    top_10_concentration=100.0,
                    top_holder_pct=100.0,
                    risk_level="HIGH",
                    is_concentrated=True,
                    holders=[],
                    source="rpc"
                )

            # Calculate total from top accounts
            total_amount = sum(int(acc.get("amount", 0)) for acc in accounts)

            holders = []
            for acc in accounts[:limit]:
                amount = int(acc.get("amount", 0))
                pct = (amount / total_amount * 100) if total_amount > 0 else 0
                holders.append({
                    "address": acc.get("address", ""),
                    "amount": amount,
                    "percentage": pct
                })

            holders.sort(key=lambda x: x["amount"], reverse=True)

            top_10_pct = sum(h["percentage"] for h in holders[:10])
            top_holder_pct = holders[0]["percentage"] if holders else 0

            return HolderAnalysis(
                mint=mint,
                total_holders=len(accounts),  # Note: only top 20 from RPC
                top_10_concentration=top_10_pct,
                top_holder_pct=top_holder_pct,
                risk_level=calculate_risk_level(top_10_pct, top_holder_pct),
                is_concentrated=top_10_pct > 50,
                holders=holders,
                source="rpc"
            )

        except Exception as e:
            logger.error(f"RPC holder analysis error: {e}")
            return None

    async def get_security(self, mint: str) -> Optional[TokenSecurityInfo]:
        """
        Basic security check via RPC.
        Limited compared to Birdeye but works without external API.
        """
        rpc = await self._get_rpc()
        if not rpc:
            return None

        try:
            # Get mint account info
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}]
            }

            result = await rpc.post_rpc(body)

            if not result or "result" not in result:
                return None

            account = result["result"]
            if not account or "value" not in account:
                return None

            data = account["value"].get("data", {})
            if isinstance(data, dict):
                parsed = data.get("parsed", {})
                info = parsed.get("info", {})
            else:
                # Can't parse - return basic result
                return TokenSecurityInfo(
                    mint=mint,
                    is_safe=True,
                    risk_score=0,
                    warnings=["Could not parse mint data"],
                    details={},
                    source="rpc"
                )

            warnings = []
            risk_score = 0

            # Check mint authority
            mint_authority = info.get("mintAuthority")
            if mint_authority:
                warnings.append("Mint authority active")
                risk_score += 15

            # Check freeze authority
            freeze_authority = info.get("freezeAuthority")
            if freeze_authority:
                warnings.append("Freeze authority active")
                risk_score += 20

            return TokenSecurityInfo(
                mint=mint,
                is_safe=risk_score < 30,
                risk_score=risk_score,
                warnings=warnings,
                details={
                    "mintAuthority": mint_authority,
                    "freezeAuthority": freeze_authority,
                    "decimals": info.get("decimals"),
                    "supply": info.get("supply"),
                },
                source="rpc"
            )

        except Exception as e:
            logger.error(f"RPC security check error: {e}")
            return None

    async def close(self) -> None:
        pass  # RPC manager handles its own cleanup
