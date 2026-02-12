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
    sol_spent = tx.context.get("buy_amount", 0.02)  # SOL spent on this buy

        # [edit:s12] post-buy verify — check actual balance vs Jupiter estimate
# === POST-BUY VERIFY: Check actual balance vs Jupiter estimate ===
    # Jupiter quote can be wildly wrong for tokens with non-standard decimals
    # (e.g. 9 decimals when get_token_decimals() returned 6 → 1000x error)
    try:
        from trading.fallback_seller import _post_buy_verify_balance
        # Get wallet from context, or from trader registry as fallback
        _wallet = tx.context.get("wallet_pubkey", "")
        if not _wallet:
            try:
                from trading.trader_registry import get_trader
                _trader = get_trader()
                if _trader and hasattr(_trader, 'wallet'):
                    _wallet = str(_trader.wallet.pubkey)
            except Exception:
                pass
        verified_tokens, verified_price, actual_decimals = await _post_buy_verify_balance(
            wallet_pubkey=_wallet,
            mint_str=mint,
            expected_tokens=token_amount,
            sol_spent=sol_spent,
            token_decimals_expected=6,  # default assumption
        )
        if abs(verified_tokens - token_amount) / max(token_amount, 1) > 0.1:
            logger.warning(
                f"[TX_CALLBACK] PRICE CORRECTED: {symbol} "
                f"tokens {token_amount:,.2f} -> {verified_tokens:,.2f}, "
                f"price {price:.10f} -> {verified_price:.10f} "
                f"(decimals={actual_decimals})"
            )
            token_amount = verified_tokens
            price = verified_price
    except Exception as verify_err:
        logger.warning(f"[TX_CALLBACK] Post-buy verify failed: {verify_err}")
    
    # TSL and position parameters from context
    take_profit_pct = tx.context.get("take_profit_pct", 1.0)  # 10000% default
    stop_loss_pct = tx.context.get("stop_loss_pct", 0.2)  # 20% default
    tsl_enabled = tx.context.get("tsl_enabled", True)
    tsl_activation_pct = tx.context.get("tsl_activation_pct", 0.4)
    tsl_trail_pct = tx.context.get("tsl_trail_pct", 0.3)
    tsl_sell_pct = tx.context.get("tsl_sell_pct", 0.7)
    tp_sell_pct = tx.context.get("tp_sell_pct", 0.50)  # 50% partial TP
    bonding_curve = tx.context.get("bonding_curve", None)
    whale_wallet = tx.context.get("whale_wallet", None)
    whale_label = tx.context.get("whale_label", None)
    dca_enabled = tx.context.get("dca_enabled", True)
    max_hold_time = tx.context.get("max_hold_time", 0)
    # Phase 4: Pool vault data for real-time price stream
    pool_base_vault = tx.context.get("pool_base_vault", None)
    pool_quote_vault = tx.context.get("pool_quote_vault", None)
    pool_address = tx.context.get("pool_address", None)
    
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
            whale_wallet=whale_wallet,
            whale_label=whale_label,
        )
        logger.info(f"[TX_CALLBACK] Added to purchase_history")
        
        # 2. Add to positions.json
        positions = load_positions()
        
        # Check if already exists (shouldn't happen but be safe)
        existing = [p for p in positions if str(p.mint) == mint]
        # Calculate TP/SL prices (needed for monitor start)
        take_profit_price = price * (1 + take_profit_pct) if take_profit_pct else None
        stop_loss_price = price * (1 - stop_loss_pct) if stop_loss_pct else None

        if existing:
            # IMPORTANT: buy.py already updates entry_price/SL/TP directly in positions.json
            # We only update quantity here. DO NOT recalculate entry_price!
            # buy.py sets entry = latest buy price (not weighted average)
            logger.warning(f"[TX_CALLBACK] Position already exists for {symbol}, skipping (buy.py handles update)")
            save_positions(positions)
        else:
            # Create new position with full parameters
            from solders.pubkey import Pubkey
            
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
                tp_sell_pct=tp_sell_pct,
                bonding_curve=bonding_curve,
                # DCA parameters - enable by default for whale copy
                dca_enabled=dca_enabled,
                dca_pending=dca_enabled,
                dca_trigger_pct=0.25,
                dca_first_buy_pct=0.50,
                original_entry_price=price,
                # Whale info
                whale_wallet=whale_wallet,
                whale_label=whale_label,
                # Phase 4: Pool vault data for real-time price stream
                pool_base_vault=pool_base_vault,
                pool_quote_vault=pool_quote_vault,
                pool_address=pool_address,
            )
            positions.append(position)
            save_positions(positions)
            logger.info(f"[TX_CALLBACK] New position saved with TP={take_profit_price:.10f}, SL={stop_loss_price:.10f}")
        
        # 3. Start price monitoring
        watch_token(mint)
        logger.info(f"[TX_CALLBACK] Price monitoring started for {symbol}")
        
        # 4. Add to _bought_tokens set if trader instance available
        # This is handled by the context if needed
        

        # 5. START POSITION MONITOR (CRITICAL!)
        try:
            from trading.trader_registry import start_monitor_for_position
            monitor_started = await start_monitor_for_position(
                mint=mint,
                symbol=symbol,
                position_data={
                    "entry_price": price,
                    "quantity": token_amount,
                    "take_profit_price": take_profit_price,
                    "stop_loss_price": stop_loss_price,
                    "tsl_enabled": tsl_enabled,
                    "bonding_curve": bonding_curve,
                }
            )
            if monitor_started:
                logger.warning(f"[TX_CALLBACK] ✅ MONITOR STARTED for {symbol}")
            else:
                logger.error(f"[TX_CALLBACK] ⚠️ MONITOR FAILED for {symbol} - manual restart may be needed!")
        except Exception as monitor_err:
            logger.error(f"[TX_CALLBACK] Monitor start error: {monitor_err}")
        # Schedule delayed symbol update (for newly indexed tokens)
        asyncio.create_task(_delayed_symbol_update(mint, symbol, delay=30))
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




async def _delayed_symbol_update(mint: str, current_symbol: str, delay: int = 30):
    """Update symbol from DexScreener after delay (for newly indexed tokens)."""
    import aiohttp
    
    logger.info(f"[SYMBOL_UPDATE] Scheduled for {mint[:16]}... in {delay}s (current: {current_symbol})")
    await asyncio.sleep(delay)

    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        new_symbol = pairs[0].get("baseToken", {}).get("symbol", "")
                        logger.info(f"[SYMBOL_UPDATE] DexScreener: {new_symbol} for {mint[:16]}...")
                        
                        if new_symbol and new_symbol.upper() != current_symbol.upper():
                            from trading.position import load_positions, save_positions
                            positions = load_positions()
                            for p in positions:
                                if str(p.mint) == mint:
                                    logger.warning(f"[SYMBOL_UPDATE] {current_symbol} -> {new_symbol}")
                                    p.symbol = new_symbol
                            save_positions(positions)
                            
                            # Update memory
                            try:
                                from trading.trader_registry import get_trader
                                trader = get_trader()
                                if trader and hasattr(trader, 'active_positions'):
                                    for p in trader.active_positions:
                                        if str(p.mint) == mint:
                                            p.symbol = new_symbol
                                            logger.info(f"[SYMBOL_UPDATE] Memory updated: {new_symbol}")
                            except:
                                pass
    except Exception as e:
        logger.warning(f"[SYMBOL_UPDATE] Error: {e}")
