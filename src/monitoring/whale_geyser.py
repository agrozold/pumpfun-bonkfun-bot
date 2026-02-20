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
import struct
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



@dataclass
class VaultSubscription:
    """Tracks a vault pair subscription for price calculation."""
    mint: str
    symbol: str
    base_vault: str
    quote_vault: str
    decimals: int = 6
    base_reserve: float = 0.0
    quote_reserve: float = 0.0
    price: float = 0.0
    last_update: float = 0.0


@dataclass
class CurveSubscription:
    """Tracks a bonding curve subscription for price calculation.
    Bonding curve layout (pump.fun program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P):
      offset 0x00: u64 discriminator
      offset 0x08: u64 virtualTokenReserves
      offset 0x10: u64 virtualSolReserves
      offset 0x18: u64 realTokenReserves
      offset 0x20: u64 realSolReserves
      offset 0x28: u64 tokenTotalSupply
      offset 0x30: bool complete
    Price formula (same as pumpfun/curve_manager.py):
      price = (vsr / vtr) * (10**TOKEN_DECIMALS) / LAMPORTS_PER_SOL
    """
    mint: str
    symbol: str
    curve_address: str
    decimals: int = 6
    virtual_token_reserves: int = 0
    virtual_sol_reserves: int = 0
    complete: bool = False
    price: float = 0.0
    last_update: float = 0.0




@dataclass
class GrpcInstance:
    """State for a single gRPC stream instance."""
    name: str           # "chainstack" or "publicnode"
    endpoint: str
    api_key: str
    channel: object = None
    stream_task: object = None
    ping_task: object = None
    ping_queue: object = None
    ping_counter: int = 0
    last_pong_time: float = 0.0
    healthy: bool = False
    reconnect_event: object = None
    stats: dict = None

    def __post_init__(self):
        if self.stats is None:
            self.stats = {
                "grpc_messages": 0,
                "tx_detected": 0,
                "reconnects": 0,
                "ping_sent": 0,
                "pong_received": 0,
                "ping_responded": 0,
            }

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
    virtual_sol_reserves: int = 0    # from TradeEvent (for ZERO-RPC direct buy)
    virtual_token_reserves: int = 0  # from TradeEvent (for ZERO-RPC direct buy)
    whale_token_program: str = ""       # S14: from whale TX for direct buy
    whale_creator_vault: str = ""       # S14: from whale TX for direct buy
    whale_fee_recipient: str = ""       # S14: from whale TX for direct buy
    whale_assoc_bonding_curve: str = "" # S14: from whale TX for direct buy


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
        # gRPC config — primary endpoint (PublicNode, always available)
        self.geyser_endpoint = geyser_endpoint or os.getenv(
            "GEYSER_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"
        )
        self.geyser_api_key = geyser_api_key or os.getenv(
            "GEYSER_API_KEY", ""
        )

        # Chainstack gRPC — secondary/premium endpoint (if configured)
        self._chainstack_endpoint = os.getenv("CHAINSTACK_GEYSER_ENDPOINT", "")
        self._chainstack_token = os.getenv("CHAINSTACK_GEYSER_TOKEN", "")
        if self._chainstack_endpoint and ":" not in self._chainstack_endpoint:
            self._chainstack_endpoint = self._chainstack_endpoint + ":443"

        # Build list of gRPC instances (Chainstack first = PRIMARY if available)
        self._grpc_instances: list[GrpcInstance] = []
        if self._chainstack_endpoint and self._chainstack_token:
            self._grpc_instances.append(GrpcInstance(
                name="chainstack",
                endpoint=self._chainstack_endpoint,
                api_key=self._chainstack_token,
            ))
        # PublicNode always added (free, no auth needed but key accepted)
        if self.geyser_endpoint:
            self._grpc_instances.append(GrpcInstance(
                name="publicnode",
                endpoint=self.geyser_endpoint,
                api_key=self.geyser_api_key,
            ))
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
        self._channel = None  # Legacy — kept for compatibility, points to first instance channel
        self._stream_task: Optional[asyncio.Task] = None  # Legacy — not used in dual mode
        self.running = False

        # Keepalive ping state (Phase 5.1) — legacy single-stream fields kept for stats
        self._ping_counter: int = 0
        self._ping_queue: Optional[asyncio.Queue] = None  # Legacy — see _grpc_instances
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

        # Vault tracking for price monitoring (Phase 4b)
        self._vault_subscriptions: dict[str, VaultSubscription] = {}  # mint -> VaultSubscription
        self._vault_address_map: dict[str, str] = {}  # vault_address -> mint
        self._vault_prices: dict[str, tuple[float, float]] = {}  # mint -> (price, timestamp)
        # Reactive SL/TP: mint -> {sl_price, tp_price, entry_price, symbol, triggered}
        self._sl_tp_triggers: dict[str, dict] = {}

        # Bonding curve tracking for price monitoring (Phase 4c)
        self._curve_subscriptions: dict[str, CurveSubscription] = {}  # mint -> CurveSubscription
        self._curve_address_map: dict[str, str] = {}  # curve_address -> mint

        # ATA tracking — detect when tokens arrive on our wallet (Phase 6: instant confirmation)
        self._ata_address_map: dict[str, str] = {}   # ata_address -> mint
        self._ata_pending: dict[str, str] = {}        # mint -> ata_address (pending confirmation)
        self._wallet_pubkey_str: str = ""              # Set by set_wallet_pubkey()

        _instance_names = [g.name for g in self._grpc_instances]
        logger.warning(
            f"[GEYSER] Initialized: {len(self.whale_wallets)} whales, "
            f"min_buy={min_buy_amount} SOL, "
            f"gRPC instances: {_instance_names}"
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

    def set_wallet_pubkey(self, pubkey_str: str):
        """Store our wallet pubkey for ATA derivation."""
        self._wallet_pubkey_str = pubkey_str
        logger.info(f"[GEYSER] Wallet pubkey set: {pubkey_str[:16]}...")

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
        # Trigger reconnect on ALL instances
        for inst in self._grpc_instances:
            if inst.reconnect_event:
                inst.reconnect_event.set()

    async def start(self):
        """Start gRPC streams. Same interface as WhaleWebhookReceiver.start()."""
        self.running = True

        # Pre-create ping queues so subscribe pushes work BEFORE stream connects
        for inst in self._grpc_instances:
            inst.ping_queue = asyncio.Queue(maxsize=100)
            inst.reconnect_event = asyncio.Event()

        # Legacy: point _ping_queue to first instance for early subscribe pushes
        if self._grpc_instances:
            self._ping_queue = self._grpc_instances[0].ping_queue

        # Start a stream task for each gRPC instance
        for inst in self._grpc_instances:
            inst.stream_task = asyncio.create_task(
                self._run_stream_instance(inst)
            )

        # Legacy: point _stream_task to first instance for compatibility
        if self._grpc_instances:
            self._stream_task = self._grpc_instances[0].stream_task

        logger.warning("=" * 70)
        logger.warning("[GEYSER] WHALE GEYSER TRACKER STARTED")
        for inst in self._grpc_instances:
            _label = "PRIMARY" if inst.name == "chainstack" else "SECONDARY" if len(self._grpc_instances) > 1 else "PRIMARY"
            logger.warning(f"[GEYSER] {_label}: {inst.name} ({inst.endpoint})")
        logger.warning(f"[GEYSER] Tracking {len(self.whale_wallets)} whale wallets")
        logger.warning(f"[GEYSER] Min buy amount: {self.min_buy_amount} SOL")
        logger.warning(f"[GEYSER] Keepalive: bidirectional ping every 10s per instance")
        if self.local_parser:
            logger.warning(f"[GEYSER] Mode: LOCAL PARSE (gRPC + local parser, Helius fallback)")
        else:
            logger.warning(f"[GEYSER] Mode: HYBRID (gRPC + Helius Enhanced API)")
        if len(self._grpc_instances) > 1:
            logger.warning(f"[GEYSER] DUAL gRPC: signal dedup via shared _processed_sigs — first wins!")
        logger.warning("=" * 70)

    async def stop(self):
        """Stop all gRPC streams."""
        self.running = False
        for inst in self._grpc_instances:
            # Stop ping loop
            if inst.ping_task:
                inst.ping_task.cancel()
                try:
                    await inst.ping_task
                except asyncio.CancelledError:
                    pass
            # Signal request iterator to stop
            if inst.ping_queue:
                try:
                    inst.ping_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            # Stop stream
            if inst.stream_task:
                inst.stream_task.cancel()
                try:
                    await inst.stream_task
                except asyncio.CancelledError:
                    pass
            if inst.channel:
                try:
                    await inst.channel.close()
                except Exception:
                    pass
        # Legacy cleanup
        if self._channel:
            try:
                await self._channel.close()
            except Exception:
                pass
        logger.info("[GEYSER] All gRPC streams stopped")

    async def _create_channel_for(self, inst: GrpcInstance):
        """Create authenticated gRPC channel for a specific instance."""
        api_key = inst.api_key
        auth = grpc.metadata_call_credentials(
            lambda _, callback, _key=api_key: callback(
                (("x-token", _key),), None
            )
        )
        creds = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(), auth
        )
        inst.channel = grpc_aio.secure_channel(
            inst.endpoint,
            creds,
            options=[
                ("grpc.keepalive_time_ms", 10000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ],
        )
        # Legacy: point self._channel to first instance
        if self._grpc_instances and inst is self._grpc_instances[0]:
            self._channel = inst.channel
        return geyser_pb2_grpc.GeyserStub(inst.channel)

    def _create_subscribe_request(self):
        """Create gRPC subscribe request for whale wallets + vault accounts."""
        request = geyser_pb2.SubscribeRequest()

        # Subscribe to transactions involving ANY of the whale wallets
        whale_addresses = list(self.whale_wallets.keys())

        tx_filter = request.transactions["whale_tracker"]
        for addr in whale_addresses:
            tx_filter.account_include.append(addr)

        # Only successful transactions

        # Session 3: Subscribe to our own wallet for entry price correction
        if self._wallet_pubkey_str and self._wallet_pubkey_str not in whale_addresses:
            tx_filter.account_include.append(self._wallet_pubkey_str)

        tx_filter.failed = False

        # Vault account subscriptions for price tracking (Phase 4b)
        vault_addresses = list(self._vault_address_map.keys())
        if vault_addresses:
            acct_filter = request.accounts["vault_tracker"]
            for addr in vault_addresses:
                acct_filter.account.append(addr)

        # Bonding curve account subscriptions for price tracking (Phase 4c)
        curve_addresses = list(self._curve_address_map.keys())
        if curve_addresses:
            curve_filter = request.accounts["curve_tracker"]
            for addr in curve_addresses:
                curve_filter.account.append(addr)

        # ATA account subscriptions for token arrival detection (Phase 6)
        ata_addresses = list(self._ata_address_map.keys())
        if ata_addresses:
            ata_filter = request.accounts["ata_tracker"]
            for addr in ata_addresses:
                ata_filter.account.append(addr)

        # PROCESSED = fastest, see tx before full confirmation
        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED

        logger.info(
            f"[GEYSER] Subscribe request: {len(whale_addresses)} wallets, "
            f"{len(vault_addresses)} vault accounts, "
            f"{len(self._curve_address_map)} curve accounts, "
            f"{len(self._ata_address_map)} ATA accounts, commitment=PROCESSED"
        )
        return request

    async def _request_iterator(self, initial_request, inst: GrpcInstance = None):
        """Async generator for bidirectional gRPC stream.

        Yields the initial subscription request, then keeps the write-half
        open and yields ping requests from the queue as needed.
        """
        tag = f"GEYSER-{inst.name.upper()}" if inst else "GEYSER"
        queue = inst.ping_queue if inst else self._ping_queue
        yield initial_request
        logger.info(f"[{tag}] Subscription request sent, write-half staying open for pings")

        while True:
            try:
                msg = await queue.get()
                if msg is None:
                    logger.info(f"[{tag}] Request iterator stopping (poison pill)")
                    return
                yield msg
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[{tag}] Request iterator error: {e}")
                return

    async def _ping_loop_for(self, inst: GrpcInstance):
        """Proactive keepalive: send ping every 10 seconds for a specific instance."""
        tag = f"GEYSER-{inst.name.upper()}"
        try:
            await asyncio.sleep(5)
            while self.running:
                inst.ping_counter += 1
                ping_id = inst.ping_counter
                ping_req = geyser_pb2.SubscribeRequest(
                    ping=geyser_pb2.SubscribeRequestPing(id=ping_id)
                )
                try:
                    inst.ping_queue.put_nowait(ping_req)
                    inst.stats["ping_sent"] += 1
                    self._stats["ping_sent"] += 1
                    logger.info(f"[{tag}] Ping sent (proactive, id={ping_id})")
                except asyncio.QueueFull:
                    logger.warning(f"[{tag}] Ping queue full, skipping ping")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    async def _run_stream_instance(self, inst: GrpcInstance):
        """Main gRPC stream loop for a specific instance with auto-reconnect."""
        tag = f"GEYSER-{inst.name.upper()}"
        reconnect_delay = 1.0

        while self.running:
            try:
                stub = await self._create_channel_for(inst)
                request = self._create_subscribe_request()

                # Create fresh ping queue and counter for this connection
                inst.ping_queue = asyncio.Queue(maxsize=100)
                inst.ping_counter = 0

                # Legacy: point self._ping_queue to first instance for subscribe pushes
                if self._grpc_instances and inst is self._grpc_instances[0]:
                    self._ping_queue = inst.ping_queue

                logger.warning(f"[{tag}] Connecting to {inst.endpoint}...")

                # Start proactive ping loop
                inst.ping_task = asyncio.create_task(self._ping_loop_for(inst))

                try:
                    async for update in stub.Subscribe(
                        self._request_iterator(request, inst)
                    ):
                        if not self.running:
                            break
                        if inst.reconnect_event and inst.reconnect_event.is_set():
                            inst.reconnect_event.clear()
                            logger.warning(f"[{tag}] Reconnect triggered by watchdog")
                            break

                        self._stats["grpc_messages"] += 1
                        inst.stats["grpc_messages"] += 1
                        inst.healthy = True

                        # Touch watchdog on ANY gRPC activity
                        if self._watchdog:
                            self._watchdog.touch_grpc()

                        # --- Ping/Pong ---
                        if update.HasField("pong"):
                            self._stats["pong_received"] += 1
                            inst.stats["pong_received"] += 1
                            inst.last_pong_time = time.monotonic()
                            self._last_pong_time = max(self._last_pong_time, inst.last_pong_time)
                            logger.info(f"[{tag}] Pong received (id={update.pong.id})")
                            continue

                        if update.HasField("ping"):
                            inst.ping_counter += 1
                            ping_id = inst.ping_counter
                            ping_req = geyser_pb2.SubscribeRequest(
                                ping=geyser_pb2.SubscribeRequestPing(id=ping_id)
                            )
                            try:
                                inst.ping_queue.put_nowait(ping_req)
                                self._stats["ping_responded"] += 1
                                inst.stats["ping_responded"] += 1
                                logger.info(f"[{tag}] Server ping received, responded with id={ping_id}")
                            except asyncio.QueueFull:
                                logger.warning(f"[{tag}] Ping queue full, could not respond")
                            continue

                        # --- Account updates (vaults + curves + ATA) ---
                        if update.HasField("account"):
                            acct = update.account.account
                            if acct:
                                pk_bytes = bytes(acct.pubkey)
                                pk_str = base58.b58encode(pk_bytes).decode()
                                if pk_str in self._vault_address_map:
                                    self._handle_vault_account_update(update.account)
                                elif pk_str in self._curve_address_map:
                                    self._handle_curve_account_update(update.account)
                                elif pk_str in self._ata_address_map:
                                    self._handle_ata_account_update(update.account)
                            continue

                        # --- Transactions ---
                        if not update.HasField("transaction"):
                            continue

                        grpc_receive_time = time.monotonic()

                        try:
                            tx_wrapper = update.transaction
                            tx = tx_wrapper.transaction

                            sig_bytes = bytes(tx.signature)
                            signature = base58.b58encode(sig_bytes).decode()

                            # DEDUP: shared across all instances — first wins!
                            if signature in self._processed_sigs:
                                self._stats["duplicates"] += 1
                                continue
                            self._processed_sigs.add(signature)
                            if len(self._processed_sigs) > 10000:
                                self._processed_sigs = set(
                                    list(self._processed_sigs)[-5000:]
                                )

                            self._stats["tx_detected"] += 1
                            inst.stats["tx_detected"] += 1
                            if self._watchdog:
                                self._watchdog.touch_grpc_data()

                            msg = tx.transaction.message
                            if not msg or len(msg.account_keys) == 0:
                                continue

                            fee_payer_bytes = bytes(msg.account_keys[0])
                            fee_payer = base58.b58encode(fee_payer_bytes).decode()

                            # Session 4: Diagnostic — detect our wallet in ANY account key
                            if self._wallet_pubkey_str and fee_payer == self._wallet_pubkey_str:
                                logger.warning(f"[GEYSER-SELF] OUR TX detected! sig={signature[:20]}... fee_payer=US")

                            if fee_payer not in self.whale_wallets:
                                # Session 3: Don't skip our own wallet — parse for entry fix
                                if fee_payer == self._wallet_pubkey_str and self.local_parser:
                                    try:
                                        parsed = self.local_parser.parse(tx, fee_payer)
                                        if parsed:
                                            asyncio.create_task(
                                                self._emit_from_local_parse(
                                                    parsed, grpc_receive_time, inst.name
                                                )
                                            )
                                    except Exception as _own_err:
                                        logger.warning(f'[GEYSER-SELF] Parse error: {_own_err}')
                                continue

                            logger.warning(
                                f"[{tag}] TX from whale "
                                f"{self.whale_wallets[fee_payer]['label']}: "
                                f"{signature[:20]}..."
                            )

                            if self.local_parser:
                                parsed = self.local_parser.parse(tx, fee_payer)
                                if parsed:
                                    asyncio.create_task(
                                        self._emit_from_local_parse(
                                            parsed, grpc_receive_time, inst.name
                                        )
                                    )
                                else:
                                    logger.info(
                                        f"[{tag}] Local parse missed "
                                        f"{signature[:16]}..., "
                                        f"falling back to Helius"
                                    )
                                    asyncio.create_task(
                                        self._parse_and_emit(
                                            signature, fee_payer, grpc_receive_time
                                        )
                                    )
                            else:
                                asyncio.create_task(
                                    self._parse_and_emit(
                                        signature, fee_payer, grpc_receive_time
                                    )
                                )

                        except Exception as e:
                            logger.error(f"[{tag}] Error processing update: {e}")

                finally:
                    if inst.ping_task:
                        inst.ping_task.cancel()
                        try:
                            await inst.ping_task
                        except asyncio.CancelledError:
                            pass
                        inst.ping_task = None
                    inst.healthy = False

                reconnect_delay = 1.0

            except grpc_aio.AioRpcError as e:
                inst.healthy = False
                err_code = e.code()
                details = e.details() or ""
                logger.error(f"[{tag}] gRPC error: {err_code} - {details}")
                if err_code == grpc.StatusCode.UNAUTHENTICATED:
                    logger.error(f"[{tag}] Authentication failed! Check API key")
                    await asyncio.sleep(30)
                elif err_code == grpc.StatusCode.UNAVAILABLE:
                    logger.warning(f"[{tag}] Service unavailable, reconnecting...")
                elif err_code == grpc.StatusCode.INTERNAL and "RST_STREAM" in details:
                    logger.warning(f"[{tag}] RST_STREAM received, fast reconnect in 0.5s")
                    reconnect_delay = 0.5
                else:
                    logger.warning(f"[{tag}] Reconnecting in {reconnect_delay}s...")

            except asyncio.CancelledError:
                logger.info(f"[{tag}] Stream cancelled")
                return

            except Exception as e:
                inst.healthy = False
                logger.error(f"[{tag}] Unexpected error: {e}")

            if self.running:
                self._stats["reconnects"] += 1
                inst.stats["reconnects"] += 1
                logger.warning(
                    f"[{tag}] Reconnecting in {reconnect_delay:.1f}s "
                    f"(instance reconnects: {inst.stats['reconnects']})"
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

            if inst.channel:
                try:
                    await inst.channel.close()
                except Exception:
                    pass
                inst.channel = None

    def _push_to_all_queues(self, message):
        """Push a message (subscribe request or ping) to all active gRPC instance queues."""
        pushed = 0
        for inst in self._grpc_instances:
            if inst.ping_queue:
                try:
                    inst.ping_queue.put_nowait(message)
                    pushed += 1
                except asyncio.QueueFull:
                    logger.warning(f"[GEYSER-{inst.name.upper()}] Ping queue full, message dropped")
        return pushed

    async def _emit_from_local_parse(
        self, parsed, grpc_receive_time: float, source_name: str = ""
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
                # Session 3: Check if this is OUR wallet -> fix entry price
                if fee_payer == self._wallet_pubkey_str and parsed.is_buy and parsed.token_amount > 0:
                    _real_entry = parsed.sol_amount / parsed.token_amount
                    _mint = parsed.token_mint
                    logger.warning(
                        f"[GEYSER-SELF] OWN BUY: {_mint[:12]}... "
                        f"sol={parsed.sol_amount:.4f} tok={parsed.token_amount:.2f} "
                        f"real_entry={_real_entry:.10f}"
                    )
                    try:
                        from trading.trader_registry import get_trader
                        _trader = get_trader()
                        if _trader:
                            for _pos in _trader.active_positions:
                                if str(_pos.mint) == _mint:
                                    _old = _pos.entry_price
                                    _corr = (_real_entry - _old) / _old * 100 if _old > 0 else 0
                                    if abs(_corr) > 5:
                                        _pos.entry_price = _real_entry
                                        if hasattr(_pos, 'original_entry_price'):
                                            _pos.original_entry_price = _real_entry
                                        _pos.entry_price_source = 'grpc_execution'
                                        _pos.entry_price_provisional = False
                                        # FIX S18-5: quantity NOT overwritten from CPI
                                        # _pos.quantity = parsed.token_amount  # DISABLED
                                        if hasattr(_trader, 'take_profit_percentage') and _trader.take_profit_percentage and _pos.take_profit_price:
                                            _pos.take_profit_price = _real_entry * (1 + _trader.take_profit_percentage)
                                        if hasattr(_trader, 'stop_loss_percentage') and _trader.stop_loss_percentage and _pos.stop_loss_price:
                                            _pos.stop_loss_price = _real_entry * (1 - _trader.stop_loss_percentage)
                                        _pos.high_water_mark = _real_entry
                                        # FIX S18-5: Do NOT overwrite quantity from CPI event!
                                        # Jupiter quote amount (703k) != CPI event amount (541k)
                                        # Keep original qty from buy result.
                                        logger.warning(
                                            f"[GEYSER-SELF] ENTRY FIXED: {_pos.symbol} "
                                            f"{_old:.10f} -> {_real_entry:.10f} ({_corr:+.1f}%) "
                                            f"qty_kept={_pos.quantity:.2f} (CPI={parsed.token_amount:.2f} ignored) "
                                            f"TP={_pos.take_profit_price or 0:.10f} SL={_pos.stop_loss_price or 0:.10f}"
                                        )
                                        # Register reactive SL/TP with CORRECT prices
                                        try:
                                            self.register_sl_tp(
                                                mint=_mint, symbol=_pos.symbol,
                                                entry_price=_real_entry,
                                                sl_price=_pos.stop_loss_price or 0,
                                                tp_price=_pos.take_profit_price or 0,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            from trading.position import save_positions
                                            save_positions(_trader.active_positions)
                                        except Exception:
                                            pass
                                    else:
                                        _pos.entry_price_provisional = False
                                        _pos.entry_price_source = 'grpc_verified'
                                        # Register reactive SL/TP even when no correction needed
                                        try:
                                            self.register_sl_tp(
                                                mint=_mint, symbol=_pos.symbol,
                                                entry_price=_pos.entry_price,
                                                sl_price=_pos.stop_loss_price or 0,
                                                tp_price=_pos.take_profit_price or 0,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            from trading.position import save_positions
                                            save_positions(_trader.active_positions)
                                        except Exception:
                                            pass
                                        logger.info(f"[GEYSER-SELF] {_pos.symbol}: entry ok ({_corr:+.1f}%), provisional=False, REACTIVE registered")
                                    # FIX S14-1: Check blacklist_sell_pending
                                    if getattr(_pos, 'blacklist_sell_pending', False):
                                        logger.warning(
                                            f"[GEYSER-SELF] [BLACKLIST SELL] {_pos.symbol}: "
                                            f"deployer blacklisted — triggering immediate sell (FIX S14-1)"
                                        )
                                        _pos.blacklist_sell_pending = False
                                        _pos.buy_confirmed = True
                                        _pos.tokens_arrived = True
                                        try:
                                            asyncio.create_task(
                                                _trader._blacklist_instant_sell(_mint, _pos.symbol)
                                            )
                                        except Exception as _bl_sell_err:
                                            logger.error(f"[GEYSER-SELF] Blacklist sell trigger failed: {_bl_sell_err}")
                                    break
                    except Exception as _self_err:
                        logger.warning(f"[GEYSER-SELF] Entry fix error: {_self_err}")
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
            # SPEED FIX: symbol fetch moved to background (saves ~200ms)
            token_symbol = ""

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
                virtual_sol_reserves=getattr(parsed, "virtual_sol_reserves", 0),
                virtual_token_reserves=getattr(parsed, "virtual_token_reserves", 0),
                whale_token_program=getattr(parsed, "whale_token_program", ""),
                whale_creator_vault=getattr(parsed, "whale_creator_vault", ""),
                whale_fee_recipient=getattr(parsed, "whale_fee_recipient", ""),
                whale_assoc_bonding_curve=getattr(parsed, "whale_assoc_bonding_curve", ""),
            )

            self._stats["parse_ok"] += 1

            logger.warning("=" * 70)
            _src = source_name.upper() if source_name else "?"
            logger.warning(
                f"[GEYSER-LOCAL] WHALE BUY DETECTED (LOCAL PARSE via {_src}) "
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
                # SPEED FIX: fetch symbol in background, update position later
                asyncio.create_task(self._deferred_symbol_update(token_received, whale_buy))
            else:
                logger.error("[GEYSER-LOCAL] NO CALLBACK SET!")

        except Exception as e:
            logger.error(f"[GEYSER-LOCAL] Emit error: {e}")


    async def _deferred_symbol_update(self, mint: str, whale_buy_obj):
        """Fetch symbol in background and update whale_buy + position."""
        try:
            symbol = await _fetch_symbol_dexscreener(mint)
            if symbol:
                whale_buy_obj.token_symbol = symbol
                logger.info(f"[SYMBOL] Resolved: {mint[:12]}... -> {symbol}")
                # Update position symbol if trader has it
                try:
                    from trading.trader_registry import get_trader
                    trader = get_trader()
                    if trader:
                        for p in trader.active_positions:
                            if str(p.mint) == mint and (not p.symbol or p.symbol == mint[:8]):
                                p.symbol = symbol
                                logger.info(f"[SYMBOL] Position updated: {mint[:12]}... -> {symbol}")
                                break
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[SYMBOL] Deferred fetch failed for {mint[:12]}: {e}")

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
                # SPEED FIX: symbol fetch moved to background
                token_symbol = token_symbol or ""

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
                virtual_sol_reserves=0,
                virtual_token_reserves=0,
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
                # SPEED FIX: fetch symbol in background
                if not whale_buy.token_symbol:
                    asyncio.create_task(self._deferred_symbol_update(token_received, whale_buy))
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


    # ================================================================
    # Bonding Curve Price Tracking (Phase 4c)
    # ================================================================

    async def subscribe_bonding_curve(
        self,
        mint: str,
        curve_address: str,
        symbol: str = "",
        decimals: int = 6,
    ):
        """Subscribe to bonding curve account updates for price monitoring."""
        try:
            if mint in self._curve_subscriptions:
                logger.info(f'[GEYSER] Curve already subscribed for {symbol} ({mint[:8]}...)')
                return

            sub = CurveSubscription(
                mint=mint,
                symbol=symbol,
                curve_address=curve_address,
                decimals=decimals,
            )
            self._curve_subscriptions[mint] = sub
            self._curve_address_map[curve_address] = mint

            logger.warning(
                f'[GEYSER] +CURVE_SUBSCRIBE {symbol} '
                f'(curve={curve_address[:16]}...)'
            )

            # Push updated subscribe request to ALL gRPC instances
            new_request = self._create_subscribe_request()
            _pushed = self._push_to_all_queues(new_request)
            logger.info(
                f'[GEYSER] Pushed resubscribe to {_pushed} instances: '
                f'{len(self.whale_wallets)} whales '
                f'+ {len(self._vault_subscriptions)} vault pairs '
                f'+ {len(self._curve_subscriptions)} curves '
                f'({len(self._vault_address_map) + len(self._curve_address_map)} accounts)'
            )

        except Exception as e:
            logger.error(f'[GEYSER] Failed to subscribe curve for {symbol}: {e}')

    async def unsubscribe_bonding_curve(self, mint: str) -> bool:
        """Remove bonding curve subscription and push updated request."""
        try:
            sub = self._curve_subscriptions.pop(mint, None)
            if not sub:
                return False

            self._curve_address_map.pop(sub.curve_address, None)
            # Also remove from vault prices cache (shared cache)
            self._vault_prices.pop(mint, None)

            logger.info(f'[GEYSER] -CURVE_UNSUBSCRIBE {sub.symbol} ({mint[:8]}...)')

            new_request = self._create_subscribe_request()
            self._push_to_all_queues(new_request)
            return True
        except Exception as e:
            logger.error(f'[GEYSER] Failed to unsubscribe curve for {mint[:8]}: {e}')
            return False

    def get_curve_price(self, mint: str, max_age: float = 120.0) -> float | None:
        """Get current bonding curve-derived price. Returns None if no data or stale."""
        sub = self._curve_subscriptions.get(mint)
        if not sub or sub.price <= 0:
            return None
        if time.time() - sub.last_update > max_age:
            return None
        return sub.price

    def _handle_curve_account_update(self, account_update) -> None:
        """Process bonding curve account update from gRPC stream.
        Decodes virtualTokenReserves and virtualSolReserves, calculates price.
        Uses EXACT same formula as pumpfun/curve_manager.py:
          price = (vsr / vtr) * (10**TOKEN_DECIMALS) / LAMPORTS_PER_SOL
        Where TOKEN_DECIMALS=6, LAMPORTS_PER_SOL=1_000_000_000.
        """
        try:
            acct = account_update.account
            if not acct:
                return

            pubkey_bytes = bytes(acct.pubkey)
            pubkey_str = base58.b58encode(pubkey_bytes).decode()

            mint = self._curve_address_map.get(pubkey_str)
            if not mint:
                return

            sub = self._curve_subscriptions.get(mint)
            if not sub:
                return

            data = bytes(acct.data)
            # Minimum size: 8 (discriminator) + 5*8 (reserves) + 1 (complete) = 49 bytes
            if len(data) < 49:
                logger.warning(f'[GEYSER] Curve data too short: {len(data)}b for {sub.symbol}')
                return

            # Decode bonding curve fields (all little-endian u64)
            virtual_token_reserves = struct.unpack('<Q', data[8:16])[0]
            virtual_sol_reserves = struct.unpack('<Q', data[16:24])[0]
            complete = bool(data[48])

            # If curve completed (migrated), log and stop tracking
            if complete and not sub.complete:
                sub.complete = True
                logger.warning(f'[GEYSER] Curve COMPLETE (migrated): {sub.symbol} — will need vault tracking')
                return

            if complete:
                return

            if virtual_token_reserves <= 0 or virtual_sol_reserves <= 0:
                return

            sub.virtual_token_reserves = virtual_token_reserves
            sub.virtual_sol_reserves = virtual_sol_reserves
            sub.last_update = time.time()

            # Price formula — EXACT same as pumpfun/curve_manager.py:
            # price = (virtual_sol_reserves / virtual_token_reserves) * (10**TOKEN_DECIMALS) / LAMPORTS_PER_SOL
            TOKEN_DECIMALS = sub.decimals  # 6 for pump.fun
            LAMPORTS_PER_SOL = 1_000_000_000
            old_price = sub.price
            sub.price = (virtual_sol_reserves / virtual_token_reserves) * (10 ** TOKEN_DECIMALS) / LAMPORTS_PER_SOL

            # Store in shared vault_prices cache so get_vault_price() also returns it
            self._vault_prices[mint] = (sub.price, time.time())

            # First curve price tick: sync entry_price for provisional positions (async, non-blocking)
            try:
                if old_price <= 0 and sub.price > 0:
                    asyncio.ensure_future(self._sync_entry_price_from_curve(mint, sub.price))
            except Exception:
                pass

            # === REACTIVE SL/TP — check at every gRPC tick ===
            _trigger = self._sl_tp_triggers.get(mint)
            if _trigger and not _trigger["triggered"]:
                _sl = _trigger.get("sl_price", 0)
                _tp = _trigger.get("tp_price", 0)
                _entry_p = _trigger.get("entry_price", 0)
                _pnl_now = (sub.price - _entry_p) / max(_entry_p, 1e-15) * 100 if _entry_p > 0 else 0
                _entry_t_log = _trigger.get('entry_time')
                _age_log = (time.time() - _entry_t_log) if _entry_t_log else 999
                # S18-6: Log every SL/TP check (throttled to every 2s)
                _last_log = _trigger.get('_last_log_time', 0)
                if time.time() - _last_log >= 2.0:
                    _trigger['_last_log_time'] = time.time()
                    logger.info(
                        f"[REACTIVE CHECK] {sub.symbol}: price={sub.price:.10f} "
                        f"entry={_entry_p:.10f} SL={_sl:.10f} TP={_tp:.10f} "
                        f"PnL={_pnl_now:+.1f}% age={_age_log:.1f}s"
                    )
                if _sl and sub.price <= _sl:
                    # UNIFIED DYNAMIC SL — mirrors position.py thresholds exactly
                    # FIX S18-7: Single source of truth for SL thresholds
                    _entry_t = _trigger.get('entry_time')
                    _p_age = (time.time() - _entry_t) if _entry_t else 999
                    _ep = _trigger['entry_price']
                    _pnl = (sub.price - _ep) / max(_ep, 1e-15) * 100
                    _do_sl = True
                    _effective_label = "NORMAL -20%"

                    # Thresholds MUST match position.py exactly:
                    #   0-15s:   -45% (whale impact absorption)
                    #   15-60s:  -35% (settling period)
                    #   60-120s: -30% (stabilization)
                    #   120s+:   -20% (config SL)
                    if _p_age < 15.0:
                        if _pnl > -35.0:
                            _do_sl = False
                            _effective_label = "WIDENED -35%"
                        elif sub.price <= _ep * 0.65:
                            _do_sl = True  # absolute floor
                    elif _p_age < 60.0:
                        if _pnl > -35.0:
                            _do_sl = False
                            _effective_label = "WIDENED -35%"
                        elif sub.price <= _ep * 0.65:
                            _do_sl = True
                    elif _p_age < 120.0:
                        if _pnl > -30.0:
                            _do_sl = False
                            _effective_label = "WIDENED -30%"
                        elif sub.price <= _ep * 0.70:
                            _do_sl = True

                    if _do_sl:
                        _trigger['triggered'] = True
                        logger.warning(
                            f'[REACTIVE SL] {sub.symbol}: {sub.price:.10f} <= SL {_sl:.10f} '
                            f'(PnL: {_pnl:.1f}%, age: {_p_age:.0f}s, threshold: {_effective_label}) '
                            f'— INSTANT SELL!'
                        )
                        asyncio.ensure_future(self._reactive_sell(mint, sub.symbol, sub.price, 'stop_loss', _pnl))
                    else:
                        # S18-6: Log when SL is widened (throttled)
                        _last_dsl = _trigger.get('_last_dsl_log', 0)
                        if time.time() - _last_dsl >= 2.0:
                            _trigger['_last_dsl_log'] = time.time()
                            logger.warning(
                                f'[DYNAMIC SL] {sub.symbol}: price={sub.price:.10f} '
                                f'PnL={_pnl:.1f}% age={_p_age:.1f}s — {_effective_label} active, holding '
                                f'(config SL={_sl:.10f} at -20%)'
                            )
                elif _tp and _tp > 0 and sub.price >= _tp:
                    _entry_t_tp = _trigger.get('entry_time')
                    _tp_age = (time.time() - _entry_t_tp) if _entry_t_tp else 999
                    if _tp_age < 0.3:
                        logger.info(f'[REACTIVE TP] {sub.symbol}: price >= TP but age={_tp_age:.2f}s < 0.3s — COOLDOWN')
                    else:
                        _trigger["triggered"] = True
                        _pnl = (sub.price - _trigger["entry_price"]) / max(_trigger["entry_price"], 1e-15) * 100
                        logger.warning(f'[REACTIVE TP] {sub.symbol}: {sub.price:.10f} >= TP {_tp:.10f} (PnL: {_pnl:.1f}%) — INSTANT SELL!')
                        asyncio.ensure_future(self._reactive_sell(mint, sub.symbol, sub.price, "take_profit", _pnl))

            if old_price <= 0:
                logger.warning(
                    f'[GEYSER] Curve FIRST price: {sub.symbol} '
                    f'{sub.price:.10f} SOL '
                    f'(vtr={virtual_token_reserves}, vsr={virtual_sol_reserves})'
                )
            elif abs(sub.price - old_price) / max(old_price, 1e-15) > 0.005:
                change_pct = (sub.price - old_price) / old_price * 100
                _has_trigger = mint in self._sl_tp_triggers
                _sl_tp_tag = " [SL/TP]" if _has_trigger else ""
                logger.info(
                    f'[GEYSER] Curve price: {sub.symbol} '
                    f'{sub.price:.10f} SOL ({change_pct:+.1f}%){_sl_tp_tag}'
                )

        except Exception as e:
            logger.error(f'[GEYSER] Curve account update error: {e}')

        # Vault Price Tracking (Phase 4b)
    # ================================================================

    async def subscribe_vault_accounts(
        self,
        mint: str,
        base_vault: str,
        quote_vault: str,
        symbol: str = '???',
        decimals: int = 6,
    ) -> bool:
        """Subscribe to vault account updates for price monitoring."""
        try:
            if mint in self._vault_subscriptions:
                logger.info(f'[GEYSER] Vault already subscribed for {symbol} ({mint[:8]}...)')
                return True

            sub = VaultSubscription(
                mint=mint,
                symbol=symbol,
                base_vault=base_vault,
                quote_vault=quote_vault,
                decimals=decimals,
            )

            self._vault_subscriptions[mint] = sub
            self._vault_address_map[base_vault] = mint
            self._vault_address_map[quote_vault] = mint

            logger.warning(
                f'[GEYSER] +VAULT_SUBSCRIBE {symbol} '
                f'(base={base_vault[:16]}..., quote={quote_vault[:16]}...)'
            )

            # Push updated subscription request to ALL gRPC instances
            new_request = self._create_subscribe_request()
            _pushed = self._push_to_all_queues(new_request)
            logger.warning(
                f'[GEYSER] Pushed vault subscribe to {_pushed} instances: '
                f'{len(self.whale_wallets)} wallets '
                f'+ {len(self._vault_subscriptions)} vault pairs '
                f'({len(self._vault_address_map)} vault accounts)'
            )

            return True

        except Exception as e:
            logger.error(f'[GEYSER] Failed to subscribe vaults for {symbol}: {e}')
            return False

    async def unsubscribe_vault_accounts(self, mint: str) -> bool:
        """Remove vault subscription and push updated request."""
        try:
            sub = self._vault_subscriptions.pop(mint, None)
            if not sub:
                return False

            self._vault_address_map.pop(sub.base_vault, None)
            self._vault_address_map.pop(sub.quote_vault, None)
            self._vault_prices.pop(mint, None)

            logger.info(f'[GEYSER] -VAULT_UNSUBSCRIBE {sub.symbol} ({mint[:8]}...)')

            new_request = self._create_subscribe_request()
            self._push_to_all_queues(new_request)

            return True

        except Exception as e:
            logger.error(f'[GEYSER] Failed to unsubscribe vaults for {mint[:8]}: {e}')
            return False

    async def _reactive_sell(self, mint: str, symbol: str, price: float, reason: str, pnl_pct: float):

        # === NEGATIVE-TOKEN GUARD ===
        def _safe_remaining(total, sold, decimals=6):
            total, sold = int(total), int(sold)
            if total > 0 and sold > 0:
                ratio = sold / total
                if ratio > 100:
                    total = total * (10 ** decimals)
                elif ratio < 0.00001 and total > 10 ** (decimals + 2):
                    sold = sold * (10 ** decimals)
            result = total - sold
            if result < 0:
                logger.warning(f'[NEG_GUARD] Clamped {result} to 0 (total={total}, sold={sold})')
                return 0
            return result
        # === END GUARD ===
        """Instant sell triggered by gRPC price tick. Bypasses monitor 1s delay."""
        try:
            from trading.trader_registry import get_trader
            trader = get_trader()
            if not trader:
                logger.error(f"[REACTIVE] No trader for {symbol} sell!")
                return
            # Find position and token_info
            position = None
            for p in trader.active_positions:
                if str(p.mint) == mint:
                    position = p
                    break
            if not position or not position.is_active:
                logger.warning(f"[REACTIVE] No active position for {symbol}")
                return
            if getattr(position, 'is_selling', False):
                logger.warning(f"[REACTIVE] {symbol} already selling, skip")
                return
            # Build TokenInfo
            from interfaces.core import TokenInfo
            from solders.pubkey import Pubkey
            bc = Pubkey.from_string(position.bonding_curve) if position.bonding_curve else None
            token_info = TokenInfo(
                name=symbol, symbol=symbol, uri="", mint=Pubkey.from_string(mint),
                platform=trader.platform, bonding_curve=bc, creator=None, creator_vault=None,
            )
            # Determine sell quantity
            from trading.position import ExitReason
            if reason == "take_profit" and position.tp_sell_pct < 1.0:
                sell_qty = min(position.quantity * position.tp_sell_pct, position.quantity)
                exit_reason = ExitReason.TAKE_PROFIT
                skip_cleanup = True
            else:
                sell_qty = position.quantity
                exit_reason = ExitReason.STOP_LOSS if reason == "stop_loss" else ExitReason.TAKE_PROFIT
                skip_cleanup = False
            # FIX S18-9: HARD BLOCK sell if tokens not on wallet + TP cooldown 8s
            _tokens_ok = getattr(position, 'tokens_arrived', True)
            _buy_ok = getattr(position, 'buy_confirmed', True)
            # A) tokens_arrived=False -> HARD BLOCK (can't sell what you don't have)
            if not _tokens_ok:
                logger.warning(f"[REACTIVE] HARD BLOCK: {symbol} tokens_arrived=False — cannot sell without tokens")
                _trigger = self._sl_tp_triggers.get(mint)
                if _trigger:
                    _trigger["triggered"] = False
                return
            # B) buy_confirmed=False but tokens arrived -> allow (tokens are real)
            if not _buy_ok:
                logger.info(f"[REACTIVE] {symbol} buy_confirmed=False but tokens_arrived=True — proceeding")
            # C) TP cooldown: block TP for 8s after entry fix (price bouncing from whale impact)
            if reason == "take_profit":
                import time as _time_mod
                _reactive_reg = self._sl_tp_triggers.get(mint, {})
                _reg_time = _reactive_reg.get("entry_time", 0)
                _since_reg = _time_mod.time() - _reg_time if _reg_time else 999
                if _since_reg < 2.0:
                    logger.warning(
                        f"[REACTIVE] TP COOLDOWN: {symbol} only {_since_reg:.1f}s since entry fix "
                        f"(need 2s) — skip"
                    )
                    _trigger = self._sl_tp_triggers.get(mint)
                    if _trigger:
                        _trigger["triggered"] = False
                    return
            position.is_selling = True
            logger.warning(
                f"[REACTIVE SELL] Launching FAST SELL for {symbol} "
                f"reason={reason} PnL={pnl_pct:.1f}% price={price:.10f} "
                f"qty={sell_qty:.2f} skip_cleanup={skip_cleanup} "
                f"exit_reason={exit_reason} tokens_arrived={_tokens_ok} buy_confirmed={_buy_ok}"
            )
            success = await trader._fast_sell_with_timeout(
                token_info, position, price, sell_qty, skip_cleanup=skip_cleanup, exit_reason=exit_reason
            )
            if success:
                logger.warning(
                    f"[REACTIVE SELL] SUCCESS for {symbol} reason={reason} "
                    f"sold_qty={sell_qty:.2f} price={price:.10f} PnL={pnl_pct:.1f}%"
                )
            else:
                logger.error(
                    f"[REACTIVE SELL] FAILED for {symbol} reason={reason} "
                    f"qty={sell_qty:.2f} price={price:.10f}"
                )
            if success and reason == "take_profit" and position.tp_sell_pct < 1.0:
                remaining = max(0, position.quantity * (1 - position.tp_sell_pct))
                if remaining > 1.0:
                    position.quantity = remaining
                    position.take_profit_price = None
                    position.tp_partial_done = True
                    # FIX S12-3: Apply moonbag metadata (was missing in REACTIVE path!)
                    # Must match universal_trader.py FIX 11-3 exactly.
                    # Without this, positions get is_moonbag=False, tsl_trail_pct=0.30 (wrong),
                    # HARD SL can kill moonbags, and TSL is too tight.
                    position.is_moonbag = True
                    # FIX S18-10: moonbag from yaml via trader
                    _sl_pct_r = getattr(trader, 'stop_loss_percentage', 0.20) or 0.20
                    _sl_from_r = price * (1 - _sl_pct_r)
                    position.tsl_trail_pct = getattr(trader, 'tsl_trail_pct', 0.30)
                    position.tsl_sell_pct = getattr(trader, 'tsl_sell_pct', 0.50)
                    position.stop_loss_price = max(_sl_from_r, position.entry_price)
                    # Force-activate TSL with wide trail for remaining tokens
                    if not position.tsl_active and trader.tsl_enabled:
                        position.tsl_active = True
                    position.tsl_enabled = True
                    position.high_water_mark = max(price, position.high_water_mark or 0)
                    position.tsl_trigger_price = position.high_water_mark * (1 - position.tsl_trail_pct)
                    # Re-register reactive SL (no TP) as safety net
                    self._sl_tp_triggers[mint] = {
                        'triggered': False,
                        'entry_price': position.entry_price,
                        'sl_price': position.stop_loss_price,  # FIX S18-10: calculated SL
                        'tp_price': None,
                        'entry_time': time.time(),
                        '_last_log_time': 0, '_last_dsl_log': 0,
                        'is_moonbag': True,  # FIX S18-9: flag for moonbag
                    }
                    logger.info(f"[REACTIVE TP] {mint[:8]} re-registered SL={position.entry_price*0.20:.8f}")
                    trader._save_position(position)
                    logger.warning(f"[REACTIVE TP] Partial done, keeping {remaining:.0f} tokens as MOONBAG. TSL active: HWM={position.high_water_mark:.10f}, trigger={position.tsl_trigger_price:.10f}, trail=50%")
                    # FIX S18-9: Unsubscribe from gRPC curve — moonbag switches to batch price monitor
                    try:
                        await self.unsubscribe_curve(mint)
                        logger.warning(f"[REACTIVE TP] {symbol}: Curve UNSUBSCRIBED — moonbag now on batch price monitor")
                    except Exception as _unsub_err:
                        logger.warning(f"[REACTIVE TP] Curve unsubscribe failed: {_unsub_err}")
            # FIX S17-2: Reset is_selling after BOTH success and failure
            # Previously only reset on failure — caused monitor loop to see is_selling=True
            # forever after partial TP, potentially triggering double-sell path
            position.is_selling = False
            if not success:
                pass  # is_selling already reset above
                _trigger = self._sl_tp_triggers.get(mint)
                if _trigger:
                    _trigger["triggered"] = False  # Allow retry
                logger.error(f"[REACTIVE] Sell FAILED for {symbol}, will retry via monitor")
        except Exception as e:
            logger.error(f"[REACTIVE] Sell error for {symbol}: {e}")

    def register_sl_tp(self, mint: str, symbol: str, entry_price: float, sl_price: float, tp_price: float = None):
        """Register SL/TP levels for reactive checking on every gRPC tick."""
        _now = time.time()
        self._sl_tp_triggers[mint] = {
            "symbol": symbol, "entry_price": entry_price,
            "sl_price": sl_price, "tp_price": tp_price, "triggered": False,
            "entry_time": _now, "_last_log_time": 0, "_last_dsl_log": 0,
        }
        _tp_str = f"{tp_price:.10f}" if tp_price else "None"
        _sl_pct = (entry_price - sl_price) / max(entry_price, 1e-15) * 100 if sl_price else 0
        _tp_pct = (tp_price - entry_price) / max(entry_price, 1e-15) * 100 if tp_price else 0
        logger.warning(
            f"[REACTIVE] Registered SL/TP for {symbol}: "
            f"entry={entry_price:.10f} SL={sl_price:.10f} (-{_sl_pct:.0f}%) "
            f"TP={_tp_str} (+{_tp_pct:.0f}%) time={_now:.0f}"
        )

    def unregister_sl_tp(self, mint: str):
        """Remove SL/TP trigger for mint."""
        self._sl_tp_triggers.pop(mint, None)

    async def _sync_entry_price_from_curve(self, mint: str, curve_price: float):
        """One-time correction of entry_price from first curve tick (non-blocking)."""
        if curve_price <= 0:
            return
        try:
            from trading.trader_registry import get_trader
            trader = get_trader()
            if not trader:
                return
            pos = None
            for p in trader.active_positions:
                if str(p.mint) == mint:
                    pos = p
                    break
            if not pos:
                return

            # Старые позиции без поля не трогаем
            if not getattr(pos, "entry_price_provisional", False):
                return

            old = pos.entry_price or 0.0
            if old <= 0:
                pos.entry_price = curve_price
            else:
                deviation = abs(curve_price - old) / max(old, 1e-15)
                # Если в пределах 5% — считаем нормальной, не трогаем
                if deviation <= 0.05:
                    pos.entry_price_provisional = False
                    return
                pos.entry_price = curve_price

            pos.high_water_mark = pos.entry_price

            # Обновляем TP/SL если заданы
            if pos.take_profit_price is not None:
                # trader.take_profit_percentage может быть None
                try:
                    tp_pct = getattr(trader, "take_profit_percentage", None)
                    if tp_pct is not None:
                        pos.take_profit_price = pos.entry_price * (1 + tp_pct)
                except Exception:
                    pass
            if pos.stop_loss_price is not None:
                try:
                    sl_pct = getattr(trader, "stop_loss_percentage", None)
                    if sl_pct is not None:
                        pos.stop_loss_price = pos.entry_price * (1 - sl_pct)
                except Exception:
                    pass

            pos.entry_price_provisional = False

            # Синхронизируем базу для реактивного SL/TP
            trig = self._sl_tp_triggers.get(mint)
            if trig:
                trig["entry_price"] = pos.entry_price

            try:
                from trading.position import save_positions
                save_positions(trader.active_positions)
            except Exception:
                pass

            try:
                sym = getattr(pos, "symbol", mint[:8])
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    f"[ENTRY_SYNC] {sym}: entry fixed to {pos.entry_price:.10f} from curve"
                )
            except Exception:
                pass
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).error(f"[ENTRY_SYNC] Failed for {mint[:8]}: {e}")

    def get_vault_price(self, mint: str, max_age: float = 120.0) -> float | None:
        """Get current vault-derived price. Returns None if no data or stale."""
        price_data = self._vault_prices.get(mint)
        if not price_data:
            return None
        price, timestamp = price_data
        if time.time() - timestamp > max_age:
            return None
        return price

    def _handle_vault_account_update(self, account_update) -> None:
        """Process vault account update from gRPC stream.
        Decodes SPL Token Account balance and recalculates price."""
        try:
            acct = account_update.account
            if not acct:
                return

            pubkey_bytes = bytes(acct.pubkey)
            pubkey_str = base58.b58encode(pubkey_bytes).decode()

            mint = self._vault_address_map.get(pubkey_str)
            if not mint:
                return

            sub = self._vault_subscriptions.get(mint)
            if not sub:
                return

            data = bytes(acct.data)
            if len(data) < 72:
                logger.warning(f'[GEYSER] Vault data too short: {len(data)}b for {sub.symbol}')
                return

            raw_amount = struct.unpack('<Q', data[64:72])[0]

            if pubkey_str == sub.base_vault:
                sub.base_reserve = raw_amount / (10 ** sub.decimals)
            elif pubkey_str == sub.quote_vault:
                sub.quote_reserve = raw_amount / (10 ** 9)
            else:
                return

            sub.last_update = time.time()

            if sub.base_reserve > 0 and sub.quote_reserve > 0:
                old_price = sub.price
                sub.price = sub.quote_reserve / sub.base_reserve
                self._vault_prices[mint] = (sub.price, time.time())

                if old_price <= 0:
                    logger.warning(
                        f'[GEYSER] Vault FIRST price: {sub.symbol} '
                        f'{sub.price:.10f} SOL '
                        f'(base={sub.base_reserve:.2f}, quote={sub.quote_reserve:.6f})'
                    )
                elif abs(sub.price - old_price) / max(old_price, 1e-15) > 0.05:
                    logger.info(
                        f'[GEYSER] Vault price move: {sub.symbol} '
                        f'{sub.price:.10f} SOL ({((sub.price - old_price) / old_price * 100):+.1f}%)'
                    )

        except Exception as e:
            logger.error(f'[GEYSER] Vault account update error: {e}')


    # ================================================================
    # ATA Tracking — detect token arrival on wallet (Phase 6)
    # ================================================================

    async def subscribe_ata(self, mint: str, ata_address: str, symbol: str = ""):
        """Subscribe to our wallet's ATA for this token.
        When tokens arrive (balance > 0), sets tokens_arrived=True on the position."""
        try:
            if mint in self._ata_pending:
                logger.info(f"[GEYSER] ATA already subscribed for {symbol} ({mint[:8]}...)")
                return

            self._ata_pending[mint] = ata_address
            self._ata_address_map[ata_address] = mint

            logger.warning(
                f"[GEYSER] +ATA_SUBSCRIBE {symbol} "
                f"(ata={ata_address[:16]}...)"
            )

            # Push updated subscribe request to ALL gRPC instances
            new_request = self._create_subscribe_request()
            _pushed = self._push_to_all_queues(new_request)
            logger.info(
                f"[GEYSER] Pushed ATA resubscribe to {_pushed} instances: "
                f"{len(self._ata_address_map)} ATA accounts"
            )

        except Exception as e:
            logger.error(f"[GEYSER] Failed to subscribe ATA for {symbol}: {e}")

    async def unsubscribe_ata(self, mint: str) -> bool:
        """Remove ATA subscription for a token (after sell or cleanup)."""
        try:
            ata_addr = self._ata_pending.pop(mint, None)
            if ata_addr:
                self._ata_address_map.pop(ata_addr, None)
                logger.info(f"[GEYSER] -ATA_UNSUBSCRIBE {mint[:8]}...")

                new_request = self._create_subscribe_request()
                self._push_to_all_queues(new_request)
                return True
            return False
        except Exception as e:
            logger.error(f"[GEYSER] Failed to unsubscribe ATA for {mint[:8]}: {e}")
            return False

    def _handle_ata_account_update(self, account_update) -> None:
        """Process ATA account update from gRPC stream.
        When balance > 0, tokens have arrived — mark position as ready to sell."""
        try:
            acct = account_update.account
            if not acct:
                return

            pubkey_bytes = bytes(acct.pubkey)
            pubkey_str = base58.b58encode(pubkey_bytes).decode()

            mint = self._ata_address_map.get(pubkey_str)
            if not mint:
                return

            data = bytes(acct.data)
            # SPL Token Account layout: offset 64:72 = amount (u64 little-endian)
            if len(data) < 72:
                return

            raw_amount = struct.unpack('<Q', data[64:72])[0]

            if raw_amount > 0:
                logger.warning(
                    f"[GEYSER] ATA TOKENS ARRIVED! mint={mint[:8]}... "
                    f"amount={raw_amount} (raw)"
                )

                # Mark position as tokens_arrived + buy_confirmed in trader
                try:
                    from trading.trader_registry import get_trader
                    trader = get_trader()
                    if trader:
                        for pos in trader.active_positions:
                            if str(pos.mint) == mint:
                                pos.tokens_arrived = True
                                pos.buy_confirmed = True
                                # Update actual quantity from on-chain data
                                # Token decimals = 6 for pump.fun
                                actual_qty = raw_amount / (10 ** 6)
                                if actual_qty > 0 and abs(actual_qty - pos.quantity) / max(pos.quantity, 1) > 0.01:
                                    logger.warning(
                                        f"[GEYSER] ATA quantity update: {pos.quantity:.2f} -> {actual_qty:.2f}"
                                    )
                                    pos.quantity = actual_qty
                                logger.warning(
                                    f"[GEYSER] tokens_arrived=True, buy_confirmed=True for {pos.symbol}"
                                )
                                # Save to disk
                                from trading.position import save_positions
                                save_positions(trader.active_positions)
                                break
                except Exception as e:
                    logger.error(f"[GEYSER] ATA trader update error: {e}")

                # Cleanup — no longer need to watch this ATA
                self._ata_pending.pop(mint, None)
                self._ata_address_map.pop(pubkey_str, None)

        except Exception as e:
            logger.error(f"[GEYSER] ATA account update error: {e}")

    def get_stats(self) -> dict:
        stats = {**self._stats, "latency_ms": self._last_latency_ms}
        if self._last_pong_time > 0:
            stats["last_pong_ago_s"] = round(
                time.monotonic() - self._last_pong_time, 1
            )
        stats["curve_subscriptions"] = len(self._curve_subscriptions)
        stats["curve_accounts"] = len(self._curve_address_map)
        # Per-instance stats
        stats["grpc_instances"] = {}
        for inst in self._grpc_instances:
            inst_stats = {**inst.stats, "healthy": inst.healthy, "endpoint": inst.endpoint}
            if inst.last_pong_time > 0:
                inst_stats["last_pong_ago_s"] = round(time.monotonic() - inst.last_pong_time, 1)
            stats["grpc_instances"][inst.name] = inst_stats
        if self.local_parser:
            stats["local_parser"] = self.local_parser.get_stats()
        return stats

    def get_tracked_wallets(self) -> list[str]:
        return list(self.whale_wallets.keys())
