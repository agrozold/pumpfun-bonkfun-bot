"""
Position management for take profit/stop loss functionality.
UPGRADED: Redis as primary storage, JSON as backup.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

POSITIONS_FILE = Path("positions.json")


class ExitReason(Enum):
    """Reasons for position exit."""
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    MAX_HOLD_TIME = "max_hold_time"
    MANUAL = "manual"


@dataclass
class Position:
    """Represents an active trading position."""

    mint: Pubkey
    symbol: str
    entry_price: float
    quantity: float
    entry_time: datetime

    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    max_hold_time: int | None = None

    tsl_enabled: bool = False
    tsl_activation_pct: float = 0.15
    tsl_trail_pct: float = 0.10
    tsl_sell_pct: float = 1.0
    tp_sell_pct: float = 0.80
    tsl_active: bool = False
    high_water_mark: float = 0.0
    tsl_trigger_price: float = 0.0
    tsl_triggered: bool = False

    is_active: bool = True
    is_moonbag: bool = False
    buy_confirmed: bool = True  # False until BUY TX confirmed on-chain (race condition guard)
    tokens_arrived: bool = True   # False until tokens actually appear on wallet (gRPC ATA subscribe)
    is_selling: bool = False  # Guard against double-sell race condition
    tp_partial_done: bool = False  # True after partial TP sell — prevents re-assign on restore
    restore_time: datetime | None = None  # Set on restore — grace period for TSL
    
    # DCA (Dollar Cost Averaging) fields
    dca_enabled: bool = False
    dca_pending: bool = False  # True если ждём откат для докупки
    dca_trigger_pct: float = 0.20  # Докупаем при -20%
    dca_bought: bool = False  # True после докупки
    dca_first_buy_pct: float = 0.50  # Первая покупка 50%
    original_entry_price: float = 0.0
    whale_wallet: str | None = None
    whale_label: str | None = None  # Цена первой покупки
    entry_price_provisional: bool = False
    entry_price_source: str = "unknown"
    exit_reason: ExitReason | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None

    platform: str = "pump_fun"
    bonding_curve: str | None = None

    # Pool vault addresses for real-time price stream (Phase 4)
    pool_base_vault: str | None = None   # Token vault address
    pool_quote_vault: str | None = None  # SOL vault address
    pool_address: str | None = None      # Pool/market address

    def __post_init__(self):
        if self.high_water_mark == 0.0:
            self.high_water_mark = self.entry_price

    @property
    def state(self) -> str:
        return "open" if self.is_active else "closed"

    def to_dict(self) -> dict:
        return {
            "mint": str(self.mint),
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "max_hold_time": self.max_hold_time,
            "tsl_enabled": self.tsl_enabled,
            "tsl_activation_pct": self.tsl_activation_pct,
            "tsl_trail_pct": self.tsl_trail_pct,
            "tsl_active": self.tsl_active,
            "high_water_mark": self.high_water_mark,
            "tsl_trigger_price": self.tsl_trigger_price,
            "tsl_triggered": self.tsl_triggered,
            "tsl_sell_pct": self.tsl_sell_pct,
        "tp_sell_pct": self.tp_sell_pct,
            "is_active": self.is_active,
            "is_moonbag": self.is_moonbag,
            "buy_confirmed": self.buy_confirmed,
            "tokens_arrived": self.tokens_arrived,
            "tp_partial_done": self.tp_partial_done,
            "dca_enabled": self.dca_enabled,
            "dca_pending": self.dca_pending,
            "dca_trigger_pct": self.dca_trigger_pct,
            "dca_bought": self.dca_bought,
            "dca_first_buy_pct": self.dca_first_buy_pct,
            "original_entry_price": self.original_entry_price,
            "whale_wallet": self.whale_wallet,
            "whale_label": self.whale_label,
            "entry_price_provisional": self.entry_price_provisional,
            "entry_price_source": self.entry_price_source,
            "state": self.state,
            "platform": self.platform,
            "bonding_curve": str(self.bonding_curve) if self.bonding_curve else None,
            "pool_base_vault": self.pool_base_vault,
            "pool_quote_vault": self.pool_quote_vault,
            "pool_address": self.pool_address,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        return cls(
            mint=Pubkey.from_string(data["mint"]),
            symbol=data["symbol"],
            entry_price=data["entry_price"],
            quantity=data["quantity"],
            entry_time=datetime.fromisoformat(data["entry_time"]),
            take_profit_price=data.get("take_profit_price"),
            stop_loss_price=data.get("stop_loss_price"),
            max_hold_time=data.get("max_hold_time"),
            tsl_enabled=data.get("tsl_enabled", False),
            tsl_activation_pct=data.get("tsl_activation_pct", 0.15),
            tsl_trail_pct=data.get("tsl_trail_pct", 0.10),
            tsl_active=data.get("tsl_active", False),
            high_water_mark=data.get("high_water_mark", data["entry_price"]),
            tsl_trigger_price=data.get("tsl_trigger_price", 0.0),
            tsl_triggered=data.get("tsl_triggered", False),
            tsl_sell_pct=data.get("tsl_sell_pct", 1.0),
        tp_sell_pct=data.get("tp_sell_pct", 0.80),
            is_active=data.get("is_active", True),
            is_moonbag=data.get("is_moonbag", False),
            buy_confirmed=data.get("buy_confirmed", True),
            tokens_arrived=data.get("tokens_arrived", True),
            tp_partial_done=data.get("tp_partial_done", False),
            dca_enabled=data.get("dca_enabled", False),
            dca_pending=data.get("dca_pending", False),
            dca_trigger_pct=data.get("dca_trigger_pct", 0.20),
            dca_bought=data.get("dca_bought", False),
            dca_first_buy_pct=data.get("dca_first_buy_pct", 0.50),
            original_entry_price=data.get("original_entry_price", 0.0),
            whale_wallet=data.get("whale_wallet"),
            whale_label=data.get("whale_label"),
            entry_price_provisional=data.get("entry_price_provisional", False),
            entry_price_source=data.get("entry_price_source", "unknown"),
            platform=data.get("platform", "pump_fun"),
            bonding_curve=data.get("bonding_curve"),
            pool_base_vault=data.get("pool_base_vault"),
            pool_quote_vault=data.get("pool_quote_vault"),
            pool_address=data.get("pool_address"),
        )

    @classmethod
    def create_from_buy_result(
        cls,
        mint: Pubkey,
        symbol: str,
        entry_price: float,
        quantity: float,
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        platform: str = "pump_fun",
        bonding_curve: str | None = None,
        tsl_enabled: bool = False,
        tsl_activation_pct: float = 0.15,
        tsl_trail_pct: float = 0.10,
        tsl_sell_pct: float = 1.0,
    ) -> "Position":
        if entry_price <= 0:
            logger.error(f"[POSITION] Invalid entry_price={entry_price}, using 0.0000001")
            entry_price = 0.0000001

        take_profit_price = None
        if take_profit_percentage is not None:
            take_profit_price = entry_price * (1 + take_profit_percentage)

        stop_loss_price = None
        if stop_loss_percentage is not None:
            stop_loss_price = entry_price * (1 - stop_loss_percentage)
            if stop_loss_price <= 0:
                stop_loss_price = entry_price * 0.1

        return cls(
            mint=mint,
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=datetime.utcnow(),
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            max_hold_time=max_hold_time,
            platform=platform,
            bonding_curve=str(bonding_curve) if bonding_curve else None,
            tsl_enabled=tsl_enabled,
            tsl_activation_pct=tsl_activation_pct,
            tsl_trail_pct=tsl_trail_pct,
            tsl_sell_pct=tsl_sell_pct,
            high_water_mark=entry_price,
        )

    def update_price(self, current_price: float) -> bool:
        """Update TSL state. Returns True if state changed (needs save)."""
        if not self.is_active or not self.tsl_enabled:
            return False

        profit_pct = (current_price - self.entry_price) / self.entry_price
        state_changed = False

        # Activate TSL when profit threshold reached (with cooldown)
        _tsl_cooldown = 30  # seconds after buy before TSL can activate
        _age = (datetime.utcnow() - self.entry_time).total_seconds()
        if not self.tsl_active and profit_pct >= self.tsl_activation_pct and _age >= _tsl_cooldown:
            self.tsl_active = True
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
            logger.warning(f"[TSL] {self.symbol} ACTIVATED at {current_price:.10f}, trigger at {self.tsl_trigger_price:.10f}")
            state_changed = True

        # Update HWM if price is higher
        if self.tsl_active and current_price > self.high_water_mark:
            old_hwm = self.high_water_mark
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
            logger.info(f"[TSL] {self.symbol} HWM: {old_hwm:.10f} -> {current_price:.10f}, new trigger: {self.tsl_trigger_price:.10f}")
            state_changed = True
        
        return state_changed

    def should_exit(self, current_price: float) -> tuple[bool, ExitReason | None]:
        if not self.is_active:
            return False, None

        if getattr(self, "is_selling", False):
            return False, None

        # DYNAMIC SL: wider SL in first 30s to survive impact dip from whale buy
        if self.stop_loss_price and not self.is_moonbag:
            _pos_age = (datetime.utcnow() - self.entry_time).total_seconds() if self.entry_time else 999
            if _pos_age < 10.0:
                # First 10s: only trigger at -35% (impact dip zone)
                _dynamic_sl = self.entry_price * (1 - 0.35)
                if current_price <= _dynamic_sl:
                    logger.warning(f"[DYNAMIC SL] {self.symbol}: age={_pos_age:.0f}s < 10s, price hit -35% SL")
                    return True, ExitReason.STOP_LOSS
            elif _pos_age < 30.0:
                # 10-30s: intermediate SL at -25%
                _dynamic_sl = self.entry_price * (1 - 0.25)
                if current_price <= _dynamic_sl:
                    logger.warning(f"[DYNAMIC SL] {self.symbol}: age={_pos_age:.0f}s < 30s, price hit -25% SL")
                    return True, ExitReason.STOP_LOSS
            else:
                # After 30s: normal config SL
                if current_price <= self.stop_loss_price:
                    return True, ExitReason.STOP_LOSS

        if self.tsl_active and (current_price <= self.tsl_trigger_price or self.tsl_triggered):
            # Grace period after restore: update HWM but don't trigger TSL for 15s
            _restore_t = getattr(self, 'restore_time', None)
            if _restore_t and not self.tsl_triggered:
                _since_restore = (datetime.utcnow() - _restore_t).total_seconds()
                if _since_restore < 15.0:
                    logger.info(f"[TSL] {self.symbol} GRACE PERIOD: {_since_restore:.1f}s < 15s after restore — skipping trigger")
                    return False, None
                else:
                    self.restore_time = None  # Grace period expired, clear it
            if not self.tsl_triggered:
                logger.warning(f"[TSL] {self.symbol} TRIGGERED at {current_price:.10f}")
                self.tsl_triggered = True
            else:
                logger.warning(f"[TSL] {self.symbol} RESUMING triggered sell after restart")
            return True, ExitReason.TRAILING_STOP

        if self.take_profit_price and current_price >= self.take_profit_price and not self.is_moonbag:
            return True, ExitReason.TAKE_PROFIT

        if self.max_hold_time:
            elapsed = (datetime.utcnow() - self.entry_time).total_seconds()
            if elapsed >= self.max_hold_time:
                return True, ExitReason.MAX_HOLD_TIME

        return False, None

    def get_pnl(self, current_price: float) -> dict:
        if self.entry_price <= 0:
            return {"price_change_pct": 0.0, "unrealized_pnl_sol": 0.0}
        
        price_change_pct = ((current_price - self.entry_price) / self.entry_price) * 100
        unrealized_pnl_sol = (current_price - self.entry_price) * self.quantity
        
        return {
            "price_change_pct": price_change_pct,
            "unrealized_pnl_sol": unrealized_pnl_sol
        }

    def close_position(self, exit_price: float, exit_reason: ExitReason) -> None:
        self.is_active = False
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_time = datetime.utcnow()


# ==================== REDIS HELPERS ====================

async def _get_redis():
    """Get Redis state manager (lazy import)."""
    try:
        from trading.redis_state import get_redis_state
        return await get_redis_state()
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"[REDIS] get_redis failed: {e}")
        return None


async def save_position_redis(position: Position) -> bool:
    """Save single position to Redis atomically."""
    state = await _get_redis()
    if state and await state.is_connected():
        return await state.save_position(str(position.mint), position.to_dict())
    return False


async def load_positions_redis() -> list[Position]:
    """Load all positions from Redis."""
    state = await _get_redis()
    if state and await state.is_connected():
        data = await state.get_all_positions()
        return [Position.from_dict(d) for d in data]
    return []


async def remove_position_redis(mint: str) -> bool:
    """Remove position from Redis."""
    state = await _get_redis()
    if state and await state.is_connected():
        return await state.remove_position(mint)
    return False


# ==================== SYNC FUNCTIONS (backward compatible) ====================

def save_positions(positions: list[Position], filepath: Path = POSITIONS_FILE) -> None:
    """Save positions to Redis + JSON backup."""
    unique_positions = {}
    for p in positions:
        if p.is_active:
            unique_positions[str(p.mint)] = p
    active = [p.to_dict() for p in unique_positions.values()]
    
    # Always save JSON as backup
    try:
        with open(filepath, 'w') as f:
            json.dump(active, f, indent=2)
        logger.info(f"[SAVE] Saved {len(active)} positions to {filepath}")
    except Exception as e:
        logger.error(f"[SAVE] JSON save failed: {e}")
    
    # Save to Redis (async in background)
    async def _save_redis():
        state = await _get_redis()
        if state and await state.is_connected():
            for mint, pos in unique_positions.items():
                await state.save_position(mint, pos.to_dict())
    
    try:
        loop = asyncio.get_running_loop()
        asyncio.create_task(_save_redis())
    except RuntimeError:
        pass  # No loop, skip Redis


def load_positions(filepath: Path = POSITIONS_FILE) -> list[Position]:
    """Load positions - try Redis first, fallback to JSON."""
    # Try Redis synchronously via new event loop
    try:
        async def _load():
            state = await _get_redis()
            if state and await state.is_connected():
                count = await state.get_positions_count()
                if count > 0:
                    data = await state.get_all_positions()
                    logger.info(f"[LOAD] Loaded {len(data)} positions from Redis")
                    return [Position.from_dict(d) for d in data]
            return None
        
        # Check if loop is running
        try:
            loop = asyncio.get_running_loop()
            # In async context - use JSON fallback
        except RuntimeError:
            # No loop - can use asyncio.run
            result = asyncio.run(_load())
            if result:
                return result
    except Exception as e:
        logger.warning(f"[LOAD] Redis load failed: {e}")
    
    # Fallback to JSON
    if not filepath.exists():
        logger.info("[LOAD] No positions file found")
        return []
    
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        
        if not data:
            return []
        
        positions = [Position.from_dict(p) for p in data if p.get("is_active", True)]
        logger.info(f"[LOAD] Loaded {len(positions)} positions from {filepath}")
        return positions
    except Exception as e:
        logger.error(f"[LOAD] Failed to load positions: {e}")
        return []


def remove_position(mint: str, filepath: Path = POSITIONS_FILE) -> None:
    """Remove position by mint from both Redis and JSON."""
    # Remove from JSON
    try:
        positions = load_positions(filepath)
        positions = [p for p in positions if str(p.mint) != mint]
        save_positions(positions, filepath)
    except Exception as e:
        logger.error(f"[REMOVE] JSON remove failed: {e}")
    
    # Remove from Redis (async)
    async def _remove():
        await remove_position_redis(mint)
    
    try:
        loop = asyncio.get_running_loop()
        asyncio.create_task(_remove())
    except RuntimeError:
        try:
            asyncio.run(_remove())
        except:
            pass
    # Direct Redis cleanup via CLI (fallback)
    try:
        import subprocess
        subprocess.run(["redis-cli", "HDEL", "whale:positions", mint], capture_output=True, timeout=2)
    except:
        pass
    
    logger.info(f"[REMOVE] Removed position {mint[:12]}...")


def is_token_in_positions(mint_str: str, filepath: Path = POSITIONS_FILE) -> bool:
    """Check if token is in positions."""
    # Try Redis first
    async def _check():
        state = await _get_redis()
        if state and await state.is_connected():
            return await state.position_exists(mint_str)
        return None
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            result = asyncio.run(_check())
            if result is not None:
                return result
        except:
            pass
    
    # Fallback to JSON check
    try:
        if not filepath.exists():
            return False
        with open(filepath, 'r') as f:
            positions = json.load(f)
        return any(p.get("mint") == mint_str and p.get("is_active", True) for p in positions)
    except:
        return False


_active_monitors: set[str] = set()


def register_monitor(mint_str: str) -> bool:
    """Register monitor for position."""
    if mint_str in _active_monitors:
        logger.warning(f"[MONITOR] {mint_str[:12]}... already has active monitor!")
        return False
    _active_monitors.add(mint_str)
    logger.info(f"[MONITOR] Registered monitor for {mint_str[:12]}...")
    return True


def unregister_monitor(mint_str: str) -> None:
    """Unregister monitor when position closed."""
    _active_monitors.discard(mint_str)
    logger.info(f"[MONITOR] Unregistered monitor for {mint_str[:12]}...")
