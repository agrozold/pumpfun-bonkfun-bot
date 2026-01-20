"""
Position management for take profit/stop loss functionality.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from solders.pubkey import Pubkey

from utils.logger import get_logger

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
    MAX_HOLD_TIME = "max_hold_time"
    MANUAL = "manual"


@dataclass
class Position:
    """Represents an active trading position."""

    # Token information
    mint: Pubkey
    symbol: str

    # Position details
    entry_price: float
    quantity: float
    entry_time: datetime

    # Exit conditions
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    max_hold_time: int | None = None  # seconds

    # Status
    is_active: bool = True
    exit_reason: ExitReason | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None
    
    # Platform info for restoration
    platform: str = "pump_fun"
    bonding_curve: str | None = None

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
            "is_active": self.is_active,
            "platform": self.platform,
            "bonding_curve": self.bonding_curve,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        """Create position from dictionary."""
        return cls(
            mint=Pubkey.from_string(data["mint"]),
            symbol=data["symbol"],
            entry_price=data["entry_price"],
            quantity=data["quantity"],
            entry_time=datetime.fromisoformat(data["entry_time"]),
            take_profit_price=data.get("take_profit_price"),
            stop_loss_price=data.get("stop_loss_price"),
            max_hold_time=data.get("max_hold_time"),
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

        Returns:
            Position instance
        """
        take_profit_price = None
        if take_profit_percentage is not None:
            take_profit_price = entry_price * (1 + take_profit_percentage)

        stop_loss_price = None
        if stop_loss_percentage is not None:
            stop_loss_price = entry_price * (1 - stop_loss_percentage)
            # CRITICAL: Log SL calculation for debugging
            from utils.logger import get_logger
            _logger = get_logger(__name__)
            _logger.warning(
                f"[SL CALC] entry={entry_price:.10f}, sl_pct={stop_loss_percentage*100:.1f}%, "
                f"sl_price={stop_loss_price:.10f}"
            )

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
            bonding_curve=bonding_curve,
        )

    def should_exit(self, current_price: float) -> tuple[bool, ExitReason | None]:
        """Check if position should be exited based on current conditions.

        Args:
            current_price: Current token price

        Returns:
            Tuple of (should_exit, exit_reason)
        """
        if not self.is_active:
            return False, None

        # Check take profit
        if self.take_profit_price and current_price >= self.take_profit_price:
            return True, ExitReason.TAKE_PROFIT

        # Check stop loss
        if self.stop_loss_price and current_price <= self.stop_loss_price:
            return True, ExitReason.STOP_LOSS

        # Check max hold time
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
        }

    def __str__(self) -> str:
        """String representation of position."""
        if self.is_active:
            status = "ACTIVE"
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
    except Exception as e:
        logger.exception(f"Failed to save positions: {e}")


def load_positions(filepath: Path = POSITIONS_FILE) -> list[Position]:
    """Load positions from file.
    
    Args:
        filepath: Path to positions file
        
    Returns:
        List of Position objects
    """
    if not filepath.exists():
        logger.info(f"No positions file found at {filepath}")
        return []
    
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        
        positions = [Position.from_dict(p) for p in data]
        logger.info(f"Loaded {len(positions)} positions from {filepath}")
        return positions
    except Exception as e:
        logger.exception(f"Failed to load positions: {e}")
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
