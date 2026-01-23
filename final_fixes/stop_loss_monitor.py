"""
AGGRESSIVE STOP LOSS MONITOR
Checks price EVERY SECOND and executes SL immediately when triggered
"""
import asyncio
import logging
from datetime import datetime
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

class AggressiveStopLossMonitor:
    """Monitor positions and execute stop loss immediately"""
    
    def __init__(self, curve_manager, seller, priority_fee_manager):
        self.curve_manager = curve_manager
        self.seller = seller
        self.priority_fee_manager = priority_fee_manager
        self.monitoring = {}  # mint -> monitoring task
    
    async def monitor_position(self, token_info, position):
        """Monitor a position for SL triggers"""
        mint_str = str(token_info.mint)
        logger.warning(f"[SL MONITOR] START: {token_info.symbol}")
        logger.warning(f"  Entry:  {position.entry_price:.10f} SOL")
        logger.warning(f"  SL:     {position.stop_loss_price:.10f} SOL")
        logger.warning(f"  TP:     {position.take_profit_price:.10f} SOL")
        
        consecutive_errors = 0
        max_errors = 3
        last_price = position.entry_price
        check_count = 0
        
        while position.is_active:
            check_count += 1
            
            try:
                # GET PRICE
                pool_address = token_info.bonding_curve or getattr(token_info, 'pool_state', None)
                if not pool_address:
                    logger.error(f"[SL] No pool address for {token_info.symbol}")
                    break
                
                current_price = await self.curve_manager.calculate_price(pool_address)
                last_price = current_price
                consecutive_errors = 0
                
                # CALCULATE PnL
                pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
                
                # LOG EVERY CHECK IF IN LOSS
                if pnl_pct < 0 or check_count % 10 == 1:
                    logger.info(f"[SL] {token_info.symbol}: {current_price:.10f} SOL ({pnl_pct:+.2f}%)")
                
                # AGGRESSIVE CHECKS
                # 1. CONFIG STOP LOSS
                if position.stop_loss_price and current_price <= position.stop_loss_price:
                    logger.error(f"[SL HIT!!!] {token_info.symbol}: {current_price:.10f} <= {position.stop_loss_price:.10f}")
                    await self._execute_stop_loss(token_info, position, current_price)
                    break
                
                # 2. HARD SL (25% loss)
                if pnl_pct <= -25:
                    logger.error(f"[HARD SL!!!] {token_info.symbol}: Loss {pnl_pct:.1f}%")
                    await self._execute_stop_loss(token_info, position, current_price)
                    break
                
                # 3. TAKE PROFIT
                if position.take_profit_price and current_price >= position.take_profit_price:
                    logger.warning(f"[TP HIT!!!] {token_info.symbol}: {current_price:.10f} >= {position.take_profit_price:.10f}")
                    await self._execute_take_profit(token_info, position, current_price)
                    break
                
                # Check every 1 second
                await asyncio.sleep(1)
                
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[SL ERROR] {token_info.symbol}: {e} (error #{consecutive_errors}/{max_errors})")
                
                # If too many errors, use last known price
                if consecutive_errors >= max_errors:
                    logger.error(f"[SL FALLBACK] Too many errors, using last price {last_price:.10f}")
                    
                    # Check if in critical loss
                    pnl_pct = ((last_price - position.entry_price) / position.entry_price) * 100
                    if pnl_pct <= -25:
                        logger.error(f"[EMERGENCY SL] Loss {pnl_pct:.1f}% - SELLING NOW")
                        await self._execute_stop_loss(token_info, position, last_price)
                        break
                
                await asyncio.sleep(1)
    
    async def _execute_stop_loss(self, token_info, position, current_price):
        """Execute stop loss sell"""
        logger.error(f"[EXECUTE SL] Selling {token_info.symbol}")
        
        sell_result = await self.seller.execute(
            token_info,
            token_amount=position.quantity,
            token_price=position.entry_price,
        )
        
        if sell_result.success:
            logger.warning(f"[SL SUCCESS] Sold {token_info.symbol} at {current_price:.10f}")
            position.is_active = False
            return True
        else:
            logger.error(f"[SL FAIL] Could not sell {token_info.symbol}: {sell_result.error_message}")
            return False
    
    async def _execute_take_profit(self, token_info, position, current_price):
        """Execute take profit sell"""
        logger.warning(f"[EXECUTE TP] Selling {token_info.symbol} at profit")
        
        sell_result = await self.seller.execute(
            token_info,
            token_amount=position.quantity,
            token_price=position.entry_price,
        )
        
        if sell_result.success:
            logger.warning(f"[TP SUCCESS] Sold {token_info.symbol} at {current_price:.10f}")
            position.is_active = False
            return True
        else:
            logger.error(f"[TP FAIL] Could not sell {token_info.symbol}")
            return False
