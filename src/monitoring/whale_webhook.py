"""
Whale Webhook Receiver - Real-time whale tracking via Helius Webhooks.
UPGRADED: Redis idempotency for txSignature deduplication.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from aiohttp import web

import aiohttp
logger = logging.getLogger(__name__)

TOKEN_BLACKLIST = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "So11111111111111111111111111111111111111112",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
}


SOL_MINT = "So11111111111111111111111111111111111111112"

async def _fetch_symbol_dexscreener(mint: str) -> str:
    """Fetch token symbol from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        return pairs[0].get("baseToken", {}).get("symbol", "")
    except Exception:
        pass
    return ""



@dataclass
class WhaleBuy:
    """Whale buy signal."""
    whale_wallet: str
    token_mint: str
    amount_sol: float
    timestamp: datetime
    tx_signature: str
    whale_label: str
    platform: str
    token_symbol: str = ""
    age_seconds: float = 0
    block_time: int | None = None


class WhaleWebhookReceiver:
    """Receives real-time whale SWAP notifications via Helius Webhooks."""
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.4,
        stablecoin_filter: list | None = None,
    ):
        self.host = host
        self.port = port
        self.min_buy_amount = min_buy_amount
        
        self.whale_wallets: dict[str, dict] = {}
        self._load_wallets(wallets_file)
        
        self.token_blacklist = TOKEN_BLACKLIST.copy()
        if stablecoin_filter:
            self.token_blacklist.update(set(stablecoin_filter))
        
        self.on_whale_buy: Optional[Callable] = None
        
        # In-memory backup when Redis is down
        self._processed_sigs: set[str] = set()
        self._emitted_tokens: set[str] = set()
        
        self._stats = {
            "webhooks_received": 0,
            "swaps_detected": 0,
            "buys_emitted": 0,
            "sells_skipped": 0,
            "blacklisted": 0,
            "below_min": 0,
            "duplicates": 0,
            "idempotent_skip": 0,
            "success": 0,
            "failed": 0,
        }
        
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self.running = False

        # Watchdog integration (Phase 5.3)
        self._watchdog = None
        
        logger.warning(
            f"[WEBHOOK] Initialized: {len(self.whale_wallets)} whales, "
            f"min_buy={min_buy_amount} SOL, port={port}"
        )

    def _load_wallets(self, wallets_file: str):
        """Load whale wallets from JSON file."""
        from pathlib import Path
        path = Path(wallets_file)
        
        if not path.exists():
            logger.error(f"[WEBHOOK] Wallets file NOT FOUND: {path.absolute()}")
            return
            
        try:
            with open(path) as f:
                data = json.load(f)
                
            for whale in data.get("whales", []):
                wallet = whale.get("wallet", "")
                if wallet and len(wallet) > 30:
                    self.whale_wallets[wallet] = {
                        "label": whale.get("label", "whale"),
                        "win_rate": whale.get("win_rate", 0.5),
                    }
                    
            logger.info(f"[WEBHOOK] Loaded {len(self.whale_wallets)} whale wallets")
        except Exception as e:
            logger.exception(f"[WEBHOOK] Error loading wallets: {e}")

    def set_callback(self, callback: Callable):
        """Set callback for whale buy signals."""
        self.on_whale_buy = callback
        logger.info("[WEBHOOK] Callback set")

    def set_watchdog(self, watchdog):
        """Set watchdog for channel health monitoring (Phase 5.3)."""
        self._watchdog = watchdog
        logger.info("[WEBHOOK] Watchdog set")

    async def _get_redis_state(self):
        """Get Redis state manager."""
        try:
            from trading.redis_state import get_redis_state
            return await get_redis_state()
        except ImportError:
            return None
        except Exception:
            return None

    async def start(self):
        """Start webhook server."""
        self._app = web.Application()
        self._app.router.add_post('/webhook', self._handle_webhook)
        self._app.router.add_get('/health', self._health_check)
        self._app.router.add_get('/', self._health_check)
        self._app.router.add_get('/stats', self._get_stats)
        
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        
        self.running = True
        
        logger.warning("=" * 70)
        logger.warning("[WEBHOOK] WHALE WEBHOOK SERVER STARTED")
        logger.warning(f"[WEBHOOK] Listening on http://{self.host}:{self.port}/webhook")
        logger.warning(f"[WEBHOOK] Tracking {len(self.whale_wallets)} whale wallets")
        logger.warning(f"[WEBHOOK] Min buy amount: {self.min_buy_amount} SOL")
        logger.warning("[WEBHOOK] Redis idempotency: ENABLED")
        logger.warning("=" * 70)

    async def stop(self):
        """Stop webhook server."""
        self.running = False
        if self._runner:
            await self._runner.cleanup()
        logger.info("[WEBHOOK] Server stopped")

    async def _health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        try:
            state = await self._get_redis_state()
            redis_ok = state and await state.is_connected()
            pos_count = await state.get_positions_count() if state else 0
            
            health = {
                "status": "ok",
                "redis": "connected" if redis_ok else "disconnected",
                "positions": pos_count,
                "whales": len(self.whale_wallets),
                "stats": self._stats,
            }
            return web.json_response(health)
        except Exception as e:
            return web.json_response({"status": "ok", "error": str(e)})

    async def _get_stats(self, request: web.Request) -> web.Response:
        """Get statistics endpoint."""
        return web.json_response(self._stats)

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming webhook from Helius."""
        try:
            self._stats["webhooks_received"] += 1

            # Touch watchdog on any incoming webhook (Phase 5.3)
            if self._watchdog:
                self._watchdog.touch_webhook()

            data = await request.json()
            
            transactions = data if isinstance(data, list) else [data]
            
            for tx in transactions:
                await self._process_transaction(tx)
            
            return web.Response(status=200, text="OK")
            
        except json.JSONDecodeError:
            logger.error("[WEBHOOK] Invalid JSON received")
            return web.Response(status=400, text="Invalid JSON")
        except Exception as e:
            logger.error(f"[WEBHOOK] Error: {e}")
            return web.Response(status=500, text=str(e))

    async def _process_transaction(self, tx: dict):
        """Process single transaction with Redis idempotency."""
        try:
            tx_type = tx.get("type", "UNKNOWN")
            signature = tx.get("signature", "")
            
            if tx_type != "SWAP":
                return
                
            self._stats["swaps_detected"] += 1
            logger.warning(f"[SWAP] Detected swap in tx {signature[:16]}...")
            
            # ==================== REDIS IDEMPOTENCY CHECK ====================
            state = await self._get_redis_state()
            if state and await state.is_connected():
                if await state.is_tx_processed(signature):
                    self._stats["idempotent_skip"] += 1
                    logger.debug(f"[IDEMPOTENT] TX already processed: {signature[:20]}...")
                    return
                # Mark BEFORE processing to prevent race condition
                await state.mark_tx_processed(signature)
            else:
                # Fallback to in-memory
                if signature in self._processed_sigs:
                    self._stats["duplicates"] += 1
                    return
                self._processed_sigs.add(signature)
                if len(self._processed_sigs) > 5000:
                    self._processed_sigs = set(list(self._processed_sigs)[-2500:])
            # ==================== END IDEMPOTENCY ====================
            
            fee_payer = tx.get("feePayer", "")
            if not fee_payer:
                return
                
            whale_info = self.whale_wallets.get(fee_payer)
            if not whale_info:
                logger.warning(f"[SKIP] fee_payer {fee_payer[:16]}... not in whale list")
                return
            
            token_transfers = tx.get("tokenTransfers", [])
            native_transfers = tx.get("nativeTransfers", [])
            
            sol_spent = 0.0
            token_received = None
            token_amount = 0.0
            
            for tt in token_transfers:
                mint = tt.get("mint", "")
                from_addr = tt.get("fromUserAccount", "")
                to_addr = tt.get("toUserAccount", "")
                amount = float(tt.get("tokenAmount", 0))
                
                if mint == SOL_MINT:
                    if from_addr == fee_payer:
                        sol_spent += amount
                    continue
                
                if to_addr == fee_payer and mint not in self.token_blacklist:
                    token_received = mint
                    token_amount = amount
            
            for nt in native_transfers:
                from_addr = nt.get("fromUserAccount", "")
                amount = float(nt.get("amount", 0)) / 1e9
                if from_addr == fee_payer:
                    sol_spent += amount
            
            if not token_received:
                self._stats["sells_skipped"] += 1
                logger.warning(f"[SKIP] SELL detected (whale selling, not buying), whale={whale_info.get('label','?')}, tx={signature[:16]}...")
                return
            
            if sol_spent < self.min_buy_amount:
                self._stats["below_min"] += 1
                logger.info(f"[SKIP] Below min: {sol_spent:.4f} SOL < {self.min_buy_amount} SOL, token={token_received[:16]}...")
                return
            
            if token_received in self.token_blacklist:
                self._stats["blacklisted"] += 1
                return
            
            # Anti-duplicate by token
            if token_received in self._emitted_tokens:
                self._stats["duplicates"] += 1
                return
            self._emitted_tokens.add(token_received)
            
            if len(self._emitted_tokens) > 500:
                self._emitted_tokens = set(list(self._emitted_tokens)[-400:])
            
            # Check if already have position
            if state and await state.is_connected():
                if await state.position_exists(token_received):
                    logger.warning(f"[SKIP] POSITION_EXISTS: Already have position in {token_received[:16]}...")
                    self._stats["duplicates"] += 1
                    return
            
            source = tx.get("source", "unknown")
            platform = self._map_source_to_platform(source)
            
            timestamp = tx.get("timestamp", 0)
            block_time = timestamp if timestamp else None
            
            logger.warning(f"[READY TO EMIT] whale={whale_info.get('label','?')}, token={token_received[:16]}..., sol={sol_spent:.4f}")
            await self._emit_whale_buy(
                wallet=fee_payer,
                token_mint=token_received,
                sol_spent=sol_spent,
                signature=signature,
                platform=platform,
                whale_label=whale_info.get("label", "whale"),
                block_time=block_time,
                description=tx.get("description", ""),
            )
            
        except Exception as e:
            logger.error(f"[WEBHOOK] Process error: {e}")

    def _map_source_to_platform(self, source: str) -> str:
        """Map Helius source to platform name."""
        source_lower = source.lower()
        if "pump" in source_lower:
            return "pump_fun"
        elif "jupiter" in source_lower:
            return "jupiter"
        elif "raydium" in source_lower:
            return "raydium"
        elif "meteora" in source_lower:
            return "meteora"
        elif "orca" in source_lower:
            return "orca"
        elif "bonk" in source_lower:
            return "lets_bonk"
        return source

    async def _emit_whale_buy(
        self,
        wallet: str,
        token_mint: str,
        sol_spent: float,
        signature: str,
        platform: str,
        whale_label: str,
        block_time: int | None,
        description: str,
    ):
        """Emit whale buy signal to callback."""
        token_symbol = ""
        if description:
            parts = description.split(" for ")
            if len(parts) > 1:
                parsed_symbol = parts[-1].split()[-1] if parts[-1] else ""
                # Dont use SOL as symbol - triggers DexScreener fallback
                token_symbol = parsed_symbol if parsed_symbol.upper() != "SOL" else ""
        

        # Fallback to DexScreener if symbol not parsed
        if not token_symbol:
            token_symbol = await _fetch_symbol_dexscreener(token_mint)
            if token_symbol:
                logger.info(f"[SYMBOL] Fetched from DexScreener: {token_symbol}")
        whale_buy = WhaleBuy(
            whale_wallet=wallet,
            token_mint=token_mint,
            amount_sol=sol_spent,
            timestamp=datetime.utcnow(),
            tx_signature=signature,
            whale_label=whale_label,
            platform=platform,
            token_symbol=token_symbol,
            age_seconds=0,
            block_time=block_time,
        )
        
        logger.warning("=" * 70)
        logger.warning("[WEBHOOK] WHALE BUY DETECTED (REAL-TIME)!")
        logger.warning(f"  WHALE:    {whale_label}")
        logger.warning(f"  WALLET:   {wallet}")
        logger.warning(f"  TOKEN:    {token_mint}")
        logger.warning(f"  SYMBOL:   {token_symbol or 'fetching...'}")
        logger.warning(f"  AMOUNT:   {sol_spent:.4f} SOL")
        logger.warning(f"  PLATFORM: {platform}")
        logger.warning(f"  TX:       {signature}")
        logger.warning("=" * 70)
        
        self._stats["buys_emitted"] += 1
        logger.warning(f"[EMIT] Whale BUY signal! {whale_buy.token_symbol} | {whale_buy.token_mint} | {whale_buy.amount_sol:.2f} SOL | whale={whale_buy.whale_label}")
        
        if self.on_whale_buy:
            try:
                logger.warning(f"[CALLBACK] Calling on_whale_buy callback for {whale_buy.token_symbol}")
                asyncio.create_task(self.on_whale_buy(whale_buy))
            except Exception as e:
                logger.error(f"[WEBHOOK] Callback error: {e}")
        else:
            logger.error(f"[WEBHOOK] NO CALLBACK SET! Cannot process whale buy for {whale_buy.token_mint}")

    def get_stats(self) -> dict:
        """Get webhook statistics."""
        return self._stats.copy()

    def get_tracked_wallets(self) -> list[str]:
        """Get list of tracked whale wallets."""
        return list(self.whale_wallets.keys())
