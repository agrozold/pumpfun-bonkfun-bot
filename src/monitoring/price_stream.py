"""
Phase 4: Real-time Price Stream via gRPC account subscriptions.

Subscribes to PumpSwap pool vault accounts (base_vault + quote_vault)
via PublicNode Yellowstone gRPC to get real-time price updates.

Price = quote_reserve (SOL) / base_reserve (tokens)

Decoded from SPL Token Account layout: amount at offset 64, 8 bytes LE u64.

Separate gRPC connection from whale tracking (whale_geyser.py) to avoid
overloading a single stream and to keep concerns separated.

Fallback: If no gRPC price available for a mint (stale > 3s), callers
should fall back to BatchPriceService (Jupiter Price API polling).
"""

import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import base58
import grpc
from grpc import aio as grpc_aio

from geyser.generated import geyser_pb2, geyser_pb2_grpc

logger = logging.getLogger(__name__)

# SPL Token Account layout constants
# Mint: 0-32, Owner: 32-64, Amount: 64-72
TOKEN_AMOUNT_OFFSET = 64
TOKEN_AMOUNT_SIZE = 8


@dataclass
class PoolVaults:
    """Vault addresses and state for a single pool/position."""
    mint: str
    symbol: str
    base_vault: str
    quote_vault: str
    token_decimals: int = 6
    base_reserve: float = 0.0
    quote_reserve: float = 0.0
    price: float = 0.0
    last_update: float = 0.0
    update_count: int = 0


@dataclass
class PriceUpdate:
    """Price update event pushed to callbacks."""
    mint: str
    price: float
    base_reserve: float
    quote_reserve: float
    source: str = "grpc"
    timestamp: float = 0.0


class PriceStream:
    """
    Real-time price monitoring via gRPC account subscriptions.

    Opens a SECOND gRPC connection to PublicNode (separate from whale tracking)
    and subscribes to vault account changes for all active positions.

    When a vault account changes (swap happens in the pool), the new reserves
    are decoded and price is recalculated instantly.
    """

    def __init__(
        self,
        geyser_endpoint: str = "",
        geyser_api_key: str = "",
        on_price_update: Optional[Callable] = None,
        stale_threshold: float = 3.0,
    ):
        self.geyser_endpoint = geyser_endpoint or os.getenv(
            "GEYSER_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"
        )
        self.geyser_api_key = geyser_api_key or os.getenv(
            "GEYSER_API_KEY", ""
        )
        self.stale_threshold = stale_threshold
        self.on_price_update = on_price_update

        self._pools: Dict[str, PoolVaults] = {}
        self._vault_to_pool: Dict[str, Tuple[str, str]] = {}

        self._channel = None
        self._request_queue: Optional[asyncio.Queue] = None
        self._ping_queue_counter: int = 0
        self._stream_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._last_pong_time: float = 0.0
        self.running = False

        self._sub_version: int = 0

        self._stats = {
            "account_updates": 0,
            "price_updates": 0,
            "reconnects": 0,
            "subscribe_requests": 0,
            "ping_sent": 0,
            "pong_received": 0,
            "decode_errors": 0,
        }

        logger.info(
            f"[PRICE_STREAM] Initialized: endpoint={self.geyser_endpoint}, "
            f"stale_threshold={stale_threshold}s"
        )

    # ================================================================
    # Public API
    # ================================================================

    async def start(self):
        """Start the gRPC price stream."""
        if self.running:
            return
        self.running = True
        self._stream_task = asyncio.create_task(self._run_stream())
        logger.warning("[PRICE_STREAM] Started")

    async def stop(self):
        """Stop the gRPC price stream."""
        self.running = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._request_queue:
            try:
                self._request_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        if self._channel:
            try:
                await self._channel.close()
            except Exception:
                pass
        logger.info("[PRICE_STREAM] Stopped")

    def subscribe_position(
        self,
        mint: str,
        base_vault: str,
        quote_vault: str,
        symbol: str = "",
        token_decimals: int = 6,
    ) -> bool:
        """Subscribe to price updates for a position."""
        if mint in self._pools:
            logger.info(f"[PRICE_STREAM] Already subscribed: {symbol or mint[:12]}...")
            return False

        pool = PoolVaults(
            mint=mint,
            symbol=symbol,
            base_vault=base_vault,
            quote_vault=quote_vault,
            token_decimals=token_decimals,
        )
        self._pools[mint] = pool
        self._vault_to_pool[base_vault] = (mint, "base")
        self._vault_to_pool[quote_vault] = (mint, "quote")

        logger.warning(
            f"[PRICE_STREAM] +SUBSCRIBE {symbol or mint[:12]}... "
            f"(base={base_vault[:12]}..., quote={quote_vault[:12]}..., "
            f"decimals={token_decimals})"
        )

        self._trigger_resubscribe()
        return True

    def unsubscribe_position(self, mint: str) -> bool:
        """Remove subscription for a position."""
        pool = self._pools.pop(mint, None)
        if not pool:
            return False

        self._vault_to_pool.pop(pool.base_vault, None)
        self._vault_to_pool.pop(pool.quote_vault, None)

        logger.warning(
            f"[PRICE_STREAM] -UNSUBSCRIBE {pool.symbol or mint[:12]}..."
        )

        self._trigger_resubscribe()
        return True

    def get_price(self, mint: str) -> Optional[float]:
        """Get the latest gRPC-derived price for a mint.
        Returns None if not subscribed, no data, or stale."""
        pool = self._pools.get(mint)
        if not pool or pool.price <= 0:
            return None

        age = time.monotonic() - pool.last_update
        if age > self.stale_threshold:
            return None

        return pool.price

    def get_price_with_age(self, mint: str) -> Tuple[Optional[float], float]:
        """Get price and its age in seconds."""
        pool = self._pools.get(mint)
        if not pool or pool.price <= 0:
            return None, float('inf')
        age = time.monotonic() - pool.last_update
        return pool.price, age

    def get_all_prices(self) -> Dict[str, float]:
        """Get all non-stale prices."""
        now = time.monotonic()
        return {
            mint: pool.price
            for mint, pool in self._pools.items()
            if pool.price > 0 and (now - pool.last_update) <= self.stale_threshold
        }

    def is_subscribed(self, mint: str) -> bool:
        return mint in self._pools

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "subscribed_pools": len(self._pools),
            "active_vaults": len(self._vault_to_pool),
            "last_pong_ago_s": round(
                time.monotonic() - self._last_pong_time, 1
            ) if self._last_pong_time > 0 else None,
        }

    # ================================================================
    # gRPC Connection & Stream
    # ================================================================

    async def _create_channel(self):
        """Create authenticated gRPC channel."""
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback(
                (("x-token", self.geyser_api_key),), None
            )
        )
        creds = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(), auth
        )
        self._channel = grpc_aio.secure_channel(
            self.geyser_endpoint,
            creds,
            options=[
                ("grpc.keepalive_time_ms", 10000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ],
        )
        return geyser_pb2_grpc.GeyserStub(self._channel)

    def _build_subscribe_request(self) -> geyser_pb2.SubscribeRequest:
        """Build account subscription request for all tracked vault accounts."""
        request = geyser_pb2.SubscribeRequest()

        if not self._vault_to_pool:
            logger.info("[PRICE_STREAM] No pools to track, sending minimal subscription")
            return request

        filter_name = f"price_vaults_v{self._sub_version}"
        acct_filter = request.accounts[filter_name]

        for vault_addr in self._vault_to_pool.keys():
            acct_filter.account.append(vault_addr)

        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED

        self._stats["subscribe_requests"] += 1
        logger.info(
            f"[PRICE_STREAM] Subscribe request: {len(self._vault_to_pool)} vaults "
            f"(version {self._sub_version})"
        )
        return request

    def _trigger_resubscribe(self):
        """Send new SubscribeRequest to update account filters without reconnecting."""
        if not self._request_queue or not self.running:
            return

        self._sub_version += 1
        new_request = self._build_subscribe_request()

        try:
            self._request_queue.put_nowait(new_request)
            logger.info(
                f"[PRICE_STREAM] Resubscribe triggered (v{self._sub_version}, "
                f"{len(self._vault_to_pool)} vaults)"
            )
        except asyncio.QueueFull:
            logger.warning("[PRICE_STREAM] Request queue full, resubscribe delayed")

    async def _request_iterator(self, initial_request):
        """Async generator for bidirectional gRPC stream."""
        yield initial_request
        logger.info("[PRICE_STREAM] Initial subscription sent")

        while True:
            try:
                msg = await self._request_queue.get()
                if msg is None:
                    logger.info("[PRICE_STREAM] Request iterator stopping (poison pill)")
                    return
                yield msg
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[PRICE_STREAM] Request iterator error: {e}")
                return

    async def _ping_loop(self):
        """Keepalive ping every 10 seconds."""
        try:
            await asyncio.sleep(5)
            while self.running:
                self._ping_queue_counter += 1
                ping_id = self._ping_queue_counter
                ping_req = geyser_pb2.SubscribeRequest(
                    ping=geyser_pb2.SubscribeRequestPing(id=ping_id)
                )
                try:
                    self._request_queue.put_nowait(ping_req)
                    self._stats["ping_sent"] += 1
                except asyncio.QueueFull:
                    pass
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    async def _run_stream(self):
        """Main gRPC stream loop with auto-reconnect."""
        reconnect_delay = 1.0

        while self.running:
            try:
                stub = await self._create_channel()
                request = self._build_subscribe_request()

                self._request_queue = asyncio.Queue(maxsize=100)
                self._ping_queue_counter = 0

                logger.warning(
                    f"[PRICE_STREAM] Connecting to {self.geyser_endpoint}... "
                    f"({len(self._vault_to_pool)} vaults)"
                )

                self._ping_task = asyncio.create_task(self._ping_loop())

                try:
                    async for update in stub.Subscribe(
                        self._request_iterator(request)
                    ):
                        if not self.running:
                            break

                        if update.HasField("pong"):
                            self._stats["pong_received"] += 1
                            self._last_pong_time = time.monotonic()
                            continue

                        if update.HasField("ping"):
                            self._ping_queue_counter += 1
                            ping_req = geyser_pb2.SubscribeRequest(
                                ping=geyser_pb2.SubscribeRequestPing(
                                    id=self._ping_queue_counter
                                )
                            )
                            try:
                                self._request_queue.put_nowait(ping_req)
                            except asyncio.QueueFull:
                                pass
                            continue

                        if update.HasField("account"):
                            self._handle_account_update(update.account)
                            continue

                finally:
                    if self._ping_task:
                        self._ping_task.cancel()
                        try:
                            await self._ping_task
                        except asyncio.CancelledError:
                            pass
                        self._ping_task = None

                reconnect_delay = 1.0

            except grpc_aio.AioRpcError as e:
                code = e.code()
                details = e.details() or ""
                logger.error(f"[PRICE_STREAM] gRPC error: {code} - {details}")
                if code == grpc.StatusCode.UNAUTHENTICATED:
                    logger.error("[PRICE_STREAM] Auth failed!")
                    await asyncio.sleep(30)
                elif code == grpc.StatusCode.INTERNAL and "RST_STREAM" in details:
                    reconnect_delay = 0.5
                    logger.warning("[PRICE_STREAM] RST_STREAM, fast reconnect")

            except asyncio.CancelledError:
                return

            except Exception as e:
                logger.error(f"[PRICE_STREAM] Unexpected error: {e}")

            if self.running:
                self._stats["reconnects"] += 1
                logger.warning(
                    f"[PRICE_STREAM] Reconnecting in {reconnect_delay:.1f}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

            if self._channel:
                try:
                    await self._channel.close()
                except Exception:
                    pass
                self._channel = None

    # ================================================================
    # Account Update Processing
    # ================================================================

    def _handle_account_update(self, account_update):
        """Process a gRPC account update for a vault.
        Decodes SPL Token Account data to extract amount,
        then recalculates price if both vaults have data."""
        try:
            self._stats["account_updates"] += 1

            acct = account_update.account
            if not acct:
                return

            pubkey_bytes = bytes(acct.pubkey)
            pubkey_str = base58.b58encode(pubkey_bytes).decode()

            pool_info = self._vault_to_pool.get(pubkey_str)
            if not pool_info:
                return

            mint, vault_type = pool_info
            pool = self._pools.get(mint)
            if not pool:
                return

            data = bytes(acct.data)
            if len(data) < TOKEN_AMOUNT_OFFSET + TOKEN_AMOUNT_SIZE:
                self._stats["decode_errors"] += 1
                logger.warning(
                    f"[PRICE_STREAM] Account data too short: {len(data)} bytes "
                    f"for {pubkey_str[:12]}..."
                )
                return

            raw_amount = struct.unpack(
                "<Q", data[TOKEN_AMOUNT_OFFSET:TOKEN_AMOUNT_OFFSET + TOKEN_AMOUNT_SIZE]
            )[0]

            if vault_type == "base":
                pool.base_reserve = raw_amount / (10 ** pool.token_decimals)
            elif vault_type == "quote":
                pool.quote_reserve = raw_amount / (10 ** 9)

            if pool.base_reserve > 0 and pool.quote_reserve > 0:
                old_price = pool.price
                pool.price = pool.quote_reserve / pool.base_reserve
                pool.last_update = time.monotonic()
                pool.update_count += 1
                self._stats["price_updates"] += 1

                if old_price > 0:
                    change_pct = abs(pool.price - old_price) / old_price * 100
                    if change_pct > 1.0:
                        logger.info(
                            f"[PRICE_STREAM] {pool.symbol or mint[:8]}: "
                            f"{old_price:.10f} -> {pool.price:.10f} "
                            f"({change_pct:+.1f}%)"
                        )

                if self.on_price_update:
                    price_upd = PriceUpdate(
                        mint=mint,
                        price=pool.price,
                        base_reserve=pool.base_reserve,
                        quote_reserve=pool.quote_reserve,
                        source="grpc",
                        timestamp=time.monotonic(),
                    )
                    try:
                        result = self.on_price_update(price_upd)
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                    except Exception as e:
                        logger.error(f"[PRICE_STREAM] Callback error: {e}")

        except Exception as e:
            self._stats["decode_errors"] += 1
            logger.error(f"[PRICE_STREAM] Account update error: {e}")
