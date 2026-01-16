import asyncio
import logging
import os
import sys

import multiprocessing
from datetime import datetime
from pathlib import Path

import uvloop

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
# Disable verbose httpx logging
# Disable verbose httpx logging
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpx").propagate = False
logging.getLogger("httpcore").setLevel(logging.CRITICAL)

# Suppress httpx verbose output
import io
class FilteredStream:
    def __init__(self, stream):
        self.stream = stream
    def write(self, msg):
        if "httpx" not in msg.lower() and "http request" not in msg.lower():
            self.stream.write(msg)
    def flush(self):
        self.stream.flush()

sys.stderr = FilteredStream(sys.stderr)

logging.getLogger("httpcore").propagate = False


from config_loader import (
    get_platform_from_config,
    load_bot_config,
    print_config_summary,
    validate_platform_listener_combination,
)
from trading.universal_trader import UniversalTrader
from utils.logger import setup_file_logging


def setup_logging(bot_name: str):
    """Set up logging to file for a specific bot instance."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = log_dir / f"{bot_name}_{timestamp}.log"

    setup_file_logging(str(log_filename))


async def start_bot(config_path: str):
    """Start a trading bot with the configuration from the specified path."""
    cfg = load_bot_config(config_path)
    setup_logging(cfg["name"])
    print_config_summary(cfg)

    # Get and validate platform from configuration
    try:
        platform = get_platform_from_config(cfg)
        logging.info(f"Detected platform: {platform.value}")
    except ValueError as e:
        logging.exception(f"Platform configuration error: {e}")
        return

    # Validate platform support
    try:
        from platforms import platform_factory

        if not platform_factory.registry.is_platform_supported(platform):
            logging.error(
                f"Platform {platform.value} is not supported. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
            )
            return
    except Exception as e:
        logging.exception(f"Could not validate platform support: {e}")
        return

    # Validate listener compatibility
    listener_type = cfg["filters"]["listener_type"]
    if not validate_platform_listener_combination(platform, listener_type):
        from config_loader import get_supported_listeners_for_platform

        supported = get_supported_listeners_for_platform(platform)
        logging.error(
            f"Listener '{listener_type}' is not compatible with platform '{platform.value}'. Supported listeners: {supported}"
        )
        return

    # Initialize universal trader with platform-specific configuration
    try:
        trader = UniversalTrader(
            # Connection settings
            rpc_endpoint=cfg["rpc_endpoint"],
            wss_endpoint=cfg["wss_endpoint"],
            private_key=cfg["private_key"],
            # Platform configuration - pass platform enum directly
            platform=platform,
            # Trade parameters
            buy_amount=cfg["trade"]["buy_amount"],
            buy_slippage=cfg["trade"]["buy_slippage"],
            sell_slippage=cfg["trade"]["sell_slippage"],
            # Balance protection
            min_sol_balance=cfg["trade"].get("min_sol_balance", 0.03),
            # Extreme fast mode settings
            extreme_fast_mode=cfg["trade"].get("extreme_fast_mode", False),
            extreme_fast_token_amount=cfg["trade"].get("extreme_fast_token_amount", 30),
            # Exit strategy configuration
            exit_strategy=cfg["trade"].get("exit_strategy", "time_based"),
            take_profit_percentage=cfg["trade"].get("take_profit_percentage"),
            stop_loss_percentage=cfg["trade"].get("stop_loss_percentage"),
            max_hold_time=cfg["trade"].get("max_hold_time"),
            price_check_interval=cfg["trade"].get("price_check_interval", 10),
            # Listener configuration
            listener_type=cfg["filters"]["listener_type"],
            # Geyser configuration (if applicable)
            geyser_endpoint=cfg.get("geyser", {}).get("endpoint"),
            geyser_api_token=cfg.get("geyser", {}).get("api_token"),
            geyser_auth_type=cfg.get("geyser", {}).get("auth_type", "x-token"),
            # PumpPortal configuration (if applicable)
            pumpportal_url=cfg.get("pumpportal", {}).get(
                "url", "wss://pumpportal.fun/api/data"
            ),
            pumpportal_api_key=cfg.get("pumpportal", {}).get("api_key") or os.getenv("PUMPPORTAL_API_KEY"),
            # Priority fee configuration
            enable_dynamic_priority_fee=cfg.get("priority_fees", {}).get(
                "enable_dynamic", False
            ),
            enable_fixed_priority_fee=cfg.get("priority_fees", {}).get(
                "enable_fixed", True
            ),
            fixed_priority_fee=cfg.get("priority_fees", {}).get("fixed_amount", 500000),
            extra_priority_fee=cfg.get("priority_fees", {}).get(
                "extra_percentage", 0.0
            ),
            hard_cap_prior_fee=cfg.get("priority_fees", {}).get("hard_cap", 500000),
            # Retry and timeout settings
            max_retries=cfg.get("retries", {}).get("max_attempts", 10),
            wait_time_after_creation=cfg.get("retries", {}).get(
                "wait_after_creation", 15
            ),
            wait_time_after_buy=cfg.get("retries", {}).get("wait_after_buy", 15),
            wait_time_before_new_token=cfg.get("retries", {}).get(
                "wait_before_new_token", 15
            ),
            max_token_age=cfg.get("filters", {}).get("max_token_age", 0.001),
            token_wait_timeout=cfg.get("timing", {}).get("token_wait_timeout", 120),
            # Cleanup settings
            cleanup_mode=cfg.get("cleanup", {}).get("mode", "disabled"),
            cleanup_force_close_with_burn=cfg.get("cleanup", {}).get(
                "force_close_with_burn", False
            ),
            cleanup_with_priority_fee=cfg.get("cleanup", {}).get(
                "with_priority_fee", False
            ),
            # Trading filters
            match_string=cfg["filters"].get("match_string"),
            bro_address=cfg["filters"].get("bro_address"),
            marry_mode=cfg["filters"].get("marry_mode", False),
            yolo_mode=cfg["filters"].get("yolo_mode", False),
            # Compute unit configuration
            compute_units=cfg.get("compute_units", {}),
            # Pattern detection configuration
            enable_pattern_detection=cfg.get("pattern_detection", {}).get(
                "enabled", False
            ),
            pattern_volume_spike_threshold=cfg.get("pattern_detection", {}).get(
                "volume_spike_threshold", 3.0
            ),
            pattern_holder_growth_threshold=cfg.get("pattern_detection", {}).get(
                "holder_growth_threshold", 0.5
            ),
            pattern_min_whale_buys=cfg.get("pattern_detection", {}).get(
                "min_whale_buys", 2
            ),
            pattern_min_patterns_to_buy=cfg.get("pattern_detection", {}).get(
                "min_patterns_to_buy", 2
            ),
            pattern_min_signal_strength=cfg.get("pattern_detection", {}).get(
                "min_signal_strength", 0.5
            ),
            pattern_only_mode=cfg.get("pattern_detection", {}).get(
                "pattern_only_mode", False
            ),
            # High Volume Sideways pattern configuration
            pattern_high_volume_buys_1h=cfg.get("pattern_detection", {}).get(
                "high_volume_buys_1h", 300
            ),
            pattern_high_volume_sells_1h=cfg.get("pattern_detection", {}).get(
                "high_volume_sells_1h", 200
            ),
            pattern_high_volume_alt_buys_1h=cfg.get("pattern_detection", {}).get(
                "high_volume_alt_buys_1h", 100
            ),
            pattern_high_volume_alt_max_sells_1h=cfg.get("pattern_detection", {}).get(
                "high_volume_alt_max_sells_1h", 100
            ),
            # EXTREME BUY PRESSURE 5min pattern
            pattern_extreme_buy_min_buys_5m=cfg.get("pattern_detection", {}).get(
                "extreme_buy_pressure_min_buys_5m", 500
            ),
            pattern_extreme_buy_max_sells_5m=cfg.get("pattern_detection", {}).get(
                "extreme_buy_pressure_max_sells_5m", 200
            ),
            # Token scoring configuration
            enable_scoring=cfg.get("scoring", {}).get("enabled", False),
            scoring_min_score=cfg.get("scoring", {}).get("min_score", 70),
            scoring_volume_weight=cfg.get("scoring", {}).get("volume_weight", 30),
            scoring_buy_pressure_weight=cfg.get("scoring", {}).get(
                "buy_pressure_weight", 30
            ),
            scoring_momentum_weight=cfg.get("scoring", {}).get("momentum_weight", 25),
            scoring_liquidity_weight=cfg.get("scoring", {}).get("liquidity_weight", 15),
            # Whale copy trading configuration
            enable_whale_copy=cfg.get("whale_copy", {}).get("enabled", False),
            whale_wallets_file=cfg.get("whale_copy", {}).get(
                "wallets_file", "smart_money_wallets.json"
            ),
            whale_min_buy_amount=cfg.get("whale_copy", {}).get("min_buy_amount", 0.5),
            helius_api_key=cfg.get("whale_copy", {}).get("helius_api_key") or os.getenv("HELIUS_API_KEY"),
            birdeye_api_key=os.getenv("BIRDEYE_API_KEY"),
            jupiter_api_key=os.getenv("JUPITER_API_KEY"),
            # Dev reputation check configuration
            enable_dev_check=cfg.get("dev_check", {}).get("enabled", False),
            dev_max_tokens_created=cfg.get("dev_check", {}).get("max_tokens_created", 50),
            dev_min_account_age_days=cfg.get("dev_check", {}).get("min_account_age_days", 1),
            # Trending scanner configuration
            enable_trending_scanner=cfg.get("trending_scanner", {}).get("enabled", False),
            trending_min_volume_1h=cfg.get("trending_scanner", {}).get("min_volume_1h", 50000),
            trending_min_market_cap=cfg.get("trending_scanner", {}).get("min_market_cap", 10000),
            trending_max_market_cap=cfg.get("trending_scanner", {}).get("max_market_cap", 5000000),
            trending_min_price_change_5m=cfg.get("trending_scanner", {}).get("min_price_change_5m", 5),
            trending_min_price_change_1h=cfg.get("trending_scanner", {}).get("min_price_change_1h", 20),
            trending_min_buy_pressure=cfg.get("trending_scanner", {}).get("min_buy_pressure", 0.65),
            trending_scan_interval=cfg.get("trending_scanner", {}).get("scan_interval", 30),
        )

        await trader.start()

    except Exception as e:
        logging.exception(f"Failed to initialize or start trader: {e}")
        raise


def run_bot_process(config_path):
    asyncio.run(start_bot(config_path))


def run_all_bots():
    """Run all bots defined in YAML files in the 'bots' directory."""
    bot_dir = Path("bots")
    if not bot_dir.exists():
        logging.error(f"Bot directory '{bot_dir}' not found")
        return

    bot_files = list(bot_dir.glob("*.yaml"))
    if not bot_files:
        logging.error(f"No bot configuration files found in '{bot_dir}'")
        return

    logging.info(f"Found {len(bot_files)} bot configuration files")

    processes = []
    skipped_bots = 0

    for file in bot_files:
        try:
            cfg = load_bot_config(str(file))
            bot_name = cfg.get("name", file.stem)

            # Skip bots with enabled=False
            if not cfg.get("enabled", True):
                logging.info(f"Skipping disabled bot '{bot_name}'")
                skipped_bots += 1
                continue

            # Validate platform configuration
            try:
                platform = get_platform_from_config(cfg)

                # Check platform support
                from platforms import platform_factory

                if not platform_factory.registry.is_platform_supported(platform):
                    logging.error(
                        f"Platform {platform.value} is not supported for bot '{bot_name}'. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
                    )
                    skipped_bots += 1
                    continue

                # Validate listener compatibility
                listener_type = cfg["filters"]["listener_type"]
                if not validate_platform_listener_combination(platform, listener_type):
                    from config_loader import get_supported_listeners_for_platform

                    supported = get_supported_listeners_for_platform(platform)
                    logging.error(
                        f"Listener '{listener_type}' is not compatible with platform '{platform.value}' for bot '{bot_name}'. Supported listeners: {supported}"
                    )
                    skipped_bots += 1
                    continue

            except Exception as e:
                logging.exception(
                    f"Invalid platform configuration for bot '{bot_name}': {e}. Skipping..."
                )
                skipped_bots += 1
                continue

            # Start bot in separate process or main process
            if cfg.get("separate_process", False):
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in separate process"
                )
                p = multiprocessing.Process(
                    target=run_bot_process, args=(str(file),), name=f"bot-{bot_name}"
                )
                p.start()
                processes.append(p)
            else:
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in main process"
                )
                asyncio.run(start_bot(str(file)))

        except Exception as e:
            logging.exception(f"Failed to start bot from {file}: {e}")
            skipped_bots += 1

    logging.info(
        f"Started {len(bot_files) - skipped_bots} bots, skipped {skipped_bots} disabled/invalid bots"
    )

    # Wait for all processes to complete
    for p in processes:
        p.join()
        logging.info(f"Process {p.name} completed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Log supported platforms and listeners
    try:
        from platforms import platform_factory

        supported_platforms = platform_factory.get_supported_platforms()
        logging.info(f"Supported platforms: {[p.value for p in supported_platforms]}")

        # Log listener compatibility for each platform
        from config_loader import get_supported_listeners_for_platform

        for platform in supported_platforms:
            listeners = get_supported_listeners_for_platform(platform)
            logging.info(f"Platform {platform.value} supports listeners: {listeners}")

    except Exception as e:
        logging.warning(f"Could not load platform information: {e}")

    run_all_bots()


if __name__ == "__main__":
    main()
