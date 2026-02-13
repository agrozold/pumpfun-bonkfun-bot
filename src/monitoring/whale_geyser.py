"""
Whale Geyser Tracker - Ultra-low latency whale tracking via gRPC.
Hybrid approach: gRPC catches tx signature instantly, local parser decodes it.
Drop-in replacement for WhaleWebhookReceiver - same WhaleBuy, same callback interface.

Phase 1: Local parser (eliminates ~650ms Helius API call)
Phase 5.1: Bidirectional stream with application-level keepalive ping
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
import base58
import grpc
from grpc import aio as grpc_aio

from geyser.generated import geyser_pb2, geyser_pb2_grpc

# Local transaction parser — eliminates ~650ms Helius API call
try:
    from monitoring.local_tx_parser import LocalTxParser, ParsedSwap
    LOCAL_PARSER_AVAILABLE = True
except ImportError:
    LOCAL_PARSER_AVAILABLE = False

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
    """Fetch token symbol: DexScreener -> Jupiter Token API -> short mint fallback."""
    # 1. Try DexScreener (fastest for established tokens)
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        sym = pairs[0].get("baseToken", {}).get("symbol", "")
                        if sym:
                            return sym
    except Exception:
        pass
    # 2. Fallback: Jupiter Token API V2 (indexes new tokens faster)
    try:
        url = f"https://api.jup.ag/tokens/v2/search?query={mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        sym = data[0].get("symbol", "")
                        if sym:
                            return sym
    except Exception:
        pass
    # 3. Last resort: short mint as symbol
    return mint[:8] if mint else ""



@dataclass
class WhaleBuy:
    """Whale buy signal - identical to whale_webhook.WhaleBuy."""
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


class WhaleGeyserReceiver:
    """
    Tracks whale wallets via gRPC (Yellowstone Dragon's Mouth).
    Phase 1: Local parser for ~0-5ms parse latency (Helius fallback).
    Phase 5.1: Bidirectional stream with keepalive ping for stability.
    """

    def __init__(
        self,
        geyser_endpoint: str = "",
        geyser_api_key: str = "",
        helius_parse_api_key: str = "",
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.4,
        stablecoin_filter: list | None = None,
        # Keep same interface params as WhaleWebhookReceiver (ignored but accepted)
        host: str = "0.0.0.0",
        port: int = 8000,
    ):
        # gRPC config
        self.geyser_endpoint = geyser_endpoint or os.getenv(
            "GEYSER_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"
        )
        self.geyser_api_key = geyser_api_key or os.getenv(
            "GEYSER_API_KEY", ""
        )
        # Separate key for parsing (don't burn gRPC key credits)
        self.helius_parse_api_key = helius_parse_api_key or os.getenv(
            "GEYSER_PARSE_API_KEY", self.geyser_api_key
        )

        self.min_buy_amount = min_buy_amount

        # Load whale wallets
        self.whale_wallets: dict[str, dict] = {}
        self._load_wallets(wallets_file)

        # Blacklist
        self.token_blacklist = TOKEN_BLACKLIST.copy()
        if stablecoin_filter:
            self.token_blacklist.update(set(stablecoin_filter))

        # Local transaction parser (eliminates Helius HTTP call)
        self.local_parser: LocalTxParser | None = None
        if LOCAL_PARSER_AVAILABLE:
            self.local_parser = LocalTxParser(
                extra_blacklist=set(stablecoin_filter) if stablecoin_filter else None
            )
            logger.warning(
                f"[GEYSER] Local parser ENABLED: {len(self.local_parser.blacklist)} "
                f"blacklisted tokens"
            )
        else:
            logger.warning("[GEYSER] Local parser NOT available, using Helius only")

        # Callback (same interface as webhook)
        self.on_whale_buy: Optional[Callable] = None

        # Dedup
        self._processed_sigs: set[str] = set()
        self._emitted_tokens: set[str] = set()

        # State
        self._channel = None
        self._stream_task: Optional[asyncio.Task] = None
        self.running = False

        # Keepalive ping state (Phase 5.1)
        self._ping_counter: int = 0
        self._ping_queue: Optional[asyncio.Queue] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._last_pong_time: float = 0.0

        # Stats
        self._stats = {
            "grpc_messages": 0,
            "tx_detected": 0,
            "parse_ok": 0,
            "parse_fail": 0,
            "buys_emitted": 0,
            "sells_skipped": 0,
            "below_min": 0,
            "blacklisted": 0,
            "duplicates": 0,
            "reconnects": 0,
            "ping_sent": 0,
            "ping_responded": 0,
            "pong_received": 0,
        }

        # Latency tracking
        self._last_latency_ms: float = 0

        # Watchdog integration (Phase 5.3)
        self._watchdog = None
        self._reconnect_event = asyncio.Event()

        logger.warning(
            f"[GEYSER] Initialized: {len(self.whale_wallets)} whales, "
            f"min_buy={min_buy_amount} SOL, endpoint={self.geyser_endpoint}"
        )

    def _load_wallets(self, wallets_file: str):
        """Load whale wallets from JSON file."""
        path = Path(wallets_file)
        if not path.exists():
            logger.error(f"[GEYSER] Wallets file NOT FOUND: {path.absolute()}")
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
            logger.info(f"[GEYSER] Loaded {len(self.whale_wallets)} whale wallets")
        except Exception as e:
            logger.exception(f"[GEYSER] Error loading wallets: {e}")

    def set_callback(self, callback: Callable):
        """Set callback for whale buy signals. Same interface as webhook."""
        self.on_whale_buy = callback
        logger.info("[GEYSER] Callback set")

    def set_watchdog(self, watchdog):
        self._watchdog = watchdog
        watchdog.set_reconnect_callback(self._trigger_reconnect)

    def _trigger_reconnect(self):
        # Reset watchdog data timer so next reconnect waits full 300s
        if self._watchdog:
            self._watchdog.touch_grpc_data()
        self._reconnect_event.set()

    async def start(self):
        """Start gRPC stream. Same interface as WhaleWebhookReceiver.start()."""
        self.running = True
        self._stream_task = asyncio.create_task(self._run_stream())

        logger.warning("=" * 70)
        logger.warning("[GEYSER] WHALE GEYSER TRACKER STARTED")
        logger.warning(f"[GEYSER] Endpoint: {self.geyser_endpoint}")
        logger.warning(f"[GEYSER] Tracking {len(self.whale_wallets)} whale wallets")
        logger.warning(f"[GEYSER] Min buy amount: {self.min_buy_amount} SOL")
        logger.warning(f"[GEYSER] Keepalive: bidirectional ping every 10s")
        if self.local_parser:
            logger.warning(f"[GEYSER] Mode: LOCAL PARSE (gRPC + local parser, Helius fallback)")
        else:
            logger.warning(f"[GEYSER] Mode: HYBRID (gRPC + Helius Enhanced API)")
        logger.warning("=" * 70)

    async def stop(self):
        """Stop gRPC stream."""
        self.running = False
        # Stop ping loop
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        # Signal request iterator to stop
        if self._ping_queue:
            try:
                self._ping_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        # Stop stream
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        if self._channel:
            await self._channel.close()
        logger.info("[GEYSER] Stopped")

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

    def _create_subscribe_request(self):
        """Create gRPC subscribe request for whale wallets."""
        request = geyser_pb2.SubscribeRequest()

        # Subscribe to transactions involving ANY of the whale wallets
        whale_addresses = list(self.whale_wallets.keys())

        tx_filter = request.transactions["whale_tracker"]
        for addr in whale_addresses:
            tx_filter.account_include.append(addr)

        # Only successful transactions
        tx_filter.failed = False

        # PROCESSED = fastest, see tx before full confirmation
        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED

        logger.info(
            f"[GEYSER] Subscribe request: {len(whale_addresses)} wallets, "
            f"commitment=PROCESSED"
        )
        return request

    async def _request_iterator(self, initial_request):
        """Async generator for bidirectional gRPC stream.

        Yields the initial subscription request, then keeps the write-half
        open and yields ping requests from the queue as needed.
        This enables the server to send SubscribeUpdatePing and the client
        to respond with SubscribeRequestPing, keeping the connection alive.
        """
        # First message: the actual subscription request
        yield initial_request
        logger.info("[GEYSER] Subscription request sent, write-half staying open for pings")

        # Then: yield ping/pong requests from queue forever
        while True:
            try:
                msg = await self._ping_queue.get()
                if msg is None:
                    # Poison pill — stop the iterator, closes write-half
                    logger.info("[GEYSER] Request iterator stopping (poison pill)")
                    return
                yield msg
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[GEYSER] Request iterator error: {e}")
                return

    async def _ping_loop(self):
        """Proactive keepalive: send ping every 10 seconds.

        Some providers (PublicNode, Cloudflare proxies) close idle streams.
        This ensures traffic flows on the write-half even when no whale
        transactions are happening.
        """
        try:
            # Wait a bit before first ping to let subscription establish
            await asyncio.sleep(5)
            while self.running:
                self._ping_counter += 1
                ping_id = self._ping_counter
                ping_req = geyser_pb2.SubscribeRequest(
                    ping=geyser_pb2.SubscribeRequestPing(id=ping_id)
                )
                try:
                    self._ping_queue.put_nowait(ping_req)
                    self._stats["ping_sent"] += 1
                    logger.info(f"[GEYSER] Ping sent (proactive, id={ping_id})")
                except asyncio.QueueFull:
                    logger.warning("[GEYSER] Ping queue full, skipping ping")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    async def _run_stream(self):
        """Main gRPC stream loop with auto-reconnect and bidirectional ping."""
        reconnect_delay = 1.0

        while self.running:
            try:
                stub = await self._create_channel()
                request = self._create_subscribe_request()

                # Create fresh ping queue and counter for this connection
                self._ping_queue = asyncio.Queue(maxsize=100)
                self._ping_counter = 0

                logger.warning(f"[GEYSER] Connecting to {self.geyser_endpoint}...")

                # Start proactive ping loop
                self._ping_task = asyncio.create_task(self._ping_loop())

                try:
                    # Bidirectional stream: _request_iterator yields subscription
                    # request first, then keepalive pings from queue
                    async for update in stub.Subscribe(
                        self._request_iterator(request)
                    ):
                        if not self.running:
                            break
                        if self._reconnect_event.is_set():
                            self._reconnect_event.clear()
                            logger.warning("[GEYSER] Reconnect triggered by watchdog")
                            break

                        self._stats["grpc_messages"] += 1

                        # Touch watchdog on ANY gRPC activity (Phase 5.3)
                        if self._watchdog:
                            self._watchdog.touch_grpc()

                        # --- Phase 5.1: Handle ping/pong ---
                        if update.HasField("pong"):
                            self._stats["pong_received"] += 1
                            self._last_pong_time = time.monotonic()
                            logger.info(
                                f"[GEYSER] Pong received (id={update.pong.id})"
                            )
                            continue

                        if update.HasField("ping"):
                            # Server-initiated ping — respond immediately
                            self._ping_counter += 1
                            ping_id = self._ping_counter
                            ping_req = geyser_pb2.SubscribeRequest(
                                ping=geyser_pb2.SubscribeRequestPing(id=ping_id)
                            )
                            try:
                                self._ping_queue.put_nowait(ping_req)
                                self._stats["ping_responded"] += 1
                                logger.info(
                                    f"[GEYSER] Server ping received, "
                                    f"responded with id={ping_id}"
                                )
                            except asyncio.QueueFull:
                                logger.warning(
                                    "[GEYSER] Ping queue full, could not respond to server ping"
                                )
                            continue

                        # --- We only care about transactions ---
                        if not update.HasField("transaction"):
                            continue

                        grpc_receive_time = time.monotonic()

                        try:
                            tx_wrapper = update.transaction
                            tx = tx_wrapper.transaction

                            # Extract signature
                            sig_bytes = bytes(tx.signature)
                            signature = base58.b58encode(sig_bytes).decode()

                            # Dedup by signature
                            if signature in self._processed_sigs:
                                continue
                            self._processed_sigs.add(signature)
                            if len(self._processed_sigs) > 10000:
                                self._processed_sigs = set(
                                    list(self._processed_sigs)[-5000:]
                                )

                            self._stats["tx_detected"] += 1
                            if self._watchdog:
                                self._watchdog.touch_grpc_data()

                            # Quick check: is the fee payer one of our whales?
                            msg = tx.transaction.message
                            if not msg or len(msg.account_keys) == 0:
                                continue

                            fee_payer_bytes = bytes(msg.account_keys[0])
                            fee_payer = base58.b58encode(fee_payer_bytes).decode()

                            if fee_payer not in self.whale_wallets:
                                # TX involves whale wallet but whale is not fee payer
                                # For safety, only process if fee_payer is whale
                                # (prevents false positives from transfers TO whale)
                                continue

                            logger.warning(
                                f"[GEYSER] TX from whale "
                                f"{self.whale_wallets[fee_payer]['label']}: "
                                f"{signature[:20]}..."
                            )

                            # Try local parse first (~0-5ms), fallback to Helius (~650ms)
                            if self.local_parser:
                                parsed = self.local_parser.parse(tx, fee_payer)
                                if parsed:
                                    asyncio.create_task(
                                        self._emit_from_local_parse(
                                            parsed, grpc_receive_time
                                        )
                                    )
                                else:
                                    # Local parser could not detect swap — fallback
                                    logger.info(
                                        f"[GEYSER] Local parse missed "
                                        f"{signature[:16]}..., "
                                        f"falling back to Helius"
                                    )
                                    asyncio.create_task(
                                        self._parse_and_emit(
                                            signature, fee_payer, grpc_receive_time
                                        )
                                    )
                            else:
                                # No local parser — always use Helius
                                asyncio.create_task(
                                    self._parse_and_emit(
                                        signature, fee_payer, grpc_receive_time
                                    )
                                )

                        except Exception as e:
                            logger.error(f"[GEYSER] Error processing update: {e}")

                finally:
                    # Always clean up ping loop when stream ends
                    if self._ping_task:
                        self._ping_task.cancel()
                        try:
                            await self._ping_task
                        except asyncio.CancelledError:
                            pass
                        self._ping_task = None

                # Stream ended normally
                reconnect_delay = 1.0

            except grpc_aio.AioRpcError as e:
                code = e.code()
                details = e.details() or ""
                logger.error(
                    f"[GEYSER] gRPC error: {code} - {details}"
                )
                if code == grpc.StatusCode.UNAUTHENTICATED:
                    logger.error("[GEYSER] Authentication failed! Check GEYSER_API_KEY")
                    await asyncio.sleep(30)
                elif code == grpc.StatusCode.UNAVAILABLE:
                    logger.warning("[GEYSER] Service unavailable, reconnecting...")
                elif code == grpc.StatusCode.INTERNAL and "RST_STREAM" in details:
                    # RST_STREAM is expected from PublicNode — fast reconnect
                    logger.warning(
                        "[GEYSER] RST_STREAM received (expected from PublicNode), "
                        "fast reconnect in 0.5s"
                    )
                    reconnect_delay = 0.5
                else:
                    logger.warning(f"[GEYSER] Reconnecting in {reconnect_delay}s...")

            except asyncio.CancelledError:
                logger.info("[GEYSER] Stream cancelled")
                return

            except Exception as e:
                logger.error(f"[GEYSER] Unexpected error: {e}")

            # Reconnect with backoff
            if self.running:
                self._stats["reconnects"] += 1
                logger.warning(
                    f"[GEYSER] Reconnecting in {reconnect_delay:.1f}s "
                    f"(total reconnects: {self._stats['reconnects']})"
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

            # Close old channel
            if self._channel:
                try:
                    await self._channel.close()
                except Exception:
                    pass
                self._channel = None

    async def _emit_from_local_parse(
        self, parsed, grpc_receive_time: float
    ):
        """Emit whale buy signal from locally parsed transaction data.

        Same filtering logic as _process_parsed_tx but without HTTP call.
        Latency: ~0-5ms instead of ~650ms.
        """
        try:
            signature = parsed.signature
            fee_payer = parsed.fee_payer

            whale_info = self.whale_wallets.get(fee_payer)
            if not whale_info:
                return

            # Only process BUY signals
            if not parsed.is_buy:
                self._stats["sells_skipped"] += 1
                logger.warning(
                    f"[GEYSER-LOCAL] SELL detected, "
                    f"whale={whale_info.get('label','?')}, "
                    f"tx={signature[:16]}..."
                )
                return

            sol_spent = parsed.sol_amount

            if sol_spent < self.min_buy_amount:
                self._stats["below_min"] += 1
                logger.info(
                    f"[GEYSER-LOCAL] Below min: {sol_spent:.4f} < "
                    f"{self.min_buy_amount} SOL"
                )
                return

            token_received = parsed.token_mint

            # Double-check blacklist (parser already checks, but safety first)
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

            # Check Redis for existing position
            try:
                from trading.redis_state import get_redis_state
                state = await get_redis_state()
                if state and await state.is_connected():
                    if await state.position_exists(token_received):
                        logger.warning(
                            f"[GEYSER-LOCAL] POSITION_EXISTS: "
                            f"{token_received[:16]}..."
                        )
                        self._stats["duplicates"] += 1
                        return
            except Exception:
                pass

            platform = parsed.platform

            # Calculate latency
            latency_ms = (time.monotonic() - grpc_receive_time) * 1000
            self._last_latency_ms = latency_ms

            # Get symbol (async, non-blocking for speed)
            token_symbol = await _fetch_symbol_dexscreener(token_received)

            whale_buy = WhaleBuy(
                whale_wallet=fee_payer,
                token_mint=token_received,
                amount_sol=sol_spent,
                timestamp=datetime.utcnow(),
                tx_signature=signature,
                whale_label=whale_info.get("label", "whale"),
                platform=platform,
                token_symbol=token_symbol,
                age_seconds=0,
                block_time=None,
            )

            self._stats["parse_ok"] += 1

            logger.warning("=" * 70)
            logger.warning(
                f"[GEYSER-LOCAL] WHALE BUY DETECTED (LOCAL PARSE) "
                f"[{latency_ms:.0f}ms latency]"
            )
            logger.warning(f"  WHALE:    {whale_buy.whale_label}")
            logger.warning(f"  WALLET:   {fee_payer}")
            logger.warning(f"  TOKEN:    {token_received}")
            logger.warning(f"  SYMBOL:   {token_symbol or 'fetching...'}")
            logger.warning(f"  AMOUNT:   {sol_spent:.4f} SOL")
            logger.warning(f"  PLATFORM: {platform}")
            logger.warning(f"  TX:       {signature}")
            logger.warning(f"  METHOD:   LOCAL (no Helius API call)")
            logger.warning("=" * 70)

            self._stats["buys_emitted"] += 1

            if self.on_whale_buy:
                logger.warning(
                    f"[GEYSER-LOCAL] Calling callback for "
                    f"{whale_buy.token_symbol}"
                )
                asyncio.create_task(self.on_whale_buy(whale_buy))
            else:
                logger.error("[GEYSER-LOCAL] NO CALLBACK SET!")

        except Exception as e:
            logger.error(f"[GEYSER-LOCAL] Emit error: {e}")

    async def _parse_and_emit(
        self, signature: str, fee_payer: str, grpc_receive_time: float
    ):
        """Fetch parsed transaction from Helius and emit whale buy signal."""
        try:
            # Small delay to let transaction propagate to Helius indexer
            await asyncio.sleep(0.3)

            # Fetch from Helius Enhanced Transactions API
            url = (
                f"https://api.helius.xyz/v0/transactions/"
                f"?api-key={self.helius_parse_api_key}"
            )

            payload = {"transactions": [signature]}

            async with aiohttp.ClientSession() as session:
                # Retry up to 3 times with increasing delay
                for attempt in range(3):
                    try:
                        async with session.post(
                            url,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if data and len(data) > 0:
                                    tx = data[0]
                                    self._stats["parse_ok"] += 1

                                    # Calculate latency
                                    latency_ms = (
                                        time.monotonic() - grpc_receive_time
                                    ) * 1000
                                    self._last_latency_ms = latency_ms

                                    logger.info(
                                        f"[GEYSER] Parsed tx in {latency_ms:.0f}ms: "
                                        f"{signature[:20]}..."
                                    )

                                    # Process same way as webhook
                                    await self._process_parsed_tx(tx, fee_payer)
                                    return
                                else:
                                    # TX not yet indexed, retry
                                    if attempt < 2:
                                        await asyncio.sleep(0.5 * (attempt + 1))
                                        continue
                            elif resp.status == 429:
                                logger.warning("[GEYSER] Rate limited, waiting...")
                                await asyncio.sleep(2)
                                continue
                            else:
                                text = await resp.text()
                                logger.error(
                                    f"[GEYSER] Parse API error {resp.status}: "
                                    f"{text[:200]}"
                                )
                    except asyncio.TimeoutError:
                        if attempt < 2:
                            continue

            self._stats["parse_fail"] += 1
            logger.error(
                f"[GEYSER] Failed to parse tx after 3 attempts: "
                f"{signature[:20]}..."
            )

        except Exception as e:
            self._stats["parse_fail"] += 1
            logger.error(f"[GEYSER] Parse error: {e}")

    async def _process_parsed_tx(self, tx: dict, fee_payer: str):
        """Process parsed Helius transaction — same logic as whale_webhook."""
        try:
            tx_type = tx.get("type", "UNKNOWN")
            signature = tx.get("signature", "")

            if tx_type != "SWAP":
                logger.info(f"[GEYSER] Not a SWAP: {tx_type}, skipping")
                return

            whale_info = self.whale_wallets.get(fee_payer)
            if not whale_info:
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
                logger.warning(
                    f"[GEYSER] SELL detected, "
                    f"whale={whale_info.get('label','?')}, "
                    f"tx={signature[:16]}..."
                )
                return

            if sol_spent < self.min_buy_amount:
                self._stats["below_min"] += 1
                logger.info(
                    f"[GEYSER] Below min: {sol_spent:.4f} < "
                    f"{self.min_buy_amount} SOL"
                )
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

            # Check Redis for existing position
            try:
                from trading.redis_state import get_redis_state
                state = await get_redis_state()
                if state and await state.is_connected():
                    if await state.position_exists(token_received):
                        logger.warning(
                            f"[GEYSER] POSITION_EXISTS: "
                            f"{token_received[:16]}..."
                        )
                        self._stats["duplicates"] += 1
                        return
            except Exception:
                pass

            source = tx.get("source", "unknown")
            platform = self._map_source_to_platform(source)

            timestamp = tx.get("timestamp", 0)
            block_time = timestamp if timestamp else None
            description = tx.get("description", "")

            # Get symbol
            token_symbol = ""
            if description:
                parts = description.split(" for ")
                if len(parts) > 1:
                    parsed_symbol = (
                        parts[-1].split()[-1] if parts[-1] else ""
                    )
                    token_symbol = (
                        parsed_symbol
                        if parsed_symbol.upper() != "SOL"
                        else ""
                    )

            if not token_symbol:
                token_symbol = await _fetch_symbol_dexscreener(token_received)

            whale_buy = WhaleBuy(
                whale_wallet=fee_payer,
                token_mint=token_received,
                amount_sol=sol_spent,
                timestamp=datetime.utcnow(),
                tx_signature=signature,
                whale_label=whale_info.get("label", "whale"),
                platform=platform,
                token_symbol=token_symbol,
                age_seconds=0,
                block_time=block_time,
            )

            logger.warning("=" * 70)
            logger.warning(
                f"[GEYSER] WHALE BUY DETECTED (REAL-TIME gRPC) "
                f"[{self._last_latency_ms:.0f}ms latency]"
            )
            logger.warning(f"  WHALE:    {whale_buy.whale_label}")
            logger.warning(f"  WALLET:   {fee_payer}")
            logger.warning(f"  TOKEN:    {token_received}")
            logger.warning(f"  SYMBOL:   {token_symbol or 'fetching...'}")
            logger.warning(f"  AMOUNT:   {sol_spent:.4f} SOL")
            logger.warning(f"  PLATFORM: {platform}")
            logger.warning(f"  TX:       {signature}")
            logger.warning("=" * 70)

            self._stats["buys_emitted"] += 1

            if self.on_whale_buy:
                logger.warning(
                    f"[GEYSER] Calling callback for {whale_buy.token_symbol}"
                )
                asyncio.create_task(self.on_whale_buy(whale_buy))
            else:
                logger.error("[GEYSER] NO CALLBACK SET!")

        except Exception as e:
            logger.error(f"[GEYSER] Process error: {e}")

    def _map_source_to_platform(self, source: str) -> str:
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

    def get_stats(self) -> dict:
        stats = {**self._stats, "latency_ms": self._last_latency_ms}
        if self._last_pong_time > 0:
            stats["last_pong_ago_s"] = round(
                time.monotonic() - self._last_pong_time, 1
            )
        if self.local_parser:
            stats["local_parser"] = self.local_parser.get_stats()
        return stats

    def get_tracked_wallets(self) -> list[str]:
        return list(self.whale_wallets.keys())
