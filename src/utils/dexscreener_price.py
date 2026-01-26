"""Fallback price fetcher via DexScreener API."""
import aiohttp
import logging

logger = logging.getLogger(__name__)

async def get_price_from_dexscreener(mint: str) -> float | None:
    """Get token price in SOL from DexScreener.
    
    Returns price in SOL or None if not found.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                
                pairs = data.get("pairs", [])
                if not pairs:
                    return None
                
                # Find SOL pair (priceNative is price in SOL)
                for pair in pairs:
                    price_native = pair.get("priceNative")
                    if price_native:
                        price = float(price_native)
                        logger.info(f"[DEXSCREENER] Got price for {mint[:12]}...: {price:.10f} SOL (DEX: {pair.get('dexId')})")
                        return price
                
                return None
    except Exception as e:
        logger.warning(f"[DEXSCREENER] Failed to get price for {mint[:12]}...: {e}")
        return None
