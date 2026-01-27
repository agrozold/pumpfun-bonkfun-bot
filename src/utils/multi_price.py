"""Multi-source price fetcher with fallback."""
import asyncio
import aiohttp
import logging
import os

logger = logging.getLogger(__name__)

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

async def get_price_birdeye(mint: str) -> float | None:
    """Get price from Birdeye (faster than DexScreener)."""
    if not BIRDEYE_API_KEY:
        return None
    try:
        url = f"https://public-api.birdeye.so/defi/price?address={mint}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                price_usd = data.get("data", {}).get("value")
                if not price_usd:
                    return None
                # Need SOL price to convert
                sol_url = "https://public-api.birdeye.so/defi/price?address=So11111111111111111111111111111111111111112"
                async with session.get(sol_url, headers=headers, timeout=5) as sol_resp:
                    sol_data = await sol_resp.json()
                    sol_price = sol_data.get("data", {}).get("value", 100)
                return price_usd / sol_price
    except Exception as e:
        logger.debug(f"Birdeye error: {e}")
        return None


async def get_price_dexscreener(mint: str) -> float | None:
    """Get price from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                for pair in pairs:
                    price_native = pair.get("priceNative")
                    if price_native:
                        return float(price_native)
                return None
    except Exception as e:
        logger.debug(f"DexScreener error: {e}")
        return None


async def get_price_multi(mint: str) -> float | None:
    """Get price from multiple sources with fallback."""
    # Try Birdeye first (faster)
    price = await get_price_birdeye(mint)
    if price and price > 0:
        return price
    
    # Fallback to DexScreener
    price = await get_price_dexscreener(mint)
    if price and price > 0:
        return price
    
    return None
