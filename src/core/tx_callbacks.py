"""
Transaction Verification Callbacks - Handle position management after TX verification.

These callbacks are called by TxVerifier AFTER transaction is confirmed/failed on-chain.
This ensures positions are only added when TX actually succeeded.
"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tx_verifier import PendingTransaction

logger = logging.getLogger(__name__)


async def on_buy_success(tx: "PendingTransaction"):
    """
    Called when BUY transaction is CONFIRMED on-chain.
    
    Actions:
    1. Add to purchase_history (never buy this token again)
    2. Add to positions.json (for TP/SL monitoring)
    3. Add to Redis (for cross-process sync)
    4. Start price monitoring
    """
    from trading.purchase_history import add_to_purchase_history
    from trading.position import Position, save_positions, load_positions
    from utils.batch_price_service import watch_token
    
    mint = tx.mint
    symbol = tx.symbol
    token_amount = tx.token_amount
    price = tx.price
    platform = tx.context.get("platform", "jupiter")
    bot_name = tx.context.get("bot_name", "unknown")
    
    # TSL and position parameters from context
    take_profit_pct = tx.context.get("take_profit_pct", 10000)  # 10000% default
    stop_loss_pct = tx.context.get("stop_loss_pct", 0.2)  # 20% default
    tsl_enabled = tx.context.get("tsl_enabled", True)
    tsl_activation_pct = tx.context.get("tsl_activation_pct", 0.1)
    tsl_trail_pct = tx.context.get("tsl_trail_pct", 0.5)
    tsl_sell_pct = tx.context.get("tsl_sell_pct", 0.9)
    bonding_curve = tx.context.get("bonding_curve", None)
    max_hold_time = tx.context.get("max_hold_time", 0)
    
    logger.warning(f"[TX_CALLBACK] ✅ BUY CONFIRMED: {symbol}")
    logger.info(f"[TX_CALLBACK] Adding to positions: {token_amount:,.2f} tokens @ {price:.10f}")
    
    try:
        # 1. Add to purchase history (CRITICAL - prevents duplicate buys)
        add_to_purchase_history(
            mint=mint,
            symbol=symbol,
            bot_name=bot_name,
            platform=platform,
            price=price,
            amount=token_amount,
        )
        logger.info(f"[TX_CALLBACK] Added to purchase_history")
        
        # 2. Add to positions.json
        positions = load_positions()
        
        # Check if already exists (shouldn't happen but be safe)
        existing = [p for p in positions if str(p.mint) == mint]
        if existing:
            logger.warning(f"[TX_CALLBACK] Position already exists for {symbol}, updating quantity...")
            for p in existing:
                p.quantity += token_amount
            save_positions(positions)
        else:
            # Create new position with full parameters
            from solders.pubkey import Pubkey
            
            # Calculate TP/SL prices
            take_profit_price = price * take_profit_pct if take_profit_pct else None
            stop_loss_price = price * (1 - stop_loss_pct) if stop_loss_pct else None
            
            position = Position(
                mint=Pubkey.from_string(mint),
                symbol=symbol,
                entry_price=price,
                quantity=token_amount,
                entry_time=datetime.utcnow(),
                platform=platform,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                max_hold_time=max_hold_time,
                tsl_enabled=tsl_enabled,
                tsl_activation_pct=tsl_activation_pct,
                tsl_trail_pct=tsl_trail_pct,
                tsl_sell_pct=tsl_sell_pct,
                bonding_curve=bonding_curve,
            )
            positions.append(position)
            save_positions(positions)
            logger.info(f"[TX_CALLBACK] New position saved with TP={take_profit_price:.10f}, SL={stop_loss_price:.10f}")
        
        # 3. Start price monitoring
        watch_token(mint)
        logger.info(f"[TX_CALLBACK] Price monitoring started for {symbol}")
        
        # 4. Add to _bought_tokens set if trader instance available
        # This is handled by the context if needed
        
        logger.warning(f"[TX_CALLBACK] ✅ BUY COMPLETE: {symbol} - {token_amount:,.2f} @ {price:.10f}")
        
    except Exception as e:
        logger.error(f"[TX_CALLBACK] Error in on_buy_success: {e}")
        import traceback
        traceback.print_exc()


async def on_buy_failure(tx: "PendingTransaction"):
    """
    Called when BUY transaction FAILED or timed out.
    
    Actions:
    1. Log error
    2. Do NOT add to positions
    3. Do NOT add to purchase_history (allow retry)
    """
    logger.error(
        f"[TX_CALLBACK] ❌ BUY FAILED: {tx.symbol} - {tx.error_message}\n"
        f"  Signature: {tx.signature}\n"
        f"  Check: https://solscan.io/tx/{tx.signature}"
    )
    
    # Nothing to cleanup - we never added anything
    # Token can be bought again on next signal


async def on_sell_success(tx: "PendingTransaction"):
    """
    Called when SELL transaction is CONFIRMED on-chain.
    
    Actions:
    1. Update/remove position from positions.json
    2. Update Redis
    3. Log success
    """
    from trading.position import load_positions, save_positions, remove_position
    
    mint = tx.mint
    symbol = tx.symbol
    sell_percent = tx.context.get("sell_percent", 100)
    
    logger.warning(f"[TX_CALLBACK] ✅ SELL CONFIRMED: {symbol} ({sell_percent}%)")
    
    try:
        if sell_percent >= 100:
            # Full sell - remove position
            remove_position(mint)
            logger.info(f"[TX_CALLBACK] Position removed for {symbol}")
        else:
            # Partial sell - update quantity
            positions = load_positions()
            for p in positions:
                if str(p.mint) == mint:
                    old_qty = p.quantity
                    p.quantity = p.quantity * (1 - sell_percent / 100)
                    save_positions(positions)
                    logger.info(f"[TX_CALLBACK] Position updated: {old_qty:.2f} → {p.quantity:.2f}")
                    break
        
        logger.warning(f"[TX_CALLBACK] ✅ SELL COMPLETE: {symbol}")
        
    except Exception as e:
        logger.error(f"[TX_CALLBACK] Error in on_sell_success: {e}")


async def on_sell_failure(tx: "PendingTransaction"):
    """
    Called when SELL transaction FAILED or timed out.
    
    Actions:
    1. Log error  
    2. Position remains (will retry on next trigger)
    """
    logger.error(
        f"[TX_CALLBACK] ❌ SELL FAILED: {tx.symbol} - {tx.error_message}\n"
        f"  Signature: {tx.signature}\n"
        f"  Check: https://solscan.io/tx/{tx.signature}\n"
        f"  Position remains active - will retry"
    )
    
    # Position stays - monitor will retry sell
