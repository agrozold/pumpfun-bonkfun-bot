"""
Оптимизированный анализ создателей через Helius Enhanced Transactions API.
Заменяет N+1 запросов на 1 запрос.
"""

import os
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv()


def _extract_helius_api_key() -> Optional[str]:
    """Извлекает Helius API ключ из .env."""
    api_key = os.getenv("HELIUS_API_KEY")
    if api_key and "YOUR_" not in api_key:
        return api_key
    
    rpc_url = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "")
    if "api-key=" in rpc_url:
        return rpc_url.split("api-key=")[1].split("&")[0]
    
    return None


HELIUS_API_KEY = _extract_helius_api_key()


async def get_creator_stats_optimized(
    creator_address: str,
    limit: int = 50,
    timeout: float = 10.0
) -> Optional[dict]:
    """
    Анализирует историю создателя ЗА ОДИН ЗАПРОС к Helius Enhanced API.
    """
    if not HELIUS_API_KEY:
        logger.error("HELIUS_API_KEY не найден!")
        return None

    url = f"https://api.helius.xyz/v0/addresses/{creator_address}/transactions"
    
    params = {
        "api-key": HELIUS_API_KEY,
        "limit": limit,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, 
                params=params, 
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                
                if response.status == 429:
                    logger.warning("Helius API rate limit exceeded")
                    return None
                    
                if response.status != 200:
                    error_text = await response.text()
                    logger.warning(f"Helius API Error: {response.status} - {error_text}")
                    return None

                transactions = await response.json()

        stats = {
            "total_txs": len(transactions),
            "tokens_created": 0,
            "tokens_sold": 0,
            "unique_tokens_sold": set(),
            "large_sells": 0,
            "suspicious_patterns": [],
        }

        for tx in transactions:
            tx_type = tx.get("type", "UNKNOWN")
            
            if tx_type in ("TOKEN_MINT", "COMPRESSED_NFT_MINT", "NFT_MINT"):
                stats["tokens_created"] += 1
            
            token_transfers = tx.get("tokenTransfers", [])
            for transfer in token_transfers:
                from_account = transfer.get("fromUserAccount", "")
                mint_address = transfer.get("mint", "")
                token_amount = transfer.get("tokenAmount", 0)
                
                if from_account == creator_address and mint_address:
                    stats["unique_tokens_sold"].add(mint_address)
                    
                    if token_amount and float(token_amount) > 10_000_000:
                        stats["large_sells"] += 1

        stats["tokens_sold"] = len(stats["unique_tokens_sold"])
        
        logger.info(
            f"Creator {creator_address[:8]}... stats: "
            f"txs={stats['total_txs']}, created={stats['tokens_created']}, sold={stats['tokens_sold']}"
        )
        
        return stats

    except aiohttp.ClientTimeout:
        logger.warning(f"Helius API timeout for {creator_address}")
        return None
    except Exception as e:
        logger.error(f"Error in get_creator_stats_optimized: {e}")
        return None


async def batch_parse_transactions(
    signatures: list[str],
    timeout: float = 15.0
) -> Optional[list]:
    """Парсит несколько транзакций ОДНИМ запросом."""
    if not HELIUS_API_KEY:
        return None
    
    if len(signatures) > 100:
        signatures = signatures[:100]

    url = f"https://api.helius.xyz/v0/transactions"
    params = {"api-key": HELIUS_API_KEY}
    payload = {"transactions": signatures}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params=params,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status != 200:
                    return None
                return await response.json()
    except Exception as e:
        logger.error(f"Error in batch_parse_transactions: {e}")
        return None
