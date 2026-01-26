"""
Whale Poller - polling-based whale monitoring with Weighted RPC selection.

WEIGHTED RPC DISTRIBUTION:
- Alchemy: 60% (most reliable, fastest)
- dRPC: 30% (good backup)
- Chainstack: 10% (rate limit sensitive)

If no paid RPCs available - falls back to public Solana RPC.

This module provides an alternative to WebSocket-based whale_tracker.py
for environments where WebSocket connections are unreliable.

USAGE:
    from monitoring.whale_poller import WhalePoller, WhaleBuy
    
    poller = WhalePoller(
        wallets_file="smart_money_wallets.json",
        min_buy_amount=0.4,  # SOL
        poll_interval=30.0,   # seconds
    )
    poller.set_callback(on_whale_buy_handler)
    await poller.start()
"""

import asyncio
import json
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

# ============================================
# WEIGHTED RPC SELECTOR
# ============================================

@dataclass
class RPCProvider:
    """RPC provider with weight and stats."""
    name: str
    endpoint: str
    weight: int  # Higher weight = more requests
    calls: int = 0
    successes: int = 0
    errors: int = 0
    last_error_time: float = 0
    cooldown_until: float = 0


class WeightedRPCSelector:
    """Weighted round-robin RPC selector with automatic failover.
    
    Distribution:
    - Alchemy: 60% of requests
    - dRPC: 30% of requests  
    - Chainstack: 10% of requests
    
    On error: provider goes into cooldown (30s), requests redistributed.
    """
    
    def __init__(self):
        self.providers: list[RPCProvider] = []
        self._lock = asyncio.Lock()
        self._total_weight = 0
        self._request_count = 0
        
        # Initialize providers from environment
        self._init_providers()
    
    def _init_providers(self):
        """Initialize RPC providers from environment variables."""
        # Alchemy - 60% weight (primary)
        alchemy_rpc = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy_rpc:
            self.providers.append(RPCProvider(
                name="Alchemy",
                endpoint=alchemy_rpc,
                weight=60
            ))
            logger.info("[WEIGHTED-RPC] Alchemy configured (weight: 60)")
        
        # dRPC - 30% weight (secondary)
        drpc_rpc = os.getenv("DRPC_RPC_ENDPOINT")
        if drpc_rpc:
            self.providers.append(RPCProvider(
                name="dRPC",
                endpoint=drpc_rpc,
                weight=30
            ))
            logger.info("[WEIGHTED-RPC] dRPC configured (weight: 30)")
        
        # Chainstack - 10% weight (tertiary, rate-limited)
        chainstack_rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
        if chainstack_rpc and "chainstack" in chainstack_rpc.lower():
            self.providers.append(RPCProvider(
                name="Chainstack",
                endpoint=chainstack_rpc,
                weight=10
            ))
            logger.info("[WEIGHTED-RPC] Chainstack configured (weight: 10)")
        elif chainstack_rpc:
            # Generic endpoint with low weight
            self.providers.append(RPCProvider(
                name="Primary",
                endpoint=chainstack_rpc,
                weight=10
            ))
            logger.info("[WEIGHTED-RPC] Primary RPC configured (weight: 10)")
        
        # Public Solana as fallback (always available, 0 weight unless needed)
        public_rpc = "https://api.mainnet-beta.solana.com"
        self.providers.append(RPCProvider(
            name="PublicSolana",
            endpoint=public_rpc,
            weight=0  # Only used when others fail
        ))
        logger.info("[WEIGHTED-RPC] Public Solana fallback configured")
        
        # Calculate total weight
        self._total_weight = sum(p.weight for p in self.providers if p.weight > 0)
        
        if self._total_weight == 0:
            # No paid providers - use public with weight
            for p in self.providers:
                if p.name == "PublicSolana":
                    p.weight = 100
                    self._total_weight = 100
            logger.warning("[WEIGHTED-RPC] No paid RPCs configured, using public Solana only")
    
    def select_provider(self) -> RPCProvider | None:
        """Select next RPC provider based on weights.
        
        Uses weighted random selection, skipping providers in cooldown.
        """
        now = time.time()
        
        # Filter available providers (not in cooldown)
        available = [
            p for p in self.providers 
            if p.weight > 0 and now >= p.cooldown_until
        ]
        
        if not available:
            # All providers in cooldown - use public fallback
            for p in self.providers:
                if p.name == "PublicSolana":
                    logger.warning("[WEIGHTED-RPC] All providers in cooldown, using public fallback")
                    return p
            return None
        
        # Weighted random selection
        total = sum(p.weight for p in available)
        if total == 0:
            return available[0] if available else None
        
        r = random.uniform(0, total)
        cumulative = 0
        for provider in available:
            cumulative += provider.weight
            if r <= cumulative:
                return provider
        
        return available[-1]
    
    def report_success(self, provider: RPCProvider):
        """Report successful request."""
        provider.calls += 1
        provider.successes += 1
    
    def report_error(self, provider: RPCProvider, is_rate_limit: bool = False):
        """Report failed request, apply cooldown if needed."""
        provider.calls += 1
        provider.errors += 1
        provider.last_error_time = time.time()
        
        # Apply cooldown on error
        if is_rate_limit:
            # Longer cooldown for rate limit (60s)
            provider.cooldown_until = time.time() + 60.0
            logger.warning(f"[WEIGHTED-RPC] {provider.name} rate limited, cooldown 60s")
        else:
            # Short cooldown for other errors (10s)
            provider.cooldown_until = time.time() + 10.0
            logger.warning(f"[WEIGHTED-RPC] {provider.name} error, cooldown 10s")
    
    def get_stats(self) -> dict:
        """Get provider statistics."""
        return {
            "providers": [
                {
                    "name": p.name,
                    "weight": p.weight,
                    "calls": p.calls,
                    "successes": p.successes,
                    "errors": p.errors,
                    "success_rate": p.successes / p.calls * 100 if p.calls > 0 else 0,
                    "in_cooldown": time.time() < p.cooldown_until,
                }
                for p in self.providers
            ],
            "total_requests": sum(p.calls for p in self.providers),
        }


# ============================================
# PLATFORM AND TOKEN DEFINITIONS
# ============================================

# Program IDs for all supported platforms
PLATFORM_PROGRAMS = {
    "pump_fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "lets_bonk": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
    "bags": "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
    "pumpswap": "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP",
    "raydium_amm": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "jupiter": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "jupiter_limit": "jupoNjAxXgZ4rjzxzPMP4oxduvQsQtZzyknqvzYNrNu",
    "orca_whirlpool": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "meteora_dlmm": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "raydium_clmm": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
}

PROGRAM_TO_PLATFORM = {v: k for k, v in PLATFORM_PROGRAMS.items()}

# Tokens to ignore (stablecoins, wrapped SOL, etc.)
TOKEN_BLACKLIST = {
    # USDC
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    # USDT
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    # USDH
    "USDH1SM1ojwWUga67PGrgFWUHibbjqMvuMaDkRJTgkX",
    # USDSw
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",
    # Wrapped SOL
    "So11111111111111111111111111111111111111112",
    # USDC (Wormhole)
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
    # USDT (Wormhole)
    "F3hW1kkYVXhMz9FRV8t3mEfwmLQygF7PtPSsofPCdmXR",
}


# ============================================
# DATA CLASSES
# ============================================

@dataclass
class WhaleBuy:
    """Information about a whale buy transaction."""
    whale_wallet: str
    token_mint: str
    token_symbol: str
    amount_sol: float
    timestamp: datetime
    tx_signature: str
    whale_label: str = "whale"
    block_time: int | None = None
    age_seconds: float = 0
    platform: str = "pump_fun"


# ============================================
# WHALE POLLER
# ============================================

class WhalePoller:
    """Polling-based whale activity monitor with Weighted RPC.
    
    Instead of WebSocket subscriptions, periodically polls whale wallets
    for recent transactions using Weighted RPC distribution.
    
    Advantages over WebSocket:
    - More reliable in unstable network conditions
    - No subscription limits
    - Works with any RPC provider
    
    Disadvantages:
    - Higher latency (depends on poll_interval)
    - More API calls (but distributed via weighted selection)
    """
    
    def __init__(
        self,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.4,
        poll_interval: float = 30.0,
        max_tx_age: float = 600.0,  # 10 minutes max age
        helius_api_key: str | None = None,
    ):
        self.wallets_file = wallets_file
        self.min_buy_amount = min_buy_amount
        self.poll_interval = poll_interval
        self.max_tx_age = max_tx_age
        self.helius_api_key = helius_api_key or os.getenv("HELIUS_API_KEY")
        
        self.whale_wallets: dict[str, dict] = {}
        self.on_whale_buy: Callable | None = None
        self.running = False
        
        self._session: aiohttp.ClientSession | None = None
        self._rpc_selector = WeightedRPCSelector()
        
        # Deduplication
        self._processed_txs: set[str] = set()
        self._emitted_tokens: set[str] = set()
        
        # TX cache for quota optimization
        self._tx_cache: dict[str, tuple[dict, float]] = {}
        self._cache_ttl = 300.0  # 5 min cache
        
        # Metrics
        self._metrics = {
            "polls": 0,
            "txs_checked": 0,
            "whale_buys_detected": 0,
            "signals_emitted": 0,
            "helius_calls": 0,
            "rpc_calls": 0,
            "cache_hits": 0,
            "start_time": time.time(),
        }
        
        # Last poll time per wallet (for incremental polling)
        self._last_poll: dict[str, float] = {}
        
        self._load_wallets()
        
        logger.info(
            f"[WHALE-POLLER] Initialized: {len(self.whale_wallets)} wallets, "
            f"min_buy={min_buy_amount} SOL, poll_interval={poll_interval}s"
        )
    
    def _load_wallets(self):
        """Load whale wallets from JSON file."""
        path = Path(self.wallets_file)
        if not path.exists():
            logger.error(f"[WHALE-POLLER] Wallets file not found: {path}")
            return
        
        try:
            with open(path) as f:
                data = json.load(f)
            
            for whale in data.get("whales", []):
                wallet = whale.get("wallet", "")
                if wallet:
                    self.whale_wallets[wallet] = {
                        "label": whale.get("label", "whale"),
                        "win_rate": whale.get("win_rate", 0.5),
                    }
            
            logger.info(f"[WHALE-POLLER] Loaded {len(self.whale_wallets)} whale wallets")
            
        except Exception as e:
            logger.error(f"[WHALE-POLLER] Error loading wallets: {e}")
    
    def set_callback(self, callback: Callable):
        """Set callback for whale buy signals."""
        self.on_whale_buy = callback
    
    async def start(self):
        """Start polling whale wallets."""
        if not self.whale_wallets:
            logger.error("[WHALE-POLLER] No whale wallets to poll")
            return
        
        self.running = True
        self._session = aiohttp.ClientSession()
        
        logger.warning(
            f"[WHALE-POLLER] STARTED - polling {len(self.whale_wallets)} wallets "
            f"every {self.poll_interval}s"
        )
        
        # Log RPC configuration
        stats = self._rpc_selector.get_stats()
        for p in stats["providers"]:
            if p["weight"] > 0:
                logger.info(f"[WHALE-POLLER] RPC: {p['name']} (weight: {p['weight']}%)")
        
        try:
            while self.running:
                await self._poll_all_wallets()
                self._metrics["polls"] += 1
                
                # Log stats every 10 polls
                if self._metrics["polls"] % 10 == 0:
                    self._log_stats()
                
                await asyncio.sleep(self.poll_interval)
                
        except asyncio.CancelledError:
            logger.info("[WHALE-POLLER] Cancelled")
        except Exception as e:
            logger.error(f"[WHALE-POLLER] Error: {e}")
        finally:
            await self.stop()
    
    async def stop(self):
        """Stop polling."""
        self.running = False
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("[WHALE-POLLER] Stopped")
    
    async def _poll_all_wallets(self):
        """Poll all whale wallets for recent transactions."""
        # Batch wallets into groups to avoid overwhelming RPC
        wallet_list = list(self.whale_wallets.keys())
        batch_size = 5  # Poll 5 wallets concurrently
        
        for i in range(0, len(wallet_list), batch_size):
            batch = wallet_list[i:i + batch_size]
            tasks = [self._poll_wallet(wallet) for wallet in batch]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # Small delay between batches
            if i + batch_size < len(wallet_list):
                await asyncio.sleep(0.5)
    
    async def _poll_wallet(self, wallet: str):
        """Poll a single wallet for recent buy transactions."""
        try:
            # Get recent signatures for this wallet
            signatures = await self._get_recent_signatures(wallet)
            if not signatures:
                return
            
            # Check each signature
            for sig in signatures[:10]:  # Limit to 10 most recent
                if sig in self._processed_txs:
                    continue
                
                self._processed_txs.add(sig)
                self._metrics["txs_checked"] += 1
                
                # Get transaction details
                tx = await self._get_transaction(sig)
                if tx:
                    await self._process_transaction(tx, sig, wallet)
            
            # Cleanup old processed TXs
            if len(self._processed_txs) > 5000:
                self._processed_txs = set(list(self._processed_txs)[-2500:])
                
        except Exception as e:
            logger.debug(f"[WHALE-POLLER] Error polling {wallet[:8]}...: {e}")
    
    async def _get_recent_signatures(self, wallet: str) -> list[str]:
        """Get recent transaction signatures for a wallet."""
        provider = self._rpc_selector.select_provider()
        if not provider:
            return []
        
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [
                    wallet,
                    {"limit": 15, "commitment": "confirmed"}
                ]
            }
            
            async with self._session.post(
                provider.endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._rpc_selector.report_success(provider)
                    self._metrics["rpc_calls"] += 1
                    
                    result = data.get("result", [])
                    # Filter by time (only recent transactions)
                    now = time.time()
                    signatures = []
                    for tx in result:
                        block_time = tx.get("blockTime", 0)
                        if block_time and (now - block_time) < self.max_tx_age:
                            signatures.append(tx.get("signature"))
                    return signatures
                    
                elif resp.status == 429:
                    self._rpc_selector.report_error(provider, is_rate_limit=True)
                    return []
                else:
                    self._rpc_selector.report_error(provider)
                    return []
                    
        except Exception as e:
            self._rpc_selector.report_error(provider)
            logger.debug(f"[WHALE-POLLER] Error getting signatures: {e}")
            return []
    
    async def _get_transaction(self, signature: str) -> dict | None:
        """Get transaction details using Weighted RPC."""
        # Check cache first
        if signature in self._tx_cache:
            cached, ts = self._tx_cache[signature]
            if time.time() - ts < self._cache_ttl:
                self._metrics["cache_hits"] += 1
                return cached
        
        # Try Helius Enhanced API first (best for parsed data)
        if self.helius_api_key:
            tx = await self._get_tx_helius(signature)
            if tx:
                self._tx_cache[signature] = (tx, time.time())
                return tx
        
        # Fall back to weighted RPC
        provider = self._rpc_selector.select_provider()
        if not provider:
            return None
        
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ]
            }
            
            async with self._session.post(
                provider.endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._rpc_selector.report_success(provider)
                    self._metrics["rpc_calls"] += 1
                    
                    result = data.get("result")
                    if result:
                        self._tx_cache[signature] = (result, time.time())
                        # Cleanup cache if too large
                        if len(self._tx_cache) > 1000:
                            oldest = min(self._tx_cache, key=lambda k: self._tx_cache[k][1])
                            del self._tx_cache[oldest]
                    return result
                    
                elif resp.status == 429:
                    self._rpc_selector.report_error(provider, is_rate_limit=True)
                    return None
                else:
                    self._rpc_selector.report_error(provider)
                    return None
                    
        except Exception as e:
            self._rpc_selector.report_error(provider)
            logger.debug(f"[WHALE-POLLER] Error getting TX: {e}")
            return None
    
    async def _get_tx_helius(self, signature: str) -> dict | None:
        """Get transaction from Helius Enhanced API."""
        url = f"https://api.helius.xyz/v0/transactions?api-key={self.helius_api_key}"
        
        try:
            async with self._session.post(
                url,
                json={"transactions": [signature]},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._metrics["helius_calls"] += 1
                    if data and len(data) > 0:
                        return data[0]
                elif resp.status == 429:
                    logger.debug("[WHALE-POLLER] Helius rate limited")
                return None
                
        except Exception as e:
            logger.debug(f"[WHALE-POLLER] Helius error: {e}")
            return None
    
    async def _process_transaction(self, tx: dict, signature: str, wallet: str):
        """Process a transaction to detect whale buys."""
        try:
            whale_info = self.whale_wallets.get(wallet, {})
            
            # Check if this is a Helius format or regular RPC format
            if "feePayer" in tx:
                # Helius format
                await self._process_helius_tx(tx, wallet, whale_info)
            else:
                # Regular RPC format
                await self._process_rpc_tx(tx, signature, wallet, whale_info)
                
        except Exception as e:
            logger.debug(f"[WHALE-POLLER] Error processing TX: {e}")
    
    async def _process_helius_tx(self, tx: dict, wallet: str, whale_info: dict):
        """Process Helius-formatted transaction."""
        fee_payer = tx.get("feePayer", "")
        if fee_payer != wallet:
            return
        
        signature = tx.get("signature", "")
        block_time = tx.get("timestamp")
        
        # Calculate SOL spent
        sol_spent = 0
        token_mint = None
        
        for transfer in tx.get("nativeTransfers", []):
            if transfer.get("fromUserAccount") == wallet:
                sol_spent += transfer.get("amount", 0) / 1e9
        
        for transfer in tx.get("tokenTransfers", []):
            if transfer.get("toUserAccount") == wallet:
                token_mint = transfer.get("mint")
                break
        
        # Detect platform from instructions
        platform = "pump_fun"
        for program in tx.get("accountData", []):
            program_id = program.get("account", "")
            if program_id in PROGRAM_TO_PLATFORM:
                platform = PROGRAM_TO_PLATFORM[program_id]
                break
        
        if sol_spent >= self.min_buy_amount and token_mint:
            self._metrics["whale_buys_detected"] += 1
            await self._emit_whale_buy(
                wallet=wallet,
                token_mint=token_mint,
                sol_spent=sol_spent,
                signature=signature,
                whale_label=whale_info.get("label", "whale"),
                block_time=block_time,
                platform=platform,
            )
    
    async def _process_rpc_tx(self, tx: dict, signature: str, wallet: str, whale_info: dict):
        """Process regular RPC-formatted transaction."""
        message = tx.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", [])
        
        if not account_keys:
            return
        
        # Verify fee payer matches wallet
        first_key = account_keys[0]
        fee_payer = first_key.get("pubkey", "") if isinstance(first_key, dict) else str(first_key)
        
        if fee_payer != wallet:
            return
        
        meta = tx.get("meta", {})
        block_time = tx.get("blockTime")
        
        # Calculate SOL spent
        pre = meta.get("preBalances", [])
        post = meta.get("postBalances", [])
        sol_spent = (pre[0] - post[0]) / 1e9 if pre and post else 0
        
        # Find token mint
        token_mint = None
        for bal in meta.get("postTokenBalances", []):
            if bal.get("owner") == wallet:
                token_mint = bal.get("mint")
                break
        
        # Detect platform from logs
        platform = "pump_fun"
        logs = meta.get("logMessages", [])
        for log in logs:
            for program_id, plat in PROGRAM_TO_PLATFORM.items():
                if program_id in log:
                    platform = plat
                    break
        
        if sol_spent >= self.min_buy_amount and token_mint:
            self._metrics["whale_buys_detected"] += 1
            await self._emit_whale_buy(
                wallet=wallet,
                token_mint=token_mint,
                sol_spent=sol_spent,
                signature=signature,
                whale_label=whale_info.get("label", "whale"),
                block_time=block_time,
                platform=platform,
            )
    
    async def _emit_whale_buy(
        self,
        wallet: str,
        token_mint: str,
        sol_spent: float,
        signature: str,
        whale_label: str,
        block_time: int | None = None,
        platform: str = "pump_fun",
    ):
        """Emit whale buy signal."""
        # Skip blacklisted tokens
        if token_mint in TOKEN_BLACKLIST:
            logger.debug(f"[WHALE-POLLER] Skip blacklisted token: {token_mint[:8]}...")
            return
        
        # Anti-duplicate: skip already emitted tokens
        if token_mint in self._emitted_tokens:
            logger.debug(f"[WHALE-POLLER] Skip duplicate: {token_mint[:8]}...")
            return
        
        # Check age
        now = time.time()
        age_seconds = 0.0
        if block_time:
            age_seconds = now - block_time
            if age_seconds > self.max_tx_age:
                logger.debug(f"[WHALE-POLLER] Skip old TX: {age_seconds:.0f}s ago")
                return
        
        # Mark as emitted
        self._emitted_tokens.add(token_mint)
        if len(self._emitted_tokens) > 500:
            self._emitted_tokens = set(list(self._emitted_tokens)[-250:])
        
        whale_buy = WhaleBuy(
            whale_wallet=wallet,
            token_mint=token_mint,
            token_symbol="TOKEN",
            amount_sol=sol_spent,
            timestamp=datetime.utcnow(),
            tx_signature=signature,
            whale_label=whale_label,
            block_time=block_time,
            age_seconds=age_seconds,
            platform=platform,
        )
        
        # Log the detection
        logger.warning("=" * 70)
        logger.warning("[WHALE-POLLER] BUY DETECTED")
        logger.warning(f"  WHALE:     {whale_label}")
        logger.warning(f"  WALLET:    {wallet}")
        logger.warning(f"  TOKEN:     {token_mint}")
        logger.warning(f"  AMOUNT:    {sol_spent:.4f} SOL")
        logger.warning(f"  PLATFORM:  {platform}")
        logger.warning(f"  AGE:       {age_seconds:.1f}s ago")
        logger.warning(f"  TX:        {signature}")
        logger.warning("=" * 70)
        
        self._metrics["signals_emitted"] += 1
        
        if self.on_whale_buy:
            await self.on_whale_buy(whale_buy)
    
    def _log_stats(self):
        """Log polling statistics."""
        m = self._metrics
        elapsed = time.time() - m["start_time"]
        hours = elapsed / 3600
        
        logger.info(
            f"[WHALE-POLLER STATS] Polls: {m['polls']}, TXs: {m['txs_checked']}, "
            f"Buys: {m['whale_buys_detected']}, Signals: {m['signals_emitted']}"
        )
        logger.info(
            f"[WHALE-POLLER STATS] Helius: {m['helius_calls']}, RPC: {m['rpc_calls']}, "
            f"Cache: {m['cache_hits']}"
        )
        
        if hours > 0:
            hourly_rate = (m['helius_calls'] + m['rpc_calls']) / hours
            logger.info(f"[WHALE-POLLER STATS] API rate: {hourly_rate:.0f}/hr")
        
        # Log RPC distribution
        rpc_stats = self._rpc_selector.get_stats()
        for p in rpc_stats["providers"]:
            if p["calls"] > 0:
                logger.info(
                    f"[WHALE-POLLER RPC] {p['name']}: {p['calls']} calls, "
                    f"{p['success_rate']:.1f}% success"
                )
    
    def get_metrics(self) -> dict:
        """Get current metrics."""
        return {
            **self._metrics,
            "rpc_stats": self._rpc_selector.get_stats(),
        }
