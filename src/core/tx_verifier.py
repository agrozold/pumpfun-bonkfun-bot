"""
Transaction Verification Manager - Fast Trading with Safe Position Management.

Architecture:
1. Trade functions send TX and return IMMEDIATELY with signature
2. TxVerifier runs background verification
3. On SUCCESS: callback adds to positions/history
4. On FAILURE: nothing added, error logged

Usage:
    from core.tx_verifier import get_tx_verifier, TxVerifier
    
    # In buy function:
    sig = await jito.send_transaction(signed_tx)
    
    # Schedule verification (non-blocking)
    verifier = await get_tx_verifier()
    await verifier.schedule_verification(
        signature=sig,
        mint=mint_str,
        symbol=symbol,
        action="buy",
        token_amount=expected_tokens,
        price=price,
        on_success=on_buy_success_callback,
        on_failure=on_buy_failure_callback,
    )
    
    # Return immediately - verification happens in background
    return sig
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Any
from enum import Enum

from solana.rpc.async_api import AsyncClient
from solders.signature import Signature

logger = logging.getLogger(__name__)


class TxStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class PendingTransaction:
    """Represents a transaction awaiting verification."""
    signature: str
    mint: str
    symbol: str
    action: str  # "buy" or "sell"
    token_amount: float
    price: float
    submitted_at: datetime
    rpc_endpoint: str
    on_success: Optional[Callable] = None
    on_failure: Optional[Callable] = None
    context: dict = field(default_factory=dict)
    status: TxStatus = TxStatus.PENDING
    error_message: Optional[str] = None


class TxVerifier:
    """
    Background transaction verifier.
    
    Verifies transactions asynchronously and calls appropriate callbacks.
    Does NOT block the main trading loop.
    """
    
    _instance: Optional["TxVerifier"] = None
    _lock = asyncio.Lock()
    
    # Configuration
    INITIAL_DELAY = 2.0  # Wait before first check (TX needs time to land)
    CHECK_INTERVAL = 0.5  # Check every 500ms
    MAX_WAIT = 15.0  # Max time to wait for confirmation
    MAX_QUEUE_SIZE = 100  # Prevent memory issues
    
    def __init__(self):
        self._queue: asyncio.Queue[PendingTransaction] = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._rpc_endpoint: Optional[str] = None
        self._stats = {
            "submitted": 0,
            "confirmed": 0,
            "failed": 0,
            "timeout": 0,
        }
    
    @classmethod
    async def get_instance(cls, rpc_endpoint: Optional[str] = None) -> "TxVerifier":
        """Get singleton instance."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = TxVerifier()
            if rpc_endpoint:
                cls._instance._rpc_endpoint = rpc_endpoint
            if not cls._instance._running:
                await cls._instance.start()
            return cls._instance
    
    async def start(self):
        """Start background verification worker."""
        if self._running:
            return
        
        if not self._rpc_endpoint:
            self._rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
        
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("[TxVerifier] Background worker started")
    
    async def stop(self):
        """Stop background worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("[TxVerifier] Stopped")
    
    async def schedule_verification(
        self,
        signature: str,
        mint: str,
        symbol: str,
        action: str,
        token_amount: float,
        price: float,
        on_success: Optional[Callable] = None,
        on_failure: Optional[Callable] = None,
        context: Optional[dict] = None,
    ) -> bool:
        """
        Schedule a transaction for background verification.
        
        Returns immediately. Verification happens asynchronously.
        
        Args:
            signature: Transaction signature
            mint: Token mint address
            symbol: Token symbol
            action: "buy" or "sell"
            token_amount: Expected token amount
            price: Expected price
            on_success: Callback(tx: PendingTransaction) on successful confirmation
            on_failure: Callback(tx: PendingTransaction) on failure
            context: Additional context data
            
        Returns:
            True if scheduled successfully, False if queue is full
        """
        if self._queue.full():
            logger.error("[TxVerifier] Queue full! Cannot schedule verification")
            return False
        
        tx = PendingTransaction(
            signature=signature,
            mint=mint,
            symbol=symbol,
            action=action,
            token_amount=token_amount,
            price=price,
            submitted_at=datetime.utcnow(),
            rpc_endpoint=self._rpc_endpoint or os.getenv("SOLANA_NODE_RPC_ENDPOINT"),
            on_success=on_success,
            on_failure=on_failure,
            context=context or {},
        )
        
        await self._queue.put(tx)
        self._stats["submitted"] += 1
        
        logger.info(f"[TxVerifier] Scheduled: {action.upper()} {symbol} - {signature[:20]}...")
        return True
    
    async def _worker_loop(self):
        """Main worker loop - processes verification queue."""
        while self._running:
            try:
                # Get next transaction (with timeout for graceful shutdown)
                try:
                    tx = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Process verification
                asyncio.create_task(self._verify_transaction(tx))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TxVerifier] Worker error: {e}")
                await asyncio.sleep(1)
    
    async def _verify_transaction(self, tx: PendingTransaction):
        """Verify a single transaction."""
        try:
            # Wait before first check
            await asyncio.sleep(self.INITIAL_DELAY)
            
            # Check status
            success, error = await self._check_confirmation(tx)
            
            if success:
                tx.status = TxStatus.CONFIRMED
                self._stats["confirmed"] += 1
                logger.warning(
                    f"[TxVerifier] ✅ CONFIRMED: {tx.action.upper()} {tx.symbol} "
                    f"- {tx.token_amount:,.2f} tokens @ {tx.price:.10f}"
                )
                
                # Call success callback
                if tx.on_success:
                    try:
                        if asyncio.iscoroutinefunction(tx.on_success):
                            await tx.on_success(tx)
                        else:
                            tx.on_success(tx)
                    except Exception as e:
                        logger.error(f"[TxVerifier] Success callback error: {e}")
            else:
                tx.status = TxStatus.FAILED if "failed" in (error or "").lower() else TxStatus.TIMEOUT
                tx.error_message = error
                
                if tx.status == TxStatus.FAILED:
                    self._stats["failed"] += 1
                    logger.error(f"[TxVerifier] ❌ FAILED: {tx.action.upper()} {tx.symbol} - {error}")
                else:
                    self._stats["timeout"] += 1
                    logger.warning(f"[TxVerifier] ⏱️ TIMEOUT: {tx.action.upper()} {tx.symbol} - {error}")
                
                # Call failure callback
                if tx.on_failure:
                    try:
                        if asyncio.iscoroutinefunction(tx.on_failure):
                            await tx.on_failure(tx)
                        else:
                            tx.on_failure(tx)
                    except Exception as e:
                        logger.error(f"[TxVerifier] Failure callback error: {e}")
                        
        except Exception as e:
            logger.error(f"[TxVerifier] Verification error for {tx.signature[:20]}: {e}")
    
    async def _check_confirmation(self, tx: PendingTransaction) -> tuple[bool, Optional[str]]:
        """
        Check if transaction is confirmed on-chain.
        
        Returns:
            (True, None) - Confirmed successfully
            (False, error_message) - Failed or timeout
        """
        try:
            async with AsyncClient(tx.rpc_endpoint) as client:
                sig = Signature.from_string(tx.signature)
                start_time = asyncio.get_event_loop().time()
                
                while asyncio.get_event_loop().time() - start_time < self.MAX_WAIT:
                    try:
                        resp = await client.get_signature_statuses([sig])
                        
                        if resp.value and resp.value[0]:
                            status = resp.value[0]
                            
                            # Check for on-chain error
                            if status.err:
                                return False, f"TX failed on-chain: {status.err}"
                            
                            # Check confirmation status
                            if status.confirmation_status:
                                conf_str = str(status.confirmation_status).lower()
                                if "confirmed" in conf_str or "finalized" in conf_str:
                                    return True, None
                                    
                    except Exception as e:
                        logger.debug(f"[TxVerifier] Status check error: {e}")
                    
                    await asyncio.sleep(self.CHECK_INTERVAL)
                
                return False, f"Confirmation timeout after {self.MAX_WAIT}s"
                
        except Exception as e:
            return False, f"Verification error: {e}"
    
    def get_stats(self) -> dict:
        """Get verification statistics."""
        return {
            **self._stats,
            "queue_size": self._queue.qsize(),
            "running": self._running,
        }


# Convenience function
async def get_tx_verifier(rpc_endpoint: Optional[str] = None) -> TxVerifier:
    """Get singleton TxVerifier instance."""
    return await TxVerifier.get_instance(rpc_endpoint)
