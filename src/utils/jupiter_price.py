"""
Multi-source price fetching with priority:
1. Jupiter Quote API (primary - most accurate, real AMM price)
2. Jupiter Price API V3 (fallback)
3. Birdeye (fallback, limited)
4. DexScreener (last resort)
"""
import asyncio
import aiohttp
import os
import time
from utils.logger import get_logger

logger = get_logger(__name__)

_sol_cache = {"price": None, "ts": 0}
_decimals_cache = {}
SOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_API_URL = "https://lite-api.jup.ag/swap/v1/quote"


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


async def _get_token_decimals(mint: str, session: aiohttp.ClientSession) -> int:
    """Get token decimals from Solana RPC."""
    if mint in _decimals_cache:
        return _decimals_cache[mint]
    
    try:
        rpc_url = os.getenv("SOLANA_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "base64"}]
        }
        async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                import base64
                b64_data = data.get("result", {}).get("value", {}).get("data", [None])[0]
                if b64_data:
                    raw = base64.b64decode(b64_data)
                    decimals = raw[44]
                    _decimals_cache[mint] = decimals
                    return decimals
    except Exception as e:
        logger.debug(f"[DECIMALS] Failed to get decimals for {mint[:8]}: {e}")
    
    # Default to 6 for pump.fun tokens
    _decimals_cache[mint] = 6
    return 6


async def get_price_quote_api(mint: str, session: aiohttp.ClientSession) -> float | None:
    """
    Jupiter Quote API - returns REAL price from AMM pool.
    Most accurate for all tokens including new pump.fun tokens.
    """
    try:
        # Use small amount to minimize price impact (0.001 SOL = 1M lamports)
        params = {
            "inputMint": SOL_MINT,
            "outputMint": mint,
            "amount": "1000000",  # 0.001 SOL in lamports
            "slippageBps": "100"
        }
        
        async with session.get(
            QUOTE_API_URL, 
            params=params, 
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status != 200:
                return None
            
            data = await resp.json()
            out_amount = int(data.get("outAmount", 0))
            
            if out_amount <= 0:
                return None
            
            # Get decimals for accurate calculation
            decimals = await _get_token_decimals(mint, session)
            tokens = out_amount / (10 ** decimals)
            
            if tokens <= 0:
                return None
            
            # Price = SOL spent / tokens received
            # We spent 0.001 SOL (1000000 lamports)
            price_in_sol = 0.001 / tokens
            
            return price_in_sol
            
    except Exception as e:
        logger.debug(f"[QUOTE] {mint[:8]}: {e}")
    return None


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
    
    Priority:
    1. Quote API (most accurate - real AMM price)
    2. Jupiter Price API (fast but may be stale)
    3. Birdeye
    4. DexScreener
    """
    async with aiohttp.ClientSession() as session:
        # 1. Quote API (primary - most accurate!)
        price = await get_price_quote_api(mint, session)
        if price and price > 0:
            return price, "jupiter_quote"

        # 2. Jupiter Price API (fallback)
        price = await get_price_jupiter(mint, session)
        if price and price > 0:
            return price, "jupiter_price"

        # 3. Birdeye (fallback)
        price = await get_price_birdeye(mint, session)
        if price and price > 0:
            return price, "birdeye"

        # 4. DexScreener (last resort)
        price = await get_price_dexscreener(mint, session)
        if price and price > 0:
            return price, "dexscreener"

        return None, "none"


async def get_token_price_fast(mint: str) -> float | None:
    """Quick price fetch - Quote API only."""
    async with aiohttp.ClientSession() as session:
        return await get_price_quote_api(mint, session)


# === BATCH PRICE FETCHING ===
_price_cache = {}
_cache_ts = {}
CACHE_TTL = 1.5  # seconds


async def get_prices_batch(mints: list[str]) -> dict[str, float]:
    """
    Get prices for multiple tokens.
    Uses Quote API for each (more accurate than batch Price API).
    """
    if not mints:
        return {}

    result = {}
    async with aiohttp.ClientSession() as session:
        # Fetch prices concurrently
        tasks = [get_price_quote_api(mint, session) for mint in mints[:20]]  # Limit concurrent
        prices = await asyncio.gather(*tasks, return_exceptions=True)
        
        for mint, price in zip(mints[:20], prices):
            if isinstance(price, float) and price > 0:
                result[mint] = price
    
    return result


async def get_token_price_cached(mint: str) -> float | None:
    """
    Get price with brief caching to reduce API calls.
    Good for high-frequency monitoring.
    """
    now = time.time()

    # Check cache
    if mint in _price_cache and (now - _cache_ts.get(mint, 0)) < CACHE_TTL:
        return _price_cache[mint]

    # Fetch fresh
    price, _ = await get_token_price(mint)

    if price:
        _price_cache[mint] = price
        _cache_ts[mint] = now

    return price
