"""
Creator analysis - works with or without Helius API.
Falls back to RPC-based analysis when Helius is not available.
"""

import os
from typing import Optional, List

import aiohttp
from dotenv import load_dotenv

from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv()


def _extract_helius_api_key() -> Optional[str]:
    """Extract Helius API key from .env (if available)."""
    api_key = os.getenv("HELIUS_API_KEY")
    if api_key and "YOUR_" not in api_key and len(api_key) > 10:
        return api_key
    return None


HELIUS_API_KEY = _extract_helius_api_key()

# Log status on import
if HELIUS_API_KEY:
    logger.info("[CreatorAnalyzer] Helius API available")
else:
    logger.info("[CreatorAnalyzer] Helius not configured - using RPC fallback")


async def get_creator_stats_optimized(
    creator_address: str,
    limit: int = 50,
    timeout: float = 10.0
) -> Optional[dict]:
    """
    Analyze creator history.
    Uses Helius if available, otherwise falls back to RPC.
    """
    # Try Helius first if available
    if HELIUS_API_KEY:
        result = await _get_creator_stats_helius(creator_address, limit, timeout)
        if result:
            return result
    
    # Fallback to RPC-based analysis
    return await _get_creator_stats_rpc(creator_address, limit, timeout)


async def _get_creator_stats_helius(
    creator_address: str,
    limit: int = 50,
    timeout: float = 10.0
) -> Optional[dict]:
    """Get creator stats via Helius Enhanced API."""
    if not HELIUS_API_KEY:
        return None

    url = f"https://api.helius.xyz/v0/addresses/{creator_address}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": limit}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status == 429:
                    logger.warning("Helius rate limited")
                    return None
                if response.status != 200:
                    return None

                transactions = await response.json()

        stats = {
            "total_txs": len(transactions),
            "tokens_created": 0,
            "tokens_sold": 0,
            "unique_tokens_sold": set(),
            "large_sells": 0,
            "suspicious_patterns": [],
            "source": "helius"
        }

        for tx in transactions:
            tx_type = tx.get("type", "UNKNOWN")
            if tx_type in ("TOKEN_MINT", "COMPRESSED_NFT_MINT", "NFT_MINT"):
                stats["tokens_created"] += 1

            for transfer in tx.get("tokenTransfers", []):
                from_account = transfer.get("fromUserAccount", "")
                mint_address = transfer.get("mint", "")
                token_amount = transfer.get("tokenAmount", 0)

                if from_account == creator_address and mint_address:
                    stats["unique_tokens_sold"].add(mint_address)
                    if token_amount and float(token_amount) > 10_000_000:
                        stats["large_sells"] += 1

        stats["tokens_sold"] = len(stats["unique_tokens_sold"])
        stats["unique_tokens_sold"] = list(stats["unique_tokens_sold"])  # Convert set

        logger.debug(f"Creator {creator_address[:8]}... (helius): txs={stats['total_txs']}")
        return stats

    except Exception as e:
        logger.debug(f"Helius creator stats error: {e}")
        return None


async def _get_creator_stats_rpc(
    creator_address: str,
    limit: int = 50,
    timeout: float = 10.0
) -> Optional[dict]:
    """
    Get creator stats via RPC.
    Less detailed than Helius but works without external API.
    """
    try:
        from core.rpc_manager import get_rpc_manager
        rpc = await get_rpc_manager()
    except Exception as e:
        logger.error(f"Failed to get RPC manager: {e}")
        return None

    try:
        # Get recent signatures for this address
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                creator_address,
                {"limit": limit}
            ]
        }

        result = await rpc.post_rpc(body)
        
        if not result or "result" not in result:
            return None

        signatures = result["result"]

        stats = {
            "total_txs": len(signatures),
            "tokens_created": 0,
            "tokens_sold": 0,
            "unique_tokens_sold": [],
            "large_sells": 0,
            "suspicious_patterns": [],
            "source": "rpc"
        }

        # Basic analysis - count transaction types
        # Note: RPC doesn't give us parsed transaction types like Helius
        # We can only count total transactions and check for errors
        
        error_count = 0
        for sig in signatures:
            if sig.get("err"):
                error_count += 1

        # High error rate might indicate suspicious activity
        if len(signatures) > 10 and error_count / len(signatures) > 0.5:
            stats["suspicious_patterns"].append("High transaction error rate")

        logger.debug(f"Creator {creator_address[:8]}... (rpc): txs={stats['total_txs']}")
        return stats

    except Exception as e:
        logger.error(f"RPC creator stats error: {e}")
        return None


async def batch_parse_transactions(
    signatures: List[str],
    timeout: float = 15.0
) -> Optional[list]:
    """
    Parse multiple transactions.
    Uses Helius if available, otherwise returns None (RPC doesn't support batch parsing).
    """
    if not HELIUS_API_KEY:
        logger.debug("Batch parsing not available without Helius")
        return None

    if len(signatures) > 100:
        signatures = signatures[:100]

    url = "https://api.helius.xyz/v0/transactions"
    params = {"api-key": HELIUS_API_KEY}
    payload = {"transactions": signatures}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, params=params, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status != 200:
                    return None
                return await response.json()
    except Exception as e:
        logger.error(f"Batch parse error: {e}")
        return None


async def is_creator_suspicious(creator_address: str) -> tuple[bool, list[str]]:
    """
    Quick check if creator looks suspicious.
    Returns (is_suspicious, reasons).
    """
    stats = await get_creator_stats_optimized(creator_address, limit=30)
    
    if not stats:
        return False, ["Could not analyze creator"]
    
    reasons = []
    
    # Check for suspicious patterns
    if stats.get("tokens_created", 0) > 10:
        reasons.append(f"Created {stats['tokens_created']} tokens (possible serial rugger)")
    
    if stats.get("large_sells", 0) > 5:
        reasons.append(f"Multiple large sells ({stats['large_sells']})")
    
    if stats.get("suspicious_patterns"):
        reasons.extend(stats["suspicious_patterns"])
    
    return len(reasons) > 0, reasons
