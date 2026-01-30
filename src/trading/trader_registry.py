"""
Global trader registry - allows tx_callbacks to access trader instance
and start position monitors for new purchases.
"""
import asyncio
import logging
from typing import TYPE_CHECKING, Optional, Callable, Any

if TYPE_CHECKING:
    from trading.universal_trader import UniversalTrader

logger = logging.getLogger(__name__)

_trader_instance: Optional["UniversalTrader"] = None
_monitor_callback: Optional[Callable] = None


def register_trader(trader: "UniversalTrader") -> None:
    """Register trader instance for global access."""
    global _trader_instance
    _trader_instance = trader
    logger.info("[REGISTRY] Trader registered for position monitoring")


def get_trader() -> Optional["UniversalTrader"]:
    """Get registered trader instance."""
    return _trader_instance


def unregister_trader() -> None:
    """Unregister trader instance."""
    global _trader_instance
    _trader_instance = None
    logger.info("[REGISTRY] Trader unregistered")


async def start_monitor_for_position(mint: str, symbol: str, position_data: dict) -> bool:
    """
    Start position monitor for newly bought token.
    Called from tx_callback after successful buy.
    
    Returns True if monitor started successfully.
    """
    trader = _trader_instance
    if not trader:
        logger.error(f"[REGISTRY] Cannot start monitor for {symbol} - no trader registered!")
        return False
    
    try:
        from trading.position import Position, load_positions, register_monitor
        from interfaces.core import TokenInfo
        from solders.pubkey import Pubkey
        
        # Load the position we just created
        positions = load_positions()
        position = None
        for p in positions:
            if str(p.mint) == mint:
                position = p
                break
        
        if not position:
            logger.error(f"[REGISTRY] Position not found for {symbol} ({mint[:12]}...)")
            return False
        
        # Check if already monitoring
        if not register_monitor(mint):
            logger.warning(f"[REGISTRY] {symbol} already has a monitor running")
            return True  # Already monitored - OK
        
        # Add to trader's active_positions
        if position not in trader.active_positions:
            trader.active_positions.append(position)
            logger.info(f"[REGISTRY] Added {symbol} to active_positions")
        
        # Create TokenInfo for monitoring
        bonding_curve = None
        if position.bonding_curve:
            bonding_curve = Pubkey.from_string(position.bonding_curve)
        
        token_info = TokenInfo(
            name=symbol,
            symbol=symbol,
            uri="",
            mint=position.mint,
            platform=trader.platform,
            bonding_curve=bonding_curve,
            creator=None,
            creator_vault=None,
        )
        
        # Start monitor task
        logger.warning(f"[REGISTRY] Starting monitor for {symbol} (Entry: {position.entry_price:.10f}, SL: {position.stop_loss_price:.10f})")
        asyncio.create_task(trader._monitor_position_until_exit(token_info, position))
        
        return True
        
    except Exception as e:
        logger.exception(f"[REGISTRY] Failed to start monitor for {symbol}: {e}")
        return False
