"""
Universal trading coordinator that works with any platform.
Cleaned up to remove all platform-specific hardcoding.
"""

import asyncio
import json
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
from monitoring.whale_tracker import WhaleTracker, WhaleBuy
from monitoring.dev_reputation import DevReputationChecker
from monitoring.trending_scanner import TrendingScanner, TrendingToken
from platforms import get_platform_implementations
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import Position, save_positions, load_positions, remove_position
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
        # Listener configuration
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        # Trading configuration
        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        # Exit strategy configuration
        exit_strategy: str = "time_based",
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        price_check_interval: int = 10,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        # Retry and timeout settings
        max_retries: int = 5,
        wait_time_after_creation: int = 5,
        wait_time_after_buy: int = 3,
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
        helius_api_key: str | None = None,
        birdeye_api_key: str | None = None,
        # Dev reputation settings
        enable_dev_check: bool = False,
        dev_max_tokens_created: int = 50,
        dev_min_account_age_days: int = 1,
        # Trending scanner settings
        enable_trending_scanner: bool = False,
        trending_min_volume_1h: float = 50000,
        trending_min_market_cap: float = 10000,
        trending_max_market_cap: float = 5000000,
        trending_min_price_change_5m: float = 5,
        trending_min_price_change_1h: float = 20,
        trending_min_buy_pressure: float = 0.65,
        trending_scan_interval: float = 30,
        # Balance protection
        min_sol_balance: float = 0.03,
    ):
        """Initialize the universal trader."""
        # Store endpoints for later use
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        
        # Core components
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
        )

        # Platform setup
        if isinstance(platform, str):
            self.platform = Platform(platform)
        else:
            self.platform = platform

        logger.info(f"Initialized Universal Trader for platform: {self.platform.value}")

        # Validate platform support
        try:
            from platforms import platform_factory

            if not platform_factory.registry.is_platform_supported(self.platform):
                raise ValueError(f"Platform {self.platform.value} is not supported")
        except Exception:
            logger.exception("Platform validation failed")
            raise

        # Pattern detection setup
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
            )
            self.pattern_detector.set_pump_signal_callback(self._on_pump_signal)
            logger.info(
                f"Pattern detection enabled: volume_spike={pattern_volume_spike_threshold}x, "
                f"holder_growth={pattern_holder_growth_threshold * 100}%, "
                f"min_whale_buys={pattern_min_whale_buys}, "
                f"min_signal_strength={pattern_min_signal_strength}, "
                f"pattern_only_mode={pattern_only_mode}"
            )

        # Token scoring setup
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
        self.enable_whale_copy = enable_whale_copy
        self.whale_tracker: WhaleTracker | None = None
        self.helius_api_key = helius_api_key
        
        if enable_whale_copy:
            # Каждый бот слушает whale только для СВОЕЙ платформы
            # Это избегает конфликтов WebSocket подписок между процессами
            self.whale_tracker = WhaleTracker(
                wallets_file=whale_wallets_file,
                min_buy_amount=whale_min_buy_amount,
                helius_api_key=helius_api_key,
                rpc_endpoint=rpc_endpoint,
                wss_endpoint=wss_endpoint,
                time_window_minutes=5.0,  # Only copy buys from last 5 minutes
                platform=self.platform.value,  # Слушаем только свою платформу!
            )
            self.whale_tracker.set_callback(self._on_whale_buy)
            logger.info(
                f"Whale copy trading enabled: wallets_file={whale_wallets_file}, "
                f"min_buy={whale_min_buy_amount} SOL, time_window=5 min, "
                f"platform={self.platform.value}"
            )

        # Dev reputation checker setup
        self.enable_dev_check = enable_dev_check
        self.dev_checker: DevReputationChecker | None = None

        if enable_dev_check:
            self.dev_checker = DevReputationChecker(
                helius_api_key=helius_api_key,
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
        )

        # Initialize the appropriate listener with platform filtering
        self.token_listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            wss_endpoint=wss_endpoint,
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            pumpportal_url=pumpportal_url,
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
        self.price_check_interval = price_check_interval

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

    async def _on_pump_signal(
        self, mint: str, symbol: str, patterns: list, strength: float
    ):
        """Callback when pump pattern is detected - trigger buy if in pattern_only_mode."""
        logger.warning(
            f"🚀 PUMP SIGNAL: {symbol} ({mint[:8]}...) - "
            f"{len(patterns)} patterns, strength: {strength:.2f}"
        )
        self.pump_signals[mint] = patterns
        
        # Check minimum signal strength
        if strength < self.pattern_min_signal_strength:
            logger.info(
                f"⚠️ Signal too weak for {symbol}: {strength:.2f} < {self.pattern_min_signal_strength:.2f} - skipping"
            )
            return
        
        # Cleanup old pending tokens (older than 5 minutes)
        self._cleanup_pending_tokens()
        
        # If pattern_only_mode and we have pending token_info - buy now!
        if self.pattern_only_mode and mint in self.pending_tokens:
            token_info = self.pending_tokens.pop(mint)
            logger.warning(f"🚀 BUYING on STRONG pump signal: {symbol} (strength: {strength:.2f})")
            # Process token with signal (skip_checks=False to still do dev check)
            asyncio.create_task(self._handle_token(token_info, skip_checks=False))

    def _cleanup_pending_tokens(self):
        """Remove pending tokens older than 5 minutes."""
        import time
        now = time.time()
        max_age = 300  # 5 minutes
        
        to_remove = []
        for mint_str in self.pending_tokens:
            if mint_str in self.token_timestamps:
                age = now - self.token_timestamps[mint_str]
                if age > max_age:
                    to_remove.append(mint_str)
        
        for mint_str in to_remove:
            self.pending_tokens.pop(mint_str, None)
            if to_remove:
                logger.debug(f"Cleaned up {len(to_remove)} old pending tokens")

    def _has_pump_signal(self, mint: str) -> bool:
        """Check if token has pump signal."""
        return mint in self.pump_signals and len(self.pump_signals[mint]) > 0

    async def _on_whale_buy(self, whale_buy: WhaleBuy):
        """Callback when whale buys a token - copy the trade.
        
        Whale copy trades bypass scoring and pattern checks,
        but still check for serial scammers (dev check).
        
        Supports ALL platforms: pump.fun and letsbonk.
        Each bot only copies trades from its own platform.
        """
        logger.warning(
            f"🐋 WHALE COPY START: {whale_buy.whale_label} bought {whale_buy.token_symbol} "
            f"for {whale_buy.amount_sol:.2f} SOL on {whale_buy.platform}"
        )
        
        try:
            mint_str = whale_buy.token_mint
            
            # Step 1: Check if already processed
            if mint_str in self.processed_tokens:
                logger.info(f"🐋 Already processed {mint_str[:8]}..., skipping duplicate")
                return
            
            # Step 2: Platform validation (should always match now since each bot
            # listens only to its own platform, but keep as safety check)
            whale_platform = Platform(whale_buy.platform)
            if whale_platform != self.platform:
                logger.warning(
                    f"🐋 Platform mismatch: whale={whale_buy.platform}, bot={self.platform.value} - skipping"
                )
                return
            
            logger.warning(f"🐋 Platform match: {self.platform.value} ✅ - creating TokenInfo...")
            
            # Step 3: Create platform-specific TokenInfo
            if self.platform == Platform.PUMP_FUN:
                token_info = await self._create_pumpfun_token_info(whale_buy)
            elif self.platform == Platform.LETS_BONK:
                token_info = await self._create_letsbonk_token_info(whale_buy)
            else:
                logger.error(f"🐋 Unsupported platform: {self.platform}")
                return
            
            if token_info is None:
                logger.warning(f"🐋 TokenInfo creation failed for {mint_str[:8]}...")
                return  # Token creation failed (migrated, dev check failed, etc.)
            
            logger.warning(f"🐋 TokenInfo created successfully for {token_info.symbol}")
            
            # Step 4: Execute trade
            self.processed_tokens.add(mint_str)
            logger.warning(f"🐋 EXECUTING BUY for {token_info.symbol} ({mint_str[:8]}...) on {self.platform.value}")
            await self._handle_token(token_info, skip_checks=True)
            logger.warning(f"🐋 _handle_token completed for {token_info.symbol}")
            
        except Exception as e:
            logger.exception(f"🐋 WHALE COPY FAILED: {e}")

    async def _create_pumpfun_token_info(self, whale_buy: WhaleBuy) -> TokenInfo | None:
        """Create TokenInfo for pump.fun whale buy.
        
        Args:
            whale_buy: Whale buy information
            
        Returns:
            TokenInfo for pump.fun or None if token is migrated/invalid
        """
        from interfaces.core import TokenInfo
        from platforms.pumpfun.address_provider import PumpFunAddresses
        from core.pubkeys import SystemAddresses
        
        mint_str = whale_buy.token_mint
        logger.info(f"🐋 Creating pump.fun TokenInfo for {mint_str[:8]}...")
        
        try:
            mint = Pubkey.from_string(mint_str)
        except Exception as e:
            logger.error(f"🐋 Invalid mint address: {e}")
            return None
        
        # Derive bonding curve from mint (PDA)
        bonding_curve, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint)],
            PumpFunAddresses.PROGRAM
        )
        logger.info(f"🐋 Bonding curve: {str(bonding_curve)[:8]}...")
        
        # Get pool state
        try:
            curve_manager = self.platform_implementations.curve_manager
            pool_state = await curve_manager.get_pool_state(bonding_curve)
            logger.info(f"🐋 Pool state fetched: complete={pool_state.get('complete', False)}")
        except Exception as e:
            logger.warning(f"🐋 Failed to get pump.fun pool state: {e}")
            return None
        
        if pool_state.get("complete", False):
            logger.warning(f"🐋 Token {whale_buy.token_symbol} has migrated to Raydium, skipping")
            return None
        
        # Extract creator and run dev check
        creator = self._extract_creator(pool_state)
        if not await self._check_dev_reputation(creator, whale_buy.token_symbol):
            return None
        
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
        
        logger.info(f"🐋 TokenInfo ready for {whale_buy.token_symbol}")
        
        return TokenInfo(
            name=whale_buy.token_symbol,
            symbol=whale_buy.token_symbol,
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
            creation_timestamp=int(whale_buy.timestamp.timestamp()),
        )

    async def _create_letsbonk_token_info(self, whale_buy: WhaleBuy) -> TokenInfo | None:
        """Create TokenInfo for letsbonk whale buy.
        
        Args:
            whale_buy: Whale buy information
            
        Returns:
            TokenInfo for letsbonk or None if token is migrated/invalid
        """
        from interfaces.core import TokenInfo
        from platforms.letsbonk.address_provider import (
            LetsBonkAddressProvider,
            LetsBonkAddresses,
        )
        from core.pubkeys import SystemAddresses
        
        mint_str = whale_buy.token_mint
        mint = Pubkey.from_string(mint_str)
        address_provider = LetsBonkAddressProvider()
        
        # Derive pool address
        pool_address = address_provider.derive_pool_address(mint)
        
        # Get pool state
        try:
            curve_manager = self.platform_implementations.curve_manager
            pool_state_data = await curve_manager.get_pool_state(pool_address)
        except Exception as e:
            logger.warning(f"🐋 Failed to get letsbonk pool state: {e}")
            return None
        
        # Check if migrated (letsbonk uses different migration indicator)
        if pool_state_data.get("status") == "migrated" or pool_state_data.get("complete", False):
            logger.warning(f"🐋 Token {whale_buy.token_symbol} has migrated, skipping")
            return None
        
        # Extract creator and run dev check
        creator = self._extract_creator(pool_state_data)
        if not await self._check_dev_reputation(creator, whale_buy.token_symbol):
            return None
        
        # Derive addresses using LetsBonkAddressProvider
        base_vault = address_provider.derive_base_vault(mint)
        quote_vault = address_provider.derive_quote_vault(mint)
        
        # Get global_config and platform_config from pool_state or use defaults
        global_config = pool_state_data.get("global_config") or LetsBonkAddresses.GLOBAL_CONFIG
        platform_config = pool_state_data.get("platform_config") or LetsBonkAddresses.PLATFORM_CONFIG
        
        if isinstance(global_config, str):
            global_config = Pubkey.from_string(global_config)
        if isinstance(platform_config, str):
            platform_config = Pubkey.from_string(platform_config)
        
        token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
        
        return TokenInfo(
            name=whale_buy.token_symbol,
            symbol=whale_buy.token_symbol,
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
            creation_timestamp=int(whale_buy.timestamp.timestamp()),
        )

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
                f"🐋 Dev check: tokens={dev_result.get('tokens_created', -1)}, "
                f"risk={dev_result.get('risk_score', 0)}, safe={dev_result.get('is_safe', True)}"
            )
            if not dev_result.get("is_safe", True):
                logger.warning(
                    f"🐋 Skipping {symbol} - Serial token creator: "
                    f"{dev_result.get('tokens_created', 'unknown')} tokens"
                )
                return False
        except Exception as e:
            logger.warning(f"🐋 Dev check failed for {symbol}: {e}")
            # Continue if dev check fails - better to buy than miss
        
        return True

    async def _on_trending_token(self, token: TrendingToken):
        """Callback when trending scanner finds a hot token."""
        mint_str = token.mint
        
        # Check if already processed
        if mint_str in self.processed_tokens:
            logger.info(f"🔥 Already processed {token.symbol}, skipping")
            return
        
        # Check if already have position in this token
        for pos in self.active_positions:
            if str(pos.mint) == mint_str:
                logger.info(f"🔥 Already have position in {token.symbol}, skipping")
                return
        
        # Check token age - skip if older than 5 minutes
        if token.created_at:
            from datetime import datetime, timezone
            now = datetime.utcnow()
            # Handle both naive and aware datetimes
            created = token.created_at
            if created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            token_age = (now - created).total_seconds()
            if token_age > 300:  # 5 minutes
                logger.info(f"🔥 Token {token.symbol} too old ({token_age:.0f}s), skipping")
                return
        
        logger.warning(
            f"🔥 TRENDING BUY: {token.symbol} - "
            f"MC: ${token.market_cap:,.0f}, Vol: ${token.volume_24h:,.0f}, "
            f"+{token.price_change_1h:.1f}% 1h"
        )
        
        # Только pump.fun поддерживается
        if self.platform != Platform.PUMP_FUN:
            logger.warning(f"🔥 Trending scanner only for pump_fun")
            return
        
        try:
            from interfaces.core import TokenInfo
            from platforms.pumpfun.address_provider import PumpFunAddresses
            from core.pubkeys import SystemAddresses
            
            mint = Pubkey.from_string(mint_str)
            
            # Derive bonding curve
            bonding_curve, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint)],
                PumpFunAddresses.PROGRAM
            )
            
            # Check if migrated
            is_migrated = False
            pool_state = None
            creator = None
            
            try:
                curve_manager = self.platform_implementations.curve_manager
                pool_state = await curve_manager.get_pool_state(bonding_curve)
                if pool_state.get("complete", False):
                    is_migrated = True
                    logger.info(f"🔥 {token.symbol} migrated to Raydium - using Jupiter")
                creator = pool_state.get("creator")
                if creator and isinstance(creator, str):
                    creator = Pubkey.from_string(creator)
                elif not isinstance(creator, Pubkey):
                    creator = None
            except Exception as e:
                # Bonding curve invalid = migrated
                is_migrated = True
                logger.info(f"🔥 {token.symbol} bonding curve unavailable - using Jupiter")
            
            # Mark as processed
            self.processed_tokens.add(mint_str)
            
            # If migrated - buy via PumpSwap (Raydium AMM)
            if is_migrated:
                from trading.fallback_seller import FallbackSeller
                
                logger.info(f"🔥 {token.symbol} is migrated, attempting PumpSwap buy...")
                logger.info(f"🔥 DexScreener info: dex_id={token.dex_id}, pair_address={token.pair_address}")
                
                fallback = FallbackSeller(
                    client=self.solana_client,
                    wallet=self.wallet,
                    slippage=self.buy_slippage,
                    priority_fee=self.priority_fee_manager.fixed_fee,
                    max_retries=self.max_retries,
                )
                
                # Use pair_address from DexScreener if available
                # PumpSwap pools can show as "pumpswap", "raydium", or other dex_id
                market_address = None
                if token.pair_address:
                    try:
                        market_address = Pubkey.from_string(token.pair_address)
                        logger.info(f"🔥 Using DexScreener pair as market: {token.pair_address}")
                    except Exception as e:
                        logger.warning(f"🔥 Invalid pair_address: {e}")
                
                if not market_address:
                    logger.info(f"🔥 No pair_address, will lookup PumpSwap market via RPC")
                
                success, sig, error, token_amount, price = await fallback.buy_via_pumpswap(
                    mint=mint,
                    sol_amount=self.buy_amount,
                    symbol=token.symbol,
                    market_address=market_address,
                )
                
                if success:
                    logger.warning(f"✅ TRENDING PumpSwap BUY: {token.symbol} - {sig}")
                    logger.info(f"✅ Got {token_amount:,.2f} tokens at price {price:.10f} SOL")
                    # Save position with REAL price and quantity
                    position = Position(
                        mint=mint,
                        symbol=token.symbol,
                        entry_price=price,  # ✅ REAL price from pool
                        quantity=token_amount,  # ✅ REAL token amount
                        entry_time=datetime.utcnow(),
                        platform=self.platform.value,
                    )
                    self.active_positions.append(position)
                    save_positions(self.active_positions)
                else:
                    logger.error(f"❌ TRENDING PumpSwap BUY failed: {token.symbol} - {error or 'Unknown error'}")
                return
            
            # Not migrated - use normal flow
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
            
            token_info = TokenInfo(
                name=token.name,
                symbol=token.symbol,
                uri="",
                mint=mint,
                platform=self.platform,
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
            
            # Покупаем! skip_checks=True - trending scanner уже проверил метрики
            await self._handle_token(token_info, skip_checks=True)
            
        except Exception as e:
            logger.exception(f"Failed to buy trending token: {e}")

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
            # Choose operating mode based on yolo_mode
            if not self.yolo_mode:
                # Single token mode: process one token and exit
                logger.info(
                    "Running in single token mode - will process one token and exit"
                )
                token_info = await self._wait_for_token()
                if token_info:
                    await self._handle_token(token_info)
                    logger.info("Finished processing single token. Exiting...")
                else:
                    logger.info(
                        f"No suitable token found within timeout period ({self.token_wait_timeout}s). Exiting..."
                    )
            else:
                # Continuous mode: process tokens until interrupted
                logger.info(
                    "Running in continuous mode - will process tokens until interrupted"
                )
                processor_task = asyncio.create_task(self._process_token_queue())
                
                # Start whale tracker if enabled
                whale_task = None
                if self.whale_tracker:
                    logger.info("Starting whale tracker in background...")
                    whale_task = asyncio.create_task(self.whale_tracker.start())

                # Start trending scanner if enabled
                trending_task = None
                if self.trending_scanner:
                    logger.info("Starting trending scanner in background...")
                    trending_task = asyncio.create_task(self.trending_scanner.start())

                try:
                    await self.token_listener.listen_for_tokens(
                        self._queue_token,
                        self.match_string,
                        self.bro_address,
                    )
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
        """Queue a token for processing if not already processed."""
        token_key = str(token_info.mint)

        if token_key in self.processed_tokens:
            logger.debug(f"Token {token_info.symbol} already processed. Skipping...")
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
            try:
                token_info = await self.token_queue.get()
                token_key = str(token_info.mint)

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
                    continue

                self.processed_tokens.add(token_key)

                logger.info(
                    f"Processing fresh token: {token_info.symbol} (age: {token_age:.1f}s)"
                )
                await self._handle_token(token_info)

            except asyncio.CancelledError:
                logger.info("Token queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in token queue processor")
            finally:
                self.token_queue.task_done()

    async def _handle_token(self, token_info: TokenInfo, skip_checks: bool = False) -> None:
        """Handle a new token creation event.
        
        Args:
            token_info: Token information
            skip_checks: If True, skip scoring and dev checks (used for whale copy trades)
        """
        try:
            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return

            mint_str = str(token_info.mint)

            # Start pattern tracking if enabled
            if self.pattern_detector:
                self.pattern_detector.start_tracking(mint_str, token_info.symbol)

            # Check pattern_only_mode - skip if no pump signal detected (unless skip_checks)
            if not skip_checks and self.pattern_only_mode and not self._has_pump_signal(mint_str):
                logger.info(
                    f"Pattern only mode: skipping {token_info.symbol} - no pump signal detected"
                )
                # Store token_info for later if signal arrives
                self.pending_tokens[mint_str] = token_info
                return

            # Token scoring check (runs in parallel with wait time) - skip if whale copy
            scoring_task = None
            if self.token_scorer and not skip_checks:
                scoring_task = asyncio.create_task(
                    self.token_scorer.should_buy(mint_str, token_info.symbol)
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
                logger.info(f"🐋 WHALE COPY: Buying {self.buy_amount:.6f} SOL worth of {token_info.symbol} (checks skipped)...")

            # Check scoring result if enabled
            if scoring_task:
                try:
                    should_buy, score = await scoring_task
                    logger.info(
                        f"📊 Token score for {token_info.symbol}: {score.total_score}/100 → {score.recommendation}"
                    )
                    if not should_buy:
                        logger.info(
                            f"Skipping {token_info.symbol} - score {score.total_score} below threshold"
                        )
                        return
                except Exception as e:
                    logger.warning(f"Scoring failed, proceeding anyway: {e}")

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
                            f"⚠️ Skipping {token_info.symbol} - {dev_result.get('reason', 'bad dev')}"
                        )
                        return
                except Exception as e:
                    logger.warning(f"Dev check failed, proceeding anyway: {e}")

            # Check wallet balance before buying
            balance_ok = await self._check_balance_before_buy()
            if not balance_ok:
                return

            # Buy token
            if skip_checks:
                logger.warning(
                    f"🐋 WHALE COPY: Executing buy for {token_info.symbol}..."
                )
            else:
                logger.info(
                    f"Buying {self.buy_amount:.6f} SOL worth of {token_info.symbol} on {token_info.platform.value}..."
                )
            
            try:
                logger.info(f"🔧 Calling buyer.execute for {token_info.symbol}...")
                buy_result: TradeResult = await self.buyer.execute(token_info)
                logger.info(
                    f"Buy result: success={buy_result.success}, "
                    f"tx_signature={buy_result.tx_signature}, "
                    f"error_message={buy_result.error_message}"
                )
            except Exception as e:
                logger.exception(f"❌ Buy execution failed with exception: {e}")
                return

            if buy_result.success:
                logger.warning(f"✅ BUY SUCCESS: {token_info.symbol} - {buy_result.tx_signature}")
                await self._handle_successful_buy(token_info, buy_result)
            else:
                logger.error(f"❌ BUY FAILED: {token_info.symbol} - {buy_result.error_message or 'Unknown error'}")
                await self._handle_failed_buy(token_info, buy_result)

            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")

    async def _check_balance_before_buy(self) -> bool:
        """Check if wallet has enough SOL to continue trading.
        
        Returns:
            True if balance is sufficient, False if bot should stop buying.
        """
        try:
            client = await self.solana_client.get_client()
            balance_resp = await client.get_balance(self.wallet.pubkey)
            balance_sol = balance_resp.value / 1_000_000_000  # LAMPORTS_PER_SOL
            
            # Check if we have enough for buy + reserve for sells
            required = self.buy_amount + self.min_sol_balance
            
            if balance_sol < required:
                logger.warning(
                    f"💰 LOW BALANCE: {balance_sol:.4f} SOL < {required:.4f} SOL required "
                    f"(buy: {self.buy_amount}, reserve: {self.min_sol_balance})"
                )
                logger.warning("⛔ Skipping buy to preserve SOL for selling positions")
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
        )

        logger.info(f"Created position: {position}")
        if position.take_profit_price:
            logger.info(f"Take profit target: {position.take_profit_price:.8f} SOL")
        if position.stop_loss_price:
            logger.info(f"Stop loss target: {position.stop_loss_price:.8f} SOL")

        # Save position to file for recovery after restart
        self._save_position(position)

        # Monitor position until exit condition is met
        await self._monitor_position_until_exit(token_info, position)

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
        """Monitor a position until exit conditions are met."""
        logger.info(
            f"Starting position monitoring (check interval: {self.price_check_interval}s)"
        )

        # Get pool address for price monitoring using platform-agnostic method
        pool_address = self._get_pool_address(token_info)
        curve_manager = self.platform_implementations.curve_manager

        while position.is_active:
            try:
                # Get current price from pool/curve
                current_price = await curve_manager.calculate_price(pool_address)

                # Check if position should be exited
                should_exit, exit_reason = position.should_exit(current_price)

                if should_exit and exit_reason:
                    logger.info(f"Exit condition met: {exit_reason.value}")
                    logger.info(f"Current price: {current_price:.8f} SOL")

                    # Log PnL before exit
                    pnl = position.get_pnl(current_price)
                    logger.info(
                        f"Position PnL: {pnl['price_change_pct']:.2f}% ({pnl['unrealized_pnl_sol']:.6f} SOL)"
                    )

                    # Handle moon_bag exit strategy
                    if exit_reason.value == "TAKE_PROFIT" and self.moon_bag_percentage > 0:
                        sell_quantity = position.quantity * (1 - self.moon_bag_percentage / 100)
                        logger.info(f"TP reached! Selling {100 - self.moon_bag_percentage:.0f}%, keeping {self.moon_bag_percentage:.0f}% moon bag 🌙")
                    else:
                        sell_quantity = position.quantity

                    # Execute sell with position quantity and entry price to avoid RPC delays
                    sell_result = await self.seller.execute(
                        token_info,
                        token_amount=sell_quantity,
                        token_price=position.entry_price,
                    )

                    if sell_result.success:
                        # Close position with actual exit price
                        position.close_position(sell_result.price, exit_reason)

                        logger.info(
                            f"Successfully exited position: {exit_reason.value}"
                        )
                        self._log_trade(
                            "sell",
                            token_info,
                            sell_result.price,
                            sell_result.amount,
                            sell_result.tx_signature,
                        )

                        # Log final PnL
                        final_pnl = position.get_pnl()
                        logger.info(
                            f"Final PnL: {final_pnl['price_change_pct']:.2f}% ({final_pnl['unrealized_pnl_sol']:.6f} SOL)"
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
                    else:
                        logger.error(
                            f"Failed to exit position: {sell_result.error_message}"
                        )
                        # Keep monitoring in case sell can be retried

                    break
                else:
                    # Log current status
                    pnl = position.get_pnl(current_price)
                    logger.debug(
                        f"Position status: {current_price:.8f} SOL ({pnl['price_change_pct']:+.2f}%)"
                    )

                # Wait before next price check
                await asyncio.sleep(self.price_check_interval)

            except Exception:
                logger.exception("Error monitoring position")
                await asyncio.sleep(
                    self.price_check_interval
                )  # Continue monitoring despite errors

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
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
    ) -> None:
        """Log trade information."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)

            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "platform": token_info.platform.value,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
            }

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
        positions = load_positions()
        if not positions:
            return

        logger.info(f"Found {len(positions)} saved positions to restore")
        
        for position in positions:
            # Only restore positions for our platform
            if position.platform != self.platform.value:
                logger.info(f"Skipping position {position.symbol} - different platform")
                continue
                
            if not position.is_active:
                logger.info(f"Skipping closed position {position.symbol}")
                continue

            logger.info(f"Restoring position: {position}")
            self.active_positions.append(position)
            
            # Get creator from bonding curve state for proper sell instruction
            creator = None
            creator_vault = None
            bonding_curve = None
            token_migrated = False
            
            if position.bonding_curve:
                bonding_curve = Pubkey.from_string(position.bonding_curve)
                try:
                    # Fetch curve state to get creator
                    curve_manager = self.platform_implementations.curve_manager
                    curve_state = await curve_manager.get_curve_state(bonding_curve)
                    
                    # Check if token migrated to Raydium
                    if curve_state is None:
                        logger.warning(
                            f"⚠️ Position {position.symbol}: bonding curve not found - "
                            "token may have migrated to Raydium. Removing corrupted position."
                        )
                        token_migrated = True
                    elif hasattr(curve_state, "complete") and curve_state.complete:
                        logger.warning(
                            f"⚠️ Position {position.symbol}: token migrated to Raydium. "
                            "Cannot sell via bonding curve - removing position."
                        )
                        token_migrated = True
                    elif hasattr(curve_state, "creator") and curve_state.creator:
                        creator = curve_state.creator
                        # Derive creator vault
                        address_provider = self.platform_implementations.address_provider
                        creator_vault = address_provider.derive_creator_vault(creator)
                        logger.info(f"Got creator {str(creator)[:8]}... from curve state")
                except Exception as e:
                    logger.warning(f"Failed to get creator from curve: {e} - removing position")
                    token_migrated = True
            else:
                logger.warning(f"Position {position.symbol} has no bonding_curve - removing")
                token_migrated = True
            
            # Skip and remove corrupted/migrated positions
            if token_migrated:
                remove_position(position.mint)
                continue
            
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
            
            # Start monitoring in background
            asyncio.create_task(self._monitor_position_until_exit(token_info, position))


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility
