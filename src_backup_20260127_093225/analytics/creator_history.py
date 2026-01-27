"""
Creator History Tracker
Tracks token creation history for developers on pump.fun/bonk.fun.
Uses Birdeye/Dexscreener API or Solana RPC (no Helius required).
"""

import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

# Program IDs
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LETS_BONK_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"


class CreatorHistoryTracker:
    """Track creator's token history on pump.fun/bonk.fun."""

    def __init__(
        self,
        birdeye_api_key: str | None = None,
        rpc_endpoint: str | None = None,
        cache_ttl: int = 3600,  # 1 hour cache
    ):
        self.birdeye_api_key = birdeye_api_key or os.getenv("BIRDEYE_API_KEY")
        self.rpc_endpoint = rpc_endpoint or os.getenv("SOLANA_NODE_RPC_ENDPOINT")
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[Any, float]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    def _get_cached(self, key: str) -> Any | None:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.time() - timestamp < self.cache_ttl:
                return value
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        self._cache[key] = (value, time.time())

    async def get_wallet_tokens_birdeye(
        self, wallet: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Get tokens associated with wallet using Birdeye portfolio API."""
        cache_key = f"wallet_tokens:{wallet}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        if not self.birdeye_api_key:
            return []

        session = await self._get_session()
        url = "https://public-api.birdeye.so/v1/wallet/token_list"

        headers = {
            "X-API-KEY": self.birdeye_api_key,
            "x-chain": "solana",
        }
        params = {
            "wallet": wallet,
        }

        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tokens = data.get("data", {}).get("items", [])
                    self._set_cached(cache_key, tokens)
                    return tokens
                else:
                    logger.debug(f"[CREATOR] Birdeye wallet API error: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"[CREATOR] Birdeye wallet request failed: {e}")
            return []

    async def get_creator_transactions_rpc(
        self, creator_wallet: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        Get creator's transaction history using RPC.
        Looks for token creation transactions.
        """
        cache_key = f"creator_txs:{creator_wallet}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        if not self.rpc_endpoint:
            return []

        session = await self._get_session()

        # Get signatures
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [creator_wallet, {"limit": limit}],
        }

        try:
            async with session.post(
                self.rpc_endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                signatures = data.get("result", [])

                created_tokens = []

                # Analyze transactions (limit to first 30 for speed)
                for sig_info in signatures[:30]:
                    sig = sig_info.get("signature")
                    if not sig:
                        continue

                    # Get transaction
                    tx_payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            sig,
                            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                        ],
                    }

                    async with session.post(
                        self.rpc_endpoint,
                        json=tx_payload,
                        headers={"Content-Type": "application/json"},
                    ) as tx_resp:
                        if tx_resp.status != 200:
                            continue

                        tx_data = await tx_resp.json()
                        tx = tx_data.get("result")

                        if not tx or tx.get("meta", {}).get("err"):
                            continue

                        # Check program interactions
                        instructions = (
                            tx.get("transaction", {})
                            .get("message", {})
                            .get("instructions", [])
                        )

                        is_creation = any(
                            ix.get("programId") in [PUMP_FUN_PROGRAM, LETS_BONK_PROGRAM]
                            for ix in instructions
                        )

                        if is_creation:
                            # Extract mint from token balances
                            post_balances = tx.get("meta", {}).get("postTokenBalances", [])
                            if post_balances:
                                mint = post_balances[0].get("mint")
                                if mint:
                                    created_tokens.append({
                                        "mint": mint,
                                        "signature": sig,
                                        "timestamp": sig_info.get("blockTime", 0),
                                        "slot": sig_info.get("slot", 0),
                                    })

                    await asyncio.sleep(0.05)  # Rate limit

                self._set_cached(cache_key, created_tokens)
                return created_tokens

        except Exception as e:
            logger.error(f"[CREATOR] RPC transaction fetch failed: {e}")
            return []

    async def get_creator_stats(
        self, creator_wallet: str
    ) -> dict[str, Any]:
        """
        Get comprehensive creator statistics.

        Returns:
            Dict with:
            - total_tokens_found: int
            - recent_tokens_7d: int
            - recent_tokens_24h: int
            - reputation_score: int (0-100)
            - risk_level: str ("low"/"medium"/"high"/"critical")
            - tokens: list of found tokens
        """
        # Get transactions from RPC
        tokens = await self.get_creator_transactions_rpc(creator_wallet)

        if not tokens:
            return {
                "total_tokens_found": 0,
                "recent_tokens_7d": 0,
                "recent_tokens_24h": 0,
                "reputation_score": 50,  # Neutral - unknown
                "risk_level": "unknown",
                "tokens": [],
                "note": "No token creation history found",
            }

        total = len(tokens)
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        day_ago = now - timedelta(days=1)

        recent_7d = 0
        recent_24h = 0

        for t in tokens:
            ts = t.get("timestamp")
            if ts and isinstance(ts, (int, float)):
                created = datetime.utcfromtimestamp(ts)
                if created > week_ago:
                    recent_7d += 1
                if created > day_ago:
                    recent_24h += 1

        # Calculate reputation score (0-100)
        # Lower score = higher risk
        score = 100

        # Penalize for many tokens
        if total > 100:
            score -= 50
        elif total > 50:
            score -= 30
        elif total > 20:
            score -= 15
        elif total > 10:
            score -= 5

        # Penalize for recent activity (potential rug factory)
        if recent_24h > 5:
            score -= 40
        elif recent_24h > 2:
            score -= 20
        elif recent_24h > 0:
            score -= 5

        if recent_7d > 20:
            score -= 30
        elif recent_7d > 10:
            score -= 15
        elif recent_7d > 5:
            score -= 5

        score = max(0, score)

        # Determine risk level
        if score >= 80:
            risk = "low"
        elif score >= 50:
            risk = "medium"
        elif score >= 25:
            risk = "high"
        else:
            risk = "critical"

        return {
            "total_tokens_found": total,
            "recent_tokens_7d": recent_7d,
            "recent_tokens_24h": recent_24h,
            "reputation_score": score,
            "risk_level": risk,
            "tokens": tokens[:10],  # Return first 10
        }

    async def is_safe_creator(
        self,
        creator_wallet: str,
        max_total_tokens: int = 50,
        max_tokens_24h: int = 3,
        min_score: int = 40,
    ) -> tuple[bool, dict]:
        """
        Check if creator is safe to buy from.

        Args:
            creator_wallet: Creator's wallet address
            max_total_tokens: Max allowed tokens created ever
            max_tokens_24h: Max tokens in last 24h
            min_score: Minimum reputation score required

        Returns:
            Tuple of (is_safe, stats)
        """
        stats = await self.get_creator_stats(creator_wallet)

        if stats.get("risk_level") == "unknown":
            # New creator - cautious but not blocking
            return True, stats

        is_safe = (
            stats["total_tokens_found"] <= max_total_tokens
            and stats["recent_tokens_24h"] <= max_tokens_24h
            and stats["reputation_score"] >= min_score
        )

        return is_safe, stats

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Singleton
_creator_tracker: CreatorHistoryTracker | None = None


async def get_creator_tracker() -> CreatorHistoryTracker:
    global _creator_tracker
    if _creator_tracker is None:
        _creator_tracker = CreatorHistoryTracker()
    return _creator_tracker
