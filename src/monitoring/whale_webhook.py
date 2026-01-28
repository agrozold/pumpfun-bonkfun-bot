"""
Whale Webhook Receiver - Real-time whale tracking via Helius Webhooks.

Replaces WhalePoller (30s polling) with instant webhook notifications.
Receives SWAP events from Helius for 99 whale wallets.

Compatible with existing WhaleBuy interface used by UniversalTrader._on_whale_buy()
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# BLACKLIST - stablecoins and wrapped tokens (skip these)
TOKEN_BLACKLIST = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj", # stSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", # jitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",  # bSOL
}

# SOL mint for detection
SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class WhaleBuy:
    """Whale buy signal - compatible with WhalePoller interface."""
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
    """
    Receives real-time whale SWAP notifications via Helius Webhooks.
    
    Replaces WhalePoller's 30s polling with instant webhook delivery.
    Parses enhanced transaction format from Helius.
    """
    
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
        
        # Load whale wallets for labels
        self.whale_wallets: dict[str, dict] = {}
        self._load_wallets(wallets_file)
        
        # Merge blacklist
        self.token_blacklist = TOKEN_BLACKLIST.copy()
        if stablecoin_filter:
            self.token_blacklist.update(set(stablecoin_filter))
        
        # Callback for whale buy signals
        self.on_whale_buy: Optional[Callable] = None
        
        # Anti-duplicate
        self._processed_sigs: set[str] = set()
        self._emitted_tokens: set[str] = set()
        
        # Stats
        self._stats = {
            "webhooks_received": 0,
            "swaps_detected": 0,
            "buys_emitted": 0,
            "sells_skipped": 0,
            "blacklisted": 0,
            "below_min": 0,
            "duplicates": 0,
            "success": 0,
            "failed": 0,
        }
        
        # Server
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self.running = False
        
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
        """Set callback for whale buy signals (same as WhalePoller)."""
        self.on_whale_buy = callback
        logger.info("[WEBHOOK] Callback set")

    async def start(self):
        """Start webhook server."""
        self._app = web.Application()
        self._app.router.add_post('/webhook', self._handle_webhook)
        self._app.router.add_get('/health', self._health_check)
        self._app.router.add_get('/', self._health_check)
        
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
        logger.warning("=" * 70)

    async def stop(self):
        """Stop webhook server."""
        self.running = False
        if self._runner:
            await self._runner.cleanup()
        logger.info("[WEBHOOK] Server stopped")

    async def _health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.Response(text="OK", status=200)

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming webhook from Helius."""
        try:
            self._stats["webhooks_received"] += 1
            data = await request.json()
            
            # Helius sends array of transactions
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
        """Process single transaction from Helius webhook."""
        try:
            tx_type = tx.get("type", "UNKNOWN")
            signature = tx.get("signature", "")
            
            # Only process SWAP transactions
            if tx_type != "SWAP":
                logger.info(f"[FILTER] tx_type={tx_type} != SWAP, skipping")
                return
                
            self._stats["swaps_detected"] += 1
            
            # Skip already processed
            if signature in self._processed_sigs:
                self._stats["duplicates"] += 1
                return
            self._processed_sigs.add(signature)
            
            # Cleanup old sigs
            if len(self._processed_sigs) > 5000:
                self._processed_sigs = set(list(self._processed_sigs)[-2500:])
            
            # Get fee payer (whale wallet)
            fee_payer = tx.get("feePayer", "")
            if not fee_payer:
                return
                
            # Check if this is one of our tracked whales
            whale_info = self.whale_wallets.get(fee_payer)
            if not whale_info:
                logger.info(f"[FILTER] NOT WHALE: {fee_payer[:20]}...")
                # Not our whale, skip
                return
            
            # Parse token transfers to detect BUY vs SELL
            token_transfers = tx.get("tokenTransfers", [])
            native_transfers = tx.get("nativeTransfers", [])
            
            # Find: SOL out, Token in = BUY
            # Find: Token out, SOL in = SELL
            sol_spent = 0.0
            token_received = None
            token_amount = 0.0
            
            for tt in token_transfers:
                mint = tt.get("mint", "")
                from_addr = tt.get("fromUserAccount", "")
                to_addr = tt.get("toUserAccount", "")
                amount = float(tt.get("tokenAmount", 0))
                
                # Skip SOL (wrapped)
                if mint == SOL_MINT:
                    if from_addr == fee_payer:
                        sol_spent += amount
                    continue
                
                # Token received by whale = potential BUY
                if to_addr == fee_payer and mint not in self.token_blacklist:
                    token_received = mint
                    token_amount = amount
            
            # Also check native transfers for SOL spent
            for nt in native_transfers:
                from_addr = nt.get("fromUserAccount", "")
                amount = float(nt.get("amount", 0)) / 1e9  # lamports to SOL
                if from_addr == fee_payer:
                    sol_spent += amount
            
            # Is this a BUY? (whale spent SOL, received token)
            if not token_received:
                logger.info(f"[FILTER] No token_received (SELL)")
                self._stats["sells_skipped"] += 1
                logger.debug(f"[WEBHOOK] Skipping SELL tx: {signature[:20]}...")
                return
            
            # Check minimum SOL spent
            if sol_spent < self.min_buy_amount:
                logger.info(f"[FILTER] Below min: {sol_spent:.3f} < {self.min_buy_amount} SOL")
                self._stats["below_min"] += 1
                logger.debug(f"[WEBHOOK] Below min: {sol_spent:.3f} < {self.min_buy_amount} SOL")
                return
            
            # Check blacklist
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
            
            # Detect platform from source
            source = tx.get("source", "unknown")
            platform = self._map_source_to_platform(source)
            
            # Get timestamp
            timestamp = tx.get("timestamp", 0)
            block_time = timestamp if timestamp else None
            
            # Emit whale buy signal!
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
        # Try to extract symbol from description
        token_symbol = ""
        if description:
            # Helius description format: "X swapped Y SOL for Z TOKEN"
            parts = description.split(" for ")
            if len(parts) > 1:
                token_symbol = parts[-1].split()[-1] if parts[-1] else ""
        
        whale_buy = WhaleBuy(
            whale_wallet=wallet,
            token_mint=token_mint,
            amount_sol=sol_spent,
            timestamp=datetime.utcnow(),
            tx_signature=signature,
            whale_label=whale_label,
            platform=platform,
            token_symbol=token_symbol,
            age_seconds=0,  # Real-time!
            block_time=block_time,
        )
        
        logger.warning("=" * 70)
        logger.warning("[WEBHOOK] 🐋 WHALE BUY DETECTED (REAL-TIME)!")
        logger.warning(f"  WHALE:    {whale_label}")
        logger.warning(f"  WALLET:   {wallet}")
        logger.warning(f"  TOKEN:    {token_mint}")
        logger.warning(f"  SYMBOL:   {token_symbol or 'fetching...'}")
        logger.warning(f"  AMOUNT:   {sol_spent:.4f} SOL")
        logger.warning(f"  PLATFORM: {platform}")
        logger.warning(f"  TX:       {signature}")
        logger.warning("=" * 70)
        
        self._stats["buys_emitted"] += 1
        
        if self.on_whale_buy:
            try:
                asyncio.create_task(self.on_whale_buy(whale_buy))
            except Exception as e:
                logger.error(f"[WEBHOOK] Callback error: {e}")

    def get_stats(self) -> dict:
        """Get webhook statistics."""
        return self._stats.copy()

    def get_tracked_wallets(self) -> list[str]:
        """Get list of tracked whale wallets."""
        return list(self.whale_wallets.keys())
