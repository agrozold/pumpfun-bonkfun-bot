"""
Universal trading coordinator that works with any platform.
Cleaned up to remove all platform-specific hardcoding.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from time import monotonic
import time

import uvloop
from solders.pubkey import Pubkey

from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.wallet import Wallet
from interfaces.core import Platform, TokenInfo
from monitoring.listener_factory import ListenerFactory
from monitoring.pump_pattern_detector import PumpPatternDetector
from monitoring.token_scorer import TokenScorer
# Use WhalePoller instead of WhaleTracker (logsSubscribe doesn't work for wallets)
try:
    from monitoring.whale_poller import WhalePoller, WhaleBuy
    WHALE_POLLER_AVAILABLE = True
except ImportError:
    from monitoring.whale_tracker import WhaleTracker as WhalePoller, WhaleBuy
    WHALE_POLLER_AVAILABLE = False

# Webhook-based whale tracking (real-time)
try:
    from monitoring.whale_webhook import WhaleWebhookReceiver
    WHALE_WEBHOOK_AVAILABLE = True
except ImportError:
    WHALE_WEBHOOK_AVAILABLE = False
from monitoring.dev_reputation import DevReputationChecker

# Geyser gRPC whale tracking (ultra-low latency)
try:
    from monitoring.whale_geyser import WhaleGeyserReceiver
    WHALE_GEYSER_AVAILABLE = True
except ImportError:
    WHALE_GEYSER_AVAILABLE = False
# Signal deduplication for dual-receiver mode (gRPC + Webhook)
try:
    from monitoring.signal_dedup import SignalDedup
    SIGNAL_DEDUP_AVAILABLE = True
except ImportError:
    SIGNAL_DEDUP_AVAILABLE = False
# Dual-channel watchdog for health monitoring (Phase 5.3)
try:
    from monitoring.watchdog import DualChannelWatchdog
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
# Phase 4: Real-time gRPC price stream for SL/TP monitoring
try:
    from monitoring.price_stream import PriceStream
    PRICE_STREAM_AVAILABLE = True
except ImportError:
    PRICE_STREAM_AVAILABLE = False
from monitoring.trending_scanner import TrendingScanner, TrendingToken
from monitoring.volume_pattern_analyzer import VolumePatternAnalyzer, TokenVolumeAnalysis
from platforms import get_platform_implementations
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import Position, save_positions, load_positions, load_positions_async, remove_position, ExitReason, register_monitor, unregister_monitor
from security.token_vetter import TokenVetter, VetResult
from trading.purchase_history import (
    was_token_purchased,
    add_to_purchase_history,
    load_purchase_history,
)
# === DEDUP STORE INTEGRATION ===
from trading.dedup_store import (
    get_dedup_store,
    try_acquire_token,
    mark_token_bought,
    RedisDedupStore,
)
# === TRACE CONTEXT INTEGRATION ===
from analytics.trace_context import TraceContext, get_current_trace
from analytics.trace_recorder import init_trace_recorder, shutdown_trace_recorder
from trading.position import is_token_in_positions
from utils.logger import get_logger
# Batch price service for rate-limit-safe price fetching
from utils.batch_price_service import (
    init_batch_price_service,
    watch_token,
    unwatch_token,
    get_cached_price,
)

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = get_logger(__name__)

# === ТОКЕНЫ БЕЗ STOP-LOSS (даже emergency) ===
NO_SL_MINTS = {
    "DprzJaFkjaNkGXGUCuNWcfEJmxw3dmCzjweoqknhpump",
    "GSeuzQrtQaDAjb1NKQ69Uw2GwuZUsAWhDj6AP6Rapump",
    "FDBnaGYQeGjkLVs2E53yg5ErKnUd2xSjL5SQMLgGy4wP",
    "4aiLCRmCkVeVGZBTCFXYCGtW4MFsq4dWhGSyNnoGTrrv",
    "8MdkXe5G77xaMheVQxLqAYV8e2m2Dfc5ZbuXup2epump",
    "FzLMPzqz9Ybn26qRzPKDKwsLV6Kpvugh31jF7T7npump",
    "4Xu4fp2FV3gkdj4rnYS7gWpuKXFnXPwroHDKcMwapump",
    "4ZR1R4oW9B4Ufr15FDVLoEx3rhU7YKFTDL8qgAFPpump",
    "CZwnGa1scLnW6QFMYeofiaw2XzCjyMRiA2FTeyo1pump",
    "2PzS5SYYWjUFvzXNFaMmRkpjkxGX6R5v8DnKYtdcpump",
    "EW7cWbNmTgL7PLQNiJ6tBVC62SJzJXa2pFYJjDPPpump",
    "Hz4L8oCSTZoepnNDTtVqPqkPnSA2grNDLA6E6aF8pump",
    "8FaSmBzQdnBPjAt5wZ7k8WaCQqBHTM8YRB9ZsJ44bonk",
}


class UniversalTrader:
    """Universal trading coordinator that works with any supported platform."""

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        # Platform configuration
        platform: Platform | str = Platform.PUMP_FUN,
        # ========== CODE VERSION MARKER ==========
        # Version: 2026-01-16-v1 - pumpportal api key
        # ==========================================
        # Listener configuration
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        pumpportal_api_key: str | None = None,
        # Trading configuration
        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        # Exit strategy configuration
        exit_strategy: str = "time_based",
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        # Trailing Stop-Loss parameters
        tsl_enabled: bool = True,
        tsl_activation_pct: float = 0.15,  # MUST match yaml
        tsl_trail_pct: float = 0.10,  # MUST match yaml
        tsl_sell_pct: float = 1.0,  # MUST match yaml
        tp_sell_pct: float = 0.90,  # FIX S18-8: MUST match yaml (0.9)
        dca_enabled: bool = True,  # MUST match yaml
        # Token Vetting (security)
        token_vetting_enabled: bool = False,
        vetting_require_freeze_revoked: bool = True,
        vetting_skip_bonding_curve: bool = True,
        price_check_interval: int = 1,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 500_000,
        sell_fixed_priority_fee: int = 500000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        priority_fee_strategy: str = "aggressive",
        priority_fee_min: int = 50_000,
        priority_fee_max: int = 10_000_000,
        # Retry and timeout settings
        max_retries: int = 5,
        wait_time_after_creation: int = 2,
        wait_time_after_buy: int = 2,
        wait_time_before_new_token: int = 5,
        max_token_age: int | float = 0.001,
        moon_bag_percentage: float = 0.0,
        token_wait_timeout: int = 30,
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        sniper_enabled: bool = True,  # If False, don't snipe new tokens (for whale-copy, volume-sniper)
        # Compute unit configuration
        compute_units: dict | None = None,
        # Pattern detection settings
        enable_pattern_detection: bool = False,
        pattern_volume_spike_threshold: float = 3.0,
        pattern_holder_growth_threshold: float = 0.5,
        pattern_min_whale_buys: int = 2,
        pattern_min_patterns_to_buy: int = 2,
        pattern_min_signal_strength: float = 0.5,  # Minimum signal strength (0.0-1.0)
        pattern_only_mode: bool = False,  # Only buy when patterns detected
        # High Volume Sideways pattern settings
        pattern_high_volume_buys_1h: int = 300,
        pattern_high_volume_sells_1h: int = 200,
        pattern_high_volume_alt_buys_1h: int = 100,
        pattern_high_volume_alt_max_sells_1h: int = 100,
        # EXTREME BUY PRESSURE 5min pattern settings
        pattern_extreme_buy_min_buys_5m: int = 500,
        pattern_extreme_buy_max_sells_5m: int = 200,
        # Token scoring settings
        enable_scoring: bool = False,
        scoring_min_score: int = 70,
        scoring_volume_weight: int = 30,
        scoring_buy_pressure_weight: int = 30,
        scoring_momentum_weight: int = 25,
        scoring_liquidity_weight: int = 15,
        # Whale copy trading settings
        enable_whale_copy: bool = False,
        whale_wallets_file: str = "smart_money_wallets.json",
        whale_min_buy_amount: float = 0.5,
        whale_all_platforms: bool = False,
        # Whale webhook mode (real-time instead of polling)
        whale_webhook_enabled: bool = False,
        whale_webhook_port: int = 8000,
        stablecoin_filter: list = None,
        helius_api_key: str | None = None,
        birdeye_api_key: str | None = None,
        jupiter_api_key: str | None = None,
        # Dev reputation settings
        enable_dev_check: bool = False,
        dev_max_tokens_created: int = 50,
        dev_min_account_age_days: int = 1,
        # Trending scanner settings
        enable_trending_scanner: bool = False,
        trending_min_volume_1h: float = 50000,
        trending_min_market_cap: float = 10000,
        trending_max_market_cap: float = 0,  # 0 = БЕЗ ОГРАНИЧЕНИЙ по верхней планке!
        trending_min_price_change_5m: float = 5,
        trending_min_price_change_1h: float = 20,
        trending_min_buy_pressure: float = 0.65,
        trending_scan_interval: float = 30,
        # Volume pattern analyzer settings
        enable_volume_pattern: bool = False,
        volume_pattern_min_volume_1h: float = 10000,
        volume_pattern_spike_threshold: float = 3.0,
        volume_pattern_min_trades_5m: int = 200,
        volume_pattern_scan_interval: float = 100,
        volume_pattern_max_tokens: int = 50,
        volume_pattern_min_health: int = 70,
        volume_pattern_min_opportunity: int = 70,
        # Balance protection
        min_sol_balance: float = 0.03,
    ):
        """Initialize the universal trader."""
        # ========== CODE VERSION CHECK ==========
        # Use print() with flush to guarantee output
        print("=" * 60, flush=True)
        print("[VERSION] UniversalTrader VERSION: 2026-01-16-v6-ANTI-DUPLICATE", flush=True)
        print("=" * 60, flush=True)
        logger.warning("=" * 60)
        logger.warning("[VERSION] UniversalTrader VERSION: 2026-01-16-v6-ANTI-DUPLICATE")
        logger.warning("=" * 60)

        # Store endpoints and API keys for later use
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self.jupiter_api_key = jupiter_api_key or os.getenv("JUPITER_TRADE_API_KEY")  # NO fallback to monitor key!

        # Core components
        logger.warning("=== INIT: Creating core components ===")
        self.solana_client = SolanaClient(rpc_endpoint)
        self.wallet = Wallet(private_key)
        self.min_sol_balance = min_sol_balance
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
            strategy=priority_fee_strategy,
            min_fee=priority_fee_min,
            max_fee=priority_fee_max,
            sell_fixed_fee=sell_fixed_priority_fee,
        )

        # Platform setup
        if isinstance(platform, str):
            self.platform = Platform(platform)
        else:
            self.platform = platform

        logger.warning(f"=== INIT: Platform set to {self.platform.value} ===")

        # Validate platform support
        try:
            from platforms import platform_factory

            if not platform_factory.registry.is_platform_supported(self.platform):
                raise ValueError(f"Platform {self.platform.value} is not supported")
        except Exception:
            logger.exception("Platform validation failed")
            raise

        # Pattern detection setup
        logger.warning("=== INIT: Starting pattern detection setup ===")
        self.enable_pattern_detection = enable_pattern_detection
        self.pattern_only_mode = pattern_only_mode
        self.pattern_min_signal_strength = pattern_min_signal_strength
        self.pattern_detector: PumpPatternDetector | None = None

        if enable_pattern_detection:
            self.pattern_detector = PumpPatternDetector(
                birdeye_api_key=birdeye_api_key,
                volume_spike_threshold=pattern_volume_spike_threshold,
                holder_growth_threshold=pattern_holder_growth_threshold,
                min_whale_buys=pattern_min_whale_buys,
                min_patterns_to_signal=pattern_min_patterns_to_buy,
                # High Volume Sideways pattern thresholds
                high_volume_buys_1h=pattern_high_volume_buys_1h,
                high_volume_sells_1h=pattern_high_volume_sells_1h,
                high_volume_alt_buys_1h=pattern_high_volume_alt_buys_1h,
                high_volume_alt_max_sells_1h=pattern_high_volume_alt_max_sells_1h,
                # EXTREME BUY PRESSURE 5min pattern
                extreme_buy_pressure_min_buys_5m=pattern_extreme_buy_min_buys_5m,
                extreme_buy_pressure_max_sells_5m=pattern_extreme_buy_max_sells_5m,
            )
            self.pattern_detector.set_pump_signal_callback(self._on_pump_signal)
            logger.info(
                f"Pattern detection enabled: volume_spike={pattern_volume_spike_threshold}x, "
                f"holder_growth={pattern_holder_growth_threshold * 100}%, "
                f"min_whale_buys={pattern_min_whale_buys}, "
                f"min_signal_strength={pattern_min_signal_strength}, "
                f"pattern_only_mode={pattern_only_mode}, "
                f"high_vol_sideways=[buys_1h>={pattern_high_volume_buys_1h}, sells_1h>={pattern_high_volume_sells_1h}], "
                f"extreme_buy_5m=[buys>={pattern_extreme_buy_min_buys_5m}, sells<={pattern_extreme_buy_max_sells_5m}]"
            )

        # Token scoring setup
        logger.warning("=== INIT: Starting token scoring setup ===")
        self.enable_scoring = enable_scoring
        self.token_scorer: TokenScorer | None = None

        if enable_scoring:
            self.token_scorer = TokenScorer(
                min_score=scoring_min_score,
                volume_weight=scoring_volume_weight,
                buy_pressure_weight=scoring_buy_pressure_weight,
                momentum_weight=scoring_momentum_weight,
                liquidity_weight=scoring_liquidity_weight,
            )
            logger.info(
                f"Token scoring enabled: min_score={scoring_min_score}, "
                f"weights=[vol:{scoring_volume_weight}, bp:{scoring_buy_pressure_weight}, "
                f"mom:{scoring_momentum_weight}, liq:{scoring_liquidity_weight}]"
            )

        # Whale copy trading setup
        print("=" * 50, flush=True)
        print("[WHALE] WHALE COPY SETUP START", flush=True)
        print(f"[WHALE] enable_whale_copy = {enable_whale_copy}", flush=True)
        print(f"[WHALE] wallets_file = {whale_wallets_file}", flush=True)
        print(f"[WHALE] min_buy_amount = {whale_min_buy_amount}", flush=True)
        print("=" * 50, flush=True)

        logger.warning("=" * 50)
        logger.warning("[WHALE] WHALE COPY SETUP START")
        logger.warning(f"[WHALE] enable_whale_copy = {enable_whale_copy}")
        logger.warning(f"[WHALE] wallets_file = {whale_wallets_file}")
        logger.warning(f"[WHALE] min_buy_amount = {whale_min_buy_amount}")
        logger.warning("=" * 50)

        self.enable_whale_copy = enable_whale_copy
        self.whale_webhook_enabled = whale_webhook_enabled
        self.whale_webhook_port = whale_webhook_port
        self.whale_tracker: WhalePoller | None = None
        self.whale_tracker_secondary = None  # Secondary receiver for dual mode
        self._moonbag_monitor = None  # S38: PublicNode gRPC for moonbag/dust price
        self._signal_dedup = None  # Dedup for dual-receiver mode
        self._watchdog = None  # Dual-channel watchdog (Phase 5.3)
        self._price_stream = None  # Phase 4: Real-time gRPC price stream
        self.helius_api_key = helius_api_key or os.getenv("HELIUS_API_KEY")

        if enable_whale_copy:
            try:
                # === PHASE 2: Parallel gRPC + Webhook with dedup ===
                geyser_receiver = None
                webhook_receiver = None

                # Create gRPC receiver if available
                if WHALE_GEYSER_AVAILABLE and os.getenv("GEYSER_API_KEY"):
                    logger.warning("[WHALE] Creating WhaleGeyserReceiver (gRPC via PublicNode Yellowstone)...")
                    geyser_receiver = WhaleGeyserReceiver(
                        geyser_endpoint=os.getenv("GEYSER_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"),
                        geyser_api_key=os.getenv("GEYSER_API_KEY", ""),
                        helius_parse_api_key=os.getenv("GEYSER_PARSE_API_KEY", ""),
                        wallets_file=whale_wallets_file,
                        min_buy_amount=whale_min_buy_amount,
                        stablecoin_filter=stablecoin_filter or [],
                    )
                    logger.warning(f"[WHALE] Geyser endpoint: {os.getenv('GEYSER_ENDPOINT', 'N/A')}")

                # Create webhook receiver if available (independent of gRPC)
                if self.whale_webhook_enabled and WHALE_WEBHOOK_AVAILABLE:
                    logger.warning("[WHALE] Creating WhaleWebhookReceiver (REAL-TIME via Helius webhook)...")
                    webhook_receiver = WhaleWebhookReceiver(
                        host="0.0.0.0",
                        port=self.whale_webhook_port,
                        wallets_file=whale_wallets_file,
                        min_buy_amount=whale_min_buy_amount,
                        stablecoin_filter=stablecoin_filter or [],
                    )
                    logger.warning(f"[WHALE] Webhook server will listen on port {self.whale_webhook_port}")

                # Assign receivers based on what is available
                if geyser_receiver and webhook_receiver:
                    # DUAL MODE: Both channels active with dedup
                    if SIGNAL_DEDUP_AVAILABLE:
                        self._signal_dedup = SignalDedup(ttl_seconds=300)
                    self.whale_tracker = geyser_receiver
                    self.whale_tracker_secondary = webhook_receiver
                    self.whale_tracker.set_callback(self._deduped_whale_buy)
                    self.whale_tracker_secondary.set_callback(self._deduped_whale_buy)
                    # Phase 6: Pass wallet pubkey for ATA derivation
                    if hasattr(self.whale_tracker, 'set_wallet_pubkey'):
                        self.whale_tracker.set_wallet_pubkey(str(self.wallet.pubkey))
                    logger.warning("=" * 70)
                    logger.warning("[WHALE] DUAL MODE: gRPC (primary) + Webhook (secondary)")
                    logger.warning("[WHALE] Signal dedup: ENABLED (TTL=300s)")
                    logger.warning("[WHALE] First receiver to catch TX wins, duplicate is dropped")
                    logger.warning("=" * 70)
                    # Phase 5.3: Create watchdog for dual-channel health monitoring
                    if WATCHDOG_AVAILABLE:
                        self._watchdog = DualChannelWatchdog(alert_after_seconds=300)
                        self.whale_tracker.set_watchdog(self._watchdog)
                        self.whale_tracker_secondary.set_watchdog(self._watchdog)
                        logger.warning("[WHALE] Watchdog: ENABLED (alert after 300s silence)")
                elif geyser_receiver:
                    # gRPC only
                    self.whale_tracker = geyser_receiver
                    self.whale_tracker.set_callback(self._on_whale_buy)
                    logger.warning("[WHALE] SINGLE MODE: gRPC only")
                elif webhook_receiver:
                    # Webhook only
                    self.whale_tracker = webhook_receiver
                    self.whale_tracker.set_callback(self._on_whale_buy)
                    logger.warning("[WHALE] SINGLE MODE: Webhook only")
                elif WHALE_POLLER_AVAILABLE:
                    # Fallback to poller
                    logger.warning("[WHALE] Creating WhalePoller instance (HTTP polling)...")
                    self.whale_tracker = WhalePoller(
                        wallets_file=whale_wallets_file,
                        min_buy_amount=whale_min_buy_amount,
                        poll_interval=30.0,
                        max_tx_age=600.0,
                        stablecoin_filter=stablecoin_filter or [],
                    )
                    self.whale_tracker.set_callback(self._on_whale_buy)
                    logger.warning("[WHALE] SINGLE MODE: Poller (fallback)")
                else:
                    logger.error("[WHALE] No whale tracker available!")
                    self.whale_tracker = None

                # Log tracker info
                if self.whale_tracker:
                    wallet_count = len(self.whale_tracker.whale_wallets) if self.whale_tracker.whale_wallets else 0
                    logger.warning(f"[WHALE] Primary tracker CREATED: {wallet_count} wallets")
                    logger.warning(f"[WHALE] Min buy amount: {whale_min_buy_amount} SOL")
                    if wallet_count == 0:
                        logger.error("[WHALE] ERROR: No whale wallets loaded!")
                    else:
                        sample_wallets = list(self.whale_tracker.whale_wallets.keys())[:3]
                        logger.warning(f"[WHALE] Sample wallets: {sample_wallets}")
                if self.whale_tracker_secondary:
                    logger.warning(f"[WHALE] Secondary tracker CREATED (webhook on port {self.whale_webhook_port})")

            except Exception as e:
                logger.exception(f"[WHALE] EXCEPTION creating whale tracker: {e}")
                self.whale_tracker = None
                self.whale_tracker_secondary = None
        else:
            logger.warning("[WHALE] Whale copy: DISABLED in config")
        # Phase 4: Initialize PriceStream for real-time SL/TP monitoring
        if PRICE_STREAM_AVAILABLE:
            try:
                self._price_stream = PriceStream(stale_threshold=3.0)
                logger.warning("[PRICE_STREAM] PriceStream initialized (Phase 4)")
            except Exception as e:
                logger.error(f"[PRICE_STREAM] Failed to init: {e}")
                self._price_stream = None
        else:
            logger.info("[PRICE_STREAM] PriceStream not available")

        # Dev reputation checker setup
        self.enable_dev_check = enable_dev_check
        self.dev_checker: DevReputationChecker | None = None

        if enable_dev_check:
            self.dev_checker = DevReputationChecker(
                max_tokens_created=dev_max_tokens_created,
                min_account_age_days=dev_min_account_age_days,
            )
            logger.info(
                f"Dev reputation check enabled: max_tokens={dev_max_tokens_created}, "
                f"min_age={dev_min_account_age_days} days"
            )

        # Trending scanner setup
        self.enable_trending_scanner = enable_trending_scanner
        self.trending_scanner: TrendingScanner | None = None

        if enable_trending_scanner:
            self.trending_scanner = TrendingScanner(
                min_volume_1h=trending_min_volume_1h,
                min_market_cap=trending_min_market_cap,
                max_market_cap=trending_max_market_cap,
                min_price_change_5m=trending_min_price_change_5m,
                min_price_change_1h=trending_min_price_change_1h,
                min_buy_pressure=trending_min_buy_pressure,
                scan_interval=trending_scan_interval,
            )
            self.trending_scanner.set_callback(self._on_trending_token)

        # Volume pattern analyzer setup
        self.enable_volume_pattern = enable_volume_pattern
        self.volume_pattern_analyzer: VolumePatternAnalyzer | None = None

        if enable_volume_pattern:
            self.volume_pattern_analyzer = VolumePatternAnalyzer(
                min_volume_1h=volume_pattern_min_volume_1h,
                volume_spike_threshold=volume_pattern_spike_threshold,
                min_trades_5m=volume_pattern_min_trades_5m,
                scan_interval=volume_pattern_scan_interval,
                max_tokens_per_scan=volume_pattern_max_tokens,
                min_health_score=volume_pattern_min_health,
            )
            self.volume_pattern_analyzer.set_callbacks(
                on_opportunity=self._on_volume_opportunity
            )
            logger.info(
                f"Volume pattern analyzer enabled: min_vol=${volume_pattern_min_volume_1h:,}, "
                f"spike={volume_pattern_spike_threshold}x, min_health={volume_pattern_min_health}"
            )

            logger.info(
                f"Trending scanner enabled: min_vol_1h=${trending_min_volume_1h:,.0f}, "
                f"min_mc=${trending_min_market_cap:,.0f}, max_mc=${trending_max_market_cap:,.0f}, "
                f"min_change_5m={trending_min_price_change_5m}%, min_buy_pressure={trending_min_buy_pressure*100:.0f}%"
            )

        # Get platform-specific implementations
        self.platform_implementations = get_platform_implementations(
            self.platform, self.solana_client
        )

        # Store compute unit configuration
        self.compute_units = compute_units or {}

        # Create platform-aware traders
        self.buyer = PlatformAwareBuyer(
            self.solana_client,
            self.wallet,
            self.priority_fee_manager,
            buy_amount,
            buy_slippage,
            max_retries,
            extreme_fast_token_amount,
            extreme_fast_mode,
            compute_units=self.compute_units,
        )

        self.seller = PlatformAwareSeller(
            self.solana_client,
            self.wallet,
            self.priority_fee_manager,
            sell_slippage,
            max_retries,
            compute_units=self.compute_units,
            jupiter_api_key=self.jupiter_api_key,
        )


        # Cached FallbackSeller - one TCP connection, instant sells
        from trading.fallback_seller import FallbackSeller
        self._fallback_seller = FallbackSeller(
            client=self.solana_client,
            wallet=self.wallet,
            slippage=sell_slippage,
            priority_fee=6_500_000,         # microlamports/CU for pumpfun (~650K lamports at ~100K CU)
            jupiter_priority_fee=650_000,   # lamports total for Jupiter (~0.00065 SOL)
            max_retries=1,
            jupiter_api_key=self.jupiter_api_key,
        )
        # Separate buyer with BUY slippage (30%) — never use _fallback_seller for buys!
        self._fallback_buyer = FallbackSeller(
            client=self.solana_client,
            wallet=self.wallet,
            slippage=buy_slippage,
            priority_fee=6_500_000,         # microlamports/CU for pumpfun (~650K lamports at ~100K CU)
            jupiter_priority_fee=650_000,   # lamports total for Jupiter (~0.00065 SOL)
            max_retries=1,
            jupiter_api_key=self.jupiter_api_key,
        )
        logger.warning(f"[INIT] _fallback_seller slippage={sell_slippage} (sells), _fallback_buyer slippage={buy_slippage} (buys)")
        # Initialize the appropriate listener with platform filtering
        self.token_listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            wss_endpoint=wss_endpoint,
            rpc_endpoint=rpc_endpoint,  # Needed for bonk_logs listener
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            pumpportal_url=pumpportal_url,
            pumpportal_api_key=pumpportal_api_key,
            platforms=[self.platform],  # Only listen for our platform
        )

        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

        # Exit strategy parameters
        self.exit_strategy = exit_strategy.lower()
        self.take_profit_percentage = take_profit_percentage
        self.stop_loss_percentage = stop_loss_percentage
        self.max_hold_time = max_hold_time
        self.price_check_interval = max(1, price_check_interval)  # Min 1 sec to prevent RPC spam

        # Trailing Stop-Loss (TSL) parameters
        self.tsl_enabled = tsl_enabled
        self.tsl_activation_pct = tsl_activation_pct
        self.tsl_trail_pct = tsl_trail_pct
        self.tsl_sell_pct = tsl_sell_pct
        self.tp_sell_pct = tp_sell_pct
        self.dca_enabled = dca_enabled
        if self.tsl_enabled:
            logger.warning(f"[TSL] Trailing Stop-Loss ENABLED: activates at +{tsl_activation_pct*100:.0f}%, trails {tsl_trail_pct*100:.0f}%")

        # Token Vetter initialization
        self.token_vetting_enabled = token_vetting_enabled
        self.token_vetter: TokenVetter | None = None
        if token_vetting_enabled:
            self.token_vetter = TokenVetter(
                rpc_endpoint=rpc_endpoint,
                require_freeze_revoked=vetting_require_freeze_revoked,
                skip_for_bonding_curve=vetting_skip_bonding_curve,
            )
            logger.warning("[VET] Token Vetting ENABLED")

        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.moon_bag_percentage = moon_bag_percentage
        self.token_wait_timeout = token_wait_timeout

        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode
        self.sniper_enabled = sniper_enabled
        logger.warning(f"[CONFIG] sniper_enabled = {sniper_enabled}")

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[
            str, Pubkey
        ] = {}  # Maps mint (as string) to token_program_id
        self.token_queue: asyncio.Queue = asyncio.Queue()
        self.processing: bool = False
        self.processed_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}
        self.pump_signals: dict[str, list] = {}  # mint -> detected patterns
        self.pending_tokens: dict[str, TokenInfo] = {}  # mint -> TokenInfo for pattern-only mode
        # Load existing positions from file at startup (CRITICAL FIX!)
        self.active_positions: list[Position] = load_positions()
        logger.warning(f"[INIT] Loaded {len(self.active_positions)} existing positions from file")  # Active positions for persistence

        # ANTI-DUPLICATE PROTECTION (CRITICAL!)
        # Single lock for ALL buy operations to prevent race conditions
        self._buy_lock = asyncio.Lock()
        # Unified set of tokens being bought or already bought
        # This is checked INSIDE the lock to prevent duplicates
        self._buying_tokens: set[str] = set()  # Tokens currently being bought (in progress)

        # GLOBAL PURCHASE HISTORY - tokens NEVER bought again!
        # Load from persistent file (shared across all bots)
        self._bought_tokens: set[str] = load_purchase_history()
        logger.warning(f"[HISTORY] Loaded {len(self._bought_tokens)} tokens from global purchase history")
        
        # === REDIS DEDUP STORE (initialized lazily) ===
        self._dedup_store = None
        self._dedup_enabled = True  # Set False to use only in-memory
        
        # CRITICAL BALANCE PROTECTION
        # When balance <= 0.02 SOL, bot stops completely
        self._critical_low_balance: bool = False

        # === BALANCE CACHE (Session 9: eliminates 271ms RPC from critical path) ===
        self._cached_sol_balance: float = 0.0
        self._balance_cache_time: float = 0.0
        self._balance_cache_max_age: float = 60.0  # fallback to RPC if cache older than 60s
        self._balance_cache_task: asyncio.Task | None = None
    # === DEDUP STORE HELPER METHODS ===
    
    async def _get_dedup_store(self):
        """Lazy init DedupStore"""
        if self._dedup_store is None and self._dedup_enabled:
            try:
                self._dedup_store = await get_dedup_store()
                logger.info("[DEDUP] DedupStore initialized")
            except Exception as e:
                logger.warning(f"[DEDUP] Failed to init store: {e}, using in-memory only")
                self._dedup_enabled = False
        return self._dedup_store
    
    async def _try_acquire_for_buy(self, mint_str: str, bot_name: str = "universal") -> bool:
        """
        Try to acquire token for buying using DedupStore + in-memory.
        Returns True if we can proceed with buy.
        """
        # Fast in-memory check first
        if mint_str in self._bought_tokens or mint_str in self._buying_tokens:
            return False
        
        # Try Redis/SQLite dedup
        if self._dedup_enabled:
            try:
                store = await self._get_dedup_store()
                if store:
                    acquired = await store.try_acquire(mint_str, bot_name)
                    if not acquired:
                        logger.info(f"[DEDUP] Token {mint_str[:8]}... already acquired by another process")
                        return False
            except Exception as e:
                logger.warning(f"[DEDUP] Store error: {e}, falling back to in-memory")
        
        # In-memory tracking
        self._buying_tokens.add(mint_str)
        return True
    
    async def _mark_bought(self, mint_str: str, bot_name: str = "universal") -> None:
        """Mark token as bought in all stores"""
        self._bought_tokens.add(mint_str)
        self._buying_tokens.discard(mint_str)
        
        if self._dedup_enabled:
            try:
                store = await self._get_dedup_store()
                if store:
                    await store.mark_bought(mint_str, bot_name)
            except Exception as e:
                logger.warning(f"[DEDUP] Failed to mark bought: {e}")
    
    async def _release_buy_lock(self, mint_str: str) -> None:
        """Release token if buy failed"""
        self._buying_tokens.discard(mint_str)
        
        if self._dedup_enabled:
            try:
                store = await self._get_dedup_store()
                if store:
                    await store.release(mint_str)
            except Exception as e:
                logger.warning(f"[DEDUP] Failed to release: {e}")
    # === END DEDUP HELPER METHODS ===

    # Sentinel: returned when all RPCs fail (distinct from None = "no token account")
    BALANCE_RPC_ERROR = -1.0

    async def _get_token_balance(self, mint: str) -> float | None:
        """Get real token balance from on-chain with RPC fallback.

        Returns:
            float >= 0  -- real balance (token account exists)
            None        -- token account not found (accounts list empty)
            -1.0        -- all RPCs failed (BALANCE_RPC_ERROR)
        """
        import aiohttp
        wallet = str(self.wallet.pubkey)
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet, {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"}
            ]
        }

        rpc_endpoints = []
        # Chainstack first (fastest, paid plan)
        chainstack_url = os.getenv("CHAINSTACK_RPC_ENDPOINT")
        if chainstack_url:
            rpc_endpoints.append(("Chainstack", chainstack_url))
        for env_key in ("DRPC_RPC_ENDPOINT", "ALCHEMY_RPC_ENDPOINT"):
            url = os.getenv(env_key)
            if url:
                rpc_endpoints.append((env_key.split("_")[0], url))
        helius_key = os.getenv("HELIUS_API_KEY")
        if helius_key:
            rpc_endpoints.append(("Helius", f"https://mainnet.helius-rpc.com/?api-key={helius_key}"))
        pub = os.getenv("SOLANA_PUBLIC_RPC_ENDPOINT")
        if pub:
            rpc_endpoints.append(("Public", pub))
        if not rpc_endpoints:
            rpc_endpoints.append(("default", self.rpc_endpoint))

        got_empty_accounts = False

        for rpc_name, rpc_url in rpc_endpoints:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        rpc_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(f"[BALANCE] {rpc_name} HTTP {resp.status} for {mint[:8]}...")
                            continue
                        data = await resp.json()
                        rpc_err = data.get("error")
                        if rpc_err:
                            logger.warning(f"[BALANCE] {rpc_name} RPC error for {mint[:8]}...: {rpc_err}")
                            continue
                        accounts = data.get("result", {}).get("value", [])
                        if accounts:
                            info = accounts[0]["account"]["data"]["parsed"]["info"]
                            ui_amount = info["tokenAmount"].get("uiAmount")
                            if ui_amount is not None:
                                return float(ui_amount)
                            return 0.0
                        else:
                            got_empty_accounts = True
                            continue  # FIX S28-1: try other RPCs before giving up
            except Exception as e:
                logger.warning(f"[BALANCE] {rpc_name} failed for {mint[:8]}...: {type(e).__name__}: {e}")
                continue

        if got_empty_accounts:
            return None
        logger.error(f"[BALANCE] ALL RPCs failed for {mint[:8]}... -- returning BALANCE_RPC_ERROR")
        return self.BALANCE_RPC_ERROR


    # [edit:s12] reliable decimals from on-chain parsed data
    async def _get_token_balance_with_decimals(self, mint: str) -> tuple[float, int, int] | None:
        """Get token balance WITH decimals from on-chain (parsed, 100% reliable).
        Returns (uiAmount, decimals, rawAmount) or None.
        Also updates fallback_seller decimals cache for consistency.
        """
        import aiohttp
        try:
            rpc_url = os.getenv("DRPC_RPC_ENDPOINT") or os.getenv("SOLANA_NODE_RPC_ENDPOINT") or self.rpc_endpoint
            wallet = str(self.wallet.pubkey)

            async with aiohttp.ClientSession() as session:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet, {"mint": mint},
                        {"encoding": "jsonParsed", "commitment": "confirmed"}
                    ]
                }
                async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        accounts = data.get("result", {}).get("value", [])
                        if accounts:
                            token_amount = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                            ui_amount = float(token_amount.get("uiAmount") or 0)
                            decimals = int(token_amount.get("decimals", 6))
                            raw_amount = int(token_amount.get("amount", "0"))
                            # Update fallback_seller decimals cache (CRITICAL!)
                            try:
                                from trading.fallback_seller import _decimals_cache
                                if mint in _decimals_cache and _decimals_cache[mint] != decimals:
                                    logger.warning(f"[DECIMALS FIX] {mint[:8]}...: cache had {_decimals_cache[mint]}, on-chain says {decimals}. FIXING!")
                                _decimals_cache[mint] = decimals
                            except Exception:
                                pass
                            return (ui_amount, decimals, raw_amount)
            return None
        except Exception as e:
            logger.warning(f"[BALANCE+DEC] Error for {mint[:8]}...: {e}")
            return None

    async def _on_pump_signal(
        self, mint: str, symbol: str, patterns: list, strength: float
    ):
        """Callback when pump pattern is detected - trigger buy if in pattern_only_mode.

        CRITICAL: This now performs MANDATORY scoring check before buying!
        """
        logger.warning(
            f"[SIGNAL] PUMP SIGNAL: {symbol} ({mint[:8]}...) - "
            f"{len(patterns)} patterns, strength: {strength:.2f}"
        )
        self.pump_signals[mint] = patterns

        # Check minimum signal strength
        if strength < self.pattern_min_signal_strength:
            logger.warning(
                f"[SKIP] Signal too weak for {symbol}: {strength:.2f} < {self.pattern_min_signal_strength:.2f}"
            )
            return

        # Check minimum patterns count
        if len(patterns) < 2:
            logger.warning(
                f"[SKIP] Not enough patterns for {symbol}: {len(patterns)} < 2 required"
            )
            return

        # Cleanup old pending tokens (older than 5 minutes)
        self._cleanup_pending_tokens()

        # If we have pending token_info (waiting for patterns) - check scoring and buy!
        logger.info(f"[DEBUG] Checking pending_tokens for {mint}, have {len(self.pending_tokens)} tokens: {list(self.pending_tokens.keys())[:5]}")
        if mint in self.pending_tokens:
            token_info = self.pending_tokens.pop(mint)

            # MANDATORY SCORING CHECK before buying on signal
            if self.token_scorer:
                try:
                    should_buy, score = await self.token_scorer.should_buy(mint, symbol)
                    logger.info(
                        f"[SCORE] Signal token {symbol}: {score.total_score}/100 -> {score.recommendation}"
                    )

                    # Check if no Dexscreener data - return to pending
                    if score.details.get("error") == "No Dexscreener data - SKIP":
                        logger.info(
                            f"[PENDING] {symbol} - no Dexscreener data yet, keeping in pending"
                        )
                        self.pending_tokens[mint] = token_info
                        return

                    # Check score threshold
                    if not should_buy:
                        logger.warning(
                            f"[PENDING] {symbol} - score {score.total_score} below threshold, keeping in pending"
                        )
                        self.pending_tokens[mint] = token_info
                        return

                except Exception as e:
                    logger.warning(f"Scoring failed for signal token {symbol}: {e}, keeping in pending")
                    self.pending_tokens[mint] = token_info
                    return

            logger.warning(
                f"[BUY] BUYING on STRONG pump signal: {symbol} "
                f"(strength: {strength:.2f}, patterns: {len(patterns)})"
            )
            # Process token with signal (skip_checks=False to still do dev check)
            asyncio.create_task(self._handle_token(token_info, skip_checks=False))

    def _cleanup_pending_tokens(self):
        """Remove pending tokens older than 5 minutes."""
        from time import monotonic
        now = monotonic()
        max_age = 300  # 5 minutes

        before_count = len(self.pending_tokens)
        to_remove = []
        for mint_str in self.pending_tokens:
            if mint_str in self.token_timestamps:
                age = now - self.token_timestamps[mint_str]
                if age > max_age:
                    to_remove.append(mint_str)
            # If no timestamp - don't remove (keep it)

        for mint_str in to_remove:
            self.pending_tokens.pop(mint_str, None)

        if to_remove:
            logger.warning(f"[CLEANUP] Removed {len(to_remove)} old pending tokens")

        if before_count > 0:
            logger.info(f"[CLEANUP] pending_tokens: {before_count} before, {len(self.pending_tokens)} after")

    def _has_pump_signal(self, mint: str) -> bool:
        """Check if token has pump signal."""
        return mint in self.pump_signals and len(self.pump_signals[mint]) > 0

    def _detect_token_platform(self, token: TrendingToken) -> Platform | None:
        """Detect platform from token mint address or dex_id.

        Platform detection rules:
        - Mint ending with 'pump' -> pump_fun
        - Mint ending with 'bonk' -> lets_bonk
        - Mint ending with 'bags' -> bags
        - dex_id 'pumpfun' or 'pump.fun' -> pump_fun
        - dex_id 'letsbonk' or 'bonk.fun' -> lets_bonk
        - dex_id 'bags' -> bags

        Returns None if platform cannot be determined.
        """
        mint_str = token.mint.lower()
        dex_id = (token.dex_id or "").lower()

        # Check mint suffix
        if mint_str.endswith("pump"):
            return Platform.PUMP_FUN
        elif mint_str.endswith("bonk"):
            return Platform.LETS_BONK
        elif mint_str.endswith("bags"):
            return Platform.BAGS

        # Check dex_id
        if dex_id in ("pumpfun", "pump.fun", "pump_fun"):
            return Platform.PUMP_FUN
        elif dex_id in ("letsbonk", "bonk.fun", "lets_bonk", "bonkfun"):
            return Platform.LETS_BONK
        elif dex_id == "bags":
            return Platform.BAGS

        # Cannot determine - return None (will use current bot platform)
        return None

    async def _deduped_whale_buy(self, whale_buy: WhaleBuy):
        """Wrapper that deduplicates signals from gRPC + Webhook dual receivers.

        When both receivers catch the same whale TX, only the first one
        triggers _on_whale_buy(). The duplicate is logged and dropped.
        Three layers of protection:
        1. SignalDedup (this method) - by tx_signature
        2. _on_whale_buy - by _buying_tokens/_bought_tokens + _buy_lock
        3. Redis dedup_store - cross-process
        """
        sig = whale_buy.tx_signature
        source = "unknown"

        # Determine source for logging
        if hasattr(whale_buy, "platform"):
            source = whale_buy.platform or "unknown"

        if self._signal_dedup:
            if not self._signal_dedup.is_new(sig, source=source):
                logger.info(
                    f"[DEDUP] Duplicate whale signal dropped: "
                    f"{whale_buy.token_symbol} tx={sig[:16]}... (from {source})"
                )
                return
            logger.info(
                f"[DEDUP] New whale signal accepted: "
                f"{whale_buy.token_symbol} tx={sig[:16]}... (from {source})"
            )
        else:
            # No dedup available — pass through (should not happen in dual mode)
            logger.warning("[DEDUP] SignalDedup not initialized, passing through")

        await self._on_whale_buy(whale_buy)

    async def _on_whale_buy(self, whale_buy: WhaleBuy):
        """Callback when whale buys a token - copy the trade on ANY available DEX.

        UNIVERSAL WHALE COPY: Покупаем токен там где есть ликвидность!
        Порядок попыток:
        1. Pump.Fun bonding curve (если не мигрировал)
        2. PumpSwap (для мигрированных токенов)
        3. Jupiter (универсальный fallback)

        RETRY LOGIC: Для свежих токенов (< 10 секунд) делаем до 3 попыток
        с задержкой 2 секунды между ними, т.к. RPC может не успеть
        проиндексировать bonding curve.

        SCORING CHECK: Whale copy теперь проверяет scoring если включен!
        Это предотвращает покупку мусорных токенов даже если кит их купил.

        ANTI-DUPLICATE: Uses unified _buy_lock and _buying_tokens/_bought_tokens
        to prevent ANY duplicate purchases across ALL buy paths.
        """
        # ============================================
        # CRITICAL BALANCE CHECK - STOP BOT
        # ============================================
        if self._critical_low_balance:
            logger.warning("[WHALE] Bot stopped due to critical low balance, ignoring whale signal")
            return

        mint_str = whale_buy.token_mint

        # ============================================
        # ANTI-DUPLICATE CHECK (CRITICAL!)
        # ============================================
        # FAST CHECK before lock (optimization - avoid lock contention)
        if mint_str in self._bought_tokens or mint_str in self._buying_tokens:
            logger.info(f"[WHALE] Token {mint_str[:8]}... already bought/buying, skipping")
            return

        # Double-check fresh file (other bots may have bought)
        if was_token_purchased(mint_str):
            logger.info(f"[WHALE] {whale_buy.token_symbol} found in purchase history file, skipping")
            self._bought_tokens.add(mint_str)  # Sync memory
            return

        # ============================================
        # DEPLOYER BLACKLIST CHECK (instant O(1))
        # ============================================
        from trading.deployer_blacklist import is_mint_blacklisted
        if is_mint_blacklisted(mint_str):
            logger.warning(f"[WHALE] ⛔ BLACKLISTED deployer token: {whale_buy.token_symbol} ({mint_str[:12]}...) — skipping")
            return

        # ============================================
        # ============================================
        # SCORING CHECK + PRE-FETCH QUOTE (Phase 3.3)
        # Run scoring and Jupiter quote in PARALLEL
        # ============================================
        prefetched_quote = None

        if self.token_scorer:
            # Phase 3.3: Launch scoring + quote prefetch in parallel
            scoring_task = asyncio.create_task(
                self.token_scorer.should_buy(mint_str, whale_buy.token_symbol)
            )
            quote_task = asyncio.create_task(
                self._prefetch_jupiter_quote(mint_str, self.buy_amount)
            )

            try:
                should_buy, score = await scoring_task
                logger.warning(
                    f"[WHALE SCORE] {whale_buy.token_symbol}: {score.total_score}/100 -> {score.recommendation}"
                )
                logger.warning(
                    f"[WHALE SCORE] Details: vol={score.volume_score}, bp={score.buy_pressure_score}, "
                    f"mom={score.momentum_score}, liq={score.liquidity_score}"
                )

                if not should_buy:
                    quote_task.cancel()  # Don't waste the quote
                    logger.warning(
                        f"[WHALE] SKIP LOW SCORE: {whale_buy.token_symbol} score={score.total_score} "
                        f"< min_score={self.token_scorer.min_score} | whale={whale_buy.whale_label}"
                    )
                    logger.warning(
                        f"[WHALE] Token {mint_str} rejected by scoring despite whale buy"
                    )
                    return

                logger.info(
                    f"[WHALE] SCORE OK: {whale_buy.token_symbol} score={score.total_score} >= {self.token_scorer.min_score}"
                )

                # Collect prefetched quote (may already be done)
                try:
                    prefetched_quote = await asyncio.wait_for(quote_task, timeout=5.0)
                    if prefetched_quote:
                        logger.info(f"[PHASE 3.3] Pre-fetched Jupiter quote ready for {whale_buy.token_symbol}")
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    prefetched_quote = None
                except Exception:
                    prefetched_quote = None

            except Exception as e:
                quote_task.cancel()
                logger.warning(f"[WHALE] SKIP - Scoring check failed: {e} - NOT buying without score!")
                return

        # Use lock to prevent race condition between ALL buy paths
        async with self._buy_lock:
            # Re-check after acquiring lock (another task might have started buying)
            if mint_str in self._bought_tokens:
                logger.info(f"[WHALE] Token {mint_str[:8]}... already bought (after lock), skipping")
                return

            if mint_str in self._buying_tokens:
                logger.info(f"[WHALE] Token {mint_str[:8]}... already being bought (after lock), skipping")
                return

            # Check if already have position in this token
            for pos in self.active_positions:
                if str(pos.mint) == mint_str:
                    logger.info(f"[WHALE] Already have position in {mint_str[:8]}..., skipping")
                    self._bought_tokens.add(mint_str)  # Mark as bought to prevent future attempts
                    return

            # Mark as BUYING (in progress) BEFORE releasing lock
            self._buying_tokens.add(mint_str)

        # Now proceed with buy (outside lock to not block other operations)
        try:
            # Clean readable log format
            logger.warning("=" * 70)
            logger.warning("[WHALE COPY] Starting copy trade")
            logger.warning(f"  SYMBOL:    {whale_buy.token_symbol}")
            logger.warning(f"  TOKEN:     {mint_str}")
            logger.warning(f"  WHALE:     {whale_buy.whale_label}")
            logger.warning(f"  WALLET:    {whale_buy.whale_wallet}")
            logger.warning(f"  WHALE_SOL: {whale_buy.amount_sol:.4f} SOL")
            logger.warning(f"  MY_BUY:    {self.buy_amount:.4f} SOL")
            logger.warning(f"  PLATFORM:  {whale_buy.platform}")
            logger.warning("=" * 70)

            # Check wallet balance
            balance_ok = await self._check_balance_before_buy()
            if not balance_ok:
                return

            # === PATCH 11D-ASYNC: Deployer check moved to BACKGROUND (saves ~265ms) ===
            # Original PATCH 11D did blocking RPC call here. Now we fire-and-forget:
            # if deployer is blacklisted, _abort_blacklisted_buy() cancels position.
            asyncio.create_task(self._async_deployer_check(mint_str, whale_buy))
            # === END PATCH 11D-ASYNC ===

            # RETRY LOGIC: Для свежих токенов RPC может не успеть проиндексировать
            # bonding curve. Делаем до 3 попыток с задержкой.
            max_retries = 3
            retry_delay = 2.0  # секунды между попытками

            success = False
            tx_sig = None
            dex_used = "none"
            token_amount = 0.0
            price = 0.0

            for attempt in range(1, max_retries + 1):
                logger.warning(
                    f"[WHALE] UNIVERSAL BUY attempt {attempt}/{max_retries}: "
                    f"Searching for liquidity for {mint_str[:8]}..."
                )

                # DCA: Покупаем только часть позиции сначала (50%)
                dca_enabled = self.dca_enabled
                dca_first_buy_pct = self.dca_first_buy_pct if hasattr(self, 'dca_first_buy_pct') else 0.50  # 50% первая покупка
                
                if dca_enabled:
                    actual_buy_amount = self.buy_amount * dca_first_buy_pct
                    logger.warning(f"[DCA] First buy: {actual_buy_amount:.4f} SOL (50% of {self.buy_amount:.4f})")
                else:
                    actual_buy_amount = self.buy_amount
                
                success, tx_sig, dex_used, token_amount, price = await self._buy_any_dex(
                    mint_str=mint_str,
                    symbol=whale_buy.token_symbol,
                    sol_amount=actual_buy_amount,
                    jupiter_first=True,  # Skip bonding curves for whale copy
                    prefetched_quote=prefetched_quote,
                    whale_wallet=whale_buy.whale_wallet,
                    whale_label=whale_buy.whale_label,
                    virtual_sol_reserves=getattr(whale_buy, "virtual_sol_reserves", 0),
                    virtual_token_reserves=getattr(whale_buy, "virtual_token_reserves", 0),
                    whale_token_program=getattr(whale_buy, "whale_token_program", ""),
                    whale_creator_vault=getattr(whale_buy, "whale_creator_vault", ""),
                    whale_fee_recipient=getattr(whale_buy, "whale_fee_recipient", ""),
                    whale_assoc_bonding_curve=getattr(whale_buy, "whale_assoc_bonding_curve", ""),
                )

                # Fatal errors — не ретраить, токен невалидный
                if not success and dex_used and isinstance(dex_used, str):
                    _err_upper = dex_used.upper()
                    if "NOT_TRADABLE" in _err_upper or "NOT TRADABLE" in _err_upper:
                        logger.warning(f"[WHALE] ⛔ TOKEN NOT TRADABLE: {mint_str[:12]}... — abort")
                        break

                if success:
                    break

                # Если не последняя попытка - ждём и пробуем снова
                if attempt < max_retries:
                    logger.warning(
                        f"[WHALE] Attempt {attempt} failed, waiting {retry_delay}s before retry..."
                    )
                    await asyncio.sleep(retry_delay)

            if success:
                # MOVED TO TX_CALLBACK - position/history added after TX verification
                self._bought_tokens.add(mint_str)  # PATCH 8: prevent double buy
                # add_to_purchase_history(
                #     mint=mint_str,
                #     symbol=whale_buy.token_symbol,
                #     bot_name="whale_copy",
                #     platform=dex_used,
                #     price=price,
                #     amount=token_amount,
                # )
                pass  # Callback will handle position creation

                # Clean readable success log
                logger.warning("=" * 70)
                logger.warning("[WHALE COPY] SUCCESS")
                logger.warning(f"  SYMBOL:    {whale_buy.token_symbol}")
                # stats tracked via buys_emitted
                logger.warning(f"  TOKEN:     {mint_str}")
                logger.warning(f"  DEX:       {dex_used}")
                logger.warning(f"  AMOUNT:    {token_amount:.2f} tokens")
                logger.warning(f"  PRICE:     {price:.10f} SOL")
                logger.warning(f"  WHALE:     {whale_buy.whale_label}")
                logger.warning(f"  WALLET:    {whale_buy.whale_wallet}")
                logger.warning(f"  TX:        {tx_sig}")
                logger.warning("=" * 70)

                # Save position WITH TP/SL settings!
                mint = Pubkey.from_string(mint_str)
                # USE REAL PRICE FROM TX if available, DexScreener only as fallback!
                if price > 0:
                    # Price from transaction is authoritative
                    entry_price = price
                    logger.warning(f"[WHALE] Entry price from TX: {entry_price:.10f} SOL")
                else:
                    # No TX price (Jupiter/fallback) - try DexScreener
                    from utils.dexscreener_price import get_price_from_dexscreener
                    try:
                        dex_price = await get_price_from_dexscreener(mint_str)
                        if dex_price and dex_price > 0:
                            entry_price = dex_price
                            logger.warning(f"[WHALE] Entry price from DexScreener: {entry_price:.10f} SOL")
                        else:
                            entry_price = self.buy_amount / max(token_amount, 1)
                            logger.error(f"[WHALE] WARNING: Using calculated entry_price: {entry_price:.10f} SOL")
                    except Exception as e:
                        entry_price = self.buy_amount / max(token_amount, 1)
                        logger.error(f"[WHALE] DexScreener failed ({e}), using calculated: {entry_price:.10f} SOL")
                # CRITICAL: Create position with TP/SL using same method as regular buys!
                # CRITICAL: Derive bonding_curve for fast sell path (avoid fallback)
                from solders.pubkey import Pubkey as SoldersPubkey
                PUMP_PROGRAM_ID = SoldersPubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
                bonding_curve_derived, _ = SoldersPubkey.find_program_address(
                    [b"bonding-curve", bytes(mint)],
                    PUMP_PROGRAM_ID
                )
                logger.info(f"[WHALE] Derived bonding_curve: {bonding_curve_derived}")

                # === PATCH 12: INSTANT gRPC subscribe — NO WAITING for TX callback ===
                if self.whale_tracker and hasattr(self.whale_tracker, 'subscribe_bonding_curve'):
                    asyncio.create_task(self.whale_tracker.subscribe_bonding_curve(
                        mint=mint_str,
                        curve_address=str(bonding_curve_derived),
                        symbol=whale_buy.token_symbol,
                        decimals=9 if mint_str.lower().endswith("bags") else 6,
                    ))
                    logger.warning(f"[PATCH12] \u26a1 INSTANT gRPC subscribe for {whale_buy.token_symbol} (curve={str(bonding_curve_derived)[:16]}...)")
                # === END PATCH 12 ===

                # === Phase 6: Subscribe to ATA for instant token arrival detection ===
                if self.whale_tracker and hasattr(self.whale_tracker, 'subscribe_ata'):
                    try:
                        from solders.pubkey import Pubkey as _Pk
                        _TOKEN_PROG = _Pk.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
                        _ATA_PROG = _Pk.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
                        _wallet_pk = self.wallet.pubkey
                        _ata_addr, _ = _Pk.find_program_address(
                            [bytes(_wallet_pk), bytes(_TOKEN_PROG), bytes(mint)],
                            _ATA_PROG
                        )
                        asyncio.create_task(self.whale_tracker.subscribe_ata(
                            mint=mint_str,
                            ata_address=str(_ata_addr),
                            symbol=whale_buy.token_symbol,
                        ))
                        logger.warning(f"[Phase6] ATA subscribe for {whale_buy.token_symbol} (ata={str(_ata_addr)[:16]}...)")
                    except Exception as _ata_err:
                        logger.warning(f"[Phase6] ATA subscribe failed: {_ata_err}")
                # === END Phase 6 ===

                # INSTANT Position creation — monitor starts NOW, not after TX callback (12s delay)
                from trading.position import Position as PositionCls, save_positions as _save_pos, register_monitor as _reg_mon
                from utils.batch_price_service import watch_token as _watch
                position = PositionCls.create_from_buy_result(
                    mint=mint,
                    symbol=whale_buy.token_symbol,
                    entry_price=entry_price,
                    quantity=token_amount,
                    take_profit_percentage=self.take_profit_percentage,
                    stop_loss_percentage=self.stop_loss_percentage,
                    max_hold_time=self.max_hold_time,
                    platform=dex_used,
                    bonding_curve=str(bonding_curve_derived),
                    tsl_enabled=self.tsl_enabled,
                    tsl_activation_pct=self.tsl_activation_pct,
                    tsl_trail_pct=self.tsl_trail_pct,
                    tsl_sell_pct=self.tsl_sell_pct,
                )
                position.original_entry_price = entry_price  # Save original for moonbag SL
                # Mark entry as provisional: will be corrected to first curve/batch price
                if hasattr(position, "entry_price_provisional"):
                    _ep_source_map = {
                        "pump_fun_direct": ("pumpfun_curve", True),   # Session 3: was False
                        "pump_fun":        ("pumpfun_buyer", True),
                        "lets_bonk":       ("letsbonk_tx",  True),
                        "bags":            ("bags_tx",       True),
                        "bags_fallback":   ("bags_tx",       True),
                        "jupiter":         ("jupiter_tx",    True),
                    }
                    _ep_src, _ep_prov = _ep_source_map.get(dex_used, ("unknown", True))
                    position.entry_price_provisional = _ep_prov
                    position.entry_price_source = _ep_src
                position.tp_sell_pct = self.tp_sell_pct  # FIX S18-8: from yaml (0.9)
                position.buy_confirmed = False  # PATCH: race condition guard — wait for TX confirmation
                position.tokens_arrived = False  # Phase 6: wait for gRPC ATA confirmation
                position.buy_tx_sig = tx_sig  # FIX 10-3: save for on-chain entry verification
                self.active_positions.append(position)
                # FIX S23-6: Remove from sold_mints on new buy (prevent ZOMBIE KILL on re-bought tokens)
                try:
                    import redis as _redis_sync
                    _r = _redis_sync.Redis()
                    _removed = _r.zrem("sold_mints", str(position.mint))
                    _r.close()
                    if _removed:
                        logger.warning(f"[BUY] Cleared stale sold_mint for {whale_buy.token_symbol}")
                except Exception:
                    pass
                _save_pos(self.active_positions)
                _watch(str(position.mint))
                logger.warning(f"[WHALE] ⚡ INSTANT Position created for {whale_buy.token_symbol}")
                # Register reactive SL/TP for instant gRPC-driven sells
                # Session 3: DEFER for pumpfun — GEYSER-SELF will register with correct entry
                # Session 3 fix: Only defer for pumpfun_curve (Jupiter entry is accurate)
                _is_pumpfun_provisional = getattr(position, 'entry_price_source', '') == 'pumpfun_curve'
                if not _is_pumpfun_provisional:
                    try:
                        if self.whale_tracker and hasattr(self.whale_tracker, 'register_sl_tp'):
                            self.whale_tracker.register_sl_tp(
                                mint=mint_str, symbol=whale_buy.token_symbol,
                                entry_price=entry_price,
                                sl_price=position.stop_loss_price or 0,
                                tp_price=position.take_profit_price or 0,
                            )
                    except Exception as _rsl_err:
                        logger.warning(f'[WHALE] register_sl_tp failed: {_rsl_err}')
                else:
                    logger.warning('[WHALE] REACTIVE TP/SL deferred — waiting for GEYSER-SELF entry fix')
                if position.take_profit_price:
                    logger.warning(f"[WHALE] Take profit target: {position.take_profit_price:.10f} SOL (+{(self.take_profit_percentage or 0)*100:.0f}%)")
                if position.stop_loss_price:
                    logger.warning(f"[WHALE] Stop loss target: {position.stop_loss_price:.10f} SOL (-{(self.stop_loss_percentage or 0)*100:.0f}%)")

                self._log_trade(
                    "buy",
                    None,  # No TokenInfo for universal buy
                    entry_price,
                    token_amount,
                    tx_sig,
                    extra=f"whale_copy:{dex_used}:{whale_buy.token_symbol}",
                )

                # ============================================
                # CRITICAL: START SL/TP MONITORING!
                # Without this, stop loss will NEVER trigger!
                # ============================================
                if self.exit_strategy == "tp_sl" and not self.marry_mode:
                    logger.warning(f"[WHALE] Starting TP/SL monitor for {whale_buy.token_symbol}")

                    # Create TokenInfo for monitoring WITH bonding_curve for fast sell!
                    from interfaces.core import TokenInfo
                    from core.pubkeys import SystemAddresses
                    
                    # Derive associated_bonding_curve  
                    associated_bonding_curve_derived, _ = SoldersPubkey.find_program_address(
                        [bytes(bonding_curve_derived), bytes(SystemAddresses.TOKEN_PROGRAM), bytes(mint)],
                        SoldersPubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
                    )
                    
                    # Pump.fun fee recipient (creator_vault) - required for direct sell
                    PUMP_FEE_RECIPIENT = SoldersPubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
                    
                    logger.info(f"[WHALE] Derived bonding_curve: {bonding_curve_derived}")
                    logger.info(f"[WHALE] Derived associated_bonding_curve: {associated_bonding_curve_derived}")
                    
                    token_info = TokenInfo(
                        name=whale_buy.token_symbol,
                        symbol=whale_buy.token_symbol,
                        uri="",
                        mint=mint,
                        platform=self.platform,
                        bonding_curve=bonding_curve_derived,  # CRITICAL for fast sell!
                        associated_bonding_curve=associated_bonding_curve_derived,  # CRITICAL!
                        creator_vault=PUMP_FEE_RECIPIENT,  # CRITICAL: fee recipient for pump.fun sell!
                        user=None,
                        creator=None,
                        creation_timestamp=0,
                    )

                    # INSTANT MONITOR START — no waiting for TX callback!
                    _mint_str = str(mint)
                    if _reg_mon(_mint_str):
                        asyncio.create_task(self._monitor_position_until_exit(token_info, position))
                        logger.warning(f"[WHALE] ⚡ MONITOR STARTED INSTANTLY for {whale_buy.token_symbol}")
                    else:
                        logger.warning(f"[WHALE] Monitor already running for {whale_buy.token_symbol}")
            else:
                # Clean readable failure log
                logger.error("=" * 70)
                logger.error("[WHALE COPY] FAILED - no liquidity found")
                logger.error(f"  SYMBOL:    {whale_buy.token_symbol}")
                if self.whale_tracker: self.whale_tracker._stats["failed"] += 1
                logger.error(f"  TOKEN:     {mint_str}")
                logger.error(f"  WHALE:     {whale_buy.whale_label}")
                logger.error(f"  WALLET:    {whale_buy.whale_wallet}")
                logger.error("=" * 70)

        except Exception as e:
            logger.exception(f"[WHALE] WHALE COPY FAILED: {e}")
        finally:
            # Remove from "buying" set (either succeeded or failed)
            self._buying_tokens.discard(mint_str)

    async def _monitor_whale_position(
        self,
        token_info: "TokenInfo",
        position: Position,
        dex_used: str
    ) -> None:
        """Monitor whale copy position for TP/SL exit.

        This is a wrapper around _monitor_position_until_exit that handles
        whale-specific monitoring with Jupiter fallback for selling.

        Args:
            token_info: Token information
            position: Position to monitor
            dex_used: DEX where token was bought (for logging)
        """
        logger.warning(
            f"[WHALE MONITOR] Starting TP/SL monitor for {token_info.symbol} "
            f"(bought on {dex_used})"
        )
        logger.warning(
            f"[WHALE MONITOR] Entry: {position.entry_price:.10f} SOL, "
            f"TP: {(position.take_profit_price or 0):.10f} SOL, "
            f"SL: {(position.stop_loss_price or 0):.10f} SOL"
        )

        try:
            # Use the same monitoring logic as regular positions
            await self._monitor_position_until_exit(token_info, position)
        except Exception as e:
            logger.exception(
                f"[WHALE MONITOR] CRASHED for {token_info.symbol}! Error: {e}. "
                f"Attempting emergency sell..."
            )
            # Try emergency sell on crash
            try:
                if str(token_info.mint) in NO_SL_MINTS:
                    logger.warning(f"[NO_SL] {token_info.symbol}: Monitor crashed but NO_SL - NOT selling!")
                    return
                fallback_success = await self._emergency_fallback_sell(
                    token_info, position, position.entry_price
                )
                if fallback_success:
                    logger.warning(
                        f"[WHALE MONITOR] Emergency sell SUCCESS for {token_info.symbol}"
                    )
                else:
                    logger.error(
                        f"[WHALE MONITOR] Emergency sell FAILED for {token_info.symbol}! "
                        f"MANUAL SELL REQUIRED! Mint: {token_info.mint}"
                    )
            except Exception as e2:
                logger.exception(
                    f"[WHALE MONITOR] Emergency sell also crashed: {e2}. "
                    f"MANUAL SELL REQUIRED for {token_info.symbol}! Mint: {token_info.mint}"
                )

    async def _prefetch_jupiter_quote(self, mint_str: str, sol_amount: float) -> dict | None:
        """Phase 3.3: Pre-fetch Jupiter quote while scoring runs in parallel.
        Returns quote dict or None on failure. Non-blocking, safe to cancel."""
        try:
            import aiohttp
            sol_mint = "So11111111111111111111111111111111111111112"
            amount_lamports = int(sol_amount * 1_000_000_000)
            slippage_bps = int(self.buy_slippage * 10000)

            headers = {}
            if self.jupiter_api_key:
                headers["x-api-key"] = self.jupiter_api_key

            params = {
                "inputMint": sol_mint,
                "outputMint": mint_str,
                "amount": str(amount_lamports),
                "slippageBps": str(slippage_bps),
                "restrictIntermediateTokens": "true",
                "maxAccounts": "64",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.jup.ag/swap/v1/quote",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        quote = await resp.json()
                        out_amount = int(quote.get("outAmount", 0))
                        if out_amount > 0:
                            quote["_prefetch_time"] = __import__("time").monotonic()
                            logger.info(f"[PREFETCH] Quote OK: {out_amount} tokens for {sol_amount} SOL")
                            return quote
                    else:
                        text = await resp.text()
                        logger.info(f"[PREFETCH] Quote failed: HTTP {resp.status}: {text[:100]}")
        except asyncio.CancelledError:
            raise  # Let cancellation propagate
        except Exception as e:
            logger.info(f"[PREFETCH] Quote error: {e}")
        return None


    async def _async_deployer_check(self, mint_str: str, whale_buy):
        """Background deployer blacklist check (PATCH 11D-ASYNC).
        
        Runs in parallel with Jupiter buy. If deployer is blacklisted,
        cancels the position and sells tokens immediately.
        Cost: ~265ms RPC call, but NO LONGER on critical path.
        """
        try:
            from platforms.pumpfun.address_provider import PumpFunAddresses
            _bc, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(Pubkey.from_string(mint_str))],
                PumpFunAddresses.PROGRAM
            )
            _pool = await self.platform_implementations.curve_manager.get_pool_state(_bc)
            if _pool:
                _creator = _pool.get("creator", "")
                if _creator:
                    _cstr = str(_creator)[:44]
                    from trading.deployer_blacklist import is_deployer_blacklisted, get_deployer_label
                    if is_deployer_blacklisted(_cstr):
                        _label = get_deployer_label(_cstr)
                        logger.warning(
                            f"[BLACKLIST] \u26d4 ASYNC deployer block: {whale_buy.token_symbol} "
                            f"deployer={_label} ({_cstr[:12]}...) — ABORTING POSITION"
                        )
                        # Cancel position and sell if tokens arrived
                        await self._abort_blacklisted_buy(mint_str, whale_buy.token_symbol)
                        return
            logger.debug(f"[BLACKLIST] Async deployer check OK for {mint_str[:12]}")
        except Exception as e:
            logger.debug(f"[BLACKLIST] Async deployer check skipped: {e}")

    async def _abort_blacklisted_buy(self, mint_str: str, symbol: str):
        """Abort a position after async deployer blacklist detection."""
        try:
            # Find and remove position
            position = None
            for p in self.active_positions:
                if str(p.mint) == mint_str:
                    position = p
                    break
            if not position:
                logger.info(f"[BLACKLIST] No position found for {symbol} — buy may have failed")
                return
            # If tokens arrived, sell them
            _tokens_ok = getattr(position, 'tokens_arrived', True)
            _buy_ok = getattr(position, 'buy_confirmed', True)
            if _tokens_ok and _buy_ok:
                logger.warning(f"[BLACKLIST] Selling blacklisted {symbol} — tokens on wallet")
                from interfaces.core import TokenInfo
                from solders.pubkey import Pubkey
                bc = Pubkey.from_string(position.bonding_curve) if position.bonding_curve else None
                token_info = TokenInfo(
                    name=symbol, symbol=symbol, uri="",
                    mint=Pubkey.from_string(mint_str),
                    platform=self.platform, bonding_curve=bc,
                    creator=None, creator_vault=None,
                )
                from trading.position import ExitReason
                await self._fast_sell_with_timeout(
                    token_info, position, position.entry_price,
                    position.quantity, exit_reason=ExitReason.STOP_LOSS
                )
            else:
                # FIX S14-1: TX sent but not confirmed — DON'T remove, mark for sell on confirm
                _has_tx = getattr(position, 'buy_tx_sig', None)
                if _has_tx:
                    position.blacklist_sell_pending = True
                    logger.warning(
                        f"[BLACKLIST] {symbol}: TX pending ({_has_tx[:16]}...) — "
                        f"will SELL immediately on confirm (FIX S14-1)"
                    )
                    try:
                        from trading.position import save_positions
                        save_positions(self.active_positions)
                    except Exception:
                        pass
                else:
                    # No TX sent — safe to remove
                    logger.warning(f"[BLACKLIST] Removing unconfirmed blacklisted position {symbol}")
                    self._remove_position(mint_str)
        except Exception as e:
            logger.error(f"[BLACKLIST] Abort failed for {symbol}: {e}")

    async def _blacklist_instant_sell(self, mint_str: str, symbol: str):
        """FIX S14-1: Instantly sell a blacklisted position after BUY confirmed."""
        try:
            position = None
            for p in self.active_positions:
                if str(p.mint) == mint_str:
                    position = p
                    break
            if not position:
                logger.warning(f"[BLACKLIST SELL] {symbol}: position not found, may already be sold")
                return
            logger.warning(f"[BLACKLIST SELL] {symbol}: executing immediate sell (FIX S14-1)")
            from interfaces.core import TokenInfo
            from solders.pubkey import Pubkey as SoldersPubkey
            bc = SoldersPubkey.from_string(position.bonding_curve) if position.bonding_curve else None
            token_info = TokenInfo(
                name=symbol, symbol=symbol, uri="",
                mint=SoldersPubkey.from_string(mint_str),
                platform=self.platform, bonding_curve=bc,
                creator=None, creator_vault=None,
            )
            from trading.position import ExitReason
            await self._fast_sell_with_timeout(
                token_info, position, position.entry_price,
                position.quantity, exit_reason=ExitReason.STOP_LOSS
            )
            logger.warning(f"[BLACKLIST SELL] {symbol}: sell completed (FIX S14-1)")
        except Exception as e:
            logger.error(f"[BLACKLIST SELL] {symbol}: sell failed: {e} — monitor will handle via SL")

    async def _buy_any_dex(
        self,
        mint_str: str,
        symbol: str,
        sol_amount: float,
        jupiter_first: bool = False,
        is_dca: bool = False,
        whale_wallet: str = None,
        whale_label: str = None,
        prefetched_quote: dict | None = None,
        virtual_sol_reserves: int = 0,
        virtual_token_reserves: int = 0,
        whale_token_program: str = "",
        whale_creator_vault: str = "",
        whale_fee_recipient: str = "",
        whale_assoc_bonding_curve: str = "",
    ) -> tuple[bool, str | None, str, float, float]:
        """Buy token on ANY available DEX - universal liquidity finder.

        Порядок попыток:
        1. Pump.Fun bonding curve (если бот на pump_fun)
        2. LetsBonk bonding curve (если бот на lets_bonk)
        3. PumpSwap (для мигрированных pump.fun токенов)
        4. Jupiter (универсальный aggregator - найдет любую ликвидность)

        Args:
            mint_str: Token mint address as string
            symbol: Token symbol for logging
            sol_amount: Amount of SOL to spend

        Returns:
            Tuple of (success, tx_signature, dex_used, token_amount, price)
        """

        mint = Pubkey.from_string(mint_str)

        # ============================================
        # CROSS-BOT DUPLICATE CHECK (reads positions.json)
        # Skip this check for DCA - we WANT to buy more of existing position!
        # ============================================
        if not is_dca and is_token_in_positions(mint_str):
            logger.info(f"[SKIP] {symbol} already in positions.json (another bot bought it)")
            return False, None, "skip", 0.0, 0.0

        # ============================================
        # [1/4] TRY ALL BONDING CURVES (for whale_all_platforms mode)
        # When whale_all_platforms=true, we need to check ALL platforms
        # ============================================
        
        # PUMP.FUN - Check always for whale_all_platforms or if current platform
        should_check_pumpfun = (not jupiter_first) and (self.platform == Platform.PUMP_FUN or 
                                getattr(self, 'enable_whale_copy', False))
        if should_check_pumpfun:
            logger.info(f"[CHECK] [1/4] Checking Pump.Fun bonding curve for {symbol}...")
            try:
                from platforms.pumpfun.address_provider import PumpFunAddresses

                # Derive bonding curve
                bonding_curve, _ = Pubkey.find_program_address(
                    [b"bonding-curve", bytes(mint)],
                    PumpFunAddresses.PROGRAM
                )

                # Check if bonding curve exists and not migrated
                curve_manager = self.platform_implementations.curve_manager
                pool_state = await curve_manager.get_pool_state(bonding_curve)

                if pool_state and not pool_state.get("complete", False):
                    # Bonding curve available! Use normal pump.fun buy
                    logger.info(f"[OK] Pump.Fun bonding curve available for {symbol}")

                    # === PATCH 11: Pre-buy deployer blacklist check (0ms — data already in pool_state) ===
                    _creator_addr = pool_state.get("creator", "")
                    if _creator_addr:
                        _creator_str = str(_creator_addr)[:44]
                        from trading.deployer_blacklist import is_deployer_blacklisted, get_deployer_label
                        if is_deployer_blacklisted(_creator_str):
                            _bl_label = get_deployer_label(_creator_str)
                            logger.warning(
                                f"[BLACKLIST] \u26d4 PRE-BUY BLOCK: {symbol} deployer={_bl_label} "
                                f"({_creator_str[:12]}...) — SKIPPING BUY"
                            )
                            return False, None, "blacklisted", 0.0, 0.0
                    # === END PATCH 11 ===

                    # Create TokenInfo for pump.fun buy
                    token_info = await self._create_pumpfun_token_info_from_mint(
                        mint_str, symbol, bonding_curve, pool_state
                    )

                    if token_info:
                        # Execute buy via normal flow
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] Pump.Fun BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            # Schedule TxVerifier for position creation
                            from core.tx_verifier import get_tx_verifier
                            from core.tx_callbacks import on_buy_success, on_buy_failure
                            verifier = await get_tx_verifier()
                            await verifier.schedule_verification(
                                signature=buy_result.tx_signature,
                                mint=mint_str,
                                symbol=symbol,
                                action="buy",
                                token_amount=buy_result.amount or 0,
                                price=buy_result.price or 0,
                                on_success=on_buy_success,
                                on_failure=on_buy_failure,
                                context={
                                    "platform": "pump_fun",
                                    "bot_name": "universal_trader",
                                    "take_profit_pct": self.take_profit_percentage,
                                    "stop_loss_pct": self.stop_loss_percentage,
                                    "tsl_enabled": self.tsl_enabled,
                                    "tsl_activation_pct": self.tsl_activation_pct,
                                    "tsl_trail_pct": self.tsl_trail_pct,
                                    "tsl_sell_pct": self.tsl_sell_pct,
                                    "tp_sell_pct": self.tp_sell_pct,
                                    "max_hold_time": self.max_hold_time,
                                    "whale_wallet": whale_wallet,
                                    "whale_label": whale_label,
                                    "dca_enabled": self.dca_enabled,
                                    "bonding_curve": str(bonding_curve),
                                },
                            )
                            return True, buy_result.tx_signature, "pump_fun", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.warning(f"[WARN] Pump.Fun buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] Pump.Fun bonding curve migrated or unavailable for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] Pump.Fun check failed: {e}")

        # LETSBONK - Check always for whale_all_platforms or if current platform
        should_check_letsbonk = (not jupiter_first) and (self.platform == Platform.LETS_BONK or 
                                  getattr(self, 'enable_whale_copy', False))
        if should_check_letsbonk:
            logger.info(f"[CHECK] [1/4] Checking LetsBonk bonding curve for {symbol}...")
            try:
                from platforms.letsbonk.address_provider import LetsBonkAddressProvider
                from platforms.letsbonk.curve_manager import LetsBonkCurveManager

                address_provider = LetsBonkAddressProvider()
                pool_address = address_provider.derive_pool_address(mint)

                # Use LetsBonk-specific curve manager for proper parsing
                # LetsBonk requires IDL parser - try to create it, skip if fails
                try:
                    from utils.idl_parser import IDLParser
                    idl_path = "src/platforms/letsbonk/idl/bonk_amm.json"
                    idl_parser = IDLParser(idl_path) if os.path.exists(idl_path) else None
                    if idl_parser:
                        letsbonk_curve_manager = LetsBonkCurveManager(self.solana_client, idl_parser)
                    else:
                        raise ValueError("LetsBonk IDL not found")
                except Exception as idl_err:
                    logger.info(f"[WARN] LetsBonk IDL init failed: {idl_err}, skipping LetsBonk check")
                    raise ValueError(f"LetsBonk not available: {idl_err}")
                pool_state = await letsbonk_curve_manager.get_pool_state(pool_address)

                if pool_state and not pool_state.get("complete", False) and pool_state.get("status") != "migrated":
                    # Bonding curve available! Use normal letsbonk buy
                    logger.info(f"[OK] LetsBonk bonding curve available for {symbol}")

                    # === PATCH 11B: Pre-buy deployer blacklist check (LetsBonk) ===
                    _creator_addr = pool_state.get("creator", "")
                    if _creator_addr:
                        _creator_str = str(_creator_addr)[:44]
                        from trading.deployer_blacklist import is_deployer_blacklisted, get_deployer_label
                        if is_deployer_blacklisted(_creator_str):
                            _bl_label = get_deployer_label(_creator_str)
                            logger.warning(
                                f"[BLACKLIST] \u26d4 PRE-BUY BLOCK: {symbol} deployer={_bl_label} "
                                f"({_creator_str[:12]}...) — SKIPPING BUY"
                            )
                            return False, None, "blacklisted", 0.0, 0.0
                    # === END PATCH 11B ===

                    # Create TokenInfo for letsbonk buy
                    token_info = await self._create_letsbonk_token_info_from_mint(
                        mint_str, symbol, pool_address, pool_state
                    )

                    if token_info:
                        # Execute buy via normal flow
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] LetsBonk BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            # Schedule TxVerifier for position creation
                            from core.tx_verifier import get_tx_verifier
                            from core.tx_callbacks import on_buy_success, on_buy_failure
                            verifier = await get_tx_verifier()
                            await verifier.schedule_verification(
                                signature=buy_result.tx_signature,
                                mint=mint_str,
                                symbol=symbol,
                                action="buy",
                                token_amount=buy_result.amount or 0,
                                price=buy_result.price or 0,
                                on_success=on_buy_success,
                                on_failure=on_buy_failure,
                                context={
                                    "platform": "lets_bonk",
                                    "bot_name": "universal_trader",
                                    "take_profit_pct": self.take_profit_percentage,
                                    "stop_loss_pct": self.stop_loss_percentage,
                                    "tsl_enabled": self.tsl_enabled,
                                    "tsl_activation_pct": self.tsl_activation_pct,
                                    "tsl_trail_pct": self.tsl_trail_pct,
                                    "tsl_sell_pct": self.tsl_sell_pct,
                                    "tp_sell_pct": self.tp_sell_pct,
                                    "max_hold_time": self.max_hold_time,
                                    "whale_wallet": whale_wallet,
                                    "whale_label": whale_label,
                                    "dca_enabled": self.dca_enabled,
                                },
                            )
                            return True, buy_result.tx_signature, "lets_bonk", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.warning(f"[WARN] LetsBonk buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] LetsBonk bonding curve migrated or unavailable for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] LetsBonk check failed: {e}")

        # BAGS - Check always for whale_all_platforms or if current platform
        should_check_bags = (not jupiter_first) and (self.platform == Platform.BAGS or 
                              getattr(self, 'enable_whale_copy', False))
        if should_check_bags:
            logger.info(f"[CHECK] [1/4] Checking BAGS (Meteora DBC) pool for {symbol}...")
            try:
                from platforms.bags.address_provider import BagsAddressProvider
                from platforms.bags.curve_manager import BagsCurveManager

                address_provider = BagsAddressProvider()
                pool_address = address_provider.derive_pool_address(mint)

                # Use BAGS-specific curve manager for proper parsing
                bags_curve_manager = BagsCurveManager(self.solana_client)
                pool_state = await bags_curve_manager.get_pool_state(pool_address)

                if pool_state and pool_state.get("status") != "migrated":
                    # BAGS pool available! Use normal bags buy
                    logger.info(f"[OK] BAGS pool available for {symbol}")

                    # === PATCH 11C: Pre-buy deployer blacklist check (BAGS) ===
                    _creator_addr = pool_state.get("creator", "")
                    if _creator_addr:
                        _creator_str = str(_creator_addr)[:44]
                        from trading.deployer_blacklist import is_deployer_blacklisted, get_deployer_label
                        if is_deployer_blacklisted(_creator_str):
                            _bl_label = get_deployer_label(_creator_str)
                            logger.warning(
                                f"[BLACKLIST] \u26d4 PRE-BUY BLOCK: {symbol} deployer={_bl_label} "
                                f"({_creator_str[:12]}...) — SKIPPING BUY"
                            )
                            return False, None, "blacklisted", 0.0, 0.0
                    # === END PATCH 11C ===

                    # Create TokenInfo for bags buy
                    token_info = await self._create_bags_token_info_from_mint(
                        mint_str, symbol, pool_address, pool_state
                    )

                    if token_info:
                        # Execute buy via normal flow
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] BAGS BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            # Schedule TxVerifier for position creation
                            from core.tx_verifier import get_tx_verifier
                            from core.tx_callbacks import on_buy_success, on_buy_failure
                            verifier = await get_tx_verifier()
                            await verifier.schedule_verification(
                                signature=buy_result.tx_signature,
                                mint=mint_str,
                                symbol=symbol,
                                action="buy",
                                token_amount=buy_result.amount or 0,
                                price=buy_result.price or 0,
                                on_success=on_buy_success,
                                on_failure=on_buy_failure,
                                context={
                                    "platform": "bags",
                                    "bot_name": "universal_trader",
                                    "take_profit_pct": self.take_profit_percentage,
                                    "stop_loss_pct": self.stop_loss_percentage,
                                    "tsl_enabled": self.tsl_enabled,
                                    "tsl_activation_pct": self.tsl_activation_pct,
                                    "tsl_trail_pct": self.tsl_trail_pct,
                                    "tsl_sell_pct": self.tsl_sell_pct,
                                    "tp_sell_pct": self.tp_sell_pct,
                                    "max_hold_time": self.max_hold_time,
                                    "whale_wallet": whale_wallet,
                                    "whale_label": whale_label,
                                    "dca_enabled": self.dca_enabled,
                                },
                            )
                            return True, buy_result.tx_signature, "bags", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.warning(f"[WARN] BAGS buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] BAGS pool migrated or unavailable for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] BAGS check failed: {e}")

        # ============================================
        # [2/4] PUMPSWAP DISABLED - 0% success rate, wastes 10+ seconds
        # Stats: 15 attempts, 0 successes (2026-01-30)
        # Jupiter handles all PumpSwap pools via aggregation
        # To re-enable: see git history or backup file
        # ============================================

        # ============================================
        # [2.5/4] TRY PUMP.FUN DIRECT BONDING CURVE BUY
        # ~72ms vs Jupiter 2500ms for fresh bonding curve tokens
        # Falls through to Jupiter if BC not found / complete / error
        # ============================================
        try:
            # FIX S40-1: Skip direct if no reserves from whale TX (saves 70-150ms)
            # reserves=0 = whale traded via PumpSwap/Jupiter = token migrated
            if virtual_sol_reserves == 0 or virtual_token_reserves == 0:
                logger.info(f"[SKIP] [2.5/4] No reserves — skip direct, go Jupiter ({symbol})")
                raise Exception("no_reserves_skip")
            fallback_direct = self._fallback_buyer
            logger.info(f"[CHECK] [2.5/4] Trying pump.fun DIRECT bonding curve for {symbol}...")
            
            from platforms.pumpfun.address_provider import PumpFunAddresses as _PFA
            _bc_direct, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint)], _PFA.PROGRAM,
            )
            
            _direct_pos_config = {
                "take_profit_pct": self.take_profit_percentage,
                "stop_loss_pct": self.stop_loss_percentage,
                "tsl_enabled": self.tsl_enabled,
                "tsl_activation_pct": self.tsl_activation_pct,
                "tsl_trail_pct": self.tsl_trail_pct,
                "tsl_sell_pct": self.tsl_sell_pct,
                "tp_sell_pct": self.tp_sell_pct,
                "max_hold_time": self.max_hold_time,
                "bot_name": "universal_trader",
                "whale_wallet": whale_wallet,
                "whale_label": whale_label,
                "bonding_curve": str(_bc_direct),
                "dca_enabled": self.dca_enabled,
                "dca_pending": self.dca_enabled,
                "dca_trigger_pct": 0.25,
                "dca_first_buy_pct": 0.50,
            }
            
            d_ok, d_sig, d_err, d_tokens, d_price = await fallback_direct.buy_via_pumpfun_direct(
                mint=mint,
                sol_amount=sol_amount,
                symbol=symbol,
                position_config=_direct_pos_config,
                virtual_sol_reserves=virtual_sol_reserves,
                virtual_token_reserves=virtual_token_reserves,
                whale_token_program=whale_token_program,
                whale_creator_vault=whale_creator_vault,
                whale_fee_recipient=whale_fee_recipient,
                whale_assoc_bonding_curve=whale_assoc_bonding_curve,
            )
            
            if d_ok:
                logger.warning(f"[OK] PUMPFUN DIRECT BUY: {symbol} - {d_tokens:,.2f} tokens @ {d_price:.10f} SOL")
                logger.warning(f"[OK] PUMPFUN DIRECT TX: {d_sig}")
                return True, d_sig, "pump_fun_direct", d_tokens, d_price
            else:
                logger.info(f"[PUMPFUN-DIRECT] Failed: {d_err} — falling through to Jupiter")
        except Exception as e:
            logger.info(f"[PUMPFUN-DIRECT] Error: {e} — falling through to Jupiter")

        # ============================================
        # [3/4] TRY JUPITER (universal fallback) - NOW [2/4]
        # ============================================
        logger.info(f"[CHECK] [3/4] Trying Jupiter aggregator for {symbol}...")

        try:
            fallback = self._fallback_buyer  # Use BUY slippage (30%), not sell (20%)
            # Jupiter returns 5 values: success, sig, error, token_amount, price
            # Derive bonding_curve PDA for curve tracking (pump.fun tokens)
            from platforms.pumpfun.address_provider import PumpFunAddresses
            _bc_for_ctx, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint)],
                PumpFunAddresses.PROGRAM,
            )
            # Position config for callback
            position_config = {
                "take_profit_pct": self.take_profit_percentage,
                "stop_loss_pct": self.stop_loss_percentage,
                "tsl_enabled": self.tsl_enabled,
                "tsl_activation_pct": self.tsl_activation_pct,
                "tsl_trail_pct": self.tsl_trail_pct,
                "tsl_sell_pct": self.tsl_sell_pct,
                "tp_sell_pct": self.tp_sell_pct,
                "max_hold_time": self.max_hold_time,
                "bot_name": "universal_trader",
                # Whale info
                "whale_wallet": whale_wallet,
                "whale_label": whale_label,
                # Bonding curve for gRPC price tracking
                "bonding_curve": str(_bc_for_ctx),
                # DCA parameters
                "dca_enabled": self.dca_enabled,
                "dca_pending": self.dca_enabled,  # If DCA enabled, wait for dip
                "dca_trigger_pct": 0.25,  # -25% for second buy
                "dca_first_buy_pct": 0.50,  # 50% first buy
                "prefetched_quote": prefetched_quote,
            }
            
            success, sig, error, token_amount, real_price = await fallback.buy_via_jupiter(
                mint=mint,
                sol_amount=sol_amount,
                symbol=symbol,
                position_config=position_config,
            )

            if success:
                # Use REAL price from Jupiter (calculated from quote)
                logger.warning(f"[OK] Jupiter BUY: {symbol} - {token_amount:,.2f} tokens @ {real_price:.10f} SOL")
                logger.warning(f"[OK] Jupiter TX: {sig}")
                return True, sig, "jupiter", token_amount, real_price
            else:
                logger.info(f"[WARN] Jupiter failed: {error}")

        except Exception as e:
            logger.info(f"[WARN] Jupiter error: {e}")

        # ============================================

        # ============================================
        # [4/4] TRY BAGS/METEORA (universal fallback)
        # This runs for ALL platforms when whale_all_platforms=true
        # ============================================
        if self.platform != Platform.BAGS:  # Skip if already checked above
            logger.info(f"[CHECK] [4/4] Trying BAGS (Meteora DBC) as fallback for {symbol}...")
            try:
                from platforms.bags.address_provider import BagsAddressProvider

                address_provider = BagsAddressProvider()
                pool_address = address_provider.derive_pool_address(mint)

                # Check if pool exists
                curve_manager_bags = None
                try:
                    from platforms.bags.curve_manager import BagsCurveManager
                    curve_manager_bags = BagsCurveManager(self.solana_client)
                except ImportError:
                    # Fallback: use platform_implementations if available
                    pass

                pool_state = None
                if curve_manager_bags:
                    pool_state = await curve_manager_bags.get_pool_state(pool_address)

                if pool_state and pool_state.get("status") != "migrated":
                    logger.info(f"[OK] BAGS pool available for {symbol}")

                    # Create TokenInfo for bags buy
                    token_info = await self._create_bags_token_info_from_mint(
                        mint_str, symbol, pool_address, pool_state
                    )

                    if token_info:
                        # Execute buy via buyer
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] BAGS FALLBACK BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            return True, buy_result.tx_signature, "bags_fallback", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.info(f"[WARN] BAGS fallback buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] BAGS pool not found or migrated for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] BAGS fallback check failed: {e}")

        # [FAIL] NO LIQUIDITY FOUND
        # ============================================
        logger.error(f"[FAIL] NO LIQUIDITY: Could not buy {symbol} on any DEX")
        return False, None, "none", 0, 0

    async def _create_pumpfun_token_info_from_mint(
        self,
        mint_str: str,
        symbol: str,
        bonding_curve: Pubkey,
        pool_state: dict,
    ) -> "TokenInfo | None":
        """Create TokenInfo for pump.fun from mint address (for universal buy).

        Args:
            mint_str: Token mint address as string
            symbol: Token symbol
            bonding_curve: Derived bonding curve address
            pool_state: Pool state from curve manager

        Returns:
            TokenInfo or None if creation fails
        """
        from interfaces.core import TokenInfo
        from platforms.pumpfun.address_provider import PumpFunAddresses
        from core.pubkeys import SystemAddresses

        try:
            mint = Pubkey.from_string(mint_str)

            # Extract creator
            creator = pool_state.get("creator")
            if creator and isinstance(creator, str):
                creator = Pubkey.from_string(creator)
            elif not isinstance(creator, Pubkey):
                creator = None

            # Derive addresses
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

            associated_bonding_curve, _ = Pubkey.find_program_address(
                [bytes(bonding_curve), bytes(token_program_id), bytes(mint)],
                SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
            )

            creator_vault = None
            if creator:
                creator_vault, _ = Pubkey.find_program_address(
                    [b"creator-vault", bytes(creator)],
                    PumpFunAddresses.PROGRAM
                )

            return TokenInfo(
                name=symbol,
                symbol=symbol,
                uri="",
                mint=mint,
                platform=Platform.PUMP_FUN,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=None,
                creator=creator,
                creator_vault=creator_vault,
                pool_state=pool_state,
                base_vault=None,
                quote_vault=None,
                token_program_id=token_program_id,
                creation_timestamp=0,
            )

        except Exception as e:
            logger.warning(f"Failed to create TokenInfo from mint: {e}")
            return None

    async def _create_letsbonk_token_info_from_mint(
        self,
        mint_str: str,
        symbol: str,
        pool_address: Pubkey,
        pool_state: dict,
    ) -> "TokenInfo | None":
        """Create TokenInfo for LetsBonk from mint address (for universal buy).

        Args:
            mint_str: Token mint address as string
            symbol: Token symbol
            pool_address: Derived pool address
            pool_state: Pool state from curve manager

        Returns:
            TokenInfo or None if creation fails
        """
        from interfaces.core import TokenInfo
        from platforms.letsbonk.address_provider import (
            LetsBonkAddressProvider,
            LetsBonkAddresses,
        )
        from core.pubkeys import SystemAddresses

        try:
            mint = Pubkey.from_string(mint_str)
            address_provider = LetsBonkAddressProvider()

            # Extract creator
            creator = pool_state.get("creator")
            if creator and isinstance(creator, str):
                creator = Pubkey.from_string(creator)
            elif not isinstance(creator, Pubkey):
                creator = None

            # Derive addresses
            base_vault = address_provider.derive_base_vault(mint)
            quote_vault = address_provider.derive_quote_vault(mint)

            # Get global_config and platform_config
            global_config = pool_state.get("global_config") or LetsBonkAddresses.GLOBAL_CONFIG
            platform_config = pool_state.get("platform_config") or LetsBonkAddresses.PLATFORM_CONFIG

            if isinstance(global_config, str):
                global_config = Pubkey.from_string(global_config)
            if isinstance(platform_config, str):
                platform_config = Pubkey.from_string(platform_config)

            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

            return TokenInfo(
                name=symbol,
                symbol=symbol,
                uri="",
                mint=mint,
                platform=Platform.LETS_BONK,
                pool_state=pool_address,
                base_vault=base_vault,
                quote_vault=quote_vault,
                global_config=global_config,
                platform_config=platform_config,
                creator=creator,
                user=None,
                bonding_curve=None,
                associated_bonding_curve=None,
                creator_vault=None,
                token_program_id=token_program_id,
                creation_timestamp=0,
            )

        except Exception as e:
            logger.warning(f"Failed to create LetsBonk TokenInfo from mint: {e}")
            return None

    async def _create_bags_token_info_from_mint(
        self,
        mint_str: str,
        symbol: str,
        pool_address: Pubkey,
        pool_state: dict,
    ) -> "TokenInfo | None":
        """Create TokenInfo for BAGS from mint address (for universal buy).

        Args:
            mint_str: Token mint address as string
            symbol: Token symbol
            pool_address: Derived pool address
            pool_state: Pool state from curve manager

        Returns:
            TokenInfo or None if creation fails
        """
        from interfaces.core import TokenInfo
        from platforms.bags.address_provider import BagsAddressProvider, BagsAddresses
        from core.pubkeys import SystemAddresses

        try:
            mint = Pubkey.from_string(mint_str)
            address_provider = BagsAddressProvider()

            # Extract creator
            creator = pool_state.get("creator")
            if creator and isinstance(creator, str):
                creator = Pubkey.from_string(creator)
            elif not isinstance(creator, Pubkey):
                creator = None

            # Derive addresses
            base_vault = address_provider.derive_base_vault(mint)
            quote_vault = address_provider.derive_quote_vault(mint)

            # Get config from pool_state or use default
            config = pool_state.get("config") or BagsAddresses.DEFAULT_CONFIG
            if isinstance(config, str):
                config = Pubkey.from_string(config)

            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

            return TokenInfo(
                name=symbol,
                symbol=symbol,
                uri="",
                mint=mint,
                platform=Platform.BAGS,
                pool_state=pool_address,
                base_vault=base_vault,
                quote_vault=quote_vault,
                global_config=config,  # BAGS uses config instead of global_config
                platform_config=None,
                creator=creator,
                user=None,
                bonding_curve=None,
                associated_bonding_curve=None,
                creator_vault=None,
                token_program_id=token_program_id,
                creation_timestamp=0,
            )

        except Exception as e:
            logger.warning(f"Failed to create BAGS TokenInfo from mint: {e}")
            return None

    def _extract_creator(self, pool_state: dict) -> Pubkey | None:
        """Extract creator pubkey from pool state.

        Args:
            pool_state: Pool state dictionary

        Returns:
            Creator Pubkey or None
        """
        creator = pool_state.get("creator")
        if creator and isinstance(creator, str):
            try:
                return Pubkey.from_string(creator)
            except Exception:
                return None
        elif isinstance(creator, Pubkey):
            return creator
        return None

    async def _check_dev_reputation(self, creator: Pubkey | None, symbol: str) -> bool:
        """Check if creator passes dev reputation check.

        Args:
            creator: Creator pubkey
            symbol: Token symbol for logging

        Returns:
            True if safe to trade, False if should skip
        """
        if not self.dev_checker or not creator:
            return True

        try:
            dev_result = await self.dev_checker.check_dev(str(creator))
            logger.info(
                f"[DEV] Dev check: tokens={dev_result.get('tokens_created', -1)}, "
                f"risk={dev_result.get('risk_score', 0)}, safe={dev_result.get('is_safe', True)}"
            )
            if not dev_result.get("is_safe", True):
                logger.warning(
                    f"[DEV] Skipping {symbol} - Serial token creator: "
                    f"{dev_result.get('tokens_created', 'unknown')} tokens"
                )
                return False
        except Exception as e:
            logger.warning(f"[DEV] Dev check failed for {symbol}: {e}")
            # Continue if dev check fails - better to buy than miss

        return True

    async def _on_volume_opportunity(self, analysis: TokenVolumeAnalysis):
        """Callback when volume pattern analyzer finds an opportunity."""
        # ============================================
        # CRITICAL BALANCE CHECK - STOP BOT
        # ============================================
        if self._critical_low_balance:
            logger.warning("[VOLUME] Bot stopped due to critical low balance, ignoring signal")
            return

        mint_str = analysis.mint

        # Anti-duplicate check
        if mint_str in self._bought_tokens or mint_str in self._buying_tokens:
            logger.info(f"[VOLUME] {analysis.symbol} already bought/buying, skipping")
            return

        # Double-check fresh file (other bots may have bought)
        if was_token_purchased(mint_str):
            logger.info(f"[VOLUME] {analysis.symbol} found in purchase history file, skipping")
            self._bought_tokens.add(mint_str)  # Sync memory
            return

        async with self._buy_lock:
            if mint_str in self._bought_tokens or mint_str in self._buying_tokens:
                return
            self._buying_tokens.add(mint_str)

        try:
            logger.warning(
                f"[VOLUME] OPPORTUNITY: {analysis.symbol} | "
                f"Health:{analysis.health_score} Opp:{analysis.opportunity_score} | "
                f"vol=${analysis.volume_5m:,.0f} ({analysis.volume_spike_ratio:.1f}x)"
            )

            from interfaces.core import TokenInfo
            from solders.pubkey import Pubkey

            token_info = TokenInfo(
                mint=Pubkey.from_string(mint_str),
                name=analysis.symbol,
                symbol=analysis.symbol,
                uri="",
                bonding_curve=None,
                associated_bonding_curve=None,
                user=None,
                creator=None,
                platform=self.platform,
                # pool_address removed
                pool_state=None,
                creator_vault=None,
            )

            # Check if token is actually on pump_fun (not graduated to Raydium)
            if self.platform == Platform.PUMP_FUN:
                from platforms.pumpfun.bonding_curve_address_provider import get_bonding_curve_address
                bc = get_bonding_curve_address(token_info.mint)
                bc_exists = await self.solana_client.check_account_exists(bc)
                if not bc_exists:
                    logger.warning(f"[VOLUME] {analysis.symbol} not on pump_fun (graduated/raydium), skipping")
                    return

            buy_success = await self._handle_token(token_info, skip_checks=False)

            # MOVED TO TX_CALLBACK - position/history added after TX verification
            # if buy_success:
            #     self._bought_tokens.add(mint_str)
            #     add_to_purchase_history(
            #         mint=mint_str,
            #         symbol=analysis.symbol,
            #         bot_name="volume_analyzer",
            #         platform=self.platform.value,
            #         price=0,
            #         amount=0,
            #     )
            if buy_success:
                logger.info(f"[VOLUME] TX sent, position will be created by callback")

        except Exception as e:
            logger.error(f"[VOLUME] Error processing {analysis.symbol}: {e}")
        finally:
            self._buying_tokens.discard(mint_str)

    async def _on_trending_token(self, token: TrendingToken):
        """Callback when trending scanner finds a hot token.

        Supports all platforms: pump_fun, lets_bonk, bags.
        For existing/migrated tokens, uses Jupiter/PumpSwap for trading.

        ANTI-DUPLICATE: Uses unified _buy_lock and _buying_tokens/_bought_tokens
        to prevent ANY duplicate purchases across ALL buy paths.
        """
        # ============================================
        # CRITICAL BALANCE CHECK - STOP BOT
        # ============================================
        if self._critical_low_balance:
            logger.warning("[TRENDING] Bot stopped due to critical low balance, ignoring signal")
            return

        mint_str = token.mint

        # ============================================
        # ANTI-DUPLICATE CHECK (CRITICAL!)
        # ============================================
        # FAST CHECK before lock (optimization - avoid lock contention)
        if mint_str in self._bought_tokens or mint_str in self._buying_tokens:
            logger.info(f"[TRENDING] Token {token.symbol} already bought/buying, skipping")
            return

        # Use lock to prevent race condition between ALL buy paths
        async with self._buy_lock:
            # Re-check after acquiring lock (another task might have started buying)
            if mint_str in self._bought_tokens:
                logger.info(f"[TRENDING] Token {token.symbol} already bought (after lock), skipping")
                return

            if mint_str in self._buying_tokens:
                logger.info(f"[TRENDING] Token {token.symbol} already being bought (after lock), skipping")
                return

            # Check if already have position in this token
            for pos in self.active_positions:
                if str(pos.mint) == mint_str:
                    logger.info(f"[TRENDING] Already have position in {token.symbol}, skipping")
                    self._bought_tokens.add(mint_str)  # Mark as bought to prevent future attempts
                    return

            # Mark as BUYING (in progress) BEFORE releasing lock
            self._buying_tokens.add(mint_str)

        # Now proceed with buy (outside lock to not block other operations)
        try:
            # ============================================
            # MANDATORY SCORING CHECK - ФИЛЬТР МУСОРА!
            # Trending scanner проверяет только базовые метрики,
            # но scoring проверяет качество токена глубже.
            # БЕЗ ЭТОЙ ПРОВЕРКИ ПОКУПАЕМ МУСОР!
            # ============================================
            if self.token_scorer:
                try:
                    should_buy, score = await self.token_scorer.should_buy(
                        mint_str, token.symbol
                    )
                    logger.warning(
                        f"[TRENDING SCORE] {token.symbol}: {score.total_score}/100 "
                        f"-> {score.recommendation}"
                    )
                    logger.info(
                        f"[TRENDING SCORE] Details: vol={score.volume_score}, "
                        f"bp={score.buy_pressure_score}, mom={score.momentum_score}, "
                        f"liq={score.liquidity_score}"
                    )

                    if not should_buy:
                        logger.warning(
                            f"[TRENDING] SKIP LOW SCORE: {token.symbol} "
                            f"score={score.total_score} < min_score={self.token_scorer.min_score}"
                        )
                        return

                    logger.info(
                        f"[TRENDING] SCORE OK: {token.symbol} "
                        f"score={score.total_score} >= {self.token_scorer.min_score}"
                    )
                except Exception as e:
                    # CRITICAL: Если scoring упал - НЕ покупаем! Безопасность важнее
                    logger.warning(
                        f"[TRENDING] SKIP - Scoring check failed: {e} - NOT buying!"
                    )
                    return

            logger.warning(
                f"[TRENDING] TRENDING BUY: {token.symbol} - "
                f"MC: ${token.market_cap:,.0f}, Vol: ${token.volume_24h:,.0f}, "
                f"+{token.price_change_1h:.1f}% 1h"
            )

            # Start pattern tracking for this token (works for ALL tokens, not just new)
            if self.pattern_detector:
                self.pattern_detector.start_tracking(mint_str, token.symbol)
                logger.info(f"[TRENDING] Started pattern tracking for {token.symbol}")

            from interfaces.core import TokenInfo
            from core.pubkeys import SystemAddresses

            mint = Pubkey.from_string(mint_str)

            # Determine platform from token mint suffix or dex_id
            detected_platform = self._detect_token_platform(token)

            # Use detected platform or current bot platform
            target_platform = detected_platform or self.platform

            logger.info(f"[TRENDING] Token platform: {target_platform.value}")

            # Get platform-specific addresses
            bonding_curve = None
            is_migrated = False
            pool_state = None
            creator = None

            if target_platform == Platform.PUMP_FUN:
                from platforms.pumpfun.address_provider import PumpFunAddresses
                bonding_curve, _ = Pubkey.find_program_address(
                    [b"bonding-curve", bytes(mint)],
                    PumpFunAddresses.PROGRAM
                )
            elif target_platform == Platform.LETS_BONK:
                from platforms.letsbonk.address_provider import LetsBonkAddresses
                bonding_curve, _ = Pubkey.find_program_address(
                    [b"bonding-curve", bytes(mint)],
                    LetsBonkAddresses.PROGRAM
                )
            elif target_platform == Platform.BAGS:
                from platforms.bags.address_provider import BagsAddresses
                bonding_curve, _ = Pubkey.find_program_address(
                    [b"pool", bytes(mint)],
                    BagsAddresses.DBC_PROGRAM
                )

            # Check if migrated (bonding curve complete or unavailable)
            if bonding_curve:
                try:
                    curve_manager = self.platform_implementations.curve_manager
                    pool_state = await curve_manager.get_pool_state(bonding_curve)
                    if pool_state.get("complete", False):
                        is_migrated = True
                        logger.info(f"[TRENDING] {token.symbol} migrated - using Jupiter/PumpSwap")
                    creator = pool_state.get("creator")
                    if creator and isinstance(creator, str):
                        creator = Pubkey.from_string(creator)
                    elif not isinstance(creator, Pubkey):
                        creator = None
                except Exception as e:
                    # Bonding curve invalid = migrated
                    is_migrated = True
                    logger.info(f"[TRENDING] {token.symbol} bonding curve unavailable - using Jupiter")
            else:
                # No bonding curve = migrated or unknown platform
                is_migrated = True

            # If migrated - buy via PumpSwap (Raydium AMM)
            if is_migrated:

                logger.info(f"[TRENDING] {token.symbol} is migrated, attempting PumpSwap buy...")
                logger.info(f"[TRENDING] DexScreener info: dex_id={token.dex_id}, pair_address={token.pair_address}")

                fallback = self._fallback_seller
                # Use pair_address from DexScreener if available
                # PumpSwap pools can show as "pumpswap", "raydium", or other dex_id
                market_address = None
                if token.pair_address:
                    try:
                        market_address = Pubkey.from_string(token.pair_address)
                        logger.info(f"[TRENDING] Using DexScreener pair as market: {token.pair_address}")
                    except Exception as e:
                        logger.warning(f"[TRENDING] Invalid pair_address: {e}")

                if not market_address:
                    logger.info("[TRENDING] No pair_address, will lookup PumpSwap market via RPC")

                # Position config for callback
                position_config = {
                    "take_profit_pct": self.take_profit_percentage,
                    "stop_loss_pct": self.stop_loss_percentage,
                    "tsl_enabled": self.tsl_enabled,
                    "tsl_activation_pct": self.tsl_activation_pct,
                    "tsl_trail_pct": self.tsl_trail_pct,
                    "tsl_sell_pct": self.tsl_sell_pct,
                                    "tp_sell_pct": self.tp_sell_pct,
                    "max_hold_time": self.max_hold_time,
                    "bot_name": "trending_scanner",
                    "dca_enabled": self.dca_enabled,
                }
                
                success, sig, error, token_amount, price = await fallback.buy_via_pumpswap(
                    mint=mint,
                    sol_amount=self.buy_amount,
                    symbol=token.symbol,
                    market_address=market_address,
                    position_config=position_config,
                )

                if success:
                    logger.warning(f"[OK] TRENDING PumpSwap BUY: {token.symbol} - {sig}")
                    logger.info(f"[OK] Got {token_amount:,.2f} tokens at price {price:.10f} SOL")
                    # MOVED TO TX_CALLBACK - position/history added after TX verification
                    # position = Position(
                    #     mint=mint,
                    #     symbol=token.symbol,
                    #     entry_price=price,
                    #     quantity=token_amount,
                    #     entry_time=datetime.utcnow(),
                    #     platform=self.platform.value,
                    # )
                    # self.active_positions.append(position)
                    # save_positions(self.active_positions)
                    # watch_token(mint_str)
                    # self._bought_tokens.add(mint_str)
                    # add_to_purchase_history(...)
                    logger.info(f"[TRENDING] TX sent, position will be created by callback")
                else:
                    logger.error(f"[FAIL] TRENDING PumpSwap BUY failed: {token.symbol} - {error or 'Unknown error'}")
                return

            # Not migrated - use normal flow with platform-specific addresses
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

            # Get platform-specific program for creator_vault
            program_address = None
            if target_platform == Platform.PUMP_FUN:
                from platforms.pumpfun.address_provider import PumpFunAddresses
                program_address = PumpFunAddresses.PROGRAM
            elif target_platform == Platform.LETS_BONK:
                from platforms.letsbonk.address_provider import LetsBonkAddresses
                program_address = LetsBonkAddresses.PROGRAM
            elif target_platform == Platform.BAGS:
                from platforms.bags.address_provider import BagsAddresses
                program_address = BagsAddresses.DBC_PROGRAM

            associated_bonding_curve, _ = Pubkey.find_program_address(
                [bytes(bonding_curve), bytes(token_program_id), bytes(mint)],
                SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
            )

            creator_vault = None
            if creator and program_address:
                creator_vault, _ = Pubkey.find_program_address(
                    [b"creator-vault", bytes(creator)],
                    program_address
                )

            token_info = TokenInfo(
                name=token.name,
                symbol=token.symbol,
                uri="",
                mint=mint,
                platform=target_platform,  # Use detected/target platform
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=None,
                creator=creator,
                creator_vault=creator_vault,
                pool_state=pool_state,
                base_vault=None,
                quote_vault=None,
                token_program_id=token_program_id,
                creation_timestamp=int(token.created_at.timestamp()) if token.created_at else 0,
            )

            # Покупаем! skip_checks=False - пусть проходит dev check и другие проверки
            # Scoring уже проверен выше, но _handle_token пропустит повторную проверку
            buy_success = await self._handle_token(token_info, skip_checks=False)
            # MOVED TO TX_CALLBACK - position/history added after TX verification
            # if buy_success:
            #     self._bought_tokens.add(mint_str)
            #     add_to_purchase_history(
            #         mint=mint_str,
            #         symbol=token.symbol,
            #         bot_name="trending_scanner",
            #         platform=token.dex_id or "unknown",
            #         price=0,
            #         amount=0,
            #     )
            if buy_success:
                logger.info(f"[PUMP] TX sent, position will be created by callback")

        except Exception as e:
            logger.exception(f"Failed to buy trending token: {e}")
        finally:
            # Remove from "buying" set (either succeeded or failed)
            self._buying_tokens.discard(mint_str)

    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        logger.info(f"Starting Universal Trader for {self.platform.value}")
        logger.info(
            f"Match filter: {self.match_string if self.match_string else 'None'}"
        )
        logger.info(
            f"Creator filter: {self.bro_address if self.bro_address else 'None'}"
        )
        logger.info(f"Marry mode: {self.marry_mode}")
        logger.info(f"YOLO mode: {self.yolo_mode}")
        logger.info(f"Exit strategy: {self.exit_strategy}")
        logger.info(
            f"Pattern detection: {'enabled' if self.enable_pattern_detection else 'disabled'}"
        )
        if self.enable_pattern_detection:
            logger.info(f"Pattern only mode: {self.pattern_only_mode}")

        # Log scoring and whale copy status
        logger.info(
            f"Token scoring: {'enabled' if self.enable_scoring else 'disabled'}"
        )
        logger.info(
            f"Whale copy trading: {'enabled' if self.enable_whale_copy else 'disabled'}"
        )
        logger.info(
            f"Trending scanner: {'enabled' if self.enable_trending_scanner else 'disabled'}"
        )

        if self.exit_strategy == "tp_sl":
            logger.info(
                f"Take profit: {self.take_profit_percentage * 100 if self.take_profit_percentage else 'None'}%"
            )
            logger.info(
                f"Stop loss: {self.stop_loss_percentage * 100 if self.stop_loss_percentage else 'None'}%"
            )
            logger.info(
                f"Max hold time: {self.max_hold_time if self.max_hold_time else 'None'} seconds"
            )

        logger.info(f"Max token age: {self.max_token_age} seconds")

        # Restore saved positions from previous run

        # Initialize batch price service (ONE request for ALL prices)
        logger.warning("[BATCH] Initializing batch price service...")
        await init_batch_price_service()

        # Session 9: Start balance cache (eliminates 271ms RPC from buy critical path)
        await self._start_balance_cache()

        await self._restore_positions()

        try:
            health_resp = await self.solana_client.get_health()
            logger.info(f"RPC warm-up successful (getHealth passed: {health_resp})")
        except Exception as e:
            logger.warning(f"RPC warm-up failed: {e!s}")

        try:
            # Start whale tracker BEFORE choosing operating mode
            # Whale tracker should run in ALL modes if enabled
            whale_task = None
            whale_task_secondary = None
            if self.whale_tracker:
                logger.warning("[WHALE] Starting whale tracker (primary) in background...")
                whale_task = asyncio.create_task(self.whale_tracker.start())
            if self.whale_tracker_secondary:
                logger.warning("[WHALE] Starting secondary tracker (webhook) in background...")
                whale_task_secondary = asyncio.create_task(self.whale_tracker_secondary.start())

            # Phase 5.3: Start watchdog for dual-channel health monitoring
            watchdog_task = None
            if self._watchdog:
                watchdog_task = asyncio.create_task(self._watchdog.run())
                logger.warning("[WATCHDOG] Background task STARTED")

            # === BlockhashCache: init HTTP mode, then upgrade to gRPC ===
            try:
                from core.blockhash_cache import init_blockhash_cache
                _bh_cache = await init_blockhash_cache(self.rpc_endpoint)
                logger.warning(f"[BlockhashCache] Started (HTTP mode, interval={_bh_cache.HTTP_POLL_INTERVAL}s)")
                
                # Schedule gRPC upgrade after whale_tracker channel is ready
                async def _upgrade_blockhash_to_grpc():
                    """Wait for gRPC channel, then switch blockhash to gRPC mode."""
                    await asyncio.sleep(5)  # Give whale_tracker time to connect
                    for attempt in range(10):
                        try:
                            if (self.whale_tracker 
                                and hasattr(self.whale_tracker, '_grpc_instances')
                                and self.whale_tracker._grpc_instances):
                                # Prefer Chainstack (index 0) — 13ms vs PublicNode 382ms
                                for inst in self.whale_tracker._grpc_instances:
                                    if inst.channel and inst.name == "chainstack":
                                        from geyser.generated import geyser_pb2_grpc
                                        stub = geyser_pb2_grpc.GeyserStub(inst.channel)
                                        # Test it works
                                        from geyser.generated import geyser_pb2
                                        req = geyser_pb2.GetLatestBlockhashRequest()
                                        resp = await asyncio.wait_for(
                                            stub.GetLatestBlockhash(req), timeout=5.0
                                        )
                                        _bh_cache.enable_grpc(stub)
                                        logger.warning(
                                            f"[BlockhashCache] Upgraded to gRPC ({inst.name}) "
                                            f"slot={resp.slot}"
                                        )
                                        return
                        except Exception as e:
                            logger.info(f"[BlockhashCache] gRPC upgrade attempt {attempt+1}: {e}")
                        await asyncio.sleep(3)
                    logger.warning("[BlockhashCache] gRPC upgrade failed, staying on HTTP")
                
                asyncio.create_task(_upgrade_blockhash_to_grpc())
            except Exception as e:
                logger.error(f"[BlockhashCache] Init failed: {e}")

            # Phase 4: Start PriceStream for real-time price monitoring
            price_stream_task = None
            # Phase 4b: PriceStream disabled - vault tracking moved to whale_geyser
            # if self._price_stream:
            #     price_stream_task = asyncio.create_task(self._price_stream.start())
            #     logger.warning("[PRICE_STREAM] Background task STARTED")
            logger.info("[PRICE_STREAM] Disabled - using whale_geyser vault tracking instead")
            if not self.whale_tracker and not self.whale_tracker_secondary:
                if self.enable_whale_copy:
                    logger.error("[WHALE] Whale copy enabled but no tracker initialized!")
                else:
                    logger.info("Whale tracker not enabled, skipping...")


            # S38: Start moonbag gRPC monitor (PublicNode) for moonbag/dust price tracking
            try:
                from monitoring.moonbag_grpc_monitor import get_moonbag_monitor
                self._moonbag_monitor = get_moonbag_monitor()
                await self._moonbag_monitor.start()
                # Subscribe all moonbag/dust positions with known vaults
                for _pos in self.active_positions:
                    if (getattr(_pos, 'is_moonbag', False) or getattr(_pos, 'is_dust', False)):
                        _bv = getattr(_pos, 'pool_base_vault', None)
                        _qv = getattr(_pos, 'pool_quote_vault', None)
                        if _bv and _qv:
                            self._moonbag_monitor.subscribe(
                                str(_pos.mint), _bv, _qv,
                                decimals=6, symbol=_pos.symbol
                            )
                        else:
                            # Try resolve vaults
                            try:
                                from trading.vault_resolver import resolve_vaults
                                _vaults = await resolve_vaults(str(_pos.mint))
                                if _vaults:
                                    _pos.pool_base_vault = _vaults[0]
                                    _pos.pool_quote_vault = _vaults[1]
                                    _pos.pool_address = _vaults[2]
                                    self._moonbag_monitor.subscribe(
                                        str(_pos.mint), _vaults[0], _vaults[1],
                                        decimals=6, symbol=_pos.symbol
                                    )
                                    logger.warning(f"[MOONBAG-GRPC] {_pos.symbol}: vaults resolved and subscribed")
                                else:
                                    logger.info(f"[MOONBAG-GRPC] {_pos.symbol}: no vaults found, using batch price only")
                            except Exception as _ve:
                                logger.info(f"[MOONBAG-GRPC] {_pos.symbol}: vault resolve failed: {_ve}")
                logger.warning(f"[MOONBAG-GRPC] Initialized: {self._moonbag_monitor.subscription_count} moonbag subscriptions")
            except Exception as _mge:
                logger.warning(f"[MOONBAG-GRPC] Init failed (non-critical): {_mge}")
                self._moonbag_monitor = None

            # Choose operating mode based on yolo_mode
            if not self.yolo_mode:
                # Single token mode: process one token and exit
                logger.info(
                    "Running in single token mode - will process one token and exit"
                )
                token_info = await self._wait_for_token()
                if token_info:
                    buy_success = await self._handle_token(token_info)
                    logger.info("Finished processing single token. Exiting...")
                else:
                    logger.info(
                        f"No suitable token found within timeout period ({self.token_wait_timeout}s). Exiting..."
                    )
                # Cleanup whale tracker in single token mode
                if whale_task:
                    whale_task.cancel()
                    if self.whale_tracker:
                        await self.whale_tracker.stop()
                    if whale_task_secondary:
                        whale_task_secondary.cancel()
                        if self.whale_tracker_secondary:
                            await self.whale_tracker_secondary.stop()
                    # Phase 5.3: Stop watchdog
                    if watchdog_task:
                        watchdog_task.cancel()
                        if self._watchdog:
                            self._watchdog.stop()
            else:
                # Continuous mode: process tokens until interrupted
                logger.info(
                    "Running in continuous mode - will process tokens until interrupted"
                )
                processor_task = asyncio.create_task(self._process_token_queue())

                # Start trending scanner if enabled
                trending_task = None
                if self.trending_scanner:
                    logger.info("Starting trending scanner in background...")
                    trending_task = asyncio.create_task(self.trending_scanner.start())

                # Start volume pattern analyzer if enabled
                if self.volume_pattern_analyzer:
                    logger.info("Starting volume pattern analyzer in background...")
                    volume_task = asyncio.create_task(self.volume_pattern_analyzer.start())

                # Start periodic wallet balance sync (phantom cleanup every 5 min)
                from trading.periodic_sync import start_periodic_sync
                start_periodic_sync()


                try:
                    if self.sniper_enabled:
                        # Normal sniper mode - listen for new tokens
                        await self.token_listener.listen_for_tokens(
                            self._queue_token,
                            self.match_string,
                            self.bro_address,
                        )
                    else:
                        # Non-sniper mode (whale-copy, volume-sniper)
                        # Don't listen for new tokens, just keep running for whale/trending/volume
                        logger.warning("[MODE] Sniper DISABLED - running in whale/trending/volume only mode")
                        # Keep running forever until interrupted (with balance check)
                        _low_bal_logged = False
                        while True:
                            if self._critical_low_balance:
                                if not _low_bal_logged:
                                    logger.warning("⛔ LOW BALANCE — new buys paused. Monitoring/selling continues. Top up wallet to resume.")
                                    _low_bal_logged = True
                                # Session 9: balance cache auto-resumes in _refresh_balance_cache()
                                # Just check if it already recovered
                                if not self._critical_low_balance:
                                    _low_bal_logged = False
                            await asyncio.sleep(60)
                except Exception:
                    logger.exception("Token listening stopped due to error")
                finally:
                    processor_task.cancel()
                    if whale_task:
                        whale_task.cancel()
                        if self.whale_tracker:
                            await self.whale_tracker.stop()
                    if whale_task_secondary:
                        whale_task_secondary.cancel()
                        if self.whale_tracker_secondary:
                            await self.whale_tracker_secondary.stop()
                    # Phase 5.3: Stop watchdog
                    if watchdog_task:
                        watchdog_task.cancel()
                        if self._watchdog:
                            self._watchdog.stop()
                    if trending_task:
                        trending_task.cancel()
                        if self.trending_scanner:
                            await self.trending_scanner.stop()

                        if self.volume_pattern_analyzer:
                            await self.volume_pattern_analyzer.stop()

                    try:
                        await processor_task
                    except asyncio.CancelledError:
                        pass

        except Exception:
            logger.exception("Trading stopped due to error")

        finally:
            await self._cleanup_resources()
            logger.info("Universal Trader has shut down")

    async def _wait_for_token(self) -> TokenInfo | None:
        """Wait for a single token to be detected."""
        # Create a one-time event to signal when a token is found
        token_found = asyncio.Event()
        found_token = None

        async def token_callback(token: TokenInfo) -> None:
            nonlocal found_token
            token_key = str(token.mint)

            # Only process if not already processed and fresh
            if token_key not in self.processed_tokens:
                # Record when the token was discovered
                self.token_timestamps[token_key] = monotonic()
                found_token = token
                self.processed_tokens.add(token_key)
                token_found.set()

        listener_task = asyncio.create_task(
            self.token_listener.listen_for_tokens(
                token_callback,
                self.match_string,
                self.bro_address,
            )
        )

        # Wait for a token with a timeout
        try:
            logger.info(
                f"Waiting for a suitable token (timeout: {self.token_wait_timeout}s)..."
            )
            await asyncio.wait_for(token_found.wait(), timeout=self.token_wait_timeout)
            logger.info(f"Found token: {found_token.symbol} ({found_token.mint})")
            return found_token
        except TimeoutError:
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a token"
            )
            return None
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_resources(self) -> None:
        """Perform cleanup operations before shutting down."""
        if self.traded_mints:
            try:
                logger.info(f"Cleaning up {len(self.traded_mints)} traded token(s)...")
                # Build parallel lists of mints and token_program_ids
                mints_list = list(self.traded_mints)
                token_program_ids = [
                    self.traded_token_programs.get(str(mint)) for mint in mints_list
                ]
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    mints_list,
                    token_program_ids,
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
            except Exception:
                logger.exception("Error during cleanup")

        old_keys = {k for k in self.token_timestamps if k not in self.processed_tokens}
        for key in old_keys:
            self.token_timestamps.pop(key, None)

        await self.solana_client.close()

    async def _queue_token(self, token_info: TokenInfo) -> None:
        """Queue a token for processing if not already processed.

        ANTI-DUPLICATE: Also checks _bought_tokens and _buying_tokens
        to prevent queueing tokens that are already being bought via
        whale copy or trending scanner.
        """
        token_key = str(token_info.mint)

        # Check all anti-duplicate sets
        if token_key in self.processed_tokens:
            logger.debug(f"Token {token_info.symbol} already processed. Skipping...")
            return

        if token_key in self._bought_tokens or token_key in self._buying_tokens:
            logger.debug(f"Token {token_info.symbol} already bought/buying. Skipping queue...")
            return

        # Record timestamp when token was discovered
        self.token_timestamps[token_key] = monotonic()

        await self.token_queue.put(token_info)
        logger.info(
            f"Queued new token: {token_info.symbol} ({token_info.mint}) on {token_info.platform.value}"
        )

    async def _process_token_queue(self) -> None:
        """Continuously process tokens from the queue, only if they're fresh."""
        while True:
            # ============================================
            # CRITICAL BALANCE CHECK - STOP BOT
            # ============================================
            if self._critical_low_balance:
                # Don't break — just skip buying, keep processing queue
                await asyncio.sleep(5)
                continue

            token_info = None
            try:
                token_info = await self.token_queue.get()
                token_key = str(token_info.mint)

                # ============================================
                # ANTI-DUPLICATE CHECK (CRITICAL!)
                # ============================================
                # FIRST: Check positions.json (CROSS-BOT duplicate prevention!)
                if is_token_in_positions(token_key):
                    logger.info(
                        f"[SKIP] {token_info.symbol} - already in positions.json (another bot bought)"
                    )
                    self.token_queue.task_done()
                    continue

                # SECOND: Check in-memory sets (same-bot duplicate prevention)
                if token_key in self._bought_tokens or token_key in self._buying_tokens:
                    logger.info(
                        f"Skipping token {token_info.symbol} - already bought/buying via another path"
                    )
                    self.token_queue.task_done()
                    continue

                # Check if token is still "fresh"
                current_time = monotonic()
                token_age = current_time - self.token_timestamps.get(
                    token_key, current_time
                )

                # max_token_age=0 означает "без ограничения"
                if self.max_token_age > 0 and token_age > self.max_token_age:
                    logger.info(
                        f"Skipping token {token_info.symbol} - too old ({token_age:.1f}s > {self.max_token_age}s)"
                    )
                    self.token_queue.task_done()
                    continue

                # Use lock to mark as buying (prevents race with whale/trending)
                async with self._buy_lock:
                    # Re-check after lock
                    if token_key in self._bought_tokens or token_key in self._buying_tokens:
                        logger.info(
                            f"Skipping token {token_info.symbol} - already bought/buying (after lock)"
                        )
                        self.token_queue.task_done()
                        continue

                    # Mark as BUYING before releasing lock
                    self._buying_tokens.add(token_key)

                self.processed_tokens.add(token_key)

                logger.info(
                    f"Processing fresh token: {token_info.symbol} (age: {token_age:.1f}s)"
                )

                try:
                    buy_success = await self._handle_token(token_info)
                    # MOVED TO TX_CALLBACK - position/history added after TX verification
                    # if buy_success:
                    #     self._bought_tokens.add(token_key)
                    #     add_to_purchase_history(
                    #         mint=token_key,
                    #         symbol=token_info.symbol,
                    #         bot_name="sniper",
                    #         platform=self.platform.value,
                    #         price=0,
                    #         amount=0,
                    #     )
                    if buy_success:
                        logger.info(f"[SNIPER] TX sent, position will be created by callback")
                finally:
                    # Remove from "buying" set (either succeeded or failed)
                    self._buying_tokens.discard(token_key)

                self.token_queue.task_done()

            except asyncio.CancelledError:
                logger.info("Token queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in token queue processor")
                if token_info is not None:
                    self.token_queue.task_done()

    async def _handle_token(self, token_info: TokenInfo, skip_checks: bool = False) -> bool:
        """Handle a new token creation event.

        Args:
            token_info: Token information
            skip_checks: If True, skip scoring and dev checks (used for whale copy trades)
        """
        try:
            # ============================================
            # START TRACE CONTEXT - EARLIEST POSSIBLE!
            # ============================================
            mint_str = str(token_info.mint)
            trace = TraceContext.start(
                trade_type='buy',
                mint=mint_str,
                source='listener',
                slot_detected=getattr(token_info, 'creation_slot', None)
            )
            logger.info(f"[TRACE] Started trace_id={trace.trace_id} for {token_info.symbol}")
            
            # ============================================
            # CROSS-BOT DUPLICATE CHECK - EARLIEST POSSIBLE!
            # ============================================
            if is_token_in_positions(mint_str):
                logger.info(f"[SKIP] {token_info.symbol} - already in positions.json (cross-bot check)")
                return False

            # ============================================
            # TOKEN VETTING (Security Check)
            # ============================================
            if self.token_vetter and not skip_checks:
                is_bonding_curve = hasattr(token_info, 'bonding_curve') or self.platform.value in ['pump_fun', 'lets_bonk', 'bags']
                vet_report = await self.token_vetter.vet_token(
                    mint_address=mint_str,
                    symbol=token_info.symbol,
                    is_bonding_curve=is_bonding_curve,
                )
                if not self.token_vetter.should_buy(vet_report):
                    logger.warning(
                        f"[VET] ⛔ BLOCKED: {token_info.symbol} - {vet_report.reason}"
                    )
                    return False

            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return False

            mint_str = str(token_info.mint)

            # Start pattern tracking if enabled
            if self.pattern_detector:
                self.pattern_detector.start_tracking(mint_str, token_info.symbol)

            # Check pattern_only_mode - wait for API data before checking patterns
            if not skip_checks and self.pattern_only_mode:
                # Wait for pattern detector to fetch data (up to 5 seconds)
                # Uses DexScreener as fallback when Birdeye fails
                logger.info(f"Pattern mode: waiting for market data for {token_info.symbol}...")

                signal_detected = False
                for i in range(10):  # 10 x 0.5s = 5 seconds max
                    await asyncio.sleep(0.5)
                    if self._has_pump_signal(mint_str):
                        logger.warning(f"[SIGNAL] PUMP SIGNAL detected for {token_info.symbol}!")
                        signal_detected = True
                        break

                    # Check if we have STRONG activity data even without full signal
                    # ЖЁСТКИЕ ПОРОГИ: buys_5m >= 500 ИЛИ buys_1h >= 1000
                    if self.pattern_detector and mint_str in self.pattern_detector.tokens:
                        metrics = self.pattern_detector.tokens[mint_str]
                        total_trades = metrics.buys_5m + metrics.sells_5m
                        buy_ratio = metrics.buys_5m / total_trades if total_trades > 0 else 0

                        # Требуем СИЛЬНУЮ активность И высокий buy pressure
                        if (metrics.buys_5m >= 500 or metrics.buys_1h >= 1000) and buy_ratio >= 0.75:
                            logger.warning(
                                f"[ACTIVITY] STRONG activity for {token_info.symbol}: "
                                f"buys_5m={metrics.buys_5m}, buys_1h={metrics.buys_1h}, "
                                f"buy_ratio={buy_ratio:.1%}"
                            )
                            signal_detected = True
                            break

                # Check if signal arrived
                if not signal_detected:
                    # Final check - maybe we have some activity data
                    if self.pattern_detector and mint_str in self.pattern_detector.tokens:
                        metrics = self.pattern_detector.tokens[mint_str]
                        if metrics.buys_5m > 0 or metrics.buys_1h > 0:
                            logger.info(
                                f"Pattern mode: {token_info.symbol} has weak activity "
                                f"(buys_5m={metrics.buys_5m}, buys_1h={metrics.buys_1h}) "
                                f"- storing for later signal"
                            )
                    else:
                        logger.info(
                            f"Pattern only mode: skipping {token_info.symbol} - no pump signal detected"
                        )
                    # Store token_info for later if signal arrives
                    self.pending_tokens[mint_str] = token_info
                    return False

            # Token scoring check (runs in parallel with wait time) - skip if whale copy
            scoring_task = None
            if self.token_scorer and not skip_checks:
                scoring_task = asyncio.create_task(
                    self.token_scorer.should_buy(mint_str, token_info.symbol, is_sniper_mode=True)
                )

            # Dev reputation check (runs in parallel) - skip if whale copy
            dev_check_task = None
            if self.dev_checker and token_info.creator and not skip_checks:
                dev_check_task = asyncio.create_task(
                    self.dev_checker.check_dev(str(token_info.creator))
                )

            # Wait for pool/curve to stabilize (unless in extreme fast mode or whale copy)
            if not self.extreme_fast_mode and not skip_checks:
                await self._save_token_info(token_info)
                logger.info(
                    f"Waiting for {self.wait_time_after_creation} seconds for the pool/curve to stabilize..."
                )
                await asyncio.sleep(self.wait_time_after_creation)
            elif skip_checks:
                # Whale copy - покупаем СРАЗУ без ожидания
                logger.info(f"[WHALE] WHALE COPY: Buying {self.buy_amount:.6f} SOL worth of {token_info.symbol} (checks skipped)...")

            # Check scoring result if enabled
            if scoring_task:
                try:
                    should_buy, score = await scoring_task
                    logger.info(
                        f"[SCORE] Token score for {token_info.symbol}: {score.total_score}/100 -> {score.recommendation}"
                    )
                    if not should_buy:
                        logger.info(
                            f"Skipping {token_info.symbol} - score {score.total_score} below threshold, waiting for patterns"
                        )
                        # Store for pattern detection - if patterns appear later, _on_pump_signal will buy
                        logger.warning(f"[DEBUG] SAVING to pending_tokens: {mint_str} ({token_info.symbol}), total: {len(self.pending_tokens)+1}")
                        self.pending_tokens[mint_str] = token_info
                        # Save timestamp for cleanup
                        from time import monotonic
                        self.token_timestamps[mint_str] = monotonic()
                        return False
                except Exception as e:
                    # CRITICAL: Если scoring упал - НЕ покупаем! Безопасность важнее скорости
                    logger.warning(f"[SKIP] Scoring failed for {token_info.symbol}: {e} - NOT buying!")
                    return False

            # Check dev reputation result if enabled
            if dev_check_task:
                try:
                    dev_result = await dev_check_task
                    logger.info(
                        f"👤 Dev check for {token_info.symbol}: "
                        f"tokens={dev_result.get('tokens_created', '?')}, "
                        f"risk={dev_result.get('risk_score', '?')}, "
                        f"safe={dev_result.get('is_safe', True)}"
                    )
                    if not dev_result.get("is_safe", True):
                        logger.warning(
                            f"[WARN] Skipping {token_info.symbol} - {dev_result.get('reason', 'bad dev')}"
                        )
                        return False
                except Exception as e:
                    logger.warning(f"Dev check failed, proceeding anyway: {e}")

            # Check wallet balance before buying
            balance_ok = await self._check_balance_before_buy()
            if not balance_ok:
                return False

            # Buy token
            if skip_checks:
                logger.warning(
                    f"[WHALE] WHALE COPY: Executing buy for {token_info.symbol}..."
                )
            else:
                logger.info(
                    f"Buying {self.buy_amount:.6f} SOL worth of {token_info.symbol} on {token_info.platform.value}..."
                )

            try:
                logger.info(f"[DEBUG] token_info.bonding_curve = {getattr(token_info, 'bonding_curve', None)}")
                logger.info(f"[BUY] Calling buyer.execute for {token_info.symbol}...")
                
                # === TRACE: Mark build complete ===
                if trace:
                    trace.mark_build_complete(symbol=token_info.symbol, platform=token_info.platform.value)
                
                buy_result: TradeResult = await self.buyer.execute(token_info)
                
                # === TRACE: Mark sent ===
                if trace and buy_result.tx_signature:
                    trace.mark_sent(provider='platform', signature=buy_result.tx_signature)
                logger.info(
                    f"Buy result: success={buy_result.success}, "
                    f"tx_signature={buy_result.tx_signature}, "
                    f"error_message={buy_result.error_message}"
                )
            except Exception as e:
                logger.exception(f"[FAIL] Buy execution failed with exception: {e}")
                return False

            if buy_result.success:
                logger.warning(f"[OK] BUY SUCCESS: {token_info.symbol} - {buy_result.tx_signature}")
                
                # === TRACE: Mark finalized success ===
                if trace:
                    trace.mark_finalized(success=True, slot_landed=None)
                    from analytics.trace_recorder import record_trace
                    await record_trace(trace)
                    trace.finish()
                
                await self._handle_successful_buy(token_info, buy_result)
                return True
            else:
                logger.error(f"[FAIL] BUY FAILED: {token_info.symbol} - {buy_result.error_message or 'Unknown error'}")
                
                # === TRACE: Mark finalized fail ===
                if trace:
                    trace.mark_finalized(success=False, fail_reason=buy_result.error_message or 'Unknown')
                    from analytics.trace_recorder import record_trace
                    await record_trace(trace)
                    trace.finish()
                
                await self._handle_failed_buy(token_info, buy_result)
                return False

            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")
            return False

    # === SESSION 9: BALANCE CACHE — eliminates 271ms RPC from critical path ===

    async def _start_balance_cache(self):
        """Initialize balance cache: fetch once + start background loop."""
        await self._refresh_balance_cache()
        self._balance_cache_task = asyncio.create_task(self._balance_cache_loop())
        logger.warning(
            f"[BAL_CACHE] Started: {self._cached_sol_balance:.4f} SOL, "
            f"refresh every 30s"
        )

    async def _balance_cache_loop(self):
        """Background loop: refresh SOL balance every 30 seconds."""
        while True:
            try:
                await asyncio.sleep(30)
                await self._refresh_balance_cache()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[BAL_CACHE] Loop error: {e}")
                await asyncio.sleep(10)

    async def _refresh_balance_cache(self):
        """Fetch SOL balance from RPC and update cache.
        Uses Chainstack (fastest paid RPC) with fallback to default client.
        """
        try:
            import aiohttp
            chainstack_url = os.getenv("CHAINSTACK_RPC_ENDPOINT")
            balance_sol = None

            # Try Chainstack first (fastest, ~20ms)
            if chainstack_url:
                try:
                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getBalance",
                        "params": [str(self.wallet.pubkey), {"commitment": "confirmed"}]
                    }
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            chainstack_url, json=payload,
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if "result" in data and "value" in data["result"]:
                                    balance_sol = data["result"]["value"] / 1_000_000_000
                except Exception as e:
                    logger.debug(f"[BAL_CACHE] Chainstack failed: {e}")

            # Fallback to default client
            if balance_sol is None:
                client = await self.solana_client.get_client()
                balance_resp = await client.get_balance(self.wallet.pubkey)
                balance_sol = balance_resp.value / 1_000_000_000
            self._cached_sol_balance = balance_sol
            self._balance_cache_time = monotonic()
            logger.debug(f"[BAL_CACHE] Refreshed: {balance_sol:.4f} SOL")

            # Auto-resume if balance recovered
            if balance_sol >= self.min_sol_balance and self._critical_low_balance:
                self._critical_low_balance = False
                logger.warning(f"[BAL_CACHE] Balance recovered to {balance_sol:.4f} SOL — buys RESUMED")
        except Exception as e:
            logger.warning(f"[BAL_CACHE] Refresh failed: {e}")

    async def _update_balance_after_trade(self):
        """Called after buy/sell to refresh cache immediately (in background)."""
        try:
            await self._refresh_balance_cache()
        except Exception as e:
            logger.debug(f"[BAL_CACHE] Post-trade refresh failed: {e}")

    async def _check_balance_before_buy(self) -> bool:
        """Check cached SOL balance (0ms instead of 271ms RPC).

        Returns:
            True if balance >= min_sol_balance, False otherwise.

        NOTE: Uses cached balance from _balance_cache_loop (refreshed every 30s).
        Falls back to RPC only if cache is older than 60s.
        """
        cache_age = monotonic() - self._balance_cache_time

        # If cache is fresh enough, use it (0ms!)
        if cache_age < self._balance_cache_max_age and self._balance_cache_time > 0:
            balance_sol = self._cached_sol_balance
        else:
            # Cache too old or never set — do RPC (rare, only on startup race)
            try:
                client = await self.solana_client.get_client()
                balance_resp = await client.get_balance(self.wallet.pubkey)
                balance_sol = balance_resp.value / 1_000_000_000
                self._cached_sol_balance = balance_sol
                self._balance_cache_time = monotonic()
                logger.info(f"[BAL_CACHE] Fallback RPC refresh: {balance_sol:.4f} SOL (cache was {cache_age:.0f}s old)")
            except Exception as e:
                logger.warning(f"Failed to check balance: {e} - proceeding anyway")
                return True

        if balance_sol < self.min_sol_balance:
            logger.warning("=" * 70)
            logger.warning(f"⛔ LOW BALANCE: {balance_sol:.4f} SOL < {self.min_sol_balance} SOL minimum")
            logger.warning("⛔ STOPPING NEW BUYS - but monitoring/selling continues!")
            logger.warning("⛔ Top up wallet to resume buying.")
            logger.warning("=" * 70)
            self._critical_low_balance = True
            return False

        logger.debug(f"Balance OK (cached): {balance_sol:.4f} SOL")
        return True

    async def _handle_successful_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle successful token purchase."""
        logger.info(
            f"Successfully bought {token_info.symbol} on {token_info.platform.value}"
        )
        self._log_trade(
            "buy",
            token_info,
            buy_result.price,
            buy_result.amount,
            buy_result.tx_signature,
        )
        self.traded_mints.add(token_info.mint)
        # Session 9: Refresh balance cache after buy (background, non-blocking)
        asyncio.create_task(self._update_balance_after_trade())
        # Track token program for cleanup
        mint_str = str(token_info.mint)
        if token_info.token_program_id:
            self.traded_token_programs[mint_str] = token_info.token_program_id

# DISABLED:         # ===== CRITICAL: Save position IMMEDIATELY =====
# DISABLED:         logger.warning(f"[SAVE] Saving position for {token_info.symbol}")
# DISABLED:         self.active_positions.append(Position(
# DISABLED:             mint=token_info.mint,
# DISABLED:             symbol=token_info.symbol,
# DISABLED:             entry_price=buy_result.price,
# DISABLED:             quantity=buy_result.amount,
# DISABLED:             entry_time=datetime.utcnow(),
# DISABLED:             platform=self.platform.value,
# DISABLED:         ))
# DISABLED:         save_positions(self.active_positions)
# DISABLED:         logger.warning(f"[SAVED] Position saved to file + Redis")

        # Choose exit strategy
        if not self.marry_mode:
            if self.exit_strategy == "tp_sl":
                await self._handle_tp_sl_exit(token_info, buy_result)
            elif self.exit_strategy == "time_based":
                await self._handle_time_based_exit(token_info, buy_result)
            elif self.exit_strategy == "manual":
                logger.info("Manual exit strategy - position will remain open")
        else:
            logger.info("Marry mode enabled. Skipping sell operation.")

    async def _handle_failed_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle failed token purchase."""
        logger.error(f"Failed to buy {token_info.symbol}: {buy_result.error_message}")
        # Close ATA if enabled
        await handle_cleanup_after_failure(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )

    async def _handle_tp_sl_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle take profit/stop loss exit strategy."""
        # Create position with platform info for restoration
        bonding_curve_str = None
        if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
            bonding_curve_str = str(token_info.bonding_curve)
        elif hasattr(token_info, "pool_state") and token_info.pool_state:
            bonding_curve_str = str(token_info.pool_state)

        # DCA settings
        dca_enabled = self.dca_enabled
        
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=buy_result.price,
            quantity=buy_result.amount,
            take_profit_percentage=self.take_profit_percentage,
            stop_loss_percentage=self.stop_loss_percentage,
            max_hold_time=self.max_hold_time,
            platform=self.platform.value,
            bonding_curve=bonding_curve_str,
            # TSL parameters
            tsl_enabled=self.tsl_enabled,
            tsl_activation_pct=self.tsl_activation_pct,
            tsl_trail_pct=self.tsl_trail_pct,
            tsl_sell_pct=self.tsl_sell_pct,
            tp_sell_pct=self.tp_sell_pct,
        )
        
        # Always save original entry price (needed for moonbag SL calculation)
        position.original_entry_price = buy_result.price
        # Set DCA flags after creation
        if dca_enabled:
            position.dca_enabled = True
            position.dca_pending = True
            position.dca_trigger_pct = self.dca_trigger_pct if hasattr(self, 'dca_trigger_pct') else 0.25
            position.dca_first_buy_pct = self.dca_first_buy_pct if hasattr(self, 'dca_first_buy_pct') else 0.50
            position.original_entry_price = buy_result.price
            logger.warning(f"[DCA] Position created with DCA enabled, waiting for -20% dip")

        logger.info(f"Created position: {position}")
        if position.take_profit_price:
            logger.info(f"Take profit target: {position.take_profit_price:.8f} SOL")
        if position.stop_loss_price:
            logger.info(f"Stop loss target: {position.stop_loss_price:.8f} SOL")
        if position.tsl_enabled:
            activation_price = position.entry_price * (1 + position.tsl_activation_pct)
            logger.warning(f"[TSL] Trailing Stop enabled: activates at {activation_price:.8f} SOL (+{position.tsl_activation_pct*100:.0f}%)")

        # Save position to file for recovery after restart
        self._save_position(position)

        # === POST-BUY PRICE VALIDATION ===
        # Check if entry price is stale (common with whale copy on fast-moving tokens)
        try:
            await asyncio.sleep(0.5)  # Brief pause for price to propagate
            from utils.batch_price_service import get_cached_price
            _fresh = get_cached_price(str(token_info.mint))
            if not _fresh or _fresh <= 0:
                # Try Jupiter as fallback
                try:
                    _fresh = await self._fallback_seller.get_jupiter_price(token_info.mint)
                except Exception:
                    pass
            if _fresh and _fresh > 0 and position.entry_price > 0:
                _dev = (_fresh - position.entry_price) / position.entry_price
                logger.info(
                    f"[POST-BUY] {token_info.symbol}: entry={position.entry_price:.10f}, "
                    f"fresh={_fresh:.10f}, deviation={_dev*100:+.1f}%"
                )
                if _dev < -0.15:
                    # Price dropped >15% from entry — check source before overwriting
                    _ep_src = getattr(position, "entry_price_source", "unknown")
                    _hard_sources = {"pumpfun_curve", "onchain_verified"}
                    if _ep_src in _hard_sources:
                        logger.info(
                            f"[POST-BUY] {token_info.symbol}: SKIP stale correction — "
                            f"entry_price_source='{_ep_src}' is authoritative (curve/onchain), "
                            f"deviation={_dev*100:+.1f}% is market impact, not stale price"
                        )
                    else:
                        logger.warning(
                            f"[POST-BUY] {token_info.symbol}: STALE PRICE detected! "
                            f"entry={position.entry_price:.10f} vs fresh={_fresh:.10f} ({_dev*100:+.1f}%) "
                            f"source={_ep_src}"
                        )
                        # Adjust entry to fresh price (more realistic PnL tracking)
                        old_entry = position.entry_price
                        position.entry_price = _fresh
                        # Recalculate TP/SL from corrected entry
                        if self.take_profit_percentage and position.take_profit_price:
                            position.take_profit_price = _fresh * (1 + self.take_profit_percentage)
                        if self.stop_loss_percentage and position.stop_loss_price:
                            position.stop_loss_price = _fresh * (1 - self.stop_loss_percentage)
                        position.high_water_mark = _fresh
                        self._save_position(position)
                        logger.warning(
                            f"[POST-BUY] {token_info.symbol}: Entry corrected {old_entry:.10f} -> {_fresh:.10f}, "
                            f"new TP={position.take_profit_price}, new SL={position.stop_loss_price}"
                        )
                # Session 3: Recalculate _dev from (possibly corrected) entry_price
                if _fresh and _fresh > 0 and position.entry_price > 0:
                    _dev = (_fresh - position.entry_price) / position.entry_price
                if _dev < -0.30:
                    # Price dropped >30% — emergency sell immediately
                    logger.error(
                        f"[POST-BUY] {token_info.symbol}: CATASTROPHIC LOSS {_dev*100:.1f}%! "
                        f"Emergency sell before monitor starts."
                    )
                    try:
                        mint_str = str(token_info.mint)
                        success, sig, error = await self._fallback_seller._sell_via_jupiter(
                            token_info.mint, position.quantity, token_info.symbol
                        )
                        if success:
                            logger.warning(f"[POST-BUY] {token_info.symbol}: Emergency sell sent: {sig}")
                            position.close_position(_fresh, ExitReason.STOP_LOSS)
                            self._remove_position(mint_str)
                            try:
                                from trading.redis_state import forget_position_forever
                                await forget_position_forever(mint_str, reason="postbuy_emergency")
                            except Exception:
                                pass
                            return  # Don't start monitor
                        else:
                            logger.error(f"[POST-BUY] {token_info.symbol}: Emergency sell FAILED: {error}")
                    except Exception as e:
                        logger.error(f"[POST-BUY] {token_info.symbol}: Emergency sell exception: {e}")
        except Exception as e:
            logger.debug(f"[POST-BUY] {token_info.symbol}: Price validation failed: {e}")
        # === END POST-BUY PRICE VALIDATION ===

        # Monitor position in parallel (don't block)
        # FIX S26-4: Guard against duplicate monitors
        _mint_str_mon = str(token_info.mint)
        if register_monitor(_mint_str_mon):
            logger.warning(f"[MONITOR] Starting async monitor for {token_info.symbol}")
            asyncio.create_task(self._monitor_position_wrapper(token_info, position))
        else:
            logger.warning(f"[MONITOR] {token_info.symbol}: monitor already running, SKIPPING duplicate")

    async def _monitor_position_wrapper(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Wrapper for position monitor with error handling."""
        try:
            await self._monitor_position_until_exit(token_info, position)
        except Exception as e:
            logger.exception(
                f"[CRITICAL] Position monitor CRASHED for {token_info.symbol}! "
                f"Error: {e}. Attempting emergency sell..."
            )
            try:
                if str(token_info.mint) in NO_SL_MINTS:
                    logger.warning(f"[NO_SL] {token_info.symbol}: Position monitor crashed but NO_SL - NOT selling!")
                    return
                fallback_success = await self._emergency_fallback_sell(
                    token_info, position, position.entry_price
                )
                if fallback_success:
                    logger.warning(f"[RECOVERY] Emergency sell after crash SUCCESS for {token_info.symbol}")
                else:
                    logger.error(
                        f"[CRITICAL] Emergency sell after crash FAILED for {token_info.symbol}! "
                        f"MANUAL INTERVENTION REQUIRED! Mint: {token_info.mint}"
                    )
            except Exception as e2:
                logger.exception(
                    f"[CRITICAL] Emergency sell also crashed: {e2}. "
                    f"MANUAL SELL REQUIRED for {token_info.symbol}! Mint: {token_info.mint}"
                )

    async def _handle_time_based_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle legacy time-based exit strategy.

        Args:
            token_info: Token information
            buy_result: Result from the buy operation (contains token amount)
        """
        logger.info(f"Waiting for {self.wait_time_after_buy} seconds before selling...")
        await asyncio.sleep(self.wait_time_after_buy)

        logger.info(f"Selling {token_info.symbol}...")
        # Pass token amount and price from buy result to avoid RPC delays
        sell_result: TradeResult = await self.seller.execute(
            token_info, token_amount=buy_result.amount, token_price=buy_result.price
        )

        if sell_result.success:
            logger.info(f"Successfully sold {token_info.symbol}")
            self._log_trade(
                "sell",
                token_info,
                sell_result.price,
                sell_result.amount,
                sell_result.tx_signature,
            )
            # Close ATA if enabled
            await handle_cleanup_after_sell(
                self.solana_client,
                self.wallet,
                token_info.mint,
                token_info.token_program_id,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
                self.cleanup_force_close_with_burn,
            )
        else:
            logger.error(
                f"Failed to sell {token_info.symbol}: {sell_result.error_message}"
            )

    async def _monitor_position_until_exit(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Monitor a position until exit conditions are met.

        UNIFIED STOP-LOSS for all platforms (PUMP, BONK, BAGS):
        1. Get price from platform-specific curve_manager
        2. Check TP/SL conditions
        3. If price unavailable (migrated) - fallback to Jupiter/PumpSwap

        AGGRESSIVE STOP-LOSS:
        - Reduced MAX_PRICE_ERRORS from 5 to 2 for faster fallback
        - Emergency sell if price drops > 50% regardless of SL setting
        - Log every price check when loss > 20%

        If price cannot be fetched (e.g., token migrated), will attempt
        fallback sell via PumpSwap/Jupiter after MAX_PRICE_ERRORS consecutive failures.
        """
        logger.warning(
            f"[MONITOR] Starting position monitoring for {token_info.symbol} on {self.platform.value}"
        )
        tp_str = f"{position.take_profit_price:.10f}" if position.take_profit_price else "None"
        sl_str = f"{position.stop_loss_price:.10f}" if position.stop_loss_price else "None"
        logger.warning(
            f"[MONITOR] Entry: {position.entry_price:.10f} SOL, "
            f"TP: {tp_str} SOL, "
            f"SL: {sl_str} SOL"
        )
        logger.warning(f"[MONITOR] Check interval: {self.price_check_interval}s")

        # Get pool address for price monitoring using platform-agnostic method
        pool_address = self._get_pool_address(token_info)
        curve_manager = self.platform_implementations.curve_manager

        # Track consecutive price fetch errors for fallback trigger
        # REDUCED from 5 to 2 for faster fallback on migrated tokens
        MAX_PRICE_ERRORS = 2
        consecutive_price_errors = 0
        last_known_price = position.entry_price  # Use entry price as fallback
        _anomaly_count = 0  # consecutive price anomaly counter
        _batch_anomaly_count = 0  # Session 4: separate counter for BATCH PRICE GUARD
        _entry_corrected = False  # Session 3: one-time entry price correction flag
        _entry_fix_ts = 0  # FIX 7-3: timestamp of ENTRY FIX REACTIVE registration
        _stale_count = 0  # PATCH 9A: consecutive ticks with same price
        _prev_price = 0.0
        check_count = 0

        # HARD STOP LOSS - ЖЁСТКИЙ стоп-лосс, продаём НЕМЕДЛЕННО при любом убытке > порога
        # Это ДОПОЛНИТЕЛЬНАЯ защита поверх обычного stop_loss_price
        HARD_STOP_LOSS_PCT = 35.0  # 35% убыток = matches position.py 15-60s window (FIX S18-7)
        EMERGENCY_STOP_LOSS_PCT = 45.0  # FIX S28-3: 45% (15-30s window), HARD 35% (15s+). Dynamic SL is 30% (0-60s)

        # Счётчик неудачных попыток продажи для агрессивного retry
        sell_retry_count = 0

        # CRITICAL: Track total monitor iterations to detect stuck loops
        max_iterations = 36000  # Max 24 hours of 1-second checks
        total_iterations = 0
        MAX_SELL_RETRIES = 2
        pending_stop_loss = False  # Флаг что нужно продать по SL

        while position.is_active:
            total_iterations += 1
            check_count += 1

            # FIX S12-2: Zombie monitor detection — stop if position removed or in sold_mints
            mint_str_check = str(token_info.mint)
            _still_active = any(str(p.mint) == mint_str_check for p in self.active_positions)
            if not _still_active:
                logger.warning(f"[ZOMBIE KILL] {token_info.symbol}: not in active_positions — stopping monitor")
                position.is_active = False
                unregister_monitor(mint_str_check)
                break
            # Async sold_mints check (every 10 ticks to avoid Redis spam)
            if check_count % 10 == 0:
                try:
                    from trading.redis_state import is_sold_mint
                    if await is_sold_mint(mint_str_check):
                        # FIX S16-1: NEVER kill moonbag positions via zombie checker
                        _is_mb = getattr(position, 'is_moonbag', False) or getattr(position, 'tp_partial_done', False)
                        if _is_mb:
                            logger.warning(f"[ZOMBIE SKIP] {token_info.symbol}: in sold_mints but is MOONBAG — removing from sold_mints, keeping alive")
                            try:
                                import redis as _redis_sync
                                _r = _redis_sync.Redis()
                                _r.zrem("sold_mints", mint_str_check)
                                _r.close()
                            except Exception:
                                pass
                        else:
                            # FIX S23-5: Dont kill fresh positions — sold_mints may be stale
                            _pos_age = (time.time() - position.entry_time.timestamp()) if hasattr(position.entry_time, "timestamp") else 999
                            if _pos_age < 120:
                                logger.warning(f"[ZOMBIE SKIP] {token_info.symbol}: in sold_mints but age={_pos_age:.0f}s < 120s — removing stale sold_mint, keeping alive")
                                try:
                                    import redis as _redis_sync
                                    _r = _redis_sync.Redis()
                                    _r.zrem("sold_mints", mint_str_check)
                                    _r.close()
                                except Exception:
                                    pass
                            else:
                                logger.warning(f"[ZOMBIE KILL] {token_info.symbol}: found in sold_mints (age={_pos_age:.0f}s) — stopping monitor")
                                position.is_active = False
                                self._remove_position(mint_str_check)
                                break
                except Exception:
                    pass  # Redis error — continue monitoring

            # Safety check: prevent infinite loops
            # PATCH 13: removed redundant 0.1s sleep (main sleep is at end of loop)
            if total_iterations > max_iterations:
                skip_sl_iter = str(token_info.mint) in NO_SL_MINTS
                if skip_sl_iter:
                    logger.warning(f"[NO_SL] {token_info.symbol}: max iterations but NO_SL - NOT selling!")
                    break
                logger.error(
                    f"[CRITICAL] Monitor exceeded {max_iterations} iterations for {token_info.symbol}! "
                    f"Forcing emergency sell..."
                )
                await self._emergency_fallback_sell(token_info, position, last_known_price)
                break

            try:
                # USE BATCH PRICE SERVICE (rate-limit safe - ONE request for ALL tokens!)
                mint_str = str(token_info.mint)
                price_source = "batch_cache"
                
                # Phase 4b: Try vault price from whale_geyser first (real-time, ~300ms latency)
                current_price = None
                if self.whale_tracker and hasattr(self.whale_tracker, 'get_vault_price'):
                    grpc_price = self.whale_tracker.get_vault_price(mint_str)
                    if grpc_price and grpc_price > 0:
                        current_price = grpc_price
                        price_source = "grpc_stream"
                # S38: Try moonbag gRPC price (PublicNode) for moonbag/dust positions
                if (not current_price or current_price <= 0) and getattr(self, '_moonbag_monitor', None):
                    _mb_price = self._moonbag_monitor.get_price(mint_str)
                    if _mb_price and _mb_price > 0:
                        current_price = _mb_price
                        price_source = "moonbag_grpc"
                # Fallback: Get price from batch cache (instant, no API call!)
                if not current_price or current_price <= 0:
                    current_price = get_cached_price(mint_str)

                # === PATCH 13B: Fast gRPC retry for first ticks (avoid 3s Jupiter timeout) ===
                if (not current_price or current_price <= 0) and check_count <= 10:
                    # gRPC may not have delivered first update yet — wait briefly and retry
                    for _grpc_retry in range(3):
                        await asyncio.sleep(0.2)
                        if self.whale_tracker and hasattr(self.whale_tracker, 'get_vault_price'):
                            grpc_price = self.whale_tracker.get_vault_price(mint_str)
                            if grpc_price and grpc_price > 0:
                                current_price = grpc_price
                                price_source = "grpc_retry"
                                break
                        _batch = get_cached_price(mint_str)
                        if _batch and _batch > 0:
                            current_price = _batch
                            price_source = "batch_retry"
                            break
                # === END PATCH 13B ===

                if not current_price or current_price <= 0:
                    # Cache miss - fallback to direct Jupiter (rare)
                    price_source = "jupiter_fallback"
                    try:
                        from utils.jupiter_price import get_token_price
                        fallback_price, _ = await asyncio.wait_for(
                            get_token_price(mint_str),
                            timeout=3.0
                        )
                        if fallback_price and fallback_price > 0:
                            current_price = fallback_price
                        else:
                            raise ValueError("No Jupiter price")
                    except Exception:
                        # Last resort: curve manager
                        price_source = "curve_fallback"
                        try:
                            current_price = await curve_manager.calculate_price(pool_address)
                        except Exception:
                            current_price = last_known_price  # Use last known
                            price_source = "last_known"
                
                # Log price source on first check
                if check_count == 1:
                    logger.info(f"[PRICE] {token_info.symbol}: {current_price:.10f} SOL (source: {price_source})")
                
                # DUST FILTER: Skip monitoring if position value tiny AND not a moonbag/tp_partial
                # FIX S26-5: Moonbag/tp_partial positions survived TP — they are NOT dust.
                # Dust = remnant after TSL sell on moonbag. Only kill unknown tiny positions.
                position_value_sol = position.quantity * current_price
                _is_confirmed_position = getattr(position, 'is_moonbag', False) or getattr(position, 'tp_partial_done', False) or getattr(position, 'is_dust', False)
                if position_value_sol < 0.002 and check_count == 1 and not _is_confirmed_position:
                    logger.info(f"[DUST] {token_info.symbol}: Value {position_value_sol:.6f} SOL < 0.002 SOL, skipping monitor")
                    position.is_active = False
                    try:
                        from trading.position import remove_position
                        remove_position(str(token_info.mint))
                        logger.info(f"[DUST] Removed {token_info.symbol} from positions")
                    except:
                        pass
                    break
                if pending_stop_loss:
                    # Price recovered above SL? Cancel pending sell
                    if position.stop_loss_price and current_price > position.stop_loss_price * 1.05:
                        pending_stop_loss = False
                        sell_retry_count = 0
                        logger.warning(
                            f"[RETRY SL CANCEL] {token_info.symbol}: price {current_price:.10f} "
                            f"recovered above SL {position.stop_loss_price:.10f}, cancelling pending sell"
                        )
                    else:
                        # Still below SL — force sell on this tick
                        should_exit = True
                        exit_reason = ExitReason.STOP_LOSS
                        logger.warning(
                            f"[RETRY SL] {token_info.symbol}: pending sell active, "
                            f"retry #{sell_retry_count}, price {current_price:.10f}, forcing sell"
                        )

                # === PRICE ANOMALY DETECTION ===
                # If price changes > 90% in one tick, likely garbage data from Jupiter/cache
                if last_known_price > 0 and current_price > 0 and check_count > 1:
                    price_change_ratio = current_price / last_known_price
                    if price_change_ratio < 0.1 or price_change_ratio > 10.0:
                        _anomaly_count += 1
                        if _anomaly_count < 3:
                            logger.warning(
                                f"[PRICE] {token_info.symbol}: ANOMALY #{_anomaly_count} — "
                                f"{current_price:.10f} vs last {last_known_price:.10f} "
                                f"({price_change_ratio:.2f}x change) — SKIPPING TICK"
                            )
                            current_price = last_known_price  # use last good price
                        else:
                            logger.warning(
                                f"[PRICE] {token_info.symbol}: 3+ anomalies — accepting new price "
                                f"{current_price:.10f} (was {last_known_price:.10f})"
                            )
                            _anomaly_count = 0  # reset, accept new price
                    else:
                        _anomaly_count = 0  # normal price, reset counter
                # === END PRICE ANOMALY DETECTION ===

                # Reset error counter on successful price fetch
                consecutive_price_errors = 0
                last_known_price = current_price

                # === PATCH 9A: STALE PRICE DETECTION ===
                # If price unchanged for 5+ ticks, force Jupiter refresh
                if current_price == _prev_price and current_price > 0:
                    _stale_count += 1
                    if _stale_count >= 5:
                        try:
                            from utils.jupiter_price import get_token_price
                            fresh_price, _ = await asyncio.wait_for(
                                get_token_price(mint_str), timeout=3.0
                            )
                            if fresh_price and fresh_price > 0:
                                # FIX S12-5: Sanity check — reject anomalous Jupiter prices (>100% deviation)
                                _jup_deviation = abs(fresh_price - current_price) / current_price if current_price > 0 else 0
                                if _jup_deviation > 1.0:
                                    logger.warning(
                                        f"[STALE] {token_info.symbol}: Jupiter price {fresh_price:.10f} "
                                        f"deviates {_jup_deviation*100:.0f}% from cache {current_price:.10f} — ANOMALOUS, IGNORING"
                                    )
                                    price_source = "jupiter_fallback_rejected"
                                elif _jup_deviation > 0.05:
                                    # FIX S12-5: Update batch cache, use on NEXT tick (not this tick)
                                    # This prevents false TP/SL trigger from stale->fresh jump
                                    logger.warning(
                                        f"[STALE] {token_info.symbol}: Cache stuck at {current_price:.10f}, "
                                        f"Jupiter says {fresh_price:.10f} ({_jup_deviation*100:+.1f}%) — "
                                        f"updating cache, decision on NEXT tick"
                                    )
                                    try:
                                        from utils.batch_price_service import update_cached_price
                                        update_cached_price(mint_str, fresh_price)
                                    except (ImportError, Exception):
                                        pass
                                    last_known_price = fresh_price
                                    price_source = "jupiter_fallback_deferred"
                                    # Do NOT set current_price = fresh_price (decision deferred to next tick)
                                else:
                                    # <5% deviation — cache is fine, just stale RPC
                                    pass
                            _stale_count = 0
                        except Exception as e:
                            logger.debug(f"[STALE] Jupiter refresh failed: {e}")
                else:
                    _stale_count = 0
                _prev_price = current_price
                # === END PATCH 9A ===

                # S36-2: BATCH PRICE GUARD — reject anomalous batch prices
                # Skip first 2 ticks only (was 5 — Ashen lost 5s on real rug pull)
                # Session 4: moonbag bypass + separate counter
                if price_source in ("batch_cache", "batch_retry") and position.entry_price > 0 and not position.is_moonbag:
                    _batch_pnl = (current_price - position.entry_price) / position.entry_price
                    if _batch_pnl < -0.50:
                        _batch_anomaly_count += 1
                        if _batch_anomaly_count <= 2:
                            logger.warning(
                                f"[PRICE GUARD] {token_info.symbol}: batch price {current_price:.10f} "
                                f"looks anomalous ({_batch_pnl*100:+.1f}% vs entry), skipping tick #{_batch_anomaly_count}/2"
                            )
                            await asyncio.sleep(self.price_check_interval)
                            continue
                        elif _batch_anomaly_count == 3:
                            logger.warning(
                                f"[PRICE GUARD] {token_info.symbol}: price {current_price:.10f} "
                                f"confirmed real after 2 ticks ({_batch_pnl*100:+.1f}%), proceeding with SL check"
                            )
                    else:
                        _batch_anomaly_count = 0

                # Calculate current PnL FIRST (needed for all checks)
                # SESSION 3: One-time entry price correction for PumpFun tokens
                # Quote price from bonding curve != execution price (30-40% slippage on new tokens)
                # Correct entry = buy_amount_sol / actual_tokens_received
                if not _entry_corrected:
                    _ep_src_chk = getattr(position, 'entry_price_source', '')
                    _ep_prov_chk = getattr(position, 'entry_price_provisional', False)
                    _confirmed_chk = getattr(position, 'buy_confirmed', False) or getattr(position, 'tokens_arrived', False)
                    # FIX 10-2: Skip if GEYSER-SELF already verified/corrected entry
                    if _ep_src_chk in ('grpc_verified', 'grpc_execution', 'onchain_verified'):
                        logger.info(f"[ENTRY FIX] {token_info.symbol}: SKIP — already {_ep_src_chk}, provisional={_ep_prov_chk}")
                        _entry_corrected = True
                    elif _confirmed_chk and (_ep_prov_chk or _ep_src_chk == 'pumpfun_curve'):
                        try:
                            _real_bal = await self._get_token_balance(str(token_info.mint))
                            if _real_bal is not None and _real_bal > 0 and self.buy_amount > 0:
                                # FIX 10-3c: Try on-chain TX query for REAL sol spent (not config buy_amount)
                                _actual_sol_spent = None
                                _buy_sig = getattr(position, 'buy_tx_sig', None)
                                if _buy_sig:
                                    try:
                                        from solana.rpc.async_api import AsyncClient as _AC103
                                        from solders.signature import Signature as _Sig103
                                        _cs103 = _AC103(os.environ.get("CHAINSTACK_RPC_ENDPOINT", ""))
                                        try:
                                            _tx_r = await _cs103.get_transaction(_Sig103.from_string(_buy_sig), max_supported_transaction_version=0)
                                            if _tx_r.value:
                                                _m = _tx_r.value.transaction.meta
                                                _pre_s = _m.pre_balances[0] / 1e9
                                                _post_s = _m.post_balances[0] / 1e9
                                                _fee_s = _m.fee / 1e9
                                                _actual_sol_spent = (_pre_s - _post_s) - _fee_s
                                                if _actual_sol_spent > 0:
                                                    logger.info(f"[ENTRY FIX] {token_info.symbol}: on-chain SOL spent={_actual_sol_spent:.6f} (config={self.buy_amount})")
                                                else:
                                                    logger.warning(f"[ENTRY FIX] {token_info.symbol}: on-chain SOL spent={_actual_sol_spent:.6f} invalid, using config")
                                                    _actual_sol_spent = None
                                        finally:
                                            await _cs103.close()
                                    except Exception as _tx_e:
                                        logger.warning(f"[ENTRY FIX] {token_info.symbol}: TX query failed: {_tx_e}, using config buy_amount")
                                _sol_for_entry = _actual_sol_spent if _actual_sol_spent else self.buy_amount
                                _real_entry = _sol_for_entry / _real_bal
                                _old_entry = position.entry_price
                                _correction_pct = (_real_entry - _old_entry) / _old_entry * 100 if _old_entry > 0 else 0
                                if abs(_correction_pct) > 5:
                                    position.entry_price = _real_entry
                                    if hasattr(position, 'original_entry_price'):
                                        position.original_entry_price = _real_entry
                                    position.entry_price_source = 'execution_corrected'
                                    position.entry_price_provisional = False
                                    if self.take_profit_percentage and position.take_profit_price:
                                        position.take_profit_price = _real_entry * (1 + self.take_profit_percentage)
                                    if self.stop_loss_percentage and position.stop_loss_price:
                                        position.stop_loss_price = _real_entry * (1 - self.stop_loss_percentage)
                                    position.high_water_mark = max(_real_entry, current_price)
                                    position.quantity = _real_bal
                                    self._save_position(position)
                                    logger.warning(
                                        f"[ENTRY FIX] {token_info.symbol}: CORRECTED entry "
                                        f"{_old_entry:.10f} -> {_real_entry:.10f} ({_correction_pct:+.1f}%) "
                                        f"qty={_real_bal:.2f} | new TP={position.take_profit_price or 0:.10f} SL={position.stop_loss_price or 0:.10f}"
                                    )
                                    try:
                                        if self.whale_tracker and hasattr(self.whale_tracker, 'register_sl_tp'):
                                            self.whale_tracker.register_sl_tp(
                                                mint=str(token_info.mint), symbol=token_info.symbol,
                                                entry_price=_real_entry,
                                                sl_price=position.stop_loss_price or 0,
                                                tp_price=position.take_profit_price or 0,
                                            )
                                    except Exception:
                                        pass
                                    _entry_fix_ts = time.time()  # FIX 7-3: cooldown after entry correction
                                else:
                                    logger.info(f"[ENTRY FIX] {token_info.symbol}: entry ok ({_correction_pct:+.1f}%), no correction needed")
                                    # Session 4: Register REACTIVE TP/SL even if no correction needed (was deferred for PumpFun)
                                    try:
                                        if self.whale_tracker and hasattr(self.whale_tracker, 'register_sl_tp'):
                                            self.whale_tracker.register_sl_tp(
                                                mint=str(token_info.mint), symbol=token_info.symbol,
                                                entry_price=position.entry_price,
                                                sl_price=position.stop_loss_price or 0,
                                                tp_price=position.take_profit_price or 0,
                                            )
                                            logger.warning(f"[ENTRY FIX] {token_info.symbol}: REACTIVE TP/SL registered (fallback, no correction)")
                                            _entry_fix_ts = time.time()  # FIX 7-3: cooldown before TP check
                                    except Exception:
                                        pass
                                _entry_corrected = True
                            elif _real_bal is not None and _real_bal == 0:
                                logger.warning(f"[ENTRY FIX] {token_info.symbol}: wallet balance=0, buy may have failed")
                                _entry_corrected = True
                        except Exception as _ec_err:
                            logger.warning(f"[ENTRY FIX] {token_info.symbol}: correction failed: {_ec_err}")

                pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
                
                # ============================================
                # DCA: ДОКУПКА НА -25% ИЛИ +25%
                # Только 2 покупки! После DCA - SL/TSL от НОВОЙ цены
                # ============================================
                if position.dca_enabled and position.dca_pending and not position.dca_bought:
                    # PRICE SANITY CHECK: Skip DCA if price moved > 80% from entry in wrong direction
                    # This catches garbage prices from Jupiter/batch cache
                    price_vs_entry = abs(current_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
                    if current_price < position.entry_price * 0.1:
                        logger.warning(f"[DCA] {token_info.symbol}: SKIPPED — price {current_price:.10f} looks like bad data (>90% below entry {position.entry_price:.10f})")
                        continue

                    dca_down_trigger = position.original_entry_price * (1 - position.dca_trigger_pct)  # -25%
                    dca_up_trigger = position.original_entry_price * (1 + position.dca_trigger_pct)    # +25%
                    
                    dca_triggered = False
                    dca_reason = ""
                    

                    # [edit:s12] DCA sanity check
                    # SANITY CHECK: If price is >50x entry, entry price is likely garbage (decimals bug)
                    if position.entry_price > 0 and current_price / position.entry_price > 50:
                        _ratio = current_price / position.entry_price
                        logger.warning(
                            f"[DCA] {token_info.symbol}: SKIP — entry price looks wrong! "
                            f"current={current_price:.10f} vs entry={position.entry_price:.10f} (ratio={_ratio:.0f}x). "
                            f"Attempting price correction..."
                        )
                        _bwd = await self._get_token_balance_with_decimals(str(token_info.mint))
                        if _bwd:
                            _actual_ui, _actual_dec, _actual_raw = _bwd
                            if _actual_ui > 0:
                                _corrected = self.buy_amount / _actual_ui
                                logger.warning(
                                    f"[DCA] PRICE FIX: {token_info.symbol} entry {position.entry_price:.10f} -> {_corrected:.10f} "
                                    f"(balance={_actual_ui:,.2f}, decimals={_actual_dec})"
                                )
                                position.entry_price = _corrected
                                position.original_entry_price = _corrected
                                sl_pct = self.stop_loss_percentage if self.stop_loss_percentage else 0.25
                                position.stop_loss_price = _corrected * (1 - sl_pct)
                                if self.take_profit_percentage is not None:
                                    position.take_profit_price = _corrected * (1 + self.take_profit_percentage)
                                position.high_water_mark = _corrected
                                save_positions(self.active_positions)
                        position.dca_pending = False
                        position.dca_bought = True
                        continue

                    if current_price <= dca_down_trigger:
                        dca_triggered = True
                        dca_reason = f"-{position.dca_trigger_pct*100:.0f}%"
                        logger.warning(f"[DCA] {token_info.symbol}: Price {current_price:.10f} <= {dca_down_trigger:.10f} ({dca_reason})")
                    elif current_price >= dca_up_trigger:
                        dca_triggered = True
                        dca_reason = f"+{position.dca_trigger_pct*100:.0f}%"
                        logger.warning(f"[DCA] {token_info.symbol}: Price {current_price:.10f} >= {dca_up_trigger:.10f} ({dca_reason})")
                    
                    if dca_triggered:
                        # Balance check before DCA buy
                        dca_balance_ok = await self._check_balance_before_buy()
                        if not dca_balance_ok:
                            logger.warning(f"[DCA] {token_info.symbol}: SKIPPED — insufficient balance")
                            position.dca_pending = False
                            position.dca_bought = True
                            dca_triggered = False

                    if dca_triggered:
                        logger.warning(f"[DCA] Executing second buy for {token_info.symbol} ({dca_reason})...")
                        
                        # Докупаем оставшиеся 50%
                        dca_buy_amount = self.buy_amount * (1 - position.dca_first_buy_pct)
                        
                        try:
                            # [edit:s12] DCA verify fix
                            # CRITICAL: Snapshot balance BEFORE buy (not after!)
                            _pre_dca_balance = await self._get_token_balance(str(token_info.mint))
                            logger.info(f"[DCA] Pre-buy balance: {_pre_dca_balance}")

                            success, tx_sig, dex_used, dca_tokens, dca_price = await self._buy_any_dex(
                                mint_str=str(token_info.mint),
                                symbol=token_info.symbol,
                                sol_amount=dca_buy_amount,
                                jupiter_first=True,
                                is_dca=True,
                            )

                            if success:
                                logger.warning(f"[DCA] TX sent for {token_info.symbol} (sig={tx_sig}), verifying on-chain...")

                                dca_confirmed = False
                                real_dca_tokens = dca_tokens  # fallback to Jupiter estimate

                                # PRIMARY: Verify via TX signature (deterministic, no timing issues)
                                if tx_sig:
                                    try:
                                        from trading.fallback_seller import verify_transaction_success
                                        _rpc = self._fallback_buyer._alt_client or await self._fallback_buyer._get_rpc_client()
                                        _tx_ok, _tx_err = await verify_transaction_success(_rpc, tx_sig, max_wait=12.0)
                                        if _tx_ok:
                                            dca_confirmed = True
                                            logger.warning(f"[DCA] \u2705 TX CONFIRMED via signature!")
                                            await asyncio.sleep(1)
                                            _post_balance = await self._get_token_balance(str(token_info.mint))
                                            if _post_balance is not None and _pre_dca_balance is not None and _post_balance > _pre_dca_balance + 1:
                                                real_dca_tokens = _post_balance - _pre_dca_balance
                                                logger.warning(f"[DCA] Balance diff: {real_dca_tokens:,.2f} tokens (expected {dca_tokens:,.2f})")
                                            else:
                                                _bwd = await self._get_token_balance_with_decimals(str(token_info.mint))
                                                if _bwd and _pre_dca_balance is not None:
                                                    _new_ui, _dec, _raw = _bwd
                                                    if _new_ui > _pre_dca_balance + 1:
                                                        real_dca_tokens = _new_ui - _pre_dca_balance
                                                        logger.warning(f"[DCA] Balance diff (v2): {real_dca_tokens:,.2f} (decimals={_dec})")
                                                    else:
                                                        logger.info(f"[DCA] Balance diff unavailable, using estimate {dca_tokens:,.2f}")
                                                else:
                                                    logger.info(f"[DCA] Balance check failed, using estimate {dca_tokens:,.2f}")
                                        else:
                                            logger.error(f"[DCA] \u274c TX FAILED via signature: {_tx_err}")
                                    except Exception as _ve:
                                        logger.warning(f"[DCA] TX sig verify error: {_ve}, falling back to balance check")

                                # FALLBACK: Balance diff (if TX sig verify didn't confirm)
                                if not dca_confirmed:
                                    _old_bal = _pre_dca_balance if _pre_dca_balance is not None else position.quantity
                                    for _vcheck in range(6):
                                        await asyncio.sleep(2)
                                        _new_bal = await self._get_token_balance(str(token_info.mint))
                                        if _new_bal is not None and _new_bal > _old_bal + 1:
                                            real_dca_tokens = _new_bal - _old_bal
                                            dca_confirmed = True
                                            logger.warning(f"[DCA] \u2705 CONFIRMED via balance diff! Got {real_dca_tokens:,.2f} tokens")
                                            break
                                        logger.info(f"[DCA] Verify {_vcheck+1}/6: balance={_new_bal}, waiting...")

                                if dca_confirmed:
                                    total_quantity = (_pre_dca_balance or position.quantity) + real_dca_tokens

                                    logger.warning(f"[DCA] Total tokens: {position.quantity:.2f} -> {total_quantity:.2f}")

                                    # Entry = НОВАЯ цена (не средняя!)
                                    position.quantity = total_quantity
                                    position.entry_price = dca_price
                                    position.dca_bought = True
                                    position.dca_pending = False

                                    # SL/TSL от НОВОЙ цены (use config SL percentage)
                                    sl_pct = self.stop_loss_percentage if self.stop_loss_percentage else 0.25
                                    position.stop_loss_price = dca_price * (1 - sl_pct)
                                    position.high_water_mark = dca_price
                                    position.tsl_active = False
                                    position.tsl_trigger_price = 0

                                    logger.warning(f"[DCA] New entry: {dca_price:.10f}")
                                    logger.warning(f"[DCA] New SL: {position.stop_loss_price:.10f} (-{sl_pct*100:.0f}%)")
                                    logger.warning(f"[DCA] TSL reset")

                                    if self.take_profit_percentage is not None:
                                        position.take_profit_price = dca_price * (1 + self.take_profit_percentage)
                                        logger.warning(f"[DCA] New TP: {position.take_profit_price:.10f} (+{self.take_profit_percentage*100:.0f}%)")

                                    pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
                                    logger.warning(f"[DCA] New PnL: {pnl_pct:+.1f}%")

                                    save_positions(self.active_positions)
                                else:
                                    logger.error(f"[DCA] \u274c TX NOT CONFIRMED for {token_info.symbol} \u2014 quantity UNCHANGED at {position.quantity:.2f}")
                                    position.dca_bought = True
                                    position.dca_pending = False
                                    save_positions(self.active_positions)
                            else:
                                logger.error(f"[DCA] \u274c FAILED to buy more {token_info.symbol}")
                        except Exception as e:
                            logger.error(f"[DCA] Error during second buy: {e}")



                # ============================================
                # STOP LOSS CHECKS - ORDER MATTERS!
                # 1. Config SL (from position.stop_loss_price) - checked in should_exit
                # 2. HARD SL (25%) - backup protection
                # 3. EMERGENCY SL (40%) - last resort
                # ============================================

                # Check if position should be exited (includes config SL check!)
                # UPDATE: Call update_price() for TSL (Trailing Stop-Loss) support
                if position.update_price(current_price):
                    # HWM changed - save to file (every update to prevent loss on restart)
                    save_positions(self.active_positions)
                should_exit, exit_reason = position.should_exit(current_price)
                # ============================================
                # NO_SL MASTER BLOCK - BLOCKS ALL SELL PATHS
                # If token is in NO_SL list, NEVER sell for ANY reason except manual
                # ============================================
                mint_str_nosl = str(token_info.mint)
                if mint_str_nosl in NO_SL_MINTS:
                    if should_exit:
                        import time as _time
                        _nosl_key = f"nosl_warn_{mint_str_nosl}"
                        _nosl_now = _time.monotonic()
                        _nosl_last = getattr(self, '_nosl_warn_times', {}).get(_nosl_key, 0)
                        if _nosl_now - _nosl_last >= 60:
                            if not hasattr(self, '_nosl_warn_times'):
                                self._nosl_warn_times = {}
                            self._nosl_warn_times[_nosl_key] = _nosl_now
                            logger.warning(f"[NO_SL] {token_info.symbol}: EXIT BLOCKED (reason: {exit_reason}, pnl: {pnl_pct:+.1f}%)")
                        should_exit = False
                        exit_reason = None
                        # Reset TSL flags to prevent spam loop
                        if hasattr(position, 'tsl_triggered') and position.tsl_triggered:
                            position.tsl_triggered = False
                            logger.info(f"[NO_SL] {token_info.symbol}: TSL flags reset (NO_SL active)")
                    # For NO_SL tokens: completely disable TSL to prevent re-activation
                    if hasattr(position, 'tsl_active') and position.tsl_active:
                        position.tsl_active = False
                        position.tsl_activation_pct = 999.0  # effectively disable
                        logger.info(f"[NO_SL] {token_info.symbol}: TSL fully disabled (NO_SL token)")

                # ============================================
                # CRITICAL: Log when approaching SL threshold
                # ============================================
                if position.stop_loss_price and current_price <= position.stop_loss_price * 1.1:
                    # Throttle: log SL warning max once per 30s, skip NO_SL tokens
                    _sl_warn_key = f"sl_warn_{token_info.symbol}"
                    _sl_warn_last = getattr(position, "_sl_warn_ts", 0)
                    import time as _time_mod
                    if _time_mod.monotonic() - _sl_warn_last >= 10 and str(token_info.mint) not in NO_SL_MINTS:
                        position._sl_warn_ts = _time_mod.monotonic()
                        logger.warning(
                            f"[SL WARNING] {token_info.symbol}: Price {current_price:.10f} approaching "
                            f"SL {position.stop_loss_price:.10f} (PnL: {pnl_pct:+.2f}%)"
                        )

                # Check NO_SL list BEFORE any SL logic
                mint_str_check = str(token_info.mint)
                skip_sl = mint_str_check in NO_SL_MINTS

                # If config SL triggered - mark as pending
                if should_exit and exit_reason == ExitReason.STOP_LOSS and not position.is_moonbag and not skip_sl:
                    logger.error(
                        f"[CONFIG SL] {token_info.symbol}: STOP LOSS TRIGGERED! "
                        f"Price {current_price:.10f} <= SL {position.stop_loss_price:.10f}"
                    )
                    pending_stop_loss = True


                # MOONBAG SL — safety floor
                if should_exit and exit_reason == ExitReason.STOP_LOSS and position.is_moonbag and not skip_sl:
                    logger.error(
                        f"[MOONBAG SL] {token_info.symbol}: MOONBAG STOP LOSS! "
                        f"Price {current_price:.10f} <= SL {position.stop_loss_price:.10f}"
                    )
                    pending_stop_loss = True

                # Handle TRAILING STOP (TSL) - sells with locked profit
                if should_exit and exit_reason == ExitReason.TRAILING_STOP:
                    locked_profit = ((current_price - position.entry_price) / position.entry_price) * 100
                    logger.warning(
                        f"[TSL] {token_info.symbol}: TRAILING STOP TRIGGERED! "
                        f"Price {current_price:.10f} <= TSL trigger {position.tsl_trigger_price:.10f}. "
                        f"Locked profit: +{locked_profit:.1f}%"
                    )
                    # TSL exit is like TP - we're in profit, so proceed with sell

                # ============================================
                # HARD STOP LOSS - ЖЁСТКАЯ ЗАЩИТА ОТ УБЫТКОВ
                # ============================================
                # ИСКЛЮЧЕНИЕ: токены из NO_SL_MINTS не продаются по SL
                mint_str_check = str(token_info.mint)
                skip_sl = mint_str_check in NO_SL_MINTS
                
                if skip_sl and check_count == 1:
                    logger.warning(f"[NO_SL] {token_info.symbol}: SL DISABLED for this token!")
                
                # Проверка 1: Обычный HARD STOP LOSS (35%)
                # PATCHED Session 3: HARD SL skips first 30s (Dynamic SL covers this window)
                _pos_age_hs = (datetime.utcnow() - position.entry_time).total_seconds() if position.entry_time else 999
                if pnl_pct <= -HARD_STOP_LOSS_PCT and not position.is_moonbag and not getattr(position, "tp_partial_done", False) and not skip_sl and _pos_age_hs >= 15:
                    logger.error(
                        f"[HARD SL] {token_info.symbol}: LOSS {pnl_pct:.1f}%! "
                        f"HARD STOP LOSS triggered (threshold: -{HARD_STOP_LOSS_PCT:.0f}%)"
                    )
                    should_exit = True
                    exit_reason = ExitReason.STOP_LOSS
                    pending_stop_loss = True

                # Проверка 2: EMERGENCY STOP LOSS (45%) - максимальный приоритет
                # PATCHED Session 3: EMERGENCY SL skips first 15s (Dynamic SL -45% covers)
                if pnl_pct <= -EMERGENCY_STOP_LOSS_PCT and not position.is_moonbag and not getattr(position, "tp_partial_done", False) and not skip_sl and _pos_age_hs >= 15:
                    logger.error(
                        f"[EMERGENCY] {token_info.symbol}: CATASTROPHIC LOSS {pnl_pct:.1f}%! "
                        f"EMERGENCY sell triggered (threshold: -{EMERGENCY_STOP_LOSS_PCT:.0f}%)"
                    )
                    should_exit = True
                    exit_reason = ExitReason.STOP_LOSS
                    pending_stop_loss = True

                    # CRITICAL: Reset should_exit for NO_SL tokens
                    if skip_sl and exit_reason == ExitReason.STOP_LOSS:
                        should_exit = False
                        exit_reason = None
                        pending_stop_loss = False
                        if check_count == 1:
                            logger.warning(f"[NO_SL] {token_info.symbol}: SL BLOCKED by NO_SL list!")


                # FIX S27-4: HARD SL for moonbag/dust — emergency exit at -25% from entry (S37)
                if (position.is_moonbag or getattr(position, "tp_partial_done", False)) and not skip_sl:
                    _mb_entry = position.original_entry_price if position.original_entry_price > 0 else position.entry_price
                    if _mb_entry > 0:
                        _mb_pnl = ((current_price - _mb_entry) / _mb_entry) * 100
                        if _mb_pnl <= -25.0:
                            logger.error(
                                f"[MOONBAG HARD SL] {token_info.symbol}: {_mb_pnl:.1f}% from entry! "
                                f"price={current_price:.10f} entry={_mb_entry:.10f} — EMERGENCY EXIT"
                            )
                            should_exit = True
                            exit_reason = ExitReason.STOP_LOSS
                            pending_stop_loss = True
                # Log ALL positions as WARNING every check
                if check_count % 10 == 0:  # Log every ~10s
                    _tp_str = f"{position.take_profit_price:.10f}" if position.take_profit_price else "OFF"
                    logger.warning(
                        f"[MONITOR] {token_info.symbol}: {current_price:.10f} SOL "
                        f"({pnl_pct:+.2f}%) | TP: {_tp_str} | "
                        f"SL: {(position.stop_loss_price or 0):.10f} | "
                        f"HARD_SL: -{HARD_STOP_LOSS_PCT:.0f}%"
                    )

                # FIX 7-3: Block TP for 1.5s after ENTRY FIX REACTIVE registration
                if should_exit and exit_reason == ExitReason.TAKE_PROFIT and _entry_fix_ts > 0:
                    import time as _time73
                    _since_fix = _time73.time() - _entry_fix_ts
                    if _since_fix < 2.0:
                        logger.warning(f"[TP COOLDOWN] {token_info.symbol}: TP blocked, {_since_fix:.1f}s < 2.0s after ENTRY FIX (S18-9)")
                        should_exit = False
                        exit_reason = None

                # FIX S15-2: Moonbag CANNOT exit via TP — safety net in monitor loop
                if should_exit and exit_reason == ExitReason.TAKE_PROFIT and (position.is_moonbag or getattr(position, 'tp_partial_done', False)):
                    logger.warning(
                        f"[TP BLOCKED] {token_info.symbol}: moonbag={position.is_moonbag} tp_partial={position.tp_partial_done} "
                        f"— TP exit BLOCKED, forcing TP=None (FIX S15-2)"
                    )
                    position.take_profit_price = None
                    should_exit = False
                    exit_reason = None

                # FIX S17-3: Skip sell if REACTIVE path already selling
                # is_selling=True means _reactive_sell launched _fast_sell_with_timeout
                # Monitor loop must NOT launch a second sell — it causes double-sell
                # and _remove_position with skip_cleanup=False killing the moonbag
                if should_exit and exit_reason and getattr(position, 'is_selling', False):
                    logger.warning(
                        f"[SELL SKIP] {token_info.symbol}: is_selling=True — "
                        f"REACTIVE path already selling, monitor skip (FIX S17-3)"
                    )
                    should_exit = False
                    exit_reason = None

                if should_exit and exit_reason:
                    logger.warning(f"[EXIT] Exit condition met: {exit_reason.value}")
                    logger.warning(f"[EXIT] Current price: {current_price:.10f} SOL, PnL: {pnl_pct:+.2f}%")

                    # Log PnL before exit
                    pnl = position.get_pnl(current_price)
                    logger.info(
                        f"[EXIT] Position PnL: {pnl['price_change_pct']:.2f}% ({pnl['unrealized_pnl_sol']:.6f} SOL)"
                    )

                    # Handle exit strategies based on exit_reason
                    # Calculate sell quantity
                    if exit_reason == ExitReason.TAKE_PROFIT:
                        if position.tp_sell_pct < 1.0:
                            sell_quantity = position.quantity * position.tp_sell_pct
                            logger.warning(f"[TP] Selling {position.tp_sell_pct*100:.0f}%, keeping {(1-position.tp_sell_pct)*100:.0f}%")
                        elif self.moon_bag_percentage > 0:
                            sell_quantity = position.quantity * (1 - self.moon_bag_percentage / 100)
                            logger.warning(f"[TP] Selling {100-self.moon_bag_percentage:.0f}%, keeping {self.moon_bag_percentage:.0f}% moonbag")
                        else:
                            sell_quantity = position.quantity
                    elif exit_reason == ExitReason.TRAILING_STOP:
                        sell_quantity = position.quantity * position.tsl_sell_pct
                        logger.warning(f"[TSL] Selling {position.tsl_sell_pct*100:.0f}%, keeping {(1-position.tsl_sell_pct)*100:.0f}% moonbag")
                        position.tsl_active = False
                        position.tsl_triggered = False
                        position.high_water_mark = current_price
                    elif exit_reason == ExitReason.STOP_LOSS:
                        sell_quantity = position.quantity
                        logger.warning("[SL] STOP LOSS - selling 100% of position, NO moon bag!")
                    else:
                        sell_quantity = position.quantity

                    # ============================================
                    # ============================================
                    # FAST SELL с параллельными методами (5s timeout, 10s max)
                    # ============================================
                    # --- PATCH 6: Double-sell race condition guard ---
                    position.is_selling = True
                    position._is_selling_since = datetime.utcnow()  # FIX S27-2: watchdog timestamp
                    logger.error(f"[SELL] {token_info.symbol}: PnL {pnl_pct:.1f}% - FAST SELL MODE")
                    
                    # skip_cleanup for partial sells (TP partial, TSL moonbag)
                    _is_partial = (
                        (exit_reason == ExitReason.TAKE_PROFIT and (position.tp_sell_pct < 1.0 or self.moon_bag_percentage > 0))
                        or (exit_reason == ExitReason.TRAILING_STOP and position.tsl_sell_pct < 1.0)
                    )
                    sell_success = await self._fast_sell_with_timeout(
                        token_info, position, current_price, sell_quantity, skip_cleanup=_is_partial, exit_reason=exit_reason
                    )
                    
                    if sell_success:
                        logger.warning(f"[OK] Successfully exited position: {exit_reason.value}")
                        
                        # Log final PnL
                        final_pnl = position.get_pnl(current_price)
                        logger.info(f"[FINAL] PnL: {final_pnl['price_change_pct']:.2f}% ({final_pnl['unrealized_pnl_sol']:.6f} SOL)")

                        # ========== MOONBAG LOGIC FOR TSL ==========
                        if exit_reason == ExitReason.TRAILING_STOP and position.tsl_sell_pct < 1.0:
                            # TSL partial sell - convert remaining to moonbag
                            remaining_quantity = position.quantity * (1 - position.tsl_sell_pct)
                            
                            if remaining_quantity > 1.0:  # Only keep moonbag if > 1 token
                                position.quantity = remaining_quantity
                                position.is_moonbag = True
                                position.is_dust = True  # FIX S20: TSL partial sell remnant — only entry SL, no TSL
                                # FIX S18-10: all moonbag params from yaml (self)
                                _orig_entry = position.entry_price
                                position.tsl_enabled = False
                                position.tsl_active = False
                                position.tsl_trail_pct = 0
                                # FIX S19-2: Do NOT zero tsl_sell_pct — it's used by _is_partial check
                                # tsl_sell_pct stays at yaml value (e.g. 0.5) for correct sell_quantity calc
                                # TSL won't re-trigger because tsl_enabled=False and tsl_active=False
                                # position.tsl_sell_pct = 0  # REMOVED — caused sell_quantity = qty * 0 = 0
                                position.tsl_trigger_price = 0
                                position.high_water_mark = 0
                                position.stop_loss_price = position.original_entry_price if position.original_entry_price > 0 else _orig_entry  # FIX S20: dust SL = entry (break-even)

                                logger.warning(
                                    f"[MOONBAG TSL] {token_info.symbol}: SL={position.stop_loss_price:.10f} (=entry) "
                                    f"price={current_price:.10f} entry={_orig_entry:.10f} "
                                    f"is_dust=True tsl=OFF")

                                # Save updated position
                                self._save_position(position)
                                
                                logger.warning(
                                    f"[MOONBAG] {token_info.symbol}: Converted to moonbag! "
                                    f"Keeping {remaining_quantity:.2f} tokens ({(1-position.tsl_sell_pct)*100:.0f}%) "
                                    f"for potential moon 🌙"
                                )
                                
                                logger.warning(f"[MOONBAG TSL] {token_info.symbol}: TSL partial done, continuing monitor for remaining moonbag")
                                position.is_selling = False
                                # Unsubscribe gRPC curve+ATA — moonbag uses batch price
                                try:
                                    _mint_str = str(token_info.mint)
                                    if self.whale_tracker:
                                        if hasattr(self.whale_tracker, 'unsubscribe_bonding_curve'):
                                            asyncio.create_task(self.whale_tracker.unsubscribe_bonding_curve(_mint_str))
                                        if hasattr(self.whale_tracker, 'unsubscribe_ata'):
                                            asyncio.create_task(self.whale_tracker.unsubscribe_ata(_mint_str))
                                        if hasattr(self.whale_tracker, 'unsubscribe_vault_accounts'):
                                            asyncio.create_task(self.whale_tracker.unsubscribe_vault_accounts(_mint_str))
                                        logger.warning(f"[MOONBAG TSL] {token_info.symbol}: Curve+ATA+Vault UNSUBSCRIBED — moonbag on batch price")
                                except Exception as _ue:
                                    logger.warning(f"[MOONBAG TSL] {token_info.symbol}: Unsubscribe failed: {_ue}")
                                # FIX S23-3: Clean reactive triggers for moonbag (no more curve ticks)
                                if self.whale_tracker and hasattr(self.whale_tracker, 'unregister_sl_tp'):
                                    self.whale_tracker.unregister_sl_tp(str(token_info.mint))
                                # FIX S19-1: Ensure moonbag is watched in batch price after gRPC unsubscribe
                                try:
                                    watch_token(_mint_str)
                                    logger.warning(f"[MOONBAG TSL] {token_info.symbol}: batch price WATCH ensured (FIX S19-1)")
                                except Exception:
                                    pass
                                # S38: Subscribe moonbag to PublicNode gRPC for real-time price
                                try:
                                    if getattr(self, '_moonbag_monitor', None):
                                        _bv = getattr(position, 'pool_base_vault', None)
                                        _qv = getattr(position, 'pool_quote_vault', None)
                                        if not _bv or not _qv:
                                            from trading.vault_resolver import resolve_vaults
                                            _vr = await resolve_vaults(_mint_str)
                                            if _vr:
                                                _bv, _qv = _vr[0], _vr[1]
                                                position.pool_base_vault = _bv
                                                position.pool_quote_vault = _qv
                                                position.pool_address = _vr[2]
                                        if _bv and _qv:
                                            self._moonbag_monitor.subscribe(_mint_str, _bv, _qv, decimals=6, symbol=token_info.symbol)
                                            logger.warning(f"[MOONBAG TSL] {token_info.symbol}: PublicNode gRPC subscribed")
                                except Exception as _mge:
                                    logger.info(f"[MOONBAG TSL] {token_info.symbol}: moonbag gRPC subscribe failed: {_mge}")
                                # FIX S18-11: was break — killed monitor! moonbag had NO monitoring
                                # Must continue loop so SL/TSL keeps checking price
                                continue
                            else:
                                logger.info(f"[MOONBAG] {token_info.symbol}: Remaining {remaining_quantity:.4f} too small, closing fully")
                        
                        # ========== PARTIAL TP (disable TP) ==========
                        if exit_reason == ExitReason.TAKE_PROFIT and (position.tp_sell_pct < 1.0 or self.moon_bag_percentage > 0):
                            remaining_quantity = position.quantity * (1 - position.tp_sell_pct)

                            if remaining_quantity > 1.0:
                                position.quantity = remaining_quantity
                                position.take_profit_price = None  # disable TP forever
                                position.tp_partial_done = True  # marker for restore
                                # FIX 11-3: TP partial → moonbag (was missing!)
                                # Without this, position stays is_moonbag=False after TP partial sell.
                                # RESTORE then treats it as regular position, HARD SL can kill it,
                                # and tsl_trail stays at 30% instead of 50%.
                                position.is_moonbag = True
                                # FIX S18-10: TP moonbag from yaml
                                _sl_pct_tp = self.stop_loss_percentage or 0.20
                                _sl_from_tp = current_price * (1 - _sl_pct_tp)
                                position.tsl_trail_pct = self.tsl_trail_pct
                                position.tsl_sell_pct = self.tsl_sell_pct
                                position.stop_loss_price = position.entry_price * 0.80  # FIX S20: moonbag SL -20% from entry (matches config)
                                # S22: Do NOT force-activate TSL — let monitor loop activate at tsl_activation_pct
                                # Moonbag starts with tsl_active=False, protected by SL=entry*0.80
                                position.tsl_active = False
                                position.tsl_enabled = True
                                position.high_water_mark = 0
                                position.tsl_trigger_price = 0
                                logger.warning(f"[TSL] {token_info.symbol}: moonbag TSL DEFERRED — will activate at +{self.tsl_activation_pct*100:.0f}% from entry")
                                self._save_position(position)
                                # Unsubscribe gRPC curve+ATA — moonbag uses batch price
                                try:
                                    _mint_str = str(token_info.mint)
                                    if self.whale_tracker:
                                        if hasattr(self.whale_tracker, 'unsubscribe_bonding_curve'):
                                            asyncio.create_task(self.whale_tracker.unsubscribe_bonding_curve(_mint_str))
                                        if hasattr(self.whale_tracker, 'unsubscribe_ata'):
                                            asyncio.create_task(self.whale_tracker.unsubscribe_ata(_mint_str))
                                        if hasattr(self.whale_tracker, 'unsubscribe_vault_accounts'):
                                            asyncio.create_task(self.whale_tracker.unsubscribe_vault_accounts(_mint_str))
                                        logger.warning(f"[TP MOONBAG] {token_info.symbol}: Curve+ATA+Vault UNSUBSCRIBED — moonbag on batch price")
                                except Exception as _ue:
                                    logger.warning(f"[TP MOONBAG] {token_info.symbol}: Unsubscribe failed: {_ue}")
                                # FIX S23-3: Clean reactive triggers for moonbag (no more curve ticks)
                                if self.whale_tracker and hasattr(self.whale_tracker, 'unregister_sl_tp'):
                                    self.whale_tracker.unregister_sl_tp(str(token_info.mint))
                                # FIX S19-1: Ensure moonbag is watched in batch price after gRPC unsubscribe
                                try:
                                    _mint_str_tp = str(token_info.mint)
                                    watch_token(_mint_str_tp)
                                    logger.warning(f"[TP MOONBAG] {token_info.symbol}: batch price WATCH ensured (FIX S19-1)")
                                except Exception:
                                    pass
                                # S38: Subscribe moonbag to PublicNode gRPC for real-time price
                                try:
                                    if getattr(self, '_moonbag_monitor', None):
                                        _bv_tp = getattr(position, 'pool_base_vault', None)
                                        _qv_tp = getattr(position, 'pool_quote_vault', None)
                                        if not _bv_tp or not _qv_tp:
                                            from trading.vault_resolver import resolve_vaults
                                            _vr_tp = await resolve_vaults(_mint_str_tp)
                                            if _vr_tp:
                                                _bv_tp, _qv_tp = _vr_tp[0], _vr_tp[1]
                                                position.pool_base_vault = _bv_tp
                                                position.pool_quote_vault = _qv_tp
                                                position.pool_address = _vr_tp[2]
                                        if _bv_tp and _qv_tp:
                                            self._moonbag_monitor.subscribe(_mint_str_tp, _bv_tp, _qv_tp, decimals=6, symbol=token_info.symbol)
                                            logger.warning(f"[TP MOONBAG] {token_info.symbol}: PublicNode gRPC subscribed")
                                except Exception as _mge:
                                    logger.info(f"[TP MOONBAG] {token_info.symbol}: moonbag gRPC subscribe failed: {_mge}")
                                position.is_selling = False  # FIX S27-1: reset guard (was missing — moonbag stuck forever with is_selling=True)
                                logger.warning(f"[TP] Partial TP done. Keeping {remaining_quantity:.2f} tokens, TP disabled; continue with TSL/SL.")
                                continue
                            else:
                                logger.info(f"[TP] Remaining {remaining_quantity:.4f} too small, closing fully")

                        # ========== FULL EXIT (SL, TP, or tiny moonbag) ==========
                        
                        # Close ATA if enabled
                        if not _is_partial:
                            await handle_cleanup_after_sell(
                                self.solana_client,
                                self.wallet,
                                token_info.mint,
                                token_info.token_program_id,
                                self.priority_fee_manager,
                                self.cleanup_mode,
                                self.cleanup_with_priority_fee,
                                self.cleanup_force_close_with_burn,
                            )
                    
                            # CRITICAL: Mark position as closed and remove from tracking!
                            position.is_active = False
                            self._remove_position(str(token_info.mint))
                            unregister_monitor(str(token_info.mint))
                            logger.info(f"[CLEANUP] Position {token_info.symbol} removed from tracking")
                        
                            position.is_selling = False  # PATCH 6: reset guard
                            break  # Exit monitoring loop after successful sell
                        else:
                            position.is_selling = False
                            logger.warning(f"[PARTIAL] {token_info.symbol}: Continuing after partial sell")
                    else:
                        # Не удалось продать - retry с лимитом
                        position.is_selling = False  # PATCH 6: reset guard on failure
                        sell_retry_count += 1

                        if sell_retry_count >= 5:
                            logger.error(f"[GIVE UP] {token_info.symbol}: 5 sell attempts failed! Checking balance...")
                            # Check if we actually have tokens - if not, this is a zombie
                            try:
                                _actual_bal = await self._get_token_balance(str(token_info.mint))
                            except Exception:
                                _actual_bal = None
                            # _get_token_balance returns: float>=0, None (no account), -1.0 (RPC error)
                            if _actual_bal is not None and _actual_bal != -1.0 and _actual_bal <= 0:
                                logger.warning(
                                    f"[ZOMBIE] {token_info.symbol}: 0 tokens on wallet after 5 sell fails! "
                                    f"Cleaning up phantom position."
                                )
                                position.is_active = False
                                pending_stop_loss = False
                                try:
                                    self._remove_position(str(token_info.mint))
                                    unregister_monitor(str(token_info.mint))
                                    from trading.redis_state import forget_position_forever
                                    await forget_position_forever(str(token_info.mint), reason="zombie_no_tokens")
                                except Exception as _ce:
                                    logger.warning(f"[ZOMBIE] Cleanup error (non-fatal): {_ce}")
                                break  # Exit monitoring loop
                            else:
                                # Tokens exist or RPC failed - keep trying
                                logger.error(
                                    f"[RETRY] {token_info.symbol}: balance={_actual_bal}, "
                                    f"resetting retries (tokens exist or RPC error)"
                                )
                                sell_retry_count = 0
                                pending_stop_loss = True
                                await asyncio.sleep(30)
                                continue

                        # Exponential backoff: 2s, 4s, 8s, 16s, 32s
                        backoff = min(2 ** sell_retry_count, 32)
                        logger.error(f"[RETRY {sell_retry_count}/5] {token_info.symbol} sell failed, waiting {backoff}s...")
                        pending_stop_loss = True
                        await asyncio.sleep(backoff)
                        continue

                # Wait before next price check
                await asyncio.sleep(1)  # 1s price check, logs throttled

            except Exception as e:
                consecutive_price_errors += 1
                error_msg = str(e) if str(e) else type(e).__name__
                logger.warning(
                    f"[MONITOR] Price fetch error #{consecutive_price_errors}/{MAX_PRICE_ERRORS} "
                    f"for {token_info.symbol}: {error_msg}"
                )
                
                # ============================================
                # FALLBACK: Try DexScreener if bonding curve fails
                # ============================================
                if "not found" in error_msg.lower() or "invalid" in error_msg.lower():
                    try:
                        from utils.dexscreener_price import get_price_from_dexscreener
                        dex_price = await get_price_from_dexscreener(str(token_info.mint))
                        if dex_price and dex_price > 0:
                            logger.info(f"[FALLBACK] Got price from DexScreener: {dex_price:.10f} SOL")
                            current_price = dex_price
                            last_known_price = dex_price
                            consecutive_price_errors = 0  # Reset errors
                            
                            # Calculate PnL and check SL/TP
                            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
                            if position.update_price(current_price):
                                save_positions(self.active_positions)
                            should_exit, exit_reason = position.should_exit(current_price)
                            
                            logger.info(
                                f"[MONITOR-DEX] {token_info.symbol}: {current_price:.10f} SOL "
                                f"({pnl_pct:+.2f}%) | SL: {(position.stop_loss_price or 0):.10f}"
                            )
                            
                            # Check hard SL
                            skip_sl_dex = str(token_info.mint) in NO_SL_MINTS
                            if pnl_pct <= -HARD_STOP_LOSS_PCT and not skip_sl_dex and not position.is_moonbag and not getattr(position, "tp_partial_done", False):
                                logger.error(f"[HARD SL] {token_info.symbol}: {pnl_pct:.1f}% - SELLING!")
                                should_exit = True
                                exit_reason = ExitReason.STOP_LOSS
                            
                            # Reset should_exit for NO_SL tokens (same as main path)
                            if skip_sl_dex and exit_reason == ExitReason.STOP_LOSS:
                                should_exit = False
                                exit_reason = None
                                logger.warning(f"[NO_SL] {token_info.symbol}: SL BLOCKED in DEX fallback!")
                            
                            if should_exit and exit_reason:
                                # Proceed with sell logic (same as above)
                                fallback_success = await self._emergency_fallback_sell(
                                    token_info, position, current_price
                                )
                                if fallback_success:
                                    break
                            
                            # Continue monitoring with DexScreener price
                            await asyncio.sleep(1)  # 1s price check, logs throttled
                            continue
                    except Exception as dex_err:
                        logger.warning(f"[FALLBACK] DexScreener also failed: {dex_err}")

                # ============================================
                # CRITICAL FIX: Check SL even when price fetch fails!
                # Use last_known_price to check if we should emergency sell
                # ============================================
                if last_known_price > 0:
                    pnl_pct_estimate = ((last_known_price - position.entry_price) / position.entry_price) * 100
                    logger.warning(
                        f"[MONITOR] Using last known price {last_known_price:.10f} SOL "
                        f"(estimated PnL: {pnl_pct_estimate:+.2f}%)"
                    )

                    # If we're in significant loss based on last price - SELL IMMEDIATELY
                    skip_sl_emerg = str(token_info.mint) in NO_SL_MINTS
                    if pnl_pct_estimate <= -HARD_STOP_LOSS_PCT and not skip_sl_emerg and not position.is_moonbag and not getattr(position, "tp_partial_done", False):
                        logger.error(
                            f"[EMERGENCY SL] {token_info.symbol}: Estimated loss {pnl_pct_estimate:.1f}% "
                            f"based on last price! FORCING EMERGENCY SELL!"
                        )
                        fallback_success = await self._emergency_fallback_sell(
                            token_info, position, last_known_price
                        )
                        if fallback_success:
                            logger.info(f"[OK] Emergency SL sell successful for {token_info.symbol}")
                            break
                        else:
                            logger.error("[FAIL] Emergency SL sell failed, will retry!")
                            # Don't reset counter - keep trying aggressively
                            await asyncio.sleep(1)  # Fast retry
                            continue

                # Check if token migrated (platform-agnostic detection)
                is_migrated = await self._check_if_migrated(token_info, error_msg)

                if consecutive_price_errors >= MAX_PRICE_ERRORS or is_migrated:
                    logger.warning(
                        f"[FALLBACK] {consecutive_price_errors} consecutive price errors or token migrated - "
                        f"attempting emergency fallback sell for {token_info.symbol}"
                    )

                    # Try fallback sell via PumpSwap/Jupiter
                    if str(token_info.mint) in NO_SL_MINTS:
                        logger.warning(f"[NO_SL] {token_info.symbol}: Price errors but NO_SL - NOT selling!")
                        break
                    fallback_success = await self._emergency_fallback_sell(
                        token_info, position, last_known_price
                    )

                    if fallback_success:
                        logger.info(f"[OK] Emergency fallback sell successful for {token_info.symbol}")
                        break
                    else:
                        # DON'T reset counter completely - only reduce it
                        # This ensures we keep trying more aggressively
                        consecutive_price_errors = max(0, consecutive_price_errors - 1)
                        logger.error(
                            f"[FAIL] Emergency fallback sell failed for {token_info.symbol} - "
                            f"will retry (errors: {consecutive_price_errors})"
                        )

                await asyncio.sleep(1)  # 1s price check, logs throttled

    async def _check_if_migrated(self, token_info: TokenInfo, error_msg: str) -> bool:
        """Check if token has migrated based on platform and error message.

        UNIFIED migration check for all platforms:
        - PUMP_FUN: bonding curve complete=True or account not found
        - LETS_BONK: status != 0 or account not found
        - BAGS: status != 0 or account not found

        Args:
            token_info: Token information
            error_msg: Error message from price fetch

        Returns:
            True if token appears to be migrated
        """
        # Quick check based on error message
        # NOTE: "invalid" removed - too broad, catches PumpSwap price errors
        # Only check for definitive migration indicators
        error_lower = error_msg.lower()

        # Definitive migration indicators
        if "bonding curve complete" in error_lower:
            return True
        if "migrated" in error_lower:
            return True
        if "account not found" in error_lower and "bonding" in error_lower:
            return True

        # "status" only matters if it says status changed/completed
        if "status" in error_lower and ("complete" in error_lower or "migrat" in error_lower):
            return True

        # Platform-specific migration check
        try:
            pool_address = self._get_pool_address(token_info)
            curve_manager = self.platform_implementations.curve_manager
            pool_state = await curve_manager.get_pool_state(pool_address)

            if self.platform == Platform.PUMP_FUN:
                # Pump.fun: complete=True means migrated to PumpSwap
                return pool_state.get("complete", False)

            elif self.platform == Platform.LETS_BONK:
                # LetsBonk: status != 0 means migrated
                return pool_state.get("status", 0) != 0

            elif self.platform == Platform.BAGS:
                # BAGS: status != 0 means migrated to DAMM v2
                return pool_state.get("status", 0) != 0

            return False

        except Exception:
            # If we can't check, assume might be migrated
            return True

    async def _emergency_fallback_sell(
        self, token_info: TokenInfo, position: Position, last_price: float
    ) -> bool:
        """Emergency sell via PumpSwap/Jupiter when bonding curve unavailable.

        Args:
            token_info: Token information
            position: Active position to close
            last_price: Last known price for logging

        Returns:
            True if sell was successful, False otherwise
        """
        from trading.position import ExitReason

        logger.warning(
            f"[EMERGENCY] Starting fallback sell for {token_info.symbol} "
            f"({position.quantity:.2f} tokens)"
        )

        try:
            fallback_seller = self._fallback_seller
            # Sell via Jupiter
            success, tx_sig, error = await fallback_seller.sell(
                mint=token_info.mint,
                token_amount=position.quantity,
                symbol=token_info.symbol,
            )

            if success:
                # Close position
                position.close_position(last_price, ExitReason.STOP_LOSS)
                position.is_active = False

                logger.info(f"[OK] Emergency sell SUCCESS: {tx_sig}")
                self._log_trade(
                    "sell",
                    token_info,
                    last_price,
                    position.quantity,
                    tx_sig,
                    extra="emergency_fallback",
                )

                # Remove position from saved file
                self._remove_position(str(token_info.mint))

                return True
            else:
                logger.error(f"[FAIL] Emergency sell FAILED: {error}")
                return False

        except Exception as e:
            logger.exception(f"[FAIL] Emergency fallback sell error: {e}")
            return False

    def _get_pool_address(self, token_info: TokenInfo) -> Pubkey:
        """Get the pool/curve address for price monitoring using platform-agnostic method."""
        address_provider = self.platform_implementations.address_provider

        # Use platform-specific logic to get the appropriate address
        if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
            return token_info.bonding_curve
        elif hasattr(token_info, "pool_state") and token_info.pool_state:
            return token_info.pool_state
        else:
            # Fallback to deriving the address using platform provider
            return address_provider.derive_pool_address(token_info.mint)


    # [edit:s12] lightweight moonbag SL watcher
    async def _moonbag_sl_watcher(self, mint_str: str, symbol: str, sl_price: float, check_interval: int = 60):
        """Lightweight background SL watcher for moonbag tokens.
        Checks price every 60s, sells all if price drops below SL.
        No position tracking — just price check + sell + exit.
        """
        logger.warning(f"[MOONBAG SL] {symbol}: Watcher started. SL={sl_price:.10f}, interval={check_interval}s")
        try:
            while True:
                await asyncio.sleep(check_interval)
                try:
                    # Get price from batch cache or DexScreener
                    price = None
                    from utils.batch_price_service import get_cached_price
                    price = get_cached_price(mint_str)

                    if not price or price <= 0:
                        try:
                            from utils.jupiter_price import get_token_price
                            price, _ = await asyncio.wait_for(
                                get_token_price(mint_str), timeout=5.0
                            )
                        except Exception:
                            pass

                    if not price or price <= 0:
                        continue

                    if price <= sl_price:
                        logger.warning(
                            f"[MOONBAG SL] {symbol}: TRIGGERED! Price {price:.10f} <= SL {sl_price:.10f}. Selling all..."
                        )
                        # Get actual balance
                        balance_info = await self._get_token_balance_with_decimals(mint_str)
                        if balance_info and balance_info[0] > 1.0:
                            ui_amount, decimals, raw_amount = balance_info
                            from interfaces.core import TokenInfo
                            from solders.pubkey import Pubkey
                            token_info = TokenInfo(
                                name=symbol, symbol=symbol, uri="",
                                mint=Pubkey.from_string(mint_str),
                                platform=self.platform,
                            )
                            # Create minimal position for _fast_sell_with_timeout
                            from trading.position import Position
                            temp_pos = Position(
                                mint=mint_str, symbol=symbol,
                                quantity=ui_amount, entry_price=sl_price,
                            )
                            sold = await self._fast_sell_with_timeout(
                                token_info, temp_pos, price,
                                sell_quantity=ui_amount, skip_cleanup=True, exit_reason=ExitReason.STOP_LOSS
                            )
                            if sold:
                                logger.warning(f"[MOONBAG SL] {symbol}: SOLD {ui_amount:,.2f} tokens at {price:.10f}")
                            else:
                                logger.error(f"[MOONBAG SL] {symbol}: Sell FAILED, will retry next cycle")
                                continue  # Don't exit, retry
                        else:
                            logger.info(f"[MOONBAG SL] {symbol}: No tokens on wallet, exiting watcher")
                        return  # Done — sold or no tokens

                except Exception as e:
                    logger.warning(f"[MOONBAG SL] {symbol}: Check error: {e}")
                    continue
        except asyncio.CancelledError:
            logger.info(f"[MOONBAG SL] {symbol}: Watcher cancelled")
        except Exception as e:
            logger.error(f"[MOONBAG SL] {symbol}: Watcher crashed: {e}")


    def _get_sell_lock(self, mint_str: str) -> asyncio.Lock:
        """Get or create a per-mint sell lock to prevent duplicate sells."""
        if not hasattr(self, '_sell_locks'):
            self._sell_locks = {}
        if mint_str not in self._sell_locks:
            self._sell_locks[mint_str] = asyncio.Lock()
        return self._sell_locks[mint_str]

    async def _fast_sell_with_timeout(
        self, token_info: TokenInfo, position: Position, current_price: float, sell_quantity: float = None, skip_cleanup: bool = False, exit_reason=None
    ) -> bool:
        """
        Fast sell via Jupiter with balance check.
        Checks wallet balance, then sells via Jupiter. PumpPortal removed.
        """
        from trading.position import ExitReason
        import time as _time

        _t0 = _time.monotonic()
        mint_str = str(token_info.mint)
        if sell_quantity is None:
            sell_quantity = position.quantity

        # FIX S18-9: HARD BLOCK sell if tokens not on wallet (no age bypass)
        _tokens_ok = getattr(position, 'tokens_arrived', True)
        _buy_ok = getattr(position, 'buy_confirmed', True)
        if not _tokens_ok:
            logger.warning(f"[FAST SELL] HARD BLOCK: {token_info.symbol} tokens_arrived=False — cannot sell")
            return False
        if not _buy_ok:
            logger.info(f"[FAST SELL] {token_info.symbol} buy_confirmed=False but tokens_arrived=True — proceeding")

        MIN_SELL_TOKENS = 1.0
        MIN_SELL_VALUE_SOL = 0.0001
        estimated_value = sell_quantity * current_price if current_price > 0 else 0
        if sell_quantity < MIN_SELL_TOKENS:
            logger.warning(f"[FAST SELL] SKIP DUST: {token_info.symbol} {sell_quantity:.4f} tokens")
            if not skip_cleanup:
                self._remove_position(mint_str)
            return True
        if estimated_value < MIN_SELL_VALUE_SOL:
            logger.warning(f"[FAST SELL] SKIP LOW VALUE: {token_info.symbol} {estimated_value:.6f} SOL")
            if not skip_cleanup:
                self._remove_position(mint_str)
            return True

        # Check actual wallet balance — never try to sell more than we have
        # FIX S12-4: Short timeout (2s) to avoid 2-22s latency. If timeout, use position.quantity.
        # _sell_via_jupiter has its own on-chain balance check as safety net.
        actual_balance = None  # Initialize for later use in verify
        try:
            actual_balance = await asyncio.wait_for(
                self._get_token_balance(str(token_info.mint)),
                timeout=2.0
            )
            if actual_balance is not None and actual_balance >= 0:
                # Normal balance — use it
                if actual_balance < MIN_SELL_TOKENS:
                    logger.warning(f"[FAST SELL] SKIP DUST: {token_info.symbol} wallet has {actual_balance:.4f} tokens — nothing to sell")
                    if not skip_cleanup:
                        self._remove_position(str(token_info.mint))
                    return True
                if actual_balance < sell_quantity * 0.9:
                    logger.warning(f"[FAST SELL] Quantity mismatch: position={sell_quantity:.2f} wallet={actual_balance:.2f}, using wallet balance")
                    # FIX S12-1: Compare ExitReason enum properly (was string comparison, always False)
                    _is_partial_tp = (
                        (isinstance(exit_reason, ExitReason) and exit_reason == ExitReason.TAKE_PROFIT)
                        or (isinstance(exit_reason, str) and exit_reason in ("TP", "PARTIAL_TP", "take_profit"))
                    )
                    if _is_partial_tp and position.quantity > 0 and sell_quantity < position.quantity:
                        _tp_pct = sell_quantity / position.quantity
                        sell_quantity = actual_balance * _tp_pct
                        logger.warning(f"[FAST SELL] PARTIAL_TP moonbag preserved: selling {sell_quantity:.2f} of {actual_balance:.2f} ({_tp_pct:.0%})")
                    else:
                        sell_quantity = actual_balance
            elif actual_balance is None:
                # Token account not found via RPC
                from datetime import datetime as _dt2, timezone as _tz2
                _pos_age2 = (_dt2.now(_tz2.utc) - position.entry_time.replace(tzinfo=_tz2.utc)).total_seconds() if position.entry_time else 999
                _confirmed = getattr(position, 'tokens_arrived', False) or getattr(position, 'buy_confirmed', False)
                if _confirmed:
                    # TX confirmed but RPC lags behind — skip RPC, sell with position qty
                    logger.warning(f"[FAST SELL] RPC no account but TX confirmed for {token_info.symbol} age={_pos_age2:.1f}s — selling with position qty {sell_quantity:.2f}")
                elif _pos_age2 < 3.0:  # S12: was 15s
                    # NOT confirmed and young — wait for TX confirmation
                    logger.warning(f"[FAST SELL] No token account for {token_info.symbol} age={_pos_age2:.1f}s < 3s, not confirmed — RETRY")
                    return False  # Retry — don't delete position
                else:
                    # NOT confirmed and old — buy likely failed
                    logger.warning(f"[FAST SELL] No token account for {token_info.symbol} (age={_pos_age2:.0f}s, not confirmed) — buy likely failed")
                    if not skip_cleanup:
                        self._remove_position(mint_str)
                    return True
            else:
                # BALANCE_RPC_ERROR (-1.0) — all RPCs failed, but tokens may exist
                logger.warning(f"[FAST SELL] Balance check RPC error for {token_info.symbol} — selling with position qty {sell_quantity:.2f} as fallback")
        except (asyncio.TimeoutError, Exception) as bal_err:
            # FIX S12-4: Timeout or RPC error — proceed with position.quantity (fast path)
            if isinstance(bal_err, asyncio.TimeoutError):
                logger.warning(f"[FAST SELL] Balance check TIMEOUT for {token_info.symbol} — using position qty {sell_quantity:.2f} (fast path)")
            else:
                logger.warning(f"[FAST SELL] Balance check exception: {type(bal_err).__name__}: {bal_err}, using position quantity")

        # Per-mint lock — only check, never wait
        sell_lock = self._get_sell_lock(mint_str)
        if sell_lock.locked():
            logger.warning(f"[FAST SELL] SKIP: sell already in progress for {token_info.symbol}")
            return False

        logger.warning(f"[FAST SELL] {token_info.symbol} ({sell_quantity:.2f} tokens) via Jupiter [t+{(_time.monotonic()-_t0)*1000:.0f}ms]")

        async with sell_lock:
            try:
                success, sig, error = await self._fallback_seller._sell_via_jupiter(
                    token_info.mint, sell_quantity, token_info.symbol
                )
            except Exception as e:
                logger.error(f"[FAST SELL] Jupiter exception: {e}")
                return False

            _elapsed = (_time.monotonic() - _t0) * 1000
            if success:
                logger.warning(f"[FAST SELL] Jupiter SUCCESS (confirmed): {sig} [{_elapsed:.0f}ms]")
                # Session 9: Refresh balance cache after sell (background)
                asyncio.create_task(self._update_balance_after_trade())
                original_qty = (
                    actual_balance
                    if (actual_balance is not None and actual_balance >= 0)
                    else getattr(position, "quantity", sell_quantity)
                )  # P3: pre-sell snapshot for accurate VERIFY
                _exit_reason_str = exit_reason.value if isinstance(exit_reason, ExitReason) else (str(exit_reason) if exit_reason else "")
                asyncio.create_task(self._verify_sell_in_background(mint_str, original_qty, token_info.symbol, sell_quantity, exit_reason=_exit_reason_str, tx_sig=sig))
                if not skip_cleanup:
                    position.close_position(current_price, ExitReason.STOP_LOSS)
                    self._remove_position(mint_str)
                    try:
                        from trading.redis_state import forget_position_forever
                        await forget_position_forever(mint_str, reason="sl_sell")
                    except Exception as e:
                        logger.warning(f"[FAST SELL] Redis cleanup failed: {e}")
                logger.warning(
                    f"[FAST SELL] COMPLETE: {token_info.symbol} "
                    f"exit={exit_reason} qty_sold={sell_quantity:.2f} "
                    f"price={current_price:.10f} sig={sig}"
                )
                return True

            logger.error(f"[FAST SELL] Jupiter FAILED: {error} [{_elapsed:.0f}ms]")
            return False

    async def _save_token_info(self, token_info: TokenInfo) -> None:
        """Save token information to a file."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            file_path = trades_dir / f"{token_info.mint}.txt"

            # Convert to dictionary for saving - platform-agnostic
            token_dict = {
                "name": token_info.name,
                "symbol": token_info.symbol,
                "uri": token_info.uri,
                "mint": str(token_info.mint),
                "platform": token_info.platform.value,
                "user": str(token_info.user) if token_info.user else None,
                "creator": str(token_info.creator) if token_info.creator else None,
                "creation_timestamp": token_info.creation_timestamp,
            }

            # Add platform-specific fields only if they exist
            platform_fields = {
                "bonding_curve": token_info.bonding_curve,
                "associated_bonding_curve": token_info.associated_bonding_curve,
                "creator_vault": token_info.creator_vault,
                "pool_state": token_info.pool_state,
                "base_vault": token_info.base_vault,
                "quote_vault": token_info.quote_vault,
            }

            for field_name, field_value in platform_fields.items():
                if field_value is not None:
                    token_dict[field_name] = str(field_value)

            file_path.write_text(json.dumps(token_dict, indent=2))

            logger.info(f"Token information saved to {file_path}")
        except OSError:
            logger.exception("Failed to save token information")

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo | None,
        price: float,
        amount: float,
        tx_hash: str | None,
        extra: str | None = None,
    ) -> None:
        """Log trade information.

        Args:
            action: Trade action (buy/sell)
            token_info: Token information (can be None for universal buys)
            price: Trade price
            amount: Token amount
            tx_hash: Transaction signature
            extra: Extra info string (e.g. "whale_copy:pumpswap:TOKEN")
        """
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)

            # Handle case when token_info is None (universal buy)
            if token_info:
                platform = token_info.platform.value
                token_address = str(token_info.mint)
                symbol = token_info.symbol
            else:
                # Parse from extra string: "whale_copy:dex:symbol"
                platform = "unknown"
                token_address = "unknown"
                symbol = "unknown"
                if extra:
                    parts = extra.split(":")
                    if len(parts) >= 2:
                        platform = parts[1]  # dex used
                    if len(parts) >= 3:
                        symbol = parts[2]

            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "platform": platform,
                "token_address": token_address,
                "symbol": symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
            }

            if extra:
                log_entry["extra"] = extra

            log_file_path = trades_dir / "trades.log"
            with log_file_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except OSError:
            logger.exception("Failed to log trade information")

    def _save_position(self, position: Position) -> None:
        """Save position to active positions list and persist to file."""
        # FIX S12-7: Don't append if already in list or if position is inactive
        if not position.is_active:
            return
        mint_str = str(position.mint)
        # Replace existing or append new
        found = False
        for i, p in enumerate(self.active_positions):
            if str(p.mint) == mint_str:
                self.active_positions[i] = position
                found = True
                break
        if not found:
            self.active_positions.append(position)
        save_positions(self.active_positions)
        # Add to batch price monitoring
        watch_token(str(position.mint))
        # Phase 4b/4c: Subscribe to price tracking via whale_geyser (shared gRPC stream)
        # S22: Guard — moonbag positions use batch price, skip gRPC subscribe
        if getattr(position, "is_moonbag", False) or getattr(position, "tp_partial_done", False):
            logger.info(f"[MONITOR] {position.symbol}: moonbag/tp_done — skip gRPC subscribe, using batch price")
            return
        if self.whale_tracker and hasattr(self.whale_tracker, 'subscribe_vault_accounts') and position.pool_base_vault and position.pool_quote_vault:
            asyncio.create_task(self.whale_tracker.subscribe_vault_accounts(
                mint=str(position.mint),
                base_vault=position.pool_base_vault,
                quote_vault=position.pool_quote_vault,
                symbol=position.symbol,
                decimals=9 if str(position.mint).lower().endswith("bags") else 6,
            ))
        elif self.whale_tracker and hasattr(self.whale_tracker, 'subscribe_bonding_curve') and position.bonding_curve and not position.pool_base_vault:
            asyncio.create_task(self.whale_tracker.subscribe_bonding_curve(
                mint=str(position.mint),
                curve_address=str(position.bonding_curve),
                symbol=position.symbol,
                decimals=9 if str(position.mint).lower().endswith("bags") else 6,
            ))

    async def _verify_sell_in_background(self, mint_str: str, original_qty: float, symbol: str, sell_quantity: float = None, exit_reason: str = "", tx_sig: str = None):
        """Background task to verify sell. Smarter logic for partial fills and DCA races."""
        try:
            await asyncio.sleep(5)  # Wait for TX to confirm

            # FIX 9-2: Query TX on-chain for actual SOL received
            _sol_received_actual = None
            if tx_sig:
                try:
                    from solana.rpc.async_api import AsyncClient as _AsyncClient92
                    from solders.signature import Signature as _Sig92
                    _cs = _AsyncClient92(os.environ.get("CHAINSTACK_RPC_ENDPOINT", ""))
                    try:
                        _tx_resp = await _cs.get_transaction(_Sig92.from_string(tx_sig), max_supported_transaction_version=0)
                        if _tx_resp.value:
                            _meta = _tx_resp.value.transaction.meta
                            _pre_sol = _meta.pre_balances[0] / 1e9
                            _post_sol = _meta.post_balances[0] / 1e9
                            _fee = _meta.fee / 1e9
                            _sol_received_actual = (_post_sol - _pre_sol) + _fee
                            logger.warning(
                                f"[SELL RESULT] {symbol}: SOL received={_sol_received_actual:.6f} "
                                f"(pre={_pre_sol:.6f} post={_post_sol:.6f} fee={_fee:.6f}) "
                                f"exit={exit_reason} TX={tx_sig[:20]}..."
                            )
                            if _sol_received_actual < 0:
                                logger.error(
                                    f"[SELL PRICE ANOMALY] {symbol}: NEGATIVE SOL received={_sol_received_actual:.6f}! "
                                    f"TX may have failed or been frontrun. TX={tx_sig[:20]}..."
                                )
                        else:
                            logger.warning(f"[SELL RESULT] {symbol}: TX not found on-chain (may need more time): {tx_sig[:20]}...")
                    finally:
                        await _cs.close()
                except Exception as _tx_err:
                    logger.warning(f"[SELL RESULT] {symbol}: TX query failed: {type(_tx_err).__name__}: {_tx_err}")

            # Try to get balance (with retries)
            remaining = None
            for balance_attempt in range(3):
                bal = await self._get_token_balance(mint_str)
                if bal is not None and bal >= 0:
                    remaining = bal
                    break
                if bal is None:
                    # Token account not found = fully sold or never existed
                    remaining = 0.0
                    logger.info(f"[VERIFY] {symbol}: Token account not found — treating as fully sold")
                    break
                # bal == BALANCE_RPC_ERROR (-1.0) — RPC failed, retry
                logger.warning(f"[VERIFY] {symbol}: Balance check attempt {balance_attempt+1}/3 failed (RPC error)")
                await asyncio.sleep(3)

            if remaining is None:
                # All 3 attempts returned RPC error — don't remove from sold_mints (safer)
                logger.error(f"[VERIFY] {symbol}: All RPCs failed — keeping in sold_mints (safe default)")
                return False

            expected_sell = sell_quantity if sell_quantity is not None else original_qty
            actual_sold = original_qty - remaining

            logger.info(f"[VERIFY] {symbol}: old={original_qty:.2f}, new={remaining:.2f}, expected_sell={expected_sell:.2f}, actual_sold={actual_sold:.2f}")

            # CASE 1: Balance decreased (any amount) = something was sold
            if remaining < original_qty:
                # Near-full sell (>90% sold)
                if remaining < original_qty * 0.1:
                    # For SL exits: retry selling residual if > 100 tokens (not dust)
                    _is_sl = exit_reason in ("stop_loss", "trailing_stop", "hard_stop_loss", "emergency_stop_loss")
                    if _is_sl and remaining > 100:
                        logger.warning(f"[VERIFY] {symbol}: SL RESIDUAL {remaining:.2f} tokens — scheduling cleanup sell")
                        try:
                            from solders.pubkey import Pubkey as _Pk
                            _ok, _sig, _err = await self._fallback_seller._sell_via_jupiter(
                                _Pk.from_string(mint_str), remaining, symbol
                            )
                            if _ok:
                                logger.warning(f"[VERIFY] {symbol}: SL residual sell TX: {_sig}")
                            else:
                                logger.warning(f"[VERIFY] {symbol}: SL residual sell failed: {_err}")
                        except Exception as _e:
                            logger.warning(f"[VERIFY] {symbol}: SL residual sell error: {_e}")
                    logger.info(f"[VERIFY] {symbol}: Full sell CONFIRMED! Sold {actual_sold:.2f} tokens")
                    return True

                # Partial sell with sell_quantity specified (TP/TSL partial sell — expected)
                if sell_quantity is not None and sell_quantity < original_qty * 0.95:
                    sell_ratio = actual_sold / sell_quantity if sell_quantity > 0 else 0
                    if sell_ratio >= 0.5:
                        logger.info(f"[VERIFY] {symbol}: Partial sell CONFIRMED! Sold {actual_sold:.2f} tokens, keeping {remaining:.2f}")
                        # Deferred forget (moved from _fast_sell)
                        if remaining < 1.0:
                            try:
                                from trading.redis_state import forget_position_forever
                                await forget_position_forever(mint_str, reason="verified_sell_complete")
                            except Exception:
                                pass
                        return True

                # How much of expected did we actually sell?
                sell_pct = actual_sold / expected_sell if expected_sell > 0 else 0

                _is_sl_exit = exit_reason in ("stop_loss", "trailing_stop", "hard_stop_loss", "emergency_stop_loss")
                if sell_pct >= 0.5 and not _is_sl_exit:
                    # Non-SL: Sold 50%+ — acceptable (TP partial etc.)
                    logger.warning(f"[VERIFY] {symbol}: PARTIAL FILL {sell_pct*100:.0f}% — sold {actual_sold:.2f}/{expected_sell:.2f}. Remaining {remaining:.2f}")
                    return True
                if _is_sl_exit and sell_pct >= 0.5:
                    # SL exit but partial — log and fall through to retry
                    logger.warning(f"[VERIFY] {symbol}: SL PARTIAL FILL {sell_pct*100:.0f}% — MUST retry remaining {remaining:.2f}!")

                # Sold < 50% — low liquidity, RETRY what's still needed
                still_need_to_sell = expected_sell - actual_sold
                retry_sell_amount = min(still_need_to_sell, remaining)
                if retry_sell_amount < 1.0:
                    logger.warning(f"[VERIFY] {symbol}: PARTIAL FILL {sell_pct*100:.0f}% — remaining sell {retry_sell_amount:.2f} too small, accepting")
                    return True
                logger.warning(f"[VERIFY] {symbol}: PARTIAL FILL {sell_pct*100:.0f}% — sold {actual_sold:.2f}/{expected_sell:.2f}. RETRYING {retry_sell_amount:.2f} (keeping {remaining - retry_sell_amount:.2f})...")

                for retry in range(2):
                    try:
                        from solders.pubkey import Pubkey
                        success, sig, error = await self._fallback_seller._sell_via_jupiter(
                            Pubkey.from_string(mint_str), retry_sell_amount, symbol
                        )
                        if success:
                            logger.info(f"[VERIFY] {symbol}: Retry {retry+1} TX sent: {sig}")
                            await asyncio.sleep(5)
                            new_remaining = await self._get_token_balance(mint_str)
                            if new_remaining is not None and new_remaining < remaining * 0.5:
                                logger.info(f"[VERIFY] {symbol}: Retry CONFIRMED! Remaining: {new_remaining:.2f}")
                                return True
                            else:
                                logger.warning(f"[VERIFY] {symbol}: Retry {retry+1} — balance still {new_remaining}")
                                if new_remaining is not None:
                                    remaining = new_remaining  # Update for next retry
                        else:
                            logger.warning(f"[VERIFY] {symbol}: Retry {retry+1} failed: {error}")
                    except Exception as e:
                        logger.error(f"[VERIFY] {symbol}: Retry {retry+1} error: {e}")
                    await asyncio.sleep(3)

                # All retries failed — tokens stuck, remove from sold_mints
                logger.error(f"[VERIFY] {symbol}: PARTIAL FILL retries failed. {remaining:.2f} tokens stuck on wallet.")
                await self._remove_from_sold_mints(mint_str, symbol)
                return False


            # CASE 2: Balance INCREASED = DCA race condition
            if remaining > original_qty:
                dca_added = remaining - original_qty
                logger.warning(f"[VERIFY] {symbol}: Balance INCREASED by {dca_added:.2f} (DCA race?). old={original_qty:.2f}, new={remaining:.2f}. NOT retrying.")
                # Do NOT retry - would sell DCA tokens
                return True  # Treat as success - sell TX may have succeeded, DCA added more

            # CASE 3: Balance UNCHANGED = TX did not go through at all
            logger.error(f"[VERIFY] {symbol}: SELL FAILED - balance unchanged at {remaining:.2f}")

            for retry in range(3):
                logger.warning(f"[VERIFY] {symbol}: Retry sell attempt {retry+1}/3 ({expected_sell:.2f} tokens)...")

                try:
                    from solders.pubkey import Pubkey

                    seller = self._fallback_seller
                    # Retry with original sell_quantity, NOT remaining (avoid selling DCA tokens)
                    retry_amount = min(expected_sell, remaining)
                    success, sig, error = await seller._sell_via_jupiter(
                        Pubkey.from_string(mint_str),
                        retry_amount,
                        symbol
                    )

                    if success:
                        logger.info(f"[VERIFY] {symbol}: Retry sell TX sent: {sig}")
                        await asyncio.sleep(5)

                        new_remaining = await self._get_token_balance(mint_str)
                        if new_remaining is not None and new_remaining < remaining * 0.9:
                            logger.info(f"[VERIFY] {symbol}: Retry sell CONFIRMED! Remaining: {new_remaining:.2f}")
                            return True
                        else:
                            logger.warning(f"[VERIFY] {symbol}: Retry TX sent but balance unchanged")
                    else:
                        logger.warning(f"[VERIFY] {symbol}: Retry {retry+1} failed: {error}")

                except Exception as e:
                    logger.error(f"[VERIFY] {symbol}: Retry {retry+1} error: {e}")

                await asyncio.sleep(3)

            logger.error(f"[VERIFY] {symbol}: All 3 retry attempts failed - removing from sold_mints")
            await self._remove_from_sold_mints(mint_str, symbol)
            return False

        except Exception as e:
            logger.error(f"[VERIFY] Error verifying sell for {symbol}: {e}")
            await self._remove_from_sold_mints(mint_str, symbol)
            return False


    async def _remove_from_sold_mints(self, mint_str: str, symbol: str):
        """Helper to remove mint from sold_mints Redis set."""
        try:
            import redis.asyncio as redis
            r = redis.Redis(host='localhost', port=6379, db=0)
            removed = await r.zrem("sold_mints", mint_str)
            await r.aclose()
            if removed:
                logger.info(f"[VERIFY] {symbol}: Removed from sold_mints")
        except Exception as e:
            logger.error(f"[VERIFY] Failed to remove {symbol} from sold_mints: {e}")

    def _remove_position(self, mint: str) -> None:
        """Remove position from active list and file - FORGET FOREVER."""
        # FIX S12-2: Set is_active=False on the position object before removing
        # This ensures monitor loop sees the flag even if it holds a reference
        for p in self.active_positions:
            if str(p.mint) == mint:
                p.is_active = False
        # FIX S19-1: Check moonbag BEFORE removing from list (after removal we lose the reference)
        _is_moonbag_rm = any(
            (getattr(p, 'is_moonbag', False) or getattr(p, 'tp_partial_done', False))
            for p in self.active_positions if str(p.mint) == mint
        )
        self.active_positions = [p for p in self.active_positions if str(p.mint) != mint]
        save_positions(self.active_positions)
        if not _is_moonbag_rm:
            unwatch_token(mint)
        else:
            logger.warning(f"[UNWATCH SKIP] {mint[:16]}... is MOONBAG — keeping batch price watch (FIX S19-1)")
        # Phase 4b/4c: Unsubscribe vault/curve tracking via whale_geyser
        if self.whale_tracker and hasattr(self.whale_tracker, 'unsubscribe_vault_accounts'):
            asyncio.create_task(self.whale_tracker.unsubscribe_vault_accounts(mint))
        if self.whale_tracker and hasattr(self.whale_tracker, 'unsubscribe_bonding_curve'):
            asyncio.create_task(self.whale_tracker.unsubscribe_bonding_curve(mint))
        if self.whale_tracker and hasattr(self.whale_tracker, 'unsubscribe_ata'):
            asyncio.create_task(self.whale_tracker.unsubscribe_ata(mint))
        # FIX S23-1: Clean up reactive SL/TP triggers to prevent zombie triggers
        if self.whale_tracker and hasattr(self.whale_tracker, 'unregister_sl_tp'):
            self.whale_tracker.unregister_sl_tp(mint)
            logger.info(f"[REMOVE] Cleaned _sl_tp_triggers for {mint[:16]}")
        # S38: Unsubscribe moonbag gRPC monitor
        if getattr(self, '_moonbag_monitor', None):
            self._moonbag_monitor.unsubscribe(mint)
        # FORGET FOREVER - add to sold_mints so never restored
        asyncio.create_task(self._forget_position_async(mint))
    
    async def _forget_position_async(self, mint: str) -> None:
        """Async helper to forget position forever.
        FIX S17-1: Check is_moonbag from in-memory BEFORE calling forget
        (Redis position may already be deleted by remove_position by the time
        forget_position_forever runs, causing the moonbag guard to miss it).
        """
        try:
            # Check in-memory first (most reliable — not subject to Redis HDEL race)
            _is_mb_memory = False
            for _p in self.active_positions:
                if str(_p.mint) == mint:
                    _is_mb_memory = getattr(_p, 'is_moonbag', False) or getattr(_p, 'tp_partial_done', False)
                    break
            if _is_mb_memory:
                logger.warning(f"[FORGET SKIP] {mint[:16]}... is MOONBAG in memory — NOT adding to sold_mints (FIX S17-1)")
                return
            from trading.redis_state import forget_position_forever
            await forget_position_forever(mint, reason="position_removed")
        except Exception as e:
            logger.warning(f"[FORGET] Error: {e}")

    async def _restore_positions(self) -> None:
        """Restore and resume monitoring of saved positions on startup."""
        logger.info("[RESTORE] Checking for saved positions to restore...")
        # FIX S28-2: Clear positions loaded by __init__ to prevent duplicates
        self.active_positions.clear()
        positions = await load_positions_async()

        if not positions:
            logger.info("[RESTORE] No saved positions found")
            return

        logger.info(f"[RESTORE] Found {len(positions)} saved positions to restore")

        for position in positions:
            mint_str = str(position.mint)
            logger.info(
                f"[RESTORE] Checking position: {position.symbol} ({mint_str[:8]}...) "
                f"platform={position.platform}, is_active={position.is_active}"
            )

            # DISABLED:             # Only restore positions for our platform
            # DISABLED:             if position.platform != self.platform.value:
            # DISABLED:                 logger.info(f"[RESTORE] Skipping position {position.symbol} - different platform ({position.platform} != {self.platform.value})")

            if not position.is_active:
                logger.info(f"[RESTORE] Skipping closed position {position.symbol}")
                continue

            # Check if already sold - but Redis position overrides sold_mints
            # FIX 11-4: If position loaded from Redis with is_active=True, it IS alive.
            # sold_mints may contain stale entries from partial sells.
            from trading.redis_state import is_sold_mint
            if await is_sold_mint(mint_str):
                if position.is_active:
                    # Position is in Redis AND active — sold_mints is STALE, remove it
                    try:
                        import redis.asyncio as _r114
                        _rc = _r114.Redis(host='localhost', port=6379, db=0)
                        await _rc.zrem("sold_mints", mint_str)
                        await _rc.aclose()
                        logger.warning(f"[RESTORE] {position.symbol}: ACTIVE in Redis but in sold_mints — REMOVED from sold_mints (FIX 11-4)")
                    except Exception as _e114:
                        logger.warning(f"[RESTORE] {position.symbol}: Failed to remove from sold_mints: {_e114}")
                else:
                    logger.info(f"[RESTORE] Skipping SOLD position {position.symbol} - already sold")
                    continue
            logger.info(f"[RESTORE] Restoring position: {position.symbol} on {position.platform}")

            # === DEBUG S15: Log ALL critical fields BEFORE any modification ===
            logger.warning(
                f"[RESTORE DEBUG] {position.symbol}: "
                f"is_moonbag={position.is_moonbag}, "
                f"tp_partial_done={getattr(position, 'tp_partial_done', 'N/A')}, "
                f"take_profit_price={position.take_profit_price}, "
                f"tsl_active={position.tsl_active}, "
                f"tsl_trail_pct={position.tsl_trail_pct}, "
                f"tsl_sell_pct={getattr(position, 'tsl_sell_pct', 'N/A')}, "
                f"entry={position.entry_price}, "
                f"qty={position.quantity}"
            )
            # === END DEBUG S15 ===

            # === SYNC ENTRY PRICE FROM PURCHASE HISTORY ===
            # DISABLED: This caused issues - was overwriting SL from old purchase_history
            # positions.json has correct entry_price and SL set by buy.py
            # We TRUST positions.json now
            logger.info(f"[RESTORE] {position.symbol}: Using saved entry={position.entry_price:.10f}, SL={position.stop_loss_price}")
            # === END SYNC ===
            
            # === ALWAYS CALCULATE TP/SL IF MISSING (skip moonbag) ===
            if position.is_moonbag:
                # Session 5: Moonbag KEEPS TSL as exit mechanism (wide trail 50%)
                # No TP, but TSL protects from total collapse
                position.take_profit_price = None
                # FIX S20: Dust (after TSL partial) — no TSL, only entry SL
                if getattr(position, "is_dust", False):
                    position.tsl_enabled = False
                    position.tsl_active = False
                    if not position.stop_loss_price or position.stop_loss_price <= 0:
                        position.stop_loss_price = position.entry_price
                    logger.info(f"[RESTORE] {position.symbol}: DUST — no TSL, SL=entry {position.stop_loss_price:.10f}")
                if not getattr(position, "is_dust", False) and not position.tsl_enabled:
                    position.tsl_enabled = True
                if not getattr(position, "is_dust", False) and not position.tsl_active and position.high_water_mark and position.high_water_mark > 0:
                    position.tsl_active = True
                    position.tsl_trigger_price = position.high_water_mark * (1 - (position.tsl_trail_pct or 0.30))  # FIX S25-3: use config trail (was hardcoded 0.50)
                    logger.warning(f"[RESTORE] {position.symbol}: MOONBAG TSL reactivated: HWM={position.high_water_mark:.10f}, trigger={position.tsl_trigger_price:.10f}")
                elif position.tsl_active:
                    # Ensure moonbag trail is wide (50%) not default
                    pass  # trail already set from yaml
                # FIX S20: Safety SL fallback — dust=entry, moonbag=entry*0.80 (-20%)
                if not position.stop_loss_price or position.stop_loss_price <= 0:
                    if getattr(position, "is_dust", False):
                        position.stop_loss_price = position.entry_price  # FIX S20: dust SL = entry (break-even)
                    else:
                        position.stop_loss_price = position.entry_price * 0.80  # FIX S20: moonbag SL -20% from entry
                logger.info(f"[RESTORE] {position.symbol}: MOONBAG — TSL active={position.tsl_active}, SL={position.stop_loss_price:.10f}")
            else:
                if position.take_profit_price is None and self.take_profit_percentage:
                    # Don't reassign TP if TSL is already active (means TP already fired via partial sell)
                    if getattr(position, 'tsl_active', False) or getattr(position, 'tp_partial_done', False):
                        logger.info(f"[RESTORE] {position.symbol}: TP=None — stays disabled (tsl_active={getattr(position, 'tsl_active', False)}, tp_partial={getattr(position, 'tp_partial_done', False)})")
                    else:
                        position.take_profit_price = position.entry_price * (1 + self.take_profit_percentage)
                        logger.info(f"[RESTORE] {position.symbol}: Calculated TP = {position.take_profit_price:.10f}")
                if position.stop_loss_price is None and self.stop_loss_percentage:
                    position.stop_loss_price = position.entry_price * (1 - self.stop_loss_percentage)
                    logger.info(f"[RESTORE] {position.symbol}: Calculated SL = {position.stop_loss_price:.10f}")
            # === END TP/SL FIX ===

            # === FORCE TSL_ENABLED FROM CONFIG ===
            # If bot config has TSL enabled but position doesn't (e.g. manual buy, old position),
            # force-enable it. This ensures ALL non-moonbag positions get TSL protection.
            if not position.is_moonbag and self.tsl_enabled and not position.tsl_enabled:
                position.tsl_enabled = True
                position.tsl_activation_pct = self.tsl_activation_pct
                position.tsl_trail_pct = self.tsl_trail_pct
                position.tsl_sell_pct = getattr(self, 'tsl_sell_pct', 1.0)
                logger.warning(
                    f"[RESTORE] {position.symbol}: TSL FORCE-ENABLED from config "
                    f"(activation={self.tsl_activation_pct*100:.0f}%, trail={self.tsl_trail_pct*100:.0f}%)"
                )
            # === END FORCE TSL_ENABLED ===

            # === ALWAYS SYNC TSL PARAMS FROM CONFIG ===
            # Even if tsl_enabled=True, params may be stale (old defaults)
            if not position.is_moonbag and self.tsl_enabled:
                if position.tsl_activation_pct != self.tsl_activation_pct:
                    logger.warning(
                        f"[RESTORE] {position.symbol}: tsl_activation_pct {position.tsl_activation_pct} -> {self.tsl_activation_pct} (from config)"
                    )
                position.tsl_activation_pct = self.tsl_activation_pct
                position.tsl_trail_pct = self.tsl_trail_pct
                position.tsl_sell_pct = getattr(self, 'tsl_sell_pct', 1.0)
                position.tp_sell_pct = getattr(self, 'tp_sell_pct', 0.8)
            # === END SYNC TSL PARAMS ===

            # === FIX 11-2: RESTORE GUARD for tp_partial_done positions ===
            # If TP partial sell already happened, this position MUST NOT have TP.
            # RESTORE from stale JSON could reset tp_partial_done=False or assign TP.
            # This is the FINAL safety net before monitor starts.
            if getattr(position, "tp_partial_done", False):
                if position.take_profit_price is not None:
                    logger.warning(
                        f"[RESTORE] {position.symbol}: tp_partial_done=True but TP={position.take_profit_price} — FORCING TP=None"
                    )
                    position.take_profit_price = None
                if not position.is_moonbag:
                    position.is_moonbag = True
                    position.tsl_sell_pct = getattr(self, "tsl_sell_pct", 0.5)  # FIX S25-1: use yaml (was 1.0, killed moonbag→dust flow)
                    if not position.stop_loss_price or position.stop_loss_price > position.entry_price * 0.25:
                        position.stop_loss_price = position.entry_price * 0.80  # FIX S20: moonbag SL -20% from entry
                    logger.warning(
                        f"[RESTORE] {position.symbol}: tp_partial_done=True, FORCED is_moonbag=True, trail=50%, SL={position.stop_loss_price:.10f}"
                    )
            # === END FIX 11-2 ===

            

            # === BALANCE CHECK: Skip ghost positions ===
            # FIX S17-4: Moonbag with zero balance = tokens sold via TSL/SL or token account closed
            # Clean up gracefully — remove from Redis and memory, add to sold_mints
            try:
                _bal = await self._get_token_balance(mint_str)
                _is_ghost = (_bal is not None and _bal == 0.0) or (_bal is None)
                if _is_ghost:
                    _ghost_reason = "zero_balance" if _bal == 0.0 else "no_account"
                    _is_mb = getattr(position, 'is_moonbag', False) or getattr(position, 'tp_partial_done', False)
                    if _is_mb:
                        # Moonbag ghost — tokens are gone (sold or account closed)
                        # Use allowed reasons so forget_position_forever won't block
                        logger.warning(
                            f"[RESTORE] {position.symbol}: MOONBAG ghost ({_ghost_reason}) — "
                            f"tokens gone, cleaning up position"
                        )
                        self._remove_position(mint_str)
                        try:
                            from trading.redis_state import forget_position_forever
                            await forget_position_forever(mint_str, reason="tsl_sell")  # Allowed reason for moonbag
                        except Exception:
                            pass
                    else:
                        logger.warning(f"[RESTORE] {position.symbol}: Ghost position ({_ghost_reason}) — removing")
                        self._remove_position(mint_str)
                        try:
                            from trading.redis_state import forget_position_forever
                            await forget_position_forever(mint_str, reason="ghost_{_ghost_reason}")
                        except Exception:
                            pass
                    continue
                elif _bal == self.BALANCE_RPC_ERROR:
                    # All RPCs failed — keep position as-is (don't delete, don't update)
                    logger.warning(f"[RESTORE] {position.symbol}: Balance check RPC error — keeping position unchanged")
                elif _bal > 0:
                    # Update quantity to actual on-chain balance
                    if abs(_bal - position.quantity) / max(position.quantity, 1) > 0.1:
                        logger.warning(f"[RESTORE] {position.symbol}: quantity {position.quantity:.2f} -> {_bal:.2f} (on-chain)")
                        position.quantity = _bal
            except Exception as _be:
                logger.warning(f"[RESTORE] Balance check failed for {position.symbol}: {type(_be).__name__}: {_be}")

            # === SMART RESTORE: Check current price vs TP/DCA ===
            # Prevent instant TP trigger or DCA buy when price already moved
            try:
                from utils.batch_price_service import get_cached_price
                restore_price = get_cached_price(mint_str)
                if restore_price and restore_price > 0 and not position.is_moonbag:
                    # TP CHECK: If price already above TP, force-activate TSL (keep TP as safety)
                    if position.take_profit_price and restore_price >= position.take_profit_price:
                        old_tp = position.take_profit_price
                        # DON'T kill TP — keep as safety net
                        if not position.tsl_active and getattr(position, 'tsl_enabled', False):
                            position.tsl_active = True
                            position.high_water_mark = max(restore_price, position.high_water_mark or 0)
                            position.tsl_trigger_price = position.high_water_mark * (1 - position.tsl_trail_pct)
                            logger.warning(
                                f"[RESTORE] {position.symbol}: Price {restore_price:.10f} >= TP {old_tp:.10f} — "
                                f"TSL FORCE-ACTIVATED: HWM={position.high_water_mark:.10f}, "
                                f"trigger={position.tsl_trigger_price:.10f}. TP kept as safety."
                            )
                        else:
                            logger.info(
                                f"[RESTORE] {position.symbol}: Price {restore_price:.10f} >= TP {old_tp:.10f} — "
                                f"TSL already active={position.tsl_active}. TP kept."
                            )
                    # DCA CHECK: If DCA pending but price already moved, skip DCA
                    if getattr(position, "dca_pending", False) and not getattr(position, "dca_bought", False):
                        orig_entry = getattr(position, "original_entry_price", position.entry_price)
                        dca_pct = getattr(position, "dca_trigger_pct", 0.25)
                        dca_up = orig_entry * (1 + dca_pct)
                        dca_down = orig_entry * (1 - dca_pct)
                        if restore_price >= dca_up or restore_price <= dca_down:
                            position.dca_pending = False
                            position.dca_bought = True
                            logger.warning(
                                f"[RESTORE] {position.symbol}: Price {restore_price:.10f} past DCA trigger — DCA SKIPPED"
                            )
            except Exception as e:
                logger.debug(f"[RESTORE] {position.symbol}: Smart restore price check failed: {e}")
            # === END SMART RESTORE ===

            # === TSL RECOVERY: Force-activate if conditions met but TSL inactive ===
            if (not getattr(position, 'is_moonbag', False)
                    and getattr(position, 'tsl_enabled', False)
                    and not position.tsl_active):
                _recovery_price = None
                try:
                    from utils.batch_price_service import get_cached_price
                    _recovery_price = get_cached_price(mint_str)
                except Exception:
                    pass
                _best = max(position.high_water_mark or 0, _recovery_price or 0)
                if _best > 0 and position.entry_price > 0:
                    _prof = (_best - position.entry_price) / position.entry_price
                    if _prof >= position.tsl_activation_pct:
                        position.tsl_active = True
                        position.high_water_mark = _best
                        position.tsl_trigger_price = _best * (1 - position.tsl_trail_pct)
                        logger.warning(
                            f"[RESTORE] {position.symbol}: TSL FORCE-ACTIVATED! "
                            f"best={_best:.10f} >= +{position.tsl_activation_pct*100:.0f}% threshold, "
                            f"HWM={position.high_water_mark:.10f}, trigger={position.tsl_trigger_price:.10f}"
                        )
            # === END TSL RECOVERY ===

            # Set restore_time for TSL grace period (15s warmup after restart)
            from datetime import datetime as _dt_restore
            if getattr(position, 'tsl_active', False):
                position._restore_pending = True  # FIX 7-1: grace starts at first monitor tick, not at RESTORE
                logger.info(f"[RESTORE] {position.symbol}: TSL grace period started (15s)")

            # === FIX S15-1: FINAL moonbag guard before append ===
            # No matter what happened above, if tp_partial_done=True this IS a moonbag.
            # TP MUST be None. This is the absolute last check before monitor starts.
            if getattr(position, "tp_partial_done", False) or position.is_moonbag:
                if position.take_profit_price is not None:
                    logger.warning(
                        f"[RESTORE] {position.symbol}: FINAL GUARD — tp_partial={position.tp_partial_done} "
                        f"moonbag={position.is_moonbag} but TP={position.take_profit_price} — FORCING TP=None (FIX S15-1)"
                    )
                    position.take_profit_price = None
                if not position.is_moonbag:
                    position.is_moonbag = True
                    logger.warning(f"[RESTORE] {position.symbol}: FINAL GUARD — forced is_moonbag=True (FIX S15-1)")
                # Moonbag must sell 100% on TSL, trail 50%
                position.tsl_sell_pct = getattr(self, "tsl_sell_pct", 0.5)  # FIX S25-2: use yaml (was 1.0, killed moonbag→dust flow)
                position.tp_sell_pct = 1.0
            # === END FIX S15-1 ===

            self.active_positions.append(position)

            # Get creator from bonding curve state for proper sell instruction
            creator = None
            creator_vault = None
            bonding_curve = None
            token_migrated = False

            if position.bonding_curve:
                bonding_curve = Pubkey.from_string(position.bonding_curve)
                try:
                    # Fetch pool state to get creator (FIXED: was get_curve_state)
                    curve_manager = self.platform_implementations.curve_manager
                    pool_state = await curve_manager.get_pool_state(bonding_curve)

                    # Check if token migrated to Raydium/PumpSwap
                    if pool_state is None:
                        logger.warning(
                            f"[WARN] Position {position.symbol}: bonding curve not found - "
                            "token may have migrated. Will monitor via DexScreener."
                        )
                        token_migrated = False  # Don't remove - use DexScreener!
                    elif pool_state.get("complete", False):
                        logger.warning(
                            f"[WARN] Position {position.symbol}: token migrated to PumpSwap. "
                            "Will monitor via DexScreener instead."
                        )
                        token_migrated = False  # Don't remove - use DexScreener!
                    elif pool_state.get("status", 0) != 0:
                        logger.warning(
                            f"[WARN] Position {position.symbol}: token migrated (status={pool_state.get('status')}). "
                            "Will monitor via DexScreener."
                        )
                        token_migrated = False  # Don't remove - use DexScreener!
                    elif pool_state.get("creator"):
                        creator_str = pool_state.get("creator")
                        if isinstance(creator_str, str):
                            creator = Pubkey.from_string(creator_str)
                        else:
                            creator = creator_str
                        # Derive creator vault
                        address_provider = self.platform_implementations.address_provider
                        creator_vault = address_provider.derive_creator_vault(creator)
                        logger.info(f"Got creator {str(creator)[:8]}... from pool state")
                        # === PATCH 9B: Deployer blacklist check post-buy ===
                        from trading.deployer_blacklist import is_deployer_blacklisted, _deployer_wallets
                        _creator_str = str(creator)[:44] if creator else ""
                        if _creator_str and is_deployer_blacklisted(_creator_str):
                            _label = _deployer_wallets.get(_creator_str, "unknown")
                            logger.warning(f"[BLACKLIST] ⛔ SCAMMER DETECTED post-buy: {_label} ({_creator_str[:12]}...) — EMERGENCY SELL")
                            try:
                                # FIX S18-4: current_price may not exist in RESTORE context
                                _bl_price = locals().get('current_price') or position.entry_price
                                try:
                                    from utils.batch_price_service import get_batch_price_service
                                    _bps = get_batch_price_service()
                                    if _bps:
                                        _cached = _bps.get_cached_price(str(position.mint))
                                        if _cached and _cached > 0:
                                            _bl_price = _cached
                                except Exception:
                                    pass
                                await self._fast_sell_with_timeout(token_info, position, _bl_price, exit_reason=ExitReason.STOP_LOSS)
                            except Exception as _e:
                                logger.error(f"[BLACKLIST] Emergency sell failed: {_e}")
                            break
                        # === END PATCH 9B ===
                except Exception as e:
                    logger.warning(f"Failed to get creator from pool: {e} - will try fallback sell")
                    # Don't mark as migrated - try to sell anyway via fallback
                    token_migrated = False
            else:
                # No bonding_curve - try to monitor via DexScreener instead of removing!
                logger.warning(f"Position {position.symbol} has no bonding_curve - will monitor via DexScreener")
                token_migrated = False  # Don't remove! Try DexScreener fallback

            # Skip and remove ONLY if truly corrupted (not just missing bonding_curve)
            if token_migrated and not position.mint:
                logger.error(f"Position truly corrupted (no mint) - removing")
                remove_position(position.mint)
                continue
            elif token_migrated:
                # Token migrated but still tradeable - continue monitoring via DexScreener
                logger.warning(f"Position {position.symbol} migrated but will try DexScreener monitoring")

            # Create TokenInfo with creator info for proper sell
            token_info = TokenInfo(
                name=position.symbol,
                symbol=position.symbol,
                uri="",
                mint=position.mint,
                platform=self.platform,
                bonding_curve=bonding_curve,
                creator=creator,
                creator_vault=creator_vault,
            )

            # Start monitoring in background (with duplicate protection)
            mint_str = str(position.mint)
            if not register_monitor(mint_str):
                logger.warning(f"[RESTORE] Skipping {position.symbol} - already has monitor")
                continue
            
            logger.info(f"[RESTORE] Starting monitor for {position.symbol} (TP: {position.take_profit_price}, SL: {position.stop_loss_price})")
            # === Resolve missing vault addresses for existing positions ===
            if not position.pool_base_vault or not position.pool_quote_vault:
                try:
                    from trading.vault_resolver import resolve_vaults
                    logger.info(f"[RESTORE] Resolving vaults for {position.symbol}...")
                    vault_result = await resolve_vaults(mint_str)
                    if vault_result:
                        position.pool_base_vault = vault_result[0]
                        position.pool_quote_vault = vault_result[1]
                        position.pool_address = vault_result[2]
                        save_positions(self.active_positions)
                        logger.warning(
                            f"[RESTORE] ✅ VAULTS RESOLVED for {position.symbol}: "
                            f"base={vault_result[0][:12]}..., quote={vault_result[1][:12]}..."
                        )
                    else:
                        logger.warning(f"[RESTORE] No pool found for {position.symbol} - Jupiter polling fallback")
                except Exception as ve:
                    logger.warning(f"[RESTORE] Vault resolve error for {position.symbol}: {ve}")
            # === END vault resolve ===
            # Phase 4b/4c: Subscribe restored position to price tracking via whale_geyser
            # Moonbags use batch price — no gRPC subscription needed
            if position.is_moonbag or getattr(position, 'tp_partial_done', False):
                # FIX S19-1: Moonbags MUST be watched in batch price on restore
                try:
                    watch_token(mint_str)
                    logger.warning(f"[RESTORE] {position.symbol}: MOONBAG — batch price WATCH ensured (FIX S19-1)")
                except Exception as _we:
                    logger.warning(f"[RESTORE] {position.symbol}: watch_token failed: {_we}")
                logger.info(f"[RESTORE] {position.symbol}: MOONBAG — skipping gRPC subscribe, using batch price")
            elif self.whale_tracker and hasattr(self.whale_tracker, 'subscribe_vault_accounts') and position.pool_base_vault and position.pool_quote_vault:
                await self.whale_tracker.subscribe_vault_accounts(
                    mint=mint_str,
                    base_vault=position.pool_base_vault,
                    quote_vault=position.pool_quote_vault,
                    symbol=position.symbol,
                    decimals=9 if mint_str.lower().endswith("bags") else 6,
                )
                logger.info(f"[RESTORE] {position.symbol}: Subscribed to vault tracking via whale_geyser")
            elif self.whale_tracker and hasattr(self.whale_tracker, 'subscribe_bonding_curve') and position.bonding_curve and not position.pool_base_vault:
                await self.whale_tracker.subscribe_bonding_curve(
                    mint=mint_str,
                    curve_address=str(position.bonding_curve),
                    symbol=position.symbol,
                    decimals=9 if mint_str.lower().endswith("bags") else 6,
                )
                logger.info(f"[RESTORE] {position.symbol}: Subscribed to bonding curve tracking via whale_geyser")
            asyncio.create_task(self._monitor_position_until_exit(token_info, position))


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility
