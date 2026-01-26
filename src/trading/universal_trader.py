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
from monitoring.dev_reputation import DevReputationChecker
from monitoring.trending_scanner import TrendingScanner, TrendingToken
from monitoring.volume_pattern_analyzer import VolumePatternAnalyzer, TokenVolumeAnalysis
from platforms import get_platform_implementations
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import Position, save_positions, load_positions, remove_position, ExitReason, register_monitor, unregister_monitor
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

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = get_logger(__name__)


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
        tsl_enabled: bool = False,
        tsl_activation_pct: float = 0.20,  # Activate after +20% profit
        tsl_trail_pct: float = 0.10,  # Trail 10% below high
        tsl_sell_pct: float = 0.50,  # Sell 50% when TSL triggers
        # Token Vetting (security)
        token_vetting_enabled: bool = False,
        vetting_require_freeze_revoked: bool = True,
        vetting_skip_bonding_curve: bool = True,
        price_check_interval: int = 10,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        sell_fixed_priority_fee: int = 10000,
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
        self.jupiter_api_key = jupiter_api_key or os.getenv("JUPITER_API_KEY")

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
        self.whale_tracker: WhalePoller | None = None
        self.helius_api_key = helius_api_key or os.getenv("HELIUS_API_KEY")

        if enable_whale_copy:
            try:
                # CRITICAL: Use WhalePoller (HTTP polling) instead of WhaleTracker (WSS)
                # because Solana's logsSubscribe with 'mentions' filter does NOT work
                # for wallet addresses - only for Program IDs!
                if WHALE_POLLER_AVAILABLE:
                    logger.warning("[WHALE] Creating WhalePoller instance (HTTP polling)...")
                    logger.warning("[WHALE] Note: Using HTTP polling because WSS logsSubscribe")
                    logger.warning("[WHALE]       doesn't support wallet address mentions!")
                else:
                    logger.warning("[WHALE] WhalePoller not available, falling back to WhaleTracker")
                    logger.warning("[WHALE] WARNING: WSS-based tracking may not work for wallet addresses!")

                self.whale_tracker = WhalePoller(
                    wallets_file=whale_wallets_file,
                    min_buy_amount=whale_min_buy_amount,
                    poll_interval=30.0,  # Poll every 30 seconds
                    max_tx_age=600.0,    # Process transactions up to 10 minutes old
                    stablecoin_filter=stablecoin_filter or [],
                )
                self.whale_tracker.set_callback(self._on_whale_buy)

                # Log wallet count
                wallet_count = len(self.whale_tracker.whale_wallets) if self.whale_tracker.whale_wallets else 0
                logger.warning(f"[WHALE] WhalePoller CREATED: {wallet_count} wallets")
                logger.warning(f"[WHALE] Min buy amount: {whale_min_buy_amount} SOL")
                logger.warning(f"[WHALE] Poll interval: 30s")

                if wallet_count == 0:
                    logger.error("[WHALE] ERROR: No whale wallets loaded!")
                else:
                    # Log first few wallets
                    sample_wallets = list(self.whale_tracker.whale_wallets.keys())[:3]
                    logger.warning(f"[WHALE] Sample wallets: {sample_wallets}")

            except Exception as e:
                logger.exception(f"[WHALE] EXCEPTION creating WhalePoller: {e}")
                self.whale_tracker = None
        else:
            logger.warning("[WHALE] Whale copy: DISABLED in config")

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
        self.active_positions: list[Position] = []  # Active positions for persistence

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
        # SCORING CHECK - ФИЛЬТР МУСОРА!
        # ============================================
        if self.token_scorer:
            try:
                should_buy, score = await self.token_scorer.should_buy(mint_str, whale_buy.token_symbol)
                logger.warning(
                    f"[WHALE SCORE] {whale_buy.token_symbol}: {score.total_score}/100 -> {score.recommendation}"
                )
                logger.warning(
                    f"[WHALE SCORE] Details: vol={score.volume_score}, bp={score.buy_pressure_score}, "
                    f"mom={score.momentum_score}, liq={score.liquidity_score}"
                )

                if not should_buy:
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
            except Exception as e:
                # CRITICAL: Если scoring упал - НЕ покупаем! Безопасность важнее
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

                success, tx_sig, dex_used, token_amount, price = await self._buy_any_dex(
                    mint_str=mint_str,
                    symbol=whale_buy.token_symbol,
                    sol_amount=self.buy_amount,
                )

                if success:
                    break

                # Если не последняя попытка - ждём и пробуем снова
                if attempt < max_retries:
                    logger.warning(
                        f"[WHALE] Attempt {attempt} failed, waiting {retry_delay}s before retry..."
                    )
                    await asyncio.sleep(retry_delay)

            if success:
                # Mark as BOUGHT (completed) - NEVER buy again!
                self._bought_tokens.add(mint_str)
                add_to_purchase_history(
                    mint=mint_str,
                    symbol=whale_buy.token_symbol,
                    bot_name="whale_copy",
                    platform=dex_used,
                    price=price,
                    amount=token_amount,
                )

                # Clean readable success log
                logger.warning("=" * 70)
                logger.warning("[WHALE COPY] SUCCESS")
                logger.warning(f"  SYMBOL:    {whale_buy.token_symbol}")
                if self.whale_tracker: self.whale_tracker._stats["success"] += 1
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
                entry_price = price if price > 0 else self.buy_amount / max(token_amount, 1)

                # CRITICAL: Create position with TP/SL using same method as regular buys!
                # CRITICAL: Derive bonding_curve for fast sell path (avoid fallback)
                from solders.pubkey import Pubkey as SoldersPubkey
                PUMP_PROGRAM_ID = SoldersPubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
                bonding_curve_derived, _ = SoldersPubkey.find_program_address(
                    [b"bonding-curve", bytes(mint)],
                    PUMP_PROGRAM_ID
                )
                logger.info(f"[WHALE] Derived bonding_curve: {bonding_curve_derived}")

                position = Position.create_from_buy_result(
                    mint=mint,
                    symbol=whale_buy.token_symbol,
                    entry_price=entry_price,
                    quantity=token_amount,
                    take_profit_percentage=self.take_profit_percentage,
                    stop_loss_percentage=self.stop_loss_percentage,
                    max_hold_time=self.max_hold_time,
                    platform=dex_used,
                    bonding_curve=str(bonding_curve_derived),  # Properly derived!
                    # TSL parameters
                    tsl_enabled=self.tsl_enabled,
                    tsl_activation_pct=self.tsl_activation_pct,
                    tsl_trail_pct=self.tsl_trail_pct,
                    tsl_sell_pct=self.tsl_sell_pct,
                )

                self.active_positions.append(position)
                save_positions(self.active_positions)

                # Log TP/SL targets
                if position.take_profit_price:
                    logger.warning(f"[WHALE] Take profit target: {position.take_profit_price:.10f} SOL")
                if position.stop_loss_price:
                    logger.warning(f"[WHALE] Stop loss target: {position.stop_loss_price:.10f} SOL")

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
                    from core.pubkeys import TOKEN_PROGRAM_ID
                    
                    # Derive associated_bonding_curve  
                    associated_bonding_curve_derived, _ = SoldersPubkey.find_program_address(
                        [bytes(bonding_curve_derived), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
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

                    # Start monitoring in background task (don't await - let it run)
                    asyncio.create_task(
                        self._monitor_whale_position(token_info, position, dex_used)
                    )
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

    async def _buy_any_dex(
        self,
        mint_str: str,
        symbol: str,
        sol_amount: float,
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
        from trading.fallback_seller import FallbackSeller

        mint = Pubkey.from_string(mint_str)

        # ============================================
        # CROSS-BOT DUPLICATE CHECK (reads positions.json)
        # ============================================
        if is_token_in_positions(mint_str):
            logger.info(f"[SKIP] {symbol} already in positions.json (another bot bought it)")
            return False, None, "skip", 0.0, 0.0

        # ============================================
        # [1/4] TRY ALL BONDING CURVES (for whale_all_platforms mode)
        # When whale_all_platforms=true, we need to check ALL platforms
        # ============================================
        
        # PUMP.FUN - Check always for whale_all_platforms or if current platform
        should_check_pumpfun = (self.platform == Platform.PUMP_FUN or 
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

                    # Create TokenInfo for pump.fun buy
                    token_info = await self._create_pumpfun_token_info_from_mint(
                        mint_str, symbol, bonding_curve, pool_state
                    )

                    if token_info:
                        # Execute buy via normal flow
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] Pump.Fun BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            return True, buy_result.tx_signature, "pump_fun", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.warning(f"[WARN] Pump.Fun buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] Pump.Fun bonding curve migrated or unavailable for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] Pump.Fun check failed: {e}")

        # LETSBONK - Check always for whale_all_platforms or if current platform
        should_check_letsbonk = (self.platform == Platform.LETS_BONK or 
                                  getattr(self, 'enable_whale_copy', False))
        if should_check_letsbonk:
            logger.info(f"[CHECK] [1/4] Checking LetsBonk bonding curve for {symbol}...")
            try:
                from platforms.letsbonk.address_provider import LetsBonkAddressProvider
                from platforms.letsbonk.curve_manager import LetsBonkCurveManager

                address_provider = LetsBonkAddressProvider()
                pool_address = address_provider.derive_pool_address(mint)

                # Use LetsBonk-specific curve manager for proper parsing
                letsbonk_curve_manager = LetsBonkCurveManager(self.solana_client)
                pool_state = await letsbonk_curve_manager.get_pool_state(pool_address)

                if pool_state and not pool_state.get("complete", False) and pool_state.get("status") != "migrated":
                    # Bonding curve available! Use normal letsbonk buy
                    logger.info(f"[OK] LetsBonk bonding curve available for {symbol}")

                    # Create TokenInfo for letsbonk buy
                    token_info = await self._create_letsbonk_token_info_from_mint(
                        mint_str, symbol, pool_address, pool_state
                    )

                    if token_info:
                        # Execute buy via normal flow
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] LetsBonk BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            return True, buy_result.tx_signature, "lets_bonk", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.warning(f"[WARN] LetsBonk buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] LetsBonk bonding curve migrated or unavailable for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] LetsBonk check failed: {e}")

        # BAGS - Check always for whale_all_platforms or if current platform
        should_check_bags = (self.platform == Platform.BAGS or 
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

                    # Create TokenInfo for bags buy
                    token_info = await self._create_bags_token_info_from_mint(
                        mint_str, symbol, pool_address, pool_state
                    )

                    if token_info:
                        # Execute buy via normal flow
                        buy_result = await self.buyer.execute(token_info)

                        if buy_result.success:
                            logger.warning(f"[OK] BAGS BUY SUCCESS: {symbol} - {buy_result.tx_signature}")
                            return True, buy_result.tx_signature, "bags", buy_result.amount or 0, buy_result.price or 0
                        else:
                            logger.warning(f"[WARN] BAGS buy failed: {buy_result.error_message}")
                else:
                    logger.info(f"[WARN] BAGS pool migrated or unavailable for {symbol}")

            except Exception as e:
                logger.info(f"[WARN] BAGS check failed: {e}")

        # ============================================
        # [2/4] TRY PUMPSWAP (for migrated tokens)
        # ============================================
        logger.info(f"[CHECK] [2/4] Trying PumpSwap for {symbol}...")

        try:
            fallback = FallbackSeller(
                client=self.solana_client,
                wallet=self.wallet,
                slippage=self.buy_slippage,
                priority_fee=self.priority_fee_manager.fixed_fee,
                max_retries=self.max_retries,
                jupiter_api_key=self.jupiter_api_key,
            )

            success, sig, error, token_amount, price = await fallback.buy_via_pumpswap(
                mint=mint,
                sol_amount=sol_amount,
                symbol=symbol,
            )

            if success:
                logger.warning(f"[OK] PumpSwap BUY SUCCESS: {symbol} - {sig}")
                return True, sig, "pumpswap", token_amount, price
            else:
                logger.info(f"[WARN] PumpSwap failed: {error}")

        except Exception as e:
            logger.info(f"[WARN] PumpSwap error: {e}")

        # ============================================
        # [3/4] TRY JUPITER (universal fallback)
        # ============================================
        logger.info(f"[CHECK] [3/4] Trying Jupiter aggregator for {symbol}...")

        try:
            fallback = FallbackSeller(
                client=self.solana_client,
                wallet=self.wallet,
                slippage=self.buy_slippage,
                priority_fee=self.priority_fee_manager.fixed_fee,
                max_retries=self.max_retries,
                jupiter_api_key=self.jupiter_api_key,
            )

            success, sig, error = await fallback.buy_via_jupiter(
                mint=mint,
                sol_amount=sol_amount,
                symbol=symbol,
            )

            if success:
                # Jupiter doesn't return exact amounts, estimate from SOL spent
                estimated_price = sol_amount / 1000000  # Rough estimate
                estimated_tokens = sol_amount / estimated_price if estimated_price > 0 else 0
                logger.warning(f"[OK] Jupiter BUY SUCCESS: {symbol} - {sig}")
                return True, sig, "jupiter", estimated_tokens, estimated_price
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
                bc_exists = await self.client.check_account_exists(bc)
                if not bc_exists:
                    logger.warning(f"[VOLUME] {analysis.symbol} not on pump_fun (graduated/raydium), skipping")
                    return

            buy_success = await self._handle_token(token_info, skip_checks=False)

            # Mark as BOUGHT only if purchase was successful
            if buy_success:
                self._bought_tokens.add(mint_str)
                add_to_purchase_history(
                    mint=mint_str,
                    symbol=analysis.symbol,
                    bot_name="volume_analyzer",
                    platform=self.platform.value,
                    price=0,
                    amount=0,
                )

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
                from trading.fallback_seller import FallbackSeller

                logger.info(f"[TRENDING] {token.symbol} is migrated, attempting PumpSwap buy...")
                logger.info(f"[TRENDING] DexScreener info: dex_id={token.dex_id}, pair_address={token.pair_address}")

                fallback = FallbackSeller(
                    client=self.solana_client,
                    wallet=self.wallet,
                    slippage=self.buy_slippage,
                    priority_fee=self.priority_fee_manager.fixed_fee,
                    max_retries=self.max_retries,
                    jupiter_api_key=self.jupiter_api_key,
                )

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

                success, sig, error, token_amount, price = await fallback.buy_via_pumpswap(
                    mint=mint,
                    sol_amount=self.buy_amount,
                    symbol=token.symbol,
                    market_address=market_address,
                )

                if success:
                    logger.warning(f"[OK] TRENDING PumpSwap BUY: {token.symbol} - {sig}")
                    logger.info(f"[OK] Got {token_amount:,.2f} tokens at price {price:.10f} SOL")
                    # Save position with REAL price and quantity
                    position = Position(
                        mint=mint,
                        symbol=token.symbol,
                        entry_price=price,  # REAL price from pool
                        quantity=token_amount,  # REAL token amount
                        entry_time=datetime.utcnow(),
                        platform=self.platform.value,
                    )
                    self.active_positions.append(position)
                    save_positions(self.active_positions)
                    # Mark as BOUGHT (completed) - NEVER buy again!
                    self._bought_tokens.add(mint_str)
                    add_to_purchase_history(
                        mint=mint_str,
                        symbol=token.symbol,
                        bot_name="trending_scanner",
                        platform="pumpswap",
                        price=price,
                        amount=token_amount,
                    )
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
            if buy_success:
                self._bought_tokens.add(mint_str)
                add_to_purchase_history(
                    mint=mint_str,
                    symbol=token.symbol,
                    bot_name="trending_scanner",
                    platform=token.dex_id or "unknown",
                    price=0,
                    amount=0,
                )

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
            if self.whale_tracker:
                logger.warning("[WHALE] Starting whale tracker in background...")
                whale_task = asyncio.create_task(self.whale_tracker.start())
            else:
                if self.enable_whale_copy:
                    logger.error("[WHALE] Whale copy enabled but tracker not initialized!")
                else:
                    logger.info("Whale tracker not enabled, skipping...")

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
                        while True:
                            if self._critical_low_balance:
                                logger.error("🛑 Bot stopped due to critical low balance (≤ 0.02 SOL)")
                                logger.error("🛑 Please top up your wallet and restart the bot.")
                                break
                            await asyncio.sleep(60)
                except Exception:
                    logger.exception("Token listening stopped due to error")
                finally:
                    processor_task.cancel()
                    if whale_task:
                        whale_task.cancel()
                        if self.whale_tracker:
                            await self.whale_tracker.stop()
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
                logger.error("🛑 Bot stopped due to critical low balance (≤ 0.02 SOL)")
                logger.error("🛑 Please top up your wallet and restart the bot.")
                break

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
                    if buy_success:
                        self._bought_tokens.add(token_key)
                        add_to_purchase_history(
                            mint=token_key,
                            symbol=token_info.symbol,
                            bot_name="sniper",
                            platform=self.platform.value,
                            price=0,
                            amount=0,
                        )
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

    async def _check_balance_before_buy(self) -> bool:
        """Check if wallet has enough SOL to continue trading.

        Returns:
            True if balance is sufficient, False if bot should stop buying.

        CRITICAL STOP: If balance <= 0.02 SOL, sets self._critical_low_balance = True
        which signals the bot to stop completely (not just skip buys).
        """
        try:
            client = await self.solana_client.get_client()
            balance_resp = await client.get_balance(self.wallet.pubkey)
            balance_sol = balance_resp.value / 1_000_000_000  # LAMPORTS_PER_SOL

            # CRITICAL BALANCE CHECK: Stop bot completely if balance <= 0.02 SOL
            # This applies to ALL platforms: PUMP, BONK, BAGS
            CRITICAL_BALANCE_THRESHOLD = 0.02
            if balance_sol <= CRITICAL_BALANCE_THRESHOLD:
                logger.error("=" * 70)
                logger.error(f"🛑 CRITICAL LOW BALANCE: {balance_sol:.4f} SOL <= {CRITICAL_BALANCE_THRESHOLD} SOL")
                logger.error("🛑 BOT STOPPING - Not enough SOL for gas fees!")
                logger.error("🛑 Please top up your wallet to continue trading.")
                logger.error("=" * 70)
                # Set flag to stop the bot completely
                self._critical_low_balance = True
                return False

            # Simple balance check: don't buy if balance < MIN_BALANCE_FOR_BUY
            # TSL/SL/TP will still work regardless of balance
            MIN_BALANCE_FOR_BUY = 0.03

            if balance_sol < MIN_BALANCE_FOR_BUY:
                logger.warning(
                    f"[BALANCE] LOW BALANCE: {balance_sol:.4f} SOL < {MIN_BALANCE_FOR_BUY} SOL minimum"
                )
                logger.warning("⛔ Skipping buy - need at least 0.03 SOL to buy new tokens")
                return False

            logger.debug(f"Balance OK: {balance_sol:.4f} SOL")
            return True

        except Exception as e:
            logger.warning(f"Failed to check balance: {e} - proceeding anyway")
            return True  # Don't block on balance check errors

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
        )

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

        # Monitor position in parallel (don't block)
        logger.warning(f"[MONITOR] Starting async monitor for {token_info.symbol}")
        asyncio.create_task(self._monitor_position_wrapper(token_info, position))

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
        check_count = 0

        # HARD STOP LOSS - ЖЁСТКИЙ стоп-лосс, продаём НЕМЕДЛЕННО при любом убытке > порога
        # Это ДОПОЛНИТЕЛЬНАЯ защита поверх обычного stop_loss_price
        HARD_STOP_LOSS_PCT = 20.0  # 25% убыток = НЕМЕДЛЕННАЯ продажа (жёстче чем обычный SL)
        EMERGENCY_STOP_LOSS_PCT = 40.0  # 40% убыток = ЭКСТРЕННАЯ продажа с максимальным приоритетом

        # Счётчик неудачных попыток продажи для агрессивного retry
        sell_retry_count = 0

        # CRITICAL: Track total monitor iterations to detect stuck loops
        max_iterations = 36000  # Max 24 hours of 1-second checks
        total_iterations = 0
        MAX_SELL_RETRIES = 5
        pending_stop_loss = False  # Флаг что нужно продать по SL

        while position.is_active:
            total_iterations += 1
            check_count += 1

            # Safety check: prevent infinite loops
            await asyncio.sleep(0.1)  # 100ms sleep между итерациями
            if total_iterations > max_iterations:
                logger.error(
                    f"[CRITICAL] Monitor exceeded {max_iterations} iterations for {token_info.symbol}! "
                    f"Forcing emergency sell..."
                )
                await self._emergency_fallback_sell(token_info, position, last_known_price)
                break

            try:
                # Get current price from pool/curve (works for all platforms)
                current_price = await curve_manager.calculate_price(pool_address)

                # Если есть pending stop loss - сразу пытаемся продать снова
                if pending_stop_loss:
                    logger.warning(
                        f"[RETRY SL] Pending stop loss for {token_info.symbol}, "
                        f"retry #{sell_retry_count}, current price: {current_price:.10f}"
                    )
                # Reset error counter on successful price fetch
                consecutive_price_errors = 0
                last_known_price = current_price

                # Calculate current PnL FIRST (needed for all checks)
                pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100

                # ============================================
                # STOP LOSS CHECKS - ORDER MATTERS!
                # 1. Config SL (from position.stop_loss_price) - checked in should_exit
                # 2. HARD SL (25%) - backup protection
                # 3. EMERGENCY SL (40%) - last resort
                # ============================================

                # Check if position should be exited (includes config SL check!)
                # UPDATE: Call update_price() for TSL (Trailing Stop-Loss) support
                position.update_price(current_price)
                should_exit, exit_reason = position.should_exit(current_price)

                # ============================================
                # CRITICAL: Log when approaching SL threshold
                # ============================================
                if position.stop_loss_price and current_price <= position.stop_loss_price * 1.1:
                    # Within 10% of SL - log warning
                    logger.warning(
                        f"[SL WARNING] {token_info.symbol}: Price {current_price:.10f} approaching "
                        f"SL {position.stop_loss_price:.10f} (PnL: {pnl_pct:+.2f}%)"
                    )

                # If config SL triggered - mark as pending
                if should_exit and exit_reason == ExitReason.STOP_LOSS:
                    logger.error(
                        f"[CONFIG SL] {token_info.symbol}: STOP LOSS TRIGGERED! "
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
                # Проверка 1: Обычный HARD STOP LOSS (25%)
                if pnl_pct <= -HARD_STOP_LOSS_PCT:
                    logger.error(
                        f"[HARD SL] {token_info.symbol}: LOSS {pnl_pct:.1f}%! "
                        f"HARD STOP LOSS triggered (threshold: -{HARD_STOP_LOSS_PCT:.0f}%)"
                    )
                    should_exit = True
                    exit_reason = ExitReason.STOP_LOSS
                    pending_stop_loss = True

                # Проверка 2: EMERGENCY STOP LOSS (40%) - максимальный приоритет
                if pnl_pct <= -EMERGENCY_STOP_LOSS_PCT:
                    logger.error(
                        f"[EMERGENCY] {token_info.symbol}: CATASTROPHIC LOSS {pnl_pct:.1f}%! "
                        f"EMERGENCY sell triggered (threshold: -{EMERGENCY_STOP_LOSS_PCT:.0f}%)"
                    )
                    should_exit = True
                    exit_reason = ExitReason.STOP_LOSS
                    pending_stop_loss = True

                # Log status EVERY check when in loss, or every 10 checks otherwise
                if pnl_pct < 0 or check_count % 10 == 1:
                    log_level = logger.error if pnl_pct < -20 else (logger.warning if pnl_pct < 0 else logger.info)
                    log_level(
                        f"[MONITOR] {token_info.symbol}: {current_price:.10f} SOL "
                        f"({pnl_pct:+.2f}%) | TP: {(position.take_profit_price or 0):.10f} | "
                        f"SL: {(position.stop_loss_price or 0):.10f} | "
                        f"HARD_SL: -{HARD_STOP_LOSS_PCT:.0f}%"
                    )

                if should_exit and exit_reason:
                    logger.warning(f"[EXIT] Exit condition met: {exit_reason.value}")
                    logger.warning(f"[EXIT] Current price: {current_price:.10f} SOL, PnL: {pnl_pct:+.2f}%")

                    # Log PnL before exit
                    pnl = position.get_pnl(current_price)
                    logger.info(
                        f"[EXIT] Position PnL: {pnl['price_change_pct']:.2f}% ({pnl['unrealized_pnl_sol']:.6f} SOL)"
                    )

                    # Handle exit strategies based on exit_reason
                    if exit_reason == ExitReason.TAKE_PROFIT and self.moon_bag_percentage > 0:
                        # Take Profit with moon bag
                        sell_quantity = position.quantity * (1 - self.moon_bag_percentage / 100)
                        logger.info(f"[MOON] TP reached! Selling {100 - self.moon_bag_percentage:.0f}%, keeping {self.moon_bag_percentage:.0f}% moon bag 🌙")
                    elif exit_reason == ExitReason.TRAILING_STOP:
                        # TSL - продаём только часть позиции (tsl_sell_pct)
                        sell_quantity = position.quantity * position.tsl_sell_pct
                        remaining_pct = (1 - position.tsl_sell_pct) * 100
                        logger.warning(
                            f"[TSL] Partial sell: {position.tsl_sell_pct*100:.0f}% of position. "
                            f"Keeping {remaining_pct:.0f}% for potential further gains!"
                        )
                        # После частичной продажи - деактивировать TSL, обновить quantity
                        # TSL может снова активироваться если цена пойдёт вверх
                        position.tsl_active = False
                        position.high_water_mark = current_price
                    elif exit_reason == ExitReason.STOP_LOSS:
                        # STOP LOSS - продаём ВСЁ, никаких moon bags!
                        sell_quantity = position.quantity
                        logger.warning("[SL] STOP LOSS - selling 100% of position, NO moon bag!")
                    else:
                        # MAX_HOLD_TIME или другие причины - продаём всё
                        sell_quantity = position.quantity

                    # ============================================
                    # AGGRESSIVE SELL RETRY для STOP LOSS
                    # ============================================
                    sell_success = False
                    
                    # AGGRESSIVE MODE: При убытке >= 20% - минимум ретраев, без пауз
                    is_emergency_sell = pnl_pct <= -HARD_STOP_LOSS_PCT
                    max_retries = 2 if is_emergency_sell else MAX_SELL_RETRIES
                    retry_delay = 0.0 if is_emergency_sell else 0.5
                    
                    if is_emergency_sell:
                        logger.error(f"[EMERGENCY SELL] {token_info.symbol}: PnL {pnl_pct:.1f}% - AGGRESSIVE MODE (max {max_retries} retries, no delay)")
                    
                    for sell_attempt in range(1, max_retries + 1):
                        logger.warning(f"[SELL] Attempt {sell_attempt}/{max_retries} for {token_info.symbol}")

                        # Execute sell with position quantity and entry price
                        sell_result = await self.seller.execute(
                            token_info,
                            token_amount=sell_quantity,
                            token_price=position.entry_price,
                        )

                        if sell_result.success:
                            sell_success = True
                            # Close position with actual exit price
                            position.close_position(sell_result.price, exit_reason)

                            logger.warning(
                                f"[OK] Successfully exited position: {exit_reason.value}"
                            )
                            self._log_trade(
                                "sell",
                                token_info,
                                sell_result.price,
                                sell_result.amount,
                                sell_result.tx_signature,
                            )

                            # Log final PnL
                            final_pnl = position.get_pnl(sell_result.price)
                            logger.info(
                                f"[FINAL] PnL: {final_pnl['price_change_pct']:.2f}% ({final_pnl['unrealized_pnl_sol']:.6f} SOL)"
                            )

                            # Remove position from saved file
                            self._remove_position(str(token_info.mint))

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
                            break
                        else:
                            logger.error(
                                f"[FAIL] Sell attempt {sell_attempt} failed: {sell_result.error_message}"
                            )
                            # Короткая пауза перед retry (0 при emergency)
                            if sell_attempt < max_retries and retry_delay > 0:
                                await asyncio.sleep(retry_delay)

                    # Если все попытки обычной продажи не удались - FALLBACK
                    if not sell_success:
                        logger.error(
                            f"[CRITICAL] All {max_retries} sell attempts failed! Trying FALLBACK..."
                        )
                        fallback_success = await self._emergency_fallback_sell(
                            token_info, position, current_price
                        )
                        if fallback_success:
                            logger.info("[OK] Fallback sell successful")
                            sell_success = True
                        else:
                            # КРИТИЧЕСКАЯ СИТУАЦИЯ - не можем продать!
                            # Продолжаем мониторинг и пробуем снова на следующей итерации
                            logger.error(
                                f"[CRITICAL] FALLBACK ALSO FAILED for {token_info.symbol}! "
                                f"Will retry on next price check. Position still open!"
                            )
                            pending_stop_loss = True
                            sell_retry_count += 1

                            # Если слишком много неудачных попыток - уменьшаем интервал проверки
                            if sell_retry_count >= 3:
                                logger.error(
                                    f"[CRITICAL] {sell_retry_count} failed sell cycles! "
                                    f"Reducing check interval to 1 second"
                                )
                            await asyncio.sleep(1)  # Быстрый retry
                            continue  # НЕ выходим из цикла, пробуем снова!

                    if sell_success:
                        break  # Успешно продали - выходим из цикла мониторинга

                # Wait before next price check
                await asyncio.sleep(self.price_check_interval)

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
                            position.update_price(current_price)
                            should_exit, exit_reason = position.should_exit(current_price)
                            
                            logger.info(
                                f"[MONITOR-DEX] {token_info.symbol}: {current_price:.10f} SOL "
                                f"({pnl_pct:+.2f}%) | SL: {(position.stop_loss_price or 0):.10f}"
                            )
                            
                            # Check hard SL
                            if pnl_pct <= -HARD_STOP_LOSS_PCT:
                                logger.error(f"[HARD SL] {token_info.symbol}: {pnl_pct:.1f}% - SELLING!")
                                should_exit = True
                                exit_reason = ExitReason.STOP_LOSS
                            
                            if should_exit and exit_reason:
                                # Proceed with sell logic (same as above)
                                fallback_success = await self._emergency_fallback_sell(
                                    token_info, position, current_price
                                )
                                if fallback_success:
                                    break
                            
                            # Continue monitoring with DexScreener price
                            await asyncio.sleep(self.price_check_interval)
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
                    if pnl_pct_estimate <= -HARD_STOP_LOSS_PCT:
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

                await asyncio.sleep(self.price_check_interval)

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
        from trading.fallback_seller import FallbackSeller
        from trading.position import ExitReason

        logger.warning(
            f"[EMERGENCY] Starting fallback sell for {token_info.symbol} "
            f"({position.quantity:.2f} tokens)"
        )

        try:
            # Create fallback seller
            fallback_seller = FallbackSeller(
                client=self.solana_client,
                wallet=self.wallet,
                slippage=self.sell_slippage,
                priority_fee=10000,  # Higher priority for emergency sell
                max_retries=3,
                jupiter_api_key=self.jupiter_api_key,
            )

            # Try to sell via PumpSwap first, then Jupiter
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
        self.active_positions.append(position)
        save_positions(self.active_positions)

    def _remove_position(self, mint: str) -> None:
        """Remove position from active list and file."""
        self.active_positions = [p for p in self.active_positions if str(p.mint) != mint]
        save_positions(self.active_positions)

    async def _restore_positions(self) -> None:
        """Restore and resume monitoring of saved positions on startup."""
        logger.info("[RESTORE] Checking for saved positions to restore...")
        positions = load_positions()

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

            # Only restore positions for our platform
            if position.platform != self.platform.value:
                logger.info(f"[RESTORE] Skipping position {position.symbol} - different platform ({position.platform} != {self.platform.value})")
                continue

            if not position.is_active:
                logger.info(f"[RESTORE] Skipping closed position {position.symbol}")
                continue

            logger.info(f"[RESTORE] Restoring position: {position.symbol} on {position.platform}")
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
            asyncio.create_task(self._monitor_position_until_exit(token_info, position))


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility
