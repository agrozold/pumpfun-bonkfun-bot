"""
Position management for take profit/stop loss functionality.
REFACTORED: Single source of truth - positions.json only
Redis is backup only, not read on startup.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from solders.pubkey import Pubkey

from utils.logger import get_logger

logger = get_logger(__name__)

# File to store active positions - SINGLE SOURCE OF TRUTH
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

    # TSL
    tsl_enabled: bool = False
    tsl_activation_pct: float = 0.20
    tsl_trail_pct: float = 0.10
    tsl_sell_pct: float = 0.50
    tsl_active: bool = False
    high_water_mark: float = 0.0
    tsl_trigger_price: float = 0.0

    is_active: bool = True
    exit_reason: ExitReason | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None

    platform: str = "pump_fun"
    bonding_curve: str | None = None

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
            "tsl_sell_pct": self.tsl_sell_pct,
            "is_active": self.is_active,
            "state": self.state,
            "platform": self.platform,
            "bonding_curve": str(self.bonding_curve) if self.bonding_curve else None,
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
            tsl_activation_pct=data.get("tsl_activation_pct", 0.20),
            tsl_trail_pct=data.get("tsl_trail_pct", 0.10),
            tsl_active=data.get("tsl_active", False),
            high_water_mark=data.get("high_water_mark", data["entry_price"]),
            tsl_trigger_price=data.get("tsl_trigger_price", 0.0),
            tsl_sell_pct=data.get("tsl_sell_pct", 0.50),
            is_active=data.get("is_active", True),
            platform=data.get("platform", "pump_fun"),
            bonding_curve=data.get("bonding_curve"),
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
        tsl_activation_pct: float = 0.20,
        tsl_trail_pct: float = 0.10,
        tsl_sell_pct: float = 0.50,
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

    def update_price(self, current_price: float) -> None:
        if not self.is_active or not self.tsl_enabled:
            return

        profit_pct = (current_price - self.entry_price) / self.entry_price

        if not self.tsl_active and profit_pct >= self.tsl_activation_pct:
            self.tsl_active = True
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
            logger.warning(f"[TSL] {self.symbol} ACTIVATED at {current_price:.10f}")

        if self.tsl_active and current_price > self.high_water_mark:
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)

    def should_exit(self, current_price: float) -> tuple[bool, ExitReason | None]:
        if not self.is_active:
            return False, None

        if self.stop_loss_price and current_price <= self.stop_loss_price:
            return True, ExitReason.STOP_LOSS

        if self.tsl_active and current_price <= self.tsl_trigger_price:
            # FILTER: If PnL > +50%, DON'T sell on TSL - position is too good!
            pnl_pct = ((current_price - self.entry_price) / self.entry_price) * 100 if self.entry_price > 0 else 0
            if pnl_pct > 50:
                # Still in big profit, just update TSL trigger higher and continue
                # This prevents selling on DexScreener price glitches
                return False, None
            return True, ExitReason.TRAILING_STOP

        if self.take_profit_price and current_price >= self.take_profit_price:
            return True, ExitReason.TAKE_PROFIT

        if self.max_hold_time:
            elapsed = (datetime.utcnow() - self.entry_time).total_seconds()
            if elapsed >= self.max_hold_time:
                return True, ExitReason.MAX_HOLD_TIME

        return False, None


    def get_pnl(self, current_price: float) -> dict:
        """Calculate PnL for position at current price."""
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


def save_positions(positions: list[Position], filepath: Path = POSITIONS_FILE) -> None:
    """Save positions to file. Redis is backup only."""
    active = [p.to_dict() for p in positions if p.is_active]
    
    try:
        # Write to file - SINGLE SOURCE OF TRUTH
        with open(filepath, 'w') as f:
            json.dump(active, f, indent=2)
        logger.info(f"[SAVE] Saved {len(active)} positions to {filepath}")
        
        # Backup to Redis (non-blocking, errors ignored)
        try:
            import redis
            r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            r.setex("positions:backup", 86400, json.dumps(active))
        except:
            pass  # Redis is optional backup
            
    except Exception as e:
        logger.error(f"[SAVE] Failed to save positions: {e}")


def load_positions(filepath: Path = POSITIONS_FILE) -> list[Position]:
    """Load positions from FILE ONLY. Redis is not used for loading."""
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
    """Remove position by mint."""
    positions = load_positions(filepath)
    positions = [p for p in positions if str(p.mint) != mint]
    save_positions(positions, filepath)
    logger.info(f"[REMOVE] Removed position {mint[:12]}...")


def is_token_in_positions(mint_str: str, filepath: Path = POSITIONS_FILE) -> bool:
    """Check if token is in positions."""
    try:
        if not filepath.exists():
            return False
        with open(filepath, 'r') as f:
            positions = json.load(f)
        return any(p.get("mint") == mint_str and p.get("is_active", True) for p in positions)
    except:
        return False


# Track which positions have active monitors (prevent duplicates)
_active_monitors: set[str] = set()


def register_monitor(mint_str: str) -> bool:
    """Register monitor for position. Returns False if already monitored."""
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
