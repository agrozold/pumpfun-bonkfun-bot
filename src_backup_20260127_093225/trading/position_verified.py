"""
Position management with VERIFIED sell completion.
Session 10 HOTFIX: Fixes desync between positions.json and actual wallet state.
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from solders.pubkey import Pubkey
from utils.logger import get_logger

logger = get_logger(__name__)

POSITIONS_FILE = Path("positions.json")


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    MAX_HOLD_TIME = "max_hold_time"
    MANUAL = "manual"
    EMERGENCY = "emergency"


@dataclass
class Position:
    """Position with verified state tracking."""
    
    mint: Pubkey
    symbol: str
    entry_price: float
    quantity: float
    entry_time: datetime
    
    take_profit_price: float = None
    stop_loss_price: float = None
    max_hold_time: int = None
    
    # TSL
    tsl_enabled: bool = False
    tsl_activation_pct: float = 0.30
    tsl_trail_pct: float = 0.30
    tsl_sell_pct: float = 0.90
    tsl_active: bool = False
    high_water_mark: float = 0.0
    tsl_trigger_price: float = 0.0
    
    # State
    is_active: bool = True
    exit_reason: ExitReason = None
    exit_price: float = None
    exit_time: datetime = None
    
    # Platform
    platform: str = "pump_fun"
    bonding_curve: str = None
    
    # NEW: Sell tracking
    pending_sell: bool = False  # TX sent, waiting confirmation
    sell_tx_signature: str = None
    sell_attempts: int = 0
    last_sell_attempt: datetime = None
    
    def __post_init__(self):
        if self.high_water_mark == 0.0:
            self.high_water_mark = self.entry_price
    
    @property
    def state(self) -> str:
        if self.pending_sell:
            return "pending_sell"
        return "open" if self.is_active else "closed"
    
    def mark_pending_sell(self, tx_signature: str) -> None:
        """Mark position as pending sell (TX sent, not yet confirmed)."""
        self.pending_sell = True
        self.sell_tx_signature = tx_signature
        self.sell_attempts += 1
        self.last_sell_attempt = datetime.utcnow()
        logger.warning(f"[POSITION] {self.symbol} marked PENDING_SELL (tx: {tx_signature[:16]}...)")
    
    def confirm_sell(self, exit_price: float, exit_reason: ExitReason) -> None:
        """Confirm sell completed - remove from active positions."""
        self.is_active = False
        self.pending_sell = False
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_time = datetime.utcnow()
        logger.warning(f"[POSITION] {self.symbol} CONFIRMED SOLD at {exit_price:.10f}")
    
    def cancel_pending_sell(self, reason: str = "") -> None:
        """Cancel pending sell if TX failed."""
        self.pending_sell = False
        self.sell_tx_signature = None
        logger.warning(f"[POSITION] {self.symbol} sell CANCELLED: {reason}")
    
    def update_price(self, current_price: float) -> None:
        """Update TSL state with new price."""
        if not self.is_active or not self.tsl_enabled or self.pending_sell:
            return
        
        if self.entry_price <= 0:
            return
            
        profit_pct = (current_price - self.entry_price) / self.entry_price
        
        # Activate TSL
        if not self.tsl_active and profit_pct >= self.tsl_activation_pct:
            self.tsl_active = True
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
            logger.warning(
                f"[TSL] {self.symbol} ACTIVATED at {current_price:.10f} "
                f"(+{profit_pct*100:.1f}%). Trail: {self.tsl_trigger_price:.10f}"
            )
        
        # Update high water mark
        if self.tsl_active and current_price > self.high_water_mark:
            self.high_water_mark = current_price
            self.tsl_trigger_price = current_price * (1 - self.tsl_trail_pct)
    
    def should_exit(self, current_price: float) -> Tuple[bool, Optional[ExitReason]]:
        """Check exit conditions."""
        if not self.is_active or self.pending_sell:
            return False, None
        
        # 1. Hard Stop Loss (ALWAYS check first)
        if self.entry_price > 0:
            pnl_pct = (current_price - self.entry_price) / self.entry_price
            if pnl_pct <= -0.20:  # -20% hard stop
                return True, ExitReason.STOP_LOSS
        
        # 2. Config Stop Loss
        if self.stop_loss_price and current_price <= self.stop_loss_price:
            return True, ExitReason.STOP_LOSS
        
        # 3. TSL (only if in profit)
        if self.tsl_active and current_price <= self.tsl_trigger_price:
            pnl_pct = (current_price - self.entry_price) / self.entry_price if self.entry_price > 0 else 0
            if pnl_pct > 0:  # Only trigger TSL if still in profit
                return True, ExitReason.TRAILING_STOP
        
        # 4. Take Profit
        if self.take_profit_price and current_price >= self.take_profit_price:
            return True, ExitReason.TAKE_PROFIT
        
        # 5. Max hold time
        if self.max_hold_time and self.max_hold_time > 0:
            elapsed = (datetime.utcnow() - self.entry_time).total_seconds()
            if elapsed >= self.max_hold_time:
                return True, ExitReason.MAX_HOLD_TIME
        
        return False, None
    
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
            "tsl_sell_pct": self.tsl_sell_pct,
            "tsl_active": self.tsl_active,
            "high_water_mark": self.high_water_mark,
            "tsl_trigger_price": self.tsl_trigger_price,
            "is_active": self.is_active,
            "state": self.state,
            "platform": self.platform,
            "bonding_curve": self.bonding_curve,
            "pending_sell": self.pending_sell,
            "sell_tx_signature": self.sell_tx_signature,
            "sell_attempts": self.sell_attempts,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        pos = cls(
            mint=Pubkey.from_string(data["mint"]),
            symbol=data.get("symbol", ""),
            entry_price=data["entry_price"],
            quantity=data["quantity"],
            entry_time=datetime.fromisoformat(data["entry_time"]),
            take_profit_price=data.get("take_profit_price"),
            stop_loss_price=data.get("stop_loss_price"),
            max_hold_time=data.get("max_hold_time"),
            tsl_enabled=data.get("tsl_enabled", False),
            tsl_activation_pct=data.get("tsl_activation_pct", 0.30),
            tsl_trail_pct=data.get("tsl_trail_pct", 0.30),
            tsl_sell_pct=data.get("tsl_sell_pct", 0.90),
            tsl_active=data.get("tsl_active", False),
            high_water_mark=data.get("high_water_mark", data["entry_price"]),
            tsl_trigger_price=data.get("tsl_trigger_price", 0.0),
            is_active=data.get("is_active", True),
            platform=data.get("platform", "pump_fun"),
            bonding_curve=data.get("bonding_curve"),
        )
        pos.pending_sell = data.get("pending_sell", False)
        pos.sell_tx_signature = data.get("sell_tx_signature")
        pos.sell_attempts = data.get("sell_attempts", 0)
        return pos
    
    @classmethod
    def create_from_buy_result(
        cls,
        mint: Pubkey,
        symbol: str,
        entry_price: float,
        quantity: float,
        take_profit_percentage: float = None,
        stop_loss_percentage: float = None,
        max_hold_time: int = None,
        platform: str = "pump_fun",
        bonding_curve: str = None,
        tsl_enabled: bool = False,
        tsl_activation_pct: float = 0.30,
        tsl_trail_pct: float = 0.30,
        tsl_sell_pct: float = 0.90,
    ) -> "Position":
        """Create position from successful buy."""
        if entry_price <= 0:
            logger.error(f"[POSITION] Invalid entry_price={entry_price}, using 0.0000001")
            entry_price = 0.0000001
        
        tp_price = entry_price * (1 + take_profit_percentage) if take_profit_percentage else None
        sl_price = None
        if stop_loss_percentage:
            sl_price = entry_price * (1 - stop_loss_percentage)
            if sl_price <= 0:
                sl_price = entry_price * 0.1
        
        return cls(
            mint=mint,
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=datetime.utcnow(),
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
            max_hold_time=max_hold_time,
            platform=platform,
            bonding_curve=str(bonding_curve) if bonding_curve else None,
            tsl_enabled=tsl_enabled,
            tsl_activation_pct=tsl_activation_pct,
            tsl_trail_pct=tsl_trail_pct,
            tsl_sell_pct=tsl_sell_pct,
            high_water_mark=entry_price,
        )


def save_positions(positions: list, filepath: Path = POSITIONS_FILE) -> None:
    """Atomic save with backup."""
    active = [p.to_dict() for p in positions if p.is_active or p.pending_sell]
    
    # Backup before write
    if filepath.exists():
        backup = filepath.with_suffix('.json.bak')
        try:
            backup.write_text(filepath.read_text())
        except:
            pass
    
    # Atomic write
    tmp = filepath.with_suffix('.json.tmp')
    try:
        tmp.write_text(json.dumps(active, indent=2))
        tmp.replace(filepath)
        logger.info(f"[SAVE] Saved {len(active)} positions")
    except Exception as e:
        logger.error(f"[SAVE] Failed: {e}")
        if tmp.exists():
            tmp.unlink()


def load_positions(filepath: Path = POSITIONS_FILE) -> list:
    """Load positions from file."""
    if not filepath.exists():
        return []
    
    try:
        data = json.loads(filepath.read_text())
        positions = [Position.from_dict(p) for p in data if p.get("is_active", True)]
        logger.info(f"[LOAD] Loaded {len(positions)} positions")
        return positions
    except Exception as e:
        logger.error(f"[LOAD] Failed: {e}")
        # Try backup
        backup = filepath.with_suffix('.json.bak')
        if backup.exists():
            try:
                data = json.loads(backup.read_text())
                return [Position.from_dict(p) for p in data if p.get("is_active", True)]
            except:
                pass
        return []


def remove_position(mint: str, filepath: Path = POSITIONS_FILE) -> None:
    """Remove position by mint address."""
    positions = load_positions(filepath)
    positions = [p for p in positions if str(p.mint) != mint]
    save_positions(positions, filepath)
    logger.info(f"[REMOVE] Removed {mint[:16]}...")


def is_token_in_positions(mint_str: str, filepath: Path = POSITIONS_FILE) -> bool:
    """Check if token is in active positions."""
    if not filepath.exists():
        return False
    try:
        data = json.loads(filepath.read_text())
        return any(
            p.get("mint") == mint_str and p.get("is_active", True) 
            for p in data
        )
    except:
        return False


async def verify_sell_on_chain(
    client,
    mint: Pubkey,
    wallet_pubkey: Pubkey,
    expected_zero: bool = True,
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> Tuple[bool, float]:
    """
    Verify sell completion ON-CHAIN with finalized commitment.
    
    Returns:
        (is_sold, remaining_balance)
    """
    from spl.token.constants import TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID
    from solders.pubkey import Pubkey as SoldersPubkey
    
    for attempt in range(max_retries):
        try:
            # Wait for finalization
            await asyncio.sleep(retry_delay)
            
            # Get balance with FINALIZED commitment
            balance = await client.get_token_account_balance(
                mint,
                wallet_pubkey,
                commitment="finalized"
            )
            
            if balance is None or balance <= 0.001:  # Consider dust as zero
                logger.info(f"[VERIFY] Sell confirmed: balance={balance or 0}")
                return True, 0.0
            
            logger.warning(f"[VERIFY] Attempt {attempt+1}: balance={balance} (not zero)")
            
        except Exception as e:
            logger.warning(f"[VERIFY] Attempt {attempt+1} error: {e}")
        
        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)
    
    # Final check
    try:
        final_balance = await client.get_token_account_balance(
            mint, wallet_pubkey, commitment="finalized"
        )
        return (final_balance is None or final_balance <= 0.001), final_balance or 0.0
    except:
        return False, -1.0


# Track active monitors
_active_monitors: set = set()

def register_monitor(mint: str) -> bool:
    if mint in _active_monitors:
        return False
    _active_monitors.add(mint)
    return True

def unregister_monitor(mint: str) -> None:
    _active_monitors.discard(mint)
