"""
Multi-source price fetching with priority:
1. Jupiter Price API V3 (primary)
2. Birdeye (fallback, limited)
3. DexScreener (last resort)
"""
import asyncio
import aiohttp
import os
import time
from utils.logger import get_logger

logger = get_logger(__name__)

_sol_cache = {"price": None, "ts": 0}
SOL_MINT = "So11111111111111111111111111111111111111112"


async def _get_sol_usd(session: aiohttp.ClientSession) -> float | None:
    """Get cached SOL/USD price."""
    now = time.time()
    if _sol_cache["price"] and (now - _sol_cache["ts"]) < 10:
        return _sol_cache["price"]
    
    try:
        url = f"https://api.jup.ag/price/v3?ids={SOL_MINT}"
        headers = {"Accept": "application/json"}
        api_key = os.getenv("JUPITER_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = data.get(SOL_MINT, {}).get("usdPrice")
                if price:
                    _sol_cache["price"] = float(price)
                    _sol_cache["ts"] = now
                    return float(price)
    except Exception:
        pass
    return _sol_cache.get("price")


async def get_price_jupiter(mint: str, session: aiohttp.ClientSession) -> float | None:
    """Jupiter Price API V3 - returns price in SOL."""
    try:
        url = f"https://api.jup.ag/price/v3?ids={mint}"
        headers = {"Accept": "application/json"}
        api_key = os.getenv("JUPITER_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            usd_price = data.get(mint, {}).get("usdPrice")
            if not usd_price:
                return None
            
            sol_usd = await _get_sol_usd(session)
            if sol_usd and sol_usd > 0:
                return float(usd_price) / sol_usd
    except Exception as e:
        logger.debug(f"[JUP] {mint[:8]}: {e}")
    return None


async def get_price_birdeye(mint: str, session: aiohttp.ClientSession) -> float | None:
    """Birdeye API - returns priceInNative (SOL) directly."""
    try:
        api_key = os.getenv("BIRDEYE_API_KEY")
        if not api_key:
            return None
        
        url = f"https://public-api.birdeye.so/defi/price?address={mint}"
        headers = {"X-API-KEY": api_key, "x-chain": "solana"}
        
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("success"):
                price = data.get("data", {}).get("priceInNative")
                if price and price > 0:
                    return float(price)
    except Exception as e:
        logger.debug(f"[BIRDEYE] {mint[:8]}: {e}")
    return None


async def get_price_dexscreener(mint: str, session: aiohttp.ClientSession) -> float | None:
    """DexScreener - returns priceNative (SOL)."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            pairs = data.get("pairs", [])
            for pair in pairs:
                price = pair.get("priceNative")
                if price:
                    return float(price)
    except Exception as e:
        logger.debug(f"[DEX] {mint[:8]}: {e}")
    return None


async def get_token_price(mint: str) -> tuple[float | None, str]:
    """
    Get token price in SOL with fallback chain.
    Returns: (price_in_sol, source)
    """
    async with aiohttp.ClientSession() as session:
        # 1. Jupiter (primary)
        price = await get_price_jupiter(mint, session)
        if price and price > 0:
            return price, "jupiter"
        
        # 2. Birdeye (fallback)
        price = await get_price_birdeye(mint, session)
        if price and price > 0:
            return price, "birdeye"
        
        # 3. DexScreener (last resort)
        price = await get_price_dexscreener(mint, session)
        if price and price > 0:
            return price, "dexscreener"
        
        return None, "none"


async def get_token_price_fast(mint: str) -> float | None:
    """Quick price fetch - Jupiter only."""
    async with aiohttp.ClientSession() as session:
        return await get_price_jupiter(mint, session)
