"""
Position management for take profit/stop loss functionality.
UPDATED: Added Trailing Stop-Loss (TSL) support.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from solders.pubkey import Pubkey

from utils.logger import get_logger

# === STATE MACHINE INTEGRATION ===
try:
    from trading.position_state import StateMachine, PositionState
    STATE_MACHINE_AVAILABLE = True
except ImportError:
    STATE_MACHINE_AVAILABLE = False
    StateMachine = None
    PositionState = None
# === END STATE MACHINE ===

# Session 9: Atomic file writes
from utils.safe_file_writer import SafeFileWriter
_positions_writer = SafeFileWriter(backup_count=10, backup_dir="backups/positions", enable_backups=True)

logger = get_logger(__name__)

# File to store active positions
POSITIONS_FILE = Path("positions.json")


class ExitReason(Enum):
    """Reasons for position exit."""

    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"  # NEW: TSL triggered
    MAX_HOLD_TIME = "max_hold_time"
    MANUAL = "manual"


@dataclass
class Position:
    """Represents an active trading position with TSL support."""

    # Token information
    mint: Pubkey
    symbol: str

    # Position details
    entry_price: float
    quantity: float
    entry_time: datetime

    # Exit conditions (static)
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    max_hold_time: int | None = None  # seconds

    # ========================================
    # TRAILING STOP-LOSS (TSL) - NEW!
    # ========================================
    tsl_enabled: bool = False
    tsl_activation_pct: float = 0.20  # Activate TSL after +20% profit
    tsl_trail_pct: float = 0.10  # Trail 10% below high water mark
    tsl_sell_pct: float = 0.50  # Sell 50% of position when TSL triggers

    # TSL State
    tsl_active: bool = False
    high_water_mark: float = 0.0  # Highest price since entry
    tsl_trigger_price: float = 0.0  # Current trailing stop level

    # Status
    is_active: bool = True
    exit_reason: ExitReason | None = None
    
    # === STATE MACHINE (optional, for new positions) ===
    _state_machine: object = None  # StateMachine instance, lazy init
    exit_price: float | None = None
    exit_time: datetime | None = None

    # Platform info for restoration
    platform: str = "pump_fun"
    bonding_curve: str | None = None

    def __post_init__(self):
        """Initialize high water mark to entry price."""
        if self.high_water_mark == 0.0:
            self.high_water_mark = self.entry_price

    # === STATE MACHINE PROPERTY ===
    @property
    def state(self) -> str:
        """Get current state as string (for compatibility)."""
        if self._state_machine is not None and STATE_MACHINE_AVAILABLE:
            return self._state_machine.current_state.value
        return "open" if self.is_active else "closed"
    
    @property
    def state_machine(self):
        """Lazy init state machine for new code."""
        if self._state_machine is None and STATE_MACHINE_AVAILABLE:
            initial = PositionState.OPEN if self.is_active else PositionState.CLOSED
            self._state_machine = StateMachine(current_state=initial)
        return self._state_machine
    # === END STATE MACHINE PROPERTY ===

    def to_dict(self) -> dict:
        """Convert position to dictionary for JSON serialization."""
        return {
            "mint": str(self.mint),
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "max_hold_time": self.max_hold_time,
            # TSL fields
            "tsl_enabled": self.tsl_enabled,
            "tsl_activation_pct": self.tsl_activation_pct,
            "tsl_trail_pct": self.tsl_trail_pct,
            "tsl_active": self.tsl_active,
            "high_water_mark": self.high_water_mark,
            "tsl_trigger_price": self.tsl_trigger_price,
            "tsl_sell_pct": self.tsl_sell_pct,
            # Status
            "is_active": self.is_active,
            "state": self.state,  # === NEW: state for future compatibility ===
            "platform": self.platform,
            "bonding_curve": str(self.bonding_curve) if self.bonding_curve else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        """Create position from dictionary."""
        position = cls(
            mint=Pubkey.from_string(data["mint"]),
            symbol=data["symbol"],
            entry_price=data["entry_price"],
            quantity=data["quantity"],
            entry_time=datetime.fromisoformat(data["entry_time"]),
            take_profit_price=data.get("take_profit_price"),
            stop_loss_price=data.get("stop_loss_price"),
            max_hold_time=data.get("max_hold_time"),
            # TSL fields
            tsl_enabled=data.get("tsl_enabled", False),
            tsl_activation_pct=data.get("tsl_activation_pct", 0.20),
            tsl_trail_pct=data.get("tsl_trail_pct", 0.10),
            tsl_active=data.get("tsl_active", False),
            high_water_mark=data.get("high_water_mark", data["entry_price"]),
            tsl_trigger_price=data.get("tsl_trigger_price", 0.0),
            tsl_sell_pct=data.get("tsl_sell_pct", 0.50),
            # Status
            is_active=data.get("is_active", True),
            platform=data.get("platform", "pump_fun"),
            bonding_curve=data.get("bonding_curve"),
        )
        return position

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
        # TSL parameters - NEW!
        tsl_enabled: bool = False,
        tsl_activation_pct: float = 0.20,
        tsl_trail_pct: float = 0.10,
        tsl_sell_pct: float = 0.50,  # Sell 50% when TSL triggers
    ) -> "Position":
        """Create a position from a successful buy transaction.

        Args:
            mint: Token mint address
            symbol: Token symbol
            entry_price: Price at which position was entered
            quantity: Quantity of tokens purchased
            take_profit_percentage: Take profit percentage (0.5 = 50% profit)
            stop_loss_percentage: Stop loss percentage (0.2 = 20% loss)
            max_hold_time: Maximum hold time in seconds
            platform: Trading platform
            bonding_curve: Bonding curve address for price checks
            tsl_enabled: Enable trailing stop-loss
            tsl_activation_pct: Profit % to activate TSL (0.20 = +20%)
            tsl_trail_pct: Trail % below high water mark (0.10 = 10%)

        Returns:
            Position instance
        """
        # VALIDATION: Entry price must be positive and reasonable
        if entry_price <= 0:
            logger.error(f"[POSITION] INVALID entry_price={entry_price}, forcing to minimum 0.0000001")
            entry_price = 0.0000001  # Minimum reasonable price
        elif entry_price > 1000:  # Price > 1000 SOL is likely an error
            logger.warning(f"[POSITION] Suspiciously high entry_price={entry_price}, keeping as-is")

        take_profit_price = None
        if take_profit_percentage is not None:
            take_profit_price = entry_price * (1 + take_profit_percentage)

        stop_loss_price = None
        if stop_loss_percentage is not None:
            stop_loss_price = entry_price * (1 - stop_loss_percentage)
            # VALIDATION: SL must always be positive!
            if stop_loss_price <= 0:
                logger.error(
                    f"[SL CALC] NEGATIVE SL DETECTED! entry={entry_price:.10f}, sl_pct={stop_loss_percentage*100:.1f}%, "
                    f"calculated_sl={stop_loss_price:.10f} - FIXING to positive value"
                )
                # Set SL to 10% of entry price as minimum
                stop_loss_price = entry_price * 0.1
            logger.warning(
                f"[SL CALC] entry={entry_price:.10f}, sl_pct={stop_loss_percentage*100:.1f}%, "
                f"sl_price={stop_loss_price:.10f}"
            )

        position = cls(
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
            # TSL
            tsl_enabled=tsl_enabled,
            tsl_activation_pct=tsl_activation_pct,
            tsl_trail_pct=tsl_trail_pct,
            tsl_sell_pct=tsl_sell_pct,
            high_water_mark=entry_price,
        )

        if tsl_enabled:
            activation_price = entry_price * (1 + tsl_activation_pct)
            logger.warning(
                f"[TSL] {symbol} TSL enabled: activates at {activation_price:.10f} "
                f"(+{tsl_activation_pct*100:.0f}%), trails {tsl_trail_pct*100:.0f}%"
            )

        return position

    def update_price(self, current_price: float) -> None:
        """
        Update position with new price - handles TSL logic.
        Call this BEFORE should_exit() for TSL to work correctly.

        Args:
            current_price: Current token price
        """
        if not self.is_active or not self.tsl_enabled:
            return

        # Calculate current profit percentage
        profit_pct = (current_price - self.entry_price) / self.entry_price

        # Check TSL activation
        if not self.tsl_active and profit_pct >= self.tsl_activation_pct:
            self.tsl_active = True
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
            logger.warning(
                f"[TSL] {self.symbol} TSL ACTIVATED at {current_price:.10f} "
                f"(+{profit_pct*100:.1f}%). Trail stop: {self.tsl_trigger_price:.10f}"
            )

        # Update high water mark and trailing stop if TSL is active
        if self.tsl_active and current_price > self.high_water_mark:
            old_hwm = self.high_water_mark
            self.high_water_mark = current_price
            old_trigger = self.tsl_trigger_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
            logger.info(
                f"[TSL] {self.symbol} NEW HIGH: {old_hwm:.10f} -> {current_price:.10f}. "
                f"Trail stop: {old_trigger:.10f} -> {self.tsl_trigger_price:.10f}"
            )

    def should_exit(self, current_price: float) -> tuple[bool, ExitReason | None]:
        """Check if position should be exited based on current conditions.

        IMPORTANT: Call update_price() before this for TSL to work!

        Args:
            current_price: Current token price

        Returns:
            Tuple of (should_exit, exit_reason)
        """
        if not self.is_active:
            return False, None

        # ========================================
        # 1. Check STATIC stop loss first (safety net)
        # ========================================
        if self.stop_loss_price and current_price <= self.stop_loss_price:
            logger.warning(
                f"[SL] {self.symbol} STOP LOSS: {current_price:.10f} <= {self.stop_loss_price:.10f}"
            )
            return True, ExitReason.STOP_LOSS

        # ========================================
        # 2. Check TRAILING stop loss (if active)
        # ========================================
        if self.tsl_active and current_price <= self.tsl_trigger_price:
            profit_pct = (current_price - self.entry_price) / self.entry_price * 100
            logger.warning(
                f"[TSL] {self.symbol} TRAILING STOP: {current_price:.10f} <= {self.tsl_trigger_price:.10f}. "
                f"Locked profit: +{profit_pct:.1f}%"
            )
            return True, ExitReason.TRAILING_STOP

        # ========================================
        # 3. Check take profit
        # ========================================
        if self.take_profit_price and current_price >= self.take_profit_price:
            return True, ExitReason.TAKE_PROFIT

        # ========================================
        # 4. Check max hold time
        # ========================================
        if self.max_hold_time:
            elapsed_time = (datetime.utcnow() - self.entry_time).total_seconds()
            if elapsed_time >= self.max_hold_time:
                return True, ExitReason.MAX_HOLD_TIME

        return False, None

    def close_position(self, exit_price: float, exit_reason: ExitReason) -> None:
        """Close the position with exit details.

        Args:
            exit_price: Price at which position was exited
            exit_reason: Reason for exit
        """
        self.is_active = False
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_time = datetime.utcnow()

    def get_pnl(self, current_price: float | None = None) -> dict:
        """Calculate profit/loss for the position.

        Args:
            current_price: Current price (uses exit_price if position is closed)

        Returns:
            Dictionary with PnL information
        """
        if self.is_active and current_price is None:
            raise ValueError("current_price required for active position")

        price_to_use = self.exit_price if not self.is_active else current_price
        if price_to_use is None:
            raise ValueError("No price available for PnL calculation")

        price_change = price_to_use - self.entry_price
        price_change_pct = (price_change / self.entry_price) * 100
        unrealized_pnl = price_change * self.quantity

        return {
            "entry_price": self.entry_price,
            "current_price": price_to_use,
            "price_change": price_change,
            "price_change_pct": price_change_pct,
            "unrealized_pnl_sol": unrealized_pnl,
            "quantity": self.quantity,
            "tsl_active": self.tsl_active,
            "high_water_mark": self.high_water_mark,
            "tsl_trigger_price": self.tsl_trigger_price if self.tsl_active else None,
        }

    def __str__(self) -> str:
        """String representation of position."""
        if self.is_active:
            status = "ACTIVE"
            if self.tsl_active:
                status += f" [TSL @ {self.tsl_trigger_price:.10f}]"
        elif self.exit_reason:
            status = f"CLOSED ({self.exit_reason.value})"
        else:
            status = "CLOSED (UNKNOWN)"
        return f"Position({self.symbol}: {self.quantity:.6f} @ {self.entry_price:.8f} SOL - {status})"



def save_positions(positions: list[Position], filepath: Path = POSITIONS_FILE) -> None:
    """Save active positions to file.

    Args:
        positions: List of positions to save
        filepath: Path to save file
    """
    active_positions = [p.to_dict() for p in positions if p.is_active]

    try:
        # SESSION 9: Atomic write
        success = _positions_writer.write_json(filepath, active_positions)
        if success:
            logger.info(f"Saved {len(active_positions)} positions (atomic)")

        # ALSO SAVE TO REDIS
        try:
            import redis
            redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            for pos_dict in active_positions:
                mint_str = pos_dict.get("mint")
                if mint_str:
                    redis_key = f"position:{mint_str}"
                    redis_client.setex(redis_key, 7 * 24 * 3600, json.dumps(pos_dict))
            mints = [p.get("mint") for p in active_positions if p.get("mint")]
            redis_client.setex("positions:all", 7 * 24 * 3600, json.dumps(mints))
            redis_client.bgsave()
            logger.debug(f"[REDIS] Saved {len(active_positions)} positions")
        except Exception as redis_err:
            logger.warning(f"[REDIS] Failed: {redis_err}")
    except Exception as e:
        logger.exception(f"Failed to save positions: {e}")

def load_positions(filepath: Path = POSITIONS_FILE) -> list[Position]:
    """Load positions from file, with Redis fallback.

    Args:
        filepath: Path to positions file

    Returns:
        List of Position objects
    """
    positions = []
    
    # Try file first
    if filepath.exists():
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            if data:
                positions = [Position.from_dict(p) for p in data]
                logger.info(f"Loaded {len(positions)} positions from {filepath}")
                return positions
        except Exception as e:
            logger.warning(f"Failed to load from file: {e}")
    
    # Fallback to Redis
    try:
        import redis
        redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        
        # Get all position keys
        keys = redis_client.keys("position:*pump")
        if not keys:
            logger.info("No positions in Redis")
            return []
        
        for key in keys:
            try:
                data = redis_client.get(key)
                if data:
                    pos_dict = json.loads(data)
                    if pos_dict.get("is_active", False):
                        positions.append(Position.from_dict(pos_dict))
            except Exception as e:
                logger.warning(f"Failed to load {key}: {e}")
        
        if positions:
            logger.info(f"Loaded {len(positions)} positions from Redis")
            # Sync to file
            save_positions(positions, filepath)
        
        return positions
    except Exception as e:
        logger.warning(f"Redis fallback failed: {e}")
        return []


def remove_position(mint: str, filepath: Path = POSITIONS_FILE) -> None:
    """Remove a position from the saved file.

    Args:
        mint: Mint address of position to remove
        filepath: Path to positions file
    """
    positions = load_positions(filepath)
    positions = [p for p in positions if str(p.mint) != mint]
    save_positions(positions, filepath)


def is_token_in_positions(mint_str: str, filepath: Path = POSITIONS_FILE) -> bool:
    """Check if token is already in positions file (for cross-bot sync)."""
    try:
        positions = _positions_writer.read_json_safe(filepath, default=[])
        if not positions:
            return False
        for pos in positions:
            if pos.get("mint") == mint_str and pos.get("is_active", True):
                return True
        return False
    except Exception:
        return False
