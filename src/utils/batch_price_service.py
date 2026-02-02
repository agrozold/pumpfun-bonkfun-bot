"""
Batch Price Service - fetches prices for ALL positions in ONE request.
Solves Jupiter API rate limit issue (1 RPS on free tier).

УМНЫЙ СЕРВИС:
- При старте загружает ТОЛЬКО токены с реальным балансом в кошельке
- Автоматически watch после покупки
- Автоматически unwatch после продажи
- Синхронизируется с Redis/JSON
"""
import asyncio
import aiohttp
import os
import time
import base58
from pathlib import Path
from typing import Dict, Optional, Set
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from utils.logger import get_logger

logger = get_logger(__name__)

JUPITER_PRICE_V3_URL = "https://api.jup.ag/price/v3"
SOL_MINT = "So11111111111111111111111111111111111111112"


class BatchPriceService:
    """Centralized price service - ONE request for ALL tokens."""
    
    def __init__(self, update_interval: float = 1.0):
        self.update_interval = max(1.0, update_interval)
        self._prices: Dict[str, float] = {}
        self._prices_usd: Dict[str, float] = {}
        self._last_update: Dict[str, float] = {}
        self._sol_price_usd: float = 0.0
        self._watched_mints: Set[str] = set()
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._api_key = os.getenv("JUPITER_API_KEY")
        self._consecutive_errors = 0
        self._last_success_time = 0.0
        
        if self._api_key:
            logger.info(f"[BATCH] API key loaded: {self._api_key[:12]}...")
        
        self._stats = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "tokens_fetched": 0,
        }
    
    def watch(self, mint: str) -> None:
        """Add a mint to watch list (called after BUY)."""
        if mint and mint != SOL_MINT:
            self._watched_mints.add(mint)
            logger.info(f"[BATCH] +WATCH {mint[:12]}... (total: {len(self._watched_mints)})")
    
    def unwatch(self, mint: str) -> None:
        """Remove a mint from watch list (called after SELL)."""
        self._watched_mints.discard(mint)
        self._prices.pop(mint, None)
        self._prices_usd.pop(mint, None)
        self._last_update.pop(mint, None)
        logger.info(f"[BATCH] -UNWATCH {mint[:12]}... (total: {len(self._watched_mints)})")
    
    def watch_many(self, mints: list[str]) -> None:
        """Add multiple mints to watch list."""
        for mint in mints:
            self.watch(mint)
    
    def get_price(self, mint: str) -> Optional[float]:
        """Get cached price in SOL (instant, no API call)."""
        return self._prices.get(mint)
    
    def get_price_usd(self, mint: str) -> Optional[float]:
        """Get cached price in USD."""
        return self._prices_usd.get(mint)
    
    def get_sol_price(self) -> float:
        """Get current SOL/USD price."""
        return self._sol_price_usd
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get all cached prices (in SOL)."""
        return self._prices.copy()
    
    def get_price_age(self, mint: str) -> float:
        """Get age of cached price in seconds."""
        if mint in self._last_update:
            return time.time() - self._last_update[mint]
        return float('inf')
    
    def is_price_fresh(self, mint: str, max_age: float = 5.0) -> bool:
        """Check if price is fresh enough."""
        return self.get_price_age(mint) < max_age
    
    async def start(self) -> None:
        """Start background price update task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._update_loop())
        logger.warning(f"[BATCH] Started price service (interval: {self.update_interval}s, watching: {len(self._watched_mints)})")
    
    async def stop(self) -> None:
        """Stop background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[BATCH] Stopped")
    
    async def _update_loop(self) -> None:
        """Main update loop."""
        while self._running:
            try:
                if self._watched_mints:
                    await self._fetch_batch_prices()
                await asyncio.sleep(self.update_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BATCH] Loop error: {e}")
                self._consecutive_errors += 1
                backoff = min(30, self.update_interval * (2 ** min(self._consecutive_errors, 4)))
                await asyncio.sleep(backoff)
    
    async def _fetch_batch_prices(self) -> None:
        """Fetch prices for all watched mints in ONE request."""
        if not self._watched_mints:
            return
        
        mints_to_fetch = list(self._watched_mints)[:49]
        mints_to_fetch.append(SOL_MINT)
        
        self._stats["requests"] += 1
        
        try:
            headers = {"Accept": "application/json"}
            if self._api_key:
                headers["x-api-key"] = self._api_key
            
            url = f"{JUPITER_PRICE_V3_URL}?ids={','.join(mints_to_fetch)}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 429:
                        logger.warning("[BATCH] Rate limited!")
                        self._consecutive_errors += 1
                        self._stats["failures"] += 1
                        return
                    
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"[BATCH] HTTP {resp.status}: {text[:100]}")
                        self._stats["failures"] += 1
                        return
                    
                    data = await resp.json()
            
            now = time.time()
            tokens_updated = 0
            
            sol_data = data.get(SOL_MINT, {})
            if isinstance(sol_data, dict) and sol_data.get("usdPrice"):
                self._sol_price_usd = float(sol_data["usdPrice"])
            
            for mint in mints_to_fetch:
                if mint == SOL_MINT:
                    continue
                    
                token_data = data.get(mint, {})
                if isinstance(token_data, dict) and token_data.get("usdPrice"):
                    usd_price = float(token_data["usdPrice"])
                    self._prices_usd[mint] = usd_price
                    
                    if self._sol_price_usd > 0:
                        sol_price = usd_price / self._sol_price_usd
                        self._prices[mint] = sol_price
                        self._last_update[mint] = now
                        tokens_updated += 1
            
            self._stats["successes"] += 1
            self._stats["tokens_fetched"] += tokens_updated
            self._consecutive_errors = 0
            self._last_success_time = now
            
            logger.debug(f"[BATCH] Updated {tokens_updated}/{len(mints_to_fetch)-1} (SOL=${self._sol_price_usd:.2f})")
            
        except asyncio.TimeoutError:
            logger.warning("[BATCH] Timeout")
            self._stats["failures"] += 1
            self._consecutive_errors += 1
        except Exception as e:
            logger.error(f"[BATCH] Error: {e}")
            self._stats["failures"] += 1
            self._consecutive_errors += 1
    
    async def fetch_once(self) -> Dict[str, float]:
        """Fetch prices once immediately."""
        await self._fetch_batch_prices()
        return self._prices.copy()
    
    def get_stats(self) -> dict:
        success_rate = 0
        if self._stats["requests"] > 0:
            success_rate = (self._stats["successes"] / self._stats["requests"]) * 100
        return {
            **self._stats,
            "success_rate": f"{success_rate:.1f}%",
            "watched_tokens": len(self._watched_mints),
            "cached_prices": len(self._prices),
            "sol_price_usd": self._sol_price_usd,
        }
    
    async def load_from_wallet(self) -> int:
        """
        УМНАЯ ЗАГРУЗКА: только токены с реальным балансом в кошельке!
        Это предотвращает мониторинг призраков.
        """
        try:
            rpc = os.getenv("SOLANA_RPC_ENDPOINT") or os.getenv("ALCHEMY_RPC_ENDPOINT")
            pk = os.getenv("SOLANA_PRIVATE_KEY")
            
            if not pk or not rpc:
                logger.warning("[BATCH] No wallet credentials, falling back to positions file")
                return await self.load_positions_from_file()
            
            from solders.keypair import Keypair
            kp = Keypair.from_bytes(base58.b58decode(pk))
            wallet = str(kp.pubkey())
            
            wallet_mints = set()
            
            # Check both token programs
            for prog_id in [
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"   # Token2022
            ]:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [wallet, {"programId": prog_id}, {"encoding": "jsonParsed"}]
                }
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(rpc, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        data = await resp.json()
                
                for acc in data.get("result", {}).get("value", []):
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    amount = float(info.get("tokenAmount", {}).get("uiAmount") or 0)
                    mint = info.get("mint", "")
                    # Only watch tokens with balance > 1 (skip dust)
                    if amount >= 1 and mint:
                        wallet_mints.add(mint)
            
            # Watch all wallet tokens
            for mint in wallet_mints:
                self.watch(mint)
            
            logger.warning(f"[BATCH] Loaded {len(wallet_mints)} tokens from WALLET (real balances)")
            return len(wallet_mints)
            
        except Exception as e:
            logger.error(f"[BATCH] Failed to load from wallet: {e}, falling back to file")
            return await self.load_positions_from_file()
    
    async def load_positions_from_file(self) -> int:
        """Fallback: load from positions.json."""
        try:
            from trading.position import load_positions
            positions = load_positions()
            count = 0
            for pos in positions:
                if pos.is_active:
                    self.watch(str(pos.mint))
                    count += 1
            logger.info(f"[BATCH] Loaded {count} positions from file")
            return count
        except Exception as e:
            logger.error(f"[BATCH] Failed to load positions: {e}")
            return 0


# Global singleton
_service: Optional[BatchPriceService] = None


def get_batch_price_service() -> BatchPriceService:
    """Get or create the global BatchPriceService instance."""
    global _service
    if _service is None:
        _service = BatchPriceService(update_interval=1.0)
    return _service


async def init_batch_price_service() -> BatchPriceService:
    """Initialize, load from WALLET, and start the service."""
    service = get_batch_price_service()
    
    # УМНАЯ ЗАГРУЗКА - только реальные токены из кошелька!
    await service.load_from_wallet()
    
    if not service._running:
        await service.start()
        await service.fetch_once()
    
    return service


# Helper functions
def watch_token(mint: str) -> None:
    """Add token to price watch (call after BUY)."""
    get_batch_price_service().watch(mint)


def unwatch_token(mint: str) -> None:
    """Remove token from price watch (call after SELL)."""
    get_batch_price_service().unwatch(mint)


def get_cached_price(mint: str) -> Optional[float]:
    """Get price from cache (instant)."""
    return get_batch_price_service().get_price(mint)


def get_cached_price_usd(mint: str) -> Optional[float]:
    """Get USD price from cache."""
    return get_batch_price_service().get_price_usd(mint)
