"""
Whale Poller - HTTP polling-based whale tracker.

CRITICAL: Solana's logsSubscribe with 'mentions' filter does NOT work for wallet addresses!
It only works for Program IDs. This module uses HTTP polling instead.

Polling approach:
- Every poll_interval seconds (default 30s)
- For each whale wallet:
  1. getSignaturesForAddress(wallet, limit=5)
  2. For new signatures: getTransaction(sig)
  3. Check if BUY (SOL spent, tokens received)
  4. Check min_buy_amount (default 0.4 SOL)
  5. Filter stablecoins
  6. Emit WhaleBuy signal

Uses weighted round-robin RPC selection to respect rate limits:
- Alchemy: 60% (generous limits)
- dRPC: 30% (good fallback)
- Chainstack: 10% (preserve 3M/month quota)
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Platform Program IDs
PLATFORM_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump_fun",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "lets_bonk",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "bags",
    "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP": "pumpswap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
}

# BLACKLIST - stablecoins and wrapped tokens (skip these)
TOKEN_BLACKLIST = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj", # stSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", # jitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",  # bSOL
    "EjmyN6qEC1Tf1JxiG1ae7UTJhUxSwk1TCCi3A6ca61U3", # USD1
    "USDH1SM1ojwWUga67PGrgFWUHibbjqMvuMaDkRJTgkX",   # USDH
}


@dataclass
class WhaleBuy:
    """Whale buy signal - compatible with existing WhaleTracker interface."""
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


class WeightedRPCSelector:
    """Weighted round-robin RPC selector for API quota management.
    
    Distributes requests based on weights to respect different provider limits:
    - Higher weight = more requests to that provider
    - Alchemy (60): generous limits
    - dRPC (30): good fallback
    - Chainstack (10): preserve 3M/month quota
    """
    
    def __init__(self):
        self.endpoints: list[str] = []
        self.weights: list[int] = []
        self.current_weights: list[int] = []
        self.names: list[str] = []
        
    def add(self, url: str, weight: int, name: str = ""):
        """Add an RPC endpoint with its weight."""
        self.endpoints.append(url)
        self.weights.append(weight)
        self.current_weights.append(0)
        self.names.append(name or url[:30])
        
    def next(self) -> str:
        """Get next RPC endpoint using weighted round-robin."""
        if not self.endpoints:
            raise ValueError("No RPC endpoints configured")
        if len(self.endpoints) == 1:
            return self.endpoints[0]
            
        # Weighted round-robin algorithm
        total = sum(self.weights)
        for i in range(len(self.endpoints)):
            self.current_weights[i] += self.weights[i]
        
        max_idx = self.current_weights.index(max(self.current_weights))
        self.current_weights[max_idx] -= total
        
        return self.endpoints[max_idx]
    
    def __len__(self) -> int:
        return len(self.endpoints)
    
    def info(self) -> str:
        """Get info string for logging."""
        parts = [f"{n}:{w}" for n, w in zip(self.names, self.weights)]
        return ", ".join(parts)


class WhalePoller:
    """HTTP polling-based whale tracker.
    
    Replaces WSS-based WhaleTracker because logsSubscribe doesn't work
    for wallet address mentions (only Program IDs).
    
    Features:
    - Polls whale wallets via getSignaturesForAddress
    - Weighted RPC selection to respect rate limits
    - Batch processing (10 wallets at a time)
    - Stablecoin filtering
    - Platform detection
    - Compatible with existing WhaleBuy interface
    """
    
    def __init__(
        self,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.4,
        poll_interval: float = 30.0,
        max_tx_age: float = 600.0,
        stablecoin_filter: list | None = None,
    ):
        """Initialize WhalePoller.
        
        Args:
            wallets_file: Path to JSON file with whale wallets
            min_buy_amount: Minimum SOL amount to trigger copy (default 0.4)
            poll_interval: Seconds between polling cycles (default 30)
            max_tx_age: Max age of transactions to process in seconds (default 600 = 10 min)
            stablecoin_filter: Additional tokens to filter (merged with TOKEN_BLACKLIST)
        """
        self.wallets_file = wallets_file
        self.min_buy_amount = min_buy_amount
        self.poll_interval = poll_interval
        self.max_tx_age = max_tx_age

        # Merge stablecoin filter with blacklist
        self.token_blacklist = TOKEN_BLACKLIST.copy()
        if stablecoin_filter:
            self.token_blacklist.update(stablecoin_filter)

        # Weighted RPC selector - Chainstack gets minimal traffic to preserve quota
        self._rpc = WeightedRPCSelector()
        self._setup_rpc_endpoints()

        # Whale wallets
        self.whale_wallets: dict[str, dict] = {}
        self._load_wallets()
        
        # Callback for whale buy signals
        self.on_whale_buy: Optional[Callable] = None
        
        # State
        self.running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._processed_sigs: set[str] = set()
        self._emitted_tokens: set[str] = set()
        
        # Stats
        self._stats = {
            "signals": 0,
            "success": 0, 
            "failed": 0,
            "skipped": 0,
            "polls": 0,
            "rpc_calls": 0,
            "rpc_errors": 0,
        }
        
        logger.warning(
            f"[POLLER] Initialized: {len(self.whale_wallets)} whales, "
            f"min_buy={min_buy_amount} SOL, poll={poll_interval}s, "
            f"RPC: {self._rpc.info()}"
        )

    def _setup_rpc_endpoints(self):
        """Setup RPC endpoints with weights."""
        # Alchemy - generous limits, use most
        alchemy = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy:
            self._rpc.add(alchemy, weight=60, name="Alchemy")
            
        # dRPC - good fallback
        drpc = os.getenv("DRPC_RPC_ENDPOINT")
        if drpc:
            self._rpc.add(drpc, weight=30, name="dRPC")
            
        # Chainstack - preserve quota (3M/month)
        chainstack = os.getenv("CHAINSTACK_RPC_ENDPOINT")
        if chainstack:
            self._rpc.add(chainstack, weight=10, name="Chainstack")
            
        # Fallback to public if nothing else
        if len(self._rpc) == 0:
            self._rpc.add("https://api.mainnet-beta.solana.com", weight=100, name="Public")
            logger.warning("[POLLER] No RPC endpoints in env, using public Solana RPC!")

    def _load_wallets(self):
        """Load whale wallets from JSON file."""
        path = Path(self.wallets_file)
        logger.info(f"[POLLER] Loading wallets from: {path.absolute()}")
        
        if not path.exists():
            logger.error(f"[POLLER] Wallets file NOT FOUND: {path.absolute()}")
            return
            
        try:
            with open(path) as f:
                data = json.load(f)
                
            whales_list = data.get("whales", [])
            for whale in whales_list:
                wallet = whale.get("wallet", "")
                if wallet and len(wallet) > 30:  # Basic validation
                    self.whale_wallets[wallet] = {
                        "label": whale.get("label", "whale"),
                        "win_rate": whale.get("win_rate", 0.5),
                        "source": whale.get("source", "manual"),
                    }
                    
            logger.warning(f"[POLLER] Loaded {len(self.whale_wallets)} whale wallets")
            
            # Log first 5 wallets
            for i, (w, info) in enumerate(list(self.whale_wallets.items())[:5]):
                logger.info(f"[POLLER] Whale {i+1}: {w[:16]}... | {info.get('label')}")
                
        except json.JSONDecodeError as e:
            logger.error(f"[POLLER] JSON parse error: {e}")
        except Exception as e:
            logger.exception(f"[POLLER] Error loading wallets: {e}")

    def set_callback(self, callback: Callable):
        """Set callback for whale buy signals."""
        self.on_whale_buy = callback
        logger.info("[POLLER] Callback set")

    async def start(self):
        """Start polling whale wallets."""
        if not self.whale_wallets:
            logger.error("[POLLER] No whale wallets loaded! Cannot start.")
            return
            
        self.running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        
        logger.warning("=" * 70)
        logger.warning("[POLLER] WHALE POLLER STARTED")
        logger.warning(f"[POLLER] Tracking {len(self.whale_wallets)} wallets")
        logger.warning(f"[POLLER] Poll interval: {self.poll_interval}s")
        logger.warning(f"[POLLER] Min buy amount: {self.min_buy_amount} SOL")
        logger.warning(f"[POLLER] RPC endpoints: {self._rpc.info()}")
        logger.warning("=" * 70)
        
        while self.running:
            try:
                await self._poll_cycle()
                self._stats["polls"] += 1
                
                # Log stats every 10 polls
                if self._stats["polls"] % 10 == 0:
                    self._log_stats()
                    
            except Exception as e:
                logger.error(f"[POLLER] Poll cycle error: {e}")
                
            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        """Stop polling."""
        self.running = False
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("[POLLER] Stopped")

    def _log_stats(self):
        """Log polling statistics."""
        s = self._stats
        logger.info(
            f"[POLLER STATS] polls={s['polls']}, signals={s['signals']}, "
            f"rpc_calls={s['rpc_calls']}, errors={s['rpc_errors']}, "
            f"processed_sigs={len(self._processed_sigs)}"
        )

    async def _poll_cycle(self):
        """Single polling cycle - check all whale wallets."""
        logger.warning("[POLLER] Starting poll cycle...")
        wallets = list(self.whale_wallets.keys())
        
        # Process in batches of 10 to avoid overwhelming RPC
        batch_size = 10
        for i in range(0, len(wallets), batch_size):
            batch = wallets[i:i + batch_size]
            tasks = [self._check_wallet(w) for w in batch]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # Small delay between batches
            if i + batch_size < len(wallets):
                await asyncio.sleep(0.5)

    async def _check_wallet(self, wallet: str):
        """Check single wallet for new transactions."""
        try:
            rpc = self._rpc.next()
            self._stats["rpc_calls"] += 1
            
            # Get recent signatures
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [wallet, {"limit": 5}]
            }
            
            async with self._session.post(rpc, json=payload) as resp:
                if resp.status != 200:
                    self._stats["rpc_errors"] += 1
                    return
                    
                data = await resp.json()
                
            sigs = data.get("result", [])
            if not sigs:
                return
                
            now = int(time.time())
            
            for sig_info in sigs:
                sig = sig_info.get("signature")
                if not sig:
                    continue
                    
                # Skip already processed
                if sig in self._processed_sigs:
                    continue
                    
                # Skip failed transactions
                if sig_info.get("err"):
                    continue
                    
                # Check age
                block_time = sig_info.get("blockTime", 0)
                age = now - block_time if block_time else 9999
                if age > self.max_tx_age:
                    continue
                    
                # Mark as processed
                self._processed_sigs.add(sig)
                
                # Cleanup old processed sigs (keep last 5000)
                if len(self._processed_sigs) > 5000:
                    self._processed_sigs = set(list(self._processed_sigs)[-2500:])
                    
                # Process the transaction
                await self._process_transaction(wallet, sig, age)
                
        except asyncio.TimeoutError:
            self._stats["rpc_errors"] += 1
            logger.debug(f"[POLLER] Timeout checking {wallet[:16]}...")
        except Exception as e:
            self._stats["rpc_errors"] += 1
            logger.debug(f"[POLLER] Error checking {wallet[:16]}...: {e}")

    async def _process_transaction(self, wallet: str, sig: str, age: float):
        """Process a single transaction to check if it's a qualifying whale buy."""
        try:
            rpc = self._rpc.next()
            self._stats["rpc_calls"] += 1
            
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ]
            }
            
            async with self._session.post(rpc, json=payload) as resp:
                if resp.status != 200:
                    self._stats["rpc_errors"] += 1
                    return
                    
                data = await resp.json()
                
            tx = data.get("result")
            if not tx:
                return
                
            meta = tx.get("meta", {})
            
            # Skip failed transactions
            if meta.get("err"):
                return
                
            logs = meta.get("logMessages", [])
            
            # Detect platform from logs
            platform = "unknown"
            for prog, plat in PLATFORM_PROGRAMS.items():
                if any(prog in log for log in logs):
                    platform = plat
                    break
                    
            # Calculate SOL spent (first account is fee payer)
            pre = meta.get("preBalances", [])
            post = meta.get("postBalances", [])
            sol_spent = (pre[0] - post[0]) / 1e9 if pre and post else 0
            
            # Check minimum buy amount
            if sol_spent < self.min_buy_amount:
                logger.debug(f"[POLLER] Skip small tx: {sol_spent:.3f} < {self.min_buy_amount} SOL")
                return
                
            # Find token mint received
            token_mint = None
            for bal in meta.get("postTokenBalances", []):
                if bal.get("owner") == wallet:
                    token_mint = bal.get("mint")
                    break
                    
            if not token_mint:
                logger.debug("[POLLER] Skip tx: no token received")
                return
                
            # Check blacklist
            if token_mint in self.token_blacklist:
                logger.debug(f"[POLLER] Skip blacklisted token: {token_mint[:16]}...")
                return
                
            # Anti-duplicate: skip if we already emitted signal for this token
            if token_mint in self._emitted_tokens:
                logger.debug(f"[POLLER] Skip duplicate token: {token_mint[:16]}...")
                return
                
            # Emit whale buy signal!
            await self._emit_whale_buy(
                wallet=wallet,
                token_mint=token_mint,
                sol_spent=sol_spent,
                signature=sig,
                platform=platform,
                age=age,
                block_time=tx.get("blockTime"),
            )
            
        except asyncio.TimeoutError:
            self._stats["rpc_errors"] += 1
        except Exception as e:
            self._stats["rpc_errors"] += 1
            logger.debug(f"[POLLER] Error processing tx {sig[:16]}...: {e}")

    async def _emit_whale_buy(
        self,
        wallet: str,
        token_mint: str,
        sol_spent: float,
        signature: str,
        platform: str,
        age: float,
        block_time: int | None,
    ):
        """Emit whale buy signal."""
        # Mark token as emitted
        self._emitted_tokens.add(token_mint)
        
        # Cleanup old emitted tokens (keep last 500)
        if len(self._emitted_tokens) > 500:
            self._emitted_tokens = set(list(self._emitted_tokens)[-400:])
            
        whale_info = self.whale_wallets.get(wallet, {})
        whale_label = whale_info.get("label", "whale")
        
        whale_buy = WhaleBuy(
            whale_wallet=wallet,
            token_mint=token_mint,
            amount_sol=sol_spent,
            timestamp=datetime.utcnow(),
            tx_signature=signature,
            whale_label=whale_label,
            platform=platform,
            age_seconds=age,
            block_time=block_time,
        )
        
        # Log the signal
        logger.warning("=" * 70)
        logger.warning("[POLLER] WHALE BUY DETECTED!")
        logger.warning(f"  WHALE:    {whale_label}")
        logger.warning(f"  WALLET:   {wallet}")
        logger.warning(f"  TOKEN:    {token_mint}")
        logger.warning(f"  AMOUNT:   {sol_spent:.4f} SOL")
        logger.warning(f"  PLATFORM: {platform}")
        logger.warning(f"  AGE:      {age:.1f}s ago")
        logger.warning(f"  TX:       {signature}")
        logger.warning("=" * 70)
        
        self._stats["signals"] += 1
        
        # Call callback
        if self.on_whale_buy:
            try:
                asyncio.create_task(self.on_whale_buy(whale_buy))
                self._stats["success"] += 1
            except Exception as e:
                logger.error(f"[POLLER] Callback error: {e}")
                self._stats["failed"] += 1

    def get_tracked_wallets(self) -> list[str]:
        """Get list of tracked whale wallets."""
        return list(self.whale_wallets.keys())
    
    def get_stats(self) -> dict:
        """Get polling statistics."""
        return self._stats.copy()
