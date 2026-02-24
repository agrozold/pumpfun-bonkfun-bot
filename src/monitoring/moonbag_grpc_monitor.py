"""
Lightweight gRPC price monitor for moonbag/dust positions.
Uses PublicNode (free) gRPC — completely separate from Chainstack whale_geyser.

Architecture:
- Single gRPC channel to PublicNode
- Subscribes to vault accounts of moonbag/dust positions
- Calculates price from vault reserves (quote/base)
- Provides get_price(mint) for monitor loop
- Does NOT touch Chainstack gRPC at all

Lifecycle:
- TP partial → moonbag: subscribe(mint, base_vault, quote_vault, ...)
- Moonbag sold (SL/TSL): unsubscribe(mint)
- Bot restart: subscribe all moonbag positions with known vaults
"""

import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

import base58

logger = logging.getLogger(__name__)

# Singleton instance
_instance: Optional["MoonbagGrpcMonitor"] = None


@dataclass
class MoonbagVaultSub:
    """Vault subscription for a moonbag position."""
    mint: str
    symbol: str
    base_vault: str
    quote_vault: str
    decimals: int = 6
    base_reserve: float = 0.0
    quote_reserve: float = 0.0
    price: float = 0.0
    last_update: float = 0.0
    last_slot: int = 0


class MoonbagGrpcMonitor:
    """Lightweight gRPC monitor for moonbag/dust vault prices via PublicNode."""

    def __init__(self):
        self._endpoint = os.getenv(
            "GEYSER_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"
        )
        self._api_key = os.getenv("GEYSER_API_KEY", "")

        # Subscriptions: mint -> MoonbagVaultSub
        self._subscriptions: dict[str, MoonbagVaultSub] = {}
        # Reverse map: vault_address -> mint
        self._vault_to_mint: dict[str, str] = {}
        # Prices: mint -> (price, timestamp)
        self._prices: dict[str, tuple[float, float]] = {}

        self._stream_task: Optional[asyncio.Task] = None
        self._running = False
        self._channel = None
        self._resubscribe_queue: asyncio.Queue = asyncio.Queue()

    async def start(self):
        """Start the gRPC stream in background."""
        if self._stream_task and not self._stream_task.done():
            return
        self._running = True
        self._stream_task = asyncio.create_task(self._run_stream())
        logger.warning("[MOONBAG-GRPC] Monitor started (PublicNode)")

    async def stop(self):
        """Stop the gRPC stream."""
        self._running = False
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._channel:
            await self._channel.close()
            self._channel = None
        logger.warning("[MOONBAG-GRPC] Monitor stopped")

    def subscribe(self, mint: str, base_vault: str, quote_vault: str,
                  decimals: int = 6, symbol: str = "") -> bool:
        """Add a moonbag position for vault price tracking."""
        if mint in self._subscriptions:
            logger.info(f"[MOONBAG-GRPC] {symbol}: already subscribed")
            return False

        sub = MoonbagVaultSub(
            mint=mint, symbol=symbol,
            base_vault=base_vault, quote_vault=quote_vault,
            decimals=decimals,
        )
        self._subscriptions[mint] = sub
        self._vault_to_mint[base_vault] = mint
        self._vault_to_mint[quote_vault] = mint

        logger.warning(
            f"[MOONBAG-GRPC] +SUBSCRIBE {symbol} ({mint[:8]}...) "
            f"base={base_vault[:12]}... quote={quote_vault[:12]}..."
        )

        # Signal resubscribe
        try:
            self._resubscribe_queue.put_nowait(True)
        except asyncio.QueueFull:
            pass
        return True

    def unsubscribe(self, mint: str) -> bool:
        """Remove moonbag position from tracking."""
        sub = self._subscriptions.pop(mint, None)
        if not sub:
            return False

        self._vault_to_mint.pop(sub.base_vault, None)
        self._vault_to_mint.pop(sub.quote_vault, None)
        self._prices.pop(mint, None)

        logger.warning(f"[MOONBAG-GRPC] -UNSUBSCRIBE {sub.symbol} ({mint[:8]}...)")

        try:
            self._resubscribe_queue.put_nowait(True)
        except asyncio.QueueFull:
            pass
        return True

    def get_price(self, mint: str, max_age: float = 60.0) -> Optional[float]:
        """Get cached vault price. Returns None if no data or stale."""
        data = self._prices.get(mint)
        if not data:
            return None
        price, ts = data
        if time.time() - ts > max_age:
            return None
        return price

    def has_subscription(self, mint: str) -> bool:
        return mint in self._subscriptions

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    def _build_subscribe_request(self):
        """Build gRPC subscribe request for all vault accounts."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'geyser', 'generated'))
        from geyser.generated import geyser_pb2

        request = geyser_pb2.SubscribeRequest()

        vault_addresses = list(self._vault_to_mint.keys())
        if vault_addresses:
            acct_filter = request.accounts["moonbag_vaults"]
            for addr in vault_addresses:
                acct_filter.account.append(addr)

        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        return request

    async def _run_stream(self):
        """Main gRPC stream loop with reconnect."""
        while self._running:
            try:
                await self._stream_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MOONBAG-GRPC] Stream error: {type(e).__name__}: {e}")
                if self._running:
                    await asyncio.sleep(5)

    async def _stream_loop(self):
        """Single gRPC stream session."""
        import grpc
        import grpc.aio as grpc_aio
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'geyser', 'generated'))
        from geyser.generated import geyser_pb2_grpc

        if self._channel:
            await self._channel.close()

        self._channel = grpc_aio.secure_channel(
            self._endpoint, grpc.ssl_channel_credentials()
        )
        stub = geyser_pb2_grpc.GeyserStub(self._channel)
        metadata = [("x-token", self._api_key)] if self._api_key else []

        logger.info(f"[MOONBAG-GRPC] Connecting to {self._endpoint} ({len(self._subscriptions)} subs)")

        async def request_iterator():
            # Initial request
            yield self._build_subscribe_request()
            # Wait for resubscribe signals or ping
            while self._running:
                try:
                    await asyncio.wait_for(self._resubscribe_queue.get(), timeout=10.0)
                    # Drain queue
                    while not self._resubscribe_queue.empty():
                        self._resubscribe_queue.get_nowait()
                    logger.info(f"[MOONBAG-GRPC] Resubscribing with {len(self._subscriptions)} subs")
                    yield self._build_subscribe_request()
                except asyncio.TimeoutError:
                    # Send ping to keep alive
                    from geyser.generated import geyser_pb2
                    ping = geyser_pb2.SubscribeRequest()
                    ping.ping.id = int(time.time()) % 1000000
                    yield ping

        stream = stub.Subscribe(request_iterator(), metadata=metadata)

        async for update in stream:
            if not self._running:
                break

            if update.HasField("account"):
                self._handle_account_update(update.account)
            # ping/pong handled automatically by keepalive

    def _handle_account_update(self, account_update) -> None:
        """Process vault account update — decode balance, recalculate price."""
        try:
            acct = account_update.account
            if not acct:
                return

            pubkey_bytes = bytes(acct.pubkey)
            pubkey_str = base58.b58encode(pubkey_bytes).decode()
            slot = account_update.slot

            mint = self._vault_to_mint.get(pubkey_str)
            if not mint:
                return

            sub = self._subscriptions.get(mint)
            if not sub:
                return

            # Slot-based filtering — reject stale updates
            if slot > 0 and sub.last_slot > 0 and slot <= sub.last_slot:
                return
            if slot > 0:
                sub.last_slot = slot

            data = bytes(acct.data)
            if len(data) < 72:
                return

            # SPL Token Account: amount at offset 64 (u64 LE)
            raw_amount = struct.unpack("<Q", data[64:72])[0]

            if pubkey_str == sub.base_vault:
                sub.base_reserve = raw_amount / (10 ** sub.decimals)
            elif pubkey_str == sub.quote_vault:
                sub.quote_reserve = raw_amount / (10 ** 9)  # SOL = 9 decimals
            else:
                return

            sub.last_update = time.time()

            if sub.base_reserve > 0 and sub.quote_reserve > 0:
                old_price = sub.price
                sub.price = sub.quote_reserve / sub.base_reserve
                self._prices[mint] = (sub.price, time.time())

                if old_price <= 0:
                    logger.warning(
                        f"[MOONBAG-GRPC] FIRST price: {sub.symbol} "
                        f"{sub.price:.10f} SOL "
                        f"(base={sub.base_reserve:.2f} quote={sub.quote_reserve:.6f})"
                    )
                elif abs(sub.price - old_price) / max(old_price, 1e-15) > 0.10:
                    logger.info(
                        f"[MOONBAG-GRPC] Price move: {sub.symbol} "
                        f"{sub.price:.10f} SOL ({((sub.price - old_price) / old_price * 100):+.1f}%)"
                    )

        except Exception as e:
            logger.error(f"[MOONBAG-GRPC] Account update error: {e}")


def get_moonbag_monitor() -> MoonbagGrpcMonitor:
    """Get or create singleton moonbag gRPC monitor."""
    global _instance
    if _instance is None:
        _instance = MoonbagGrpcMonitor()
    return _instance
