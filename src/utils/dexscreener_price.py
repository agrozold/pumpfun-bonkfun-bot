"""Fallback price fetcher via DexScreener API with retry logic."""
import asyncio
import aiohttp
import logging

logger = logging.getLogger(__name__)


async def get_price_from_dexscreener(
    mint: str,
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> float | None:
    """Get token price in SOL from DexScreener with retries."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"[DEXSCREENER] HTTP {resp.status} for {mint[:12]}... (attempt {attempt + 1}/{max_retries})")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                        continue

                    data = await resp.json()
                    pairs = data.get("pairs", [])

                    if not pairs:
                        logger.info(f"[DEXSCREENER] No pairs found for {mint[:12]}... (attempt {attempt + 1}/{max_retries})")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                        continue

                    for pair in pairs:
                        price_native = pair.get("priceNative")
                        if price_native:
                            price = float(price_native)
                            if price > 0:
                                logger.info(f"[DEXSCREENER] Got price for {mint[:12]}...: {price:.10f} SOL")
                                return price

                    logger.warning(f"[DEXSCREENER] Pairs found but no valid price for {mint[:12]}...")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)

        except asyncio.TimeoutError:
            logger.warning(f"[DEXSCREENER] Timeout for {mint[:12]}...")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
        except Exception as e:
            logger.warning(f"[DEXSCREENER] Error for {mint[:12]}...: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)

    logger.error(f"[DEXSCREENER] Failed to get price for {mint[:12]}... after {max_retries} attempts")
    return None
