"""
CRITICAL: Restore and monitor old positions immediately on startup
"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from trading.position import load_positions, Position, ExitReason
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

async def restore_and_monitor_positions(
    universal_trader,
    check_interval: int = 5,  # Check every 5 seconds
):
    """
    CRITICAL: Restore all positions from file and monitor them immediately.

    This runs BEFORE listening for new tokens.
    """
    logger.warning("=" * 70)
    logger.warning("[RESTORE MONITOR] Starting old position recovery and monitoring...")
    logger.warning("=" * 70)

    positions = load_positions()

    if not positions:
        logger.info("[RESTORE MONITOR] No old positions to restore")
        return

    logger.warning(f"[RESTORE MONITOR] Found {len(positions)} old positions to monitor:")
    for pos in positions:
        logger.warning(f"  - {pos.symbol}: {pos.quantity:.2f} tokens @ {pos.entry_price:.10f} SOL")
        logger.warning(f"    SL: {pos.stop_loss_price:.10f} SOL, TSL: {pos.tsl_enabled}")

    # Start monitoring all positions
    monitor_tasks = []
    for position in positions:
        if not position.is_active:
            logger.info(f"[RESTORE MONITOR] Skipping closed position: {position.symbol}")
            continue

        # Create minimal TokenInfo for monitoring
        from interfaces.core import TokenInfo
        token_info = TokenInfo(
            name=position.symbol,
            symbol=position.symbol,
            uri="",
            mint=position.mint,
            platform=universal_trader.platform,
            user=None,
            creator=None,
            creation_timestamp=int(datetime.utcnow().timestamp()),
        )

        # If we have bonding_curve, set it
        if position.bonding_curve:
            try:
                token_info.bonding_curve = Pubkey.from_string(position.bonding_curve)
            except Exception:
                pass

        logger.warning(f"[RESTORE MONITOR] Starting monitor for {position.symbol} (SL: {position.stop_loss_price:.2e})")

        # FIX S19-1: Ensure all restored positions are in batch price watch
        try:
            from utils.batch_price_service import watch_token
            watch_token(str(position.mint))
            logger.info(f"[RESTORE MONITOR] {position.symbol}: batch price WATCH ensured")
        except Exception as _we:
            logger.warning(f"[RESTORE MONITOR] {position.symbol}: watch_token failed: {_we}")

        # Start monitoring task
        task = asyncio.create_task(
            universal_trader._monitor_position_until_exit(token_info, position)
        )
        monitor_tasks.append(task)

    logger.warning(f"[RESTORE MONITOR] Started {len(monitor_tasks)} monitor tasks")

    # Wait for all monitors (they run until position is closed)
    if monitor_tasks:
        # await asyncio.gather(*monitor_tasks, return_exceptions=True)  # REMOVED - non-blocking
        pass  # Monitors run in background

