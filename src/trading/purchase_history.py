"""
Global purchase history - prevents buying same token twice EVER.
Shared across all bots, persisted to disk.
"""
import json
import os
import fcntl
from datetime import datetime
from pathlib import Path
from utils.logger import get_logger

logger = get_logger(__name__)

# Global history file - shared by ALL bots
HISTORY_FILE = Path("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json")


def _ensure_data_dir():
    """Ensure data directory exists."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_purchase_history() -> set[str]:
    """Load all previously purchased token mints from disk.
    
    Returns:
        Set of token mint addresses that were ever purchased.
    """
    _ensure_data_dir()

    if not HISTORY_FILE.exists():
        return set()

    try:
        with open(HISTORY_FILE, 'r') as f:
            # Use file locking for safe concurrent access
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                data = json.load(f)
                tokens = set(data.get("purchased_tokens", {}).keys())
                logger.info(f"[HISTORY] Loaded {len(tokens)} tokens from purchase history")
                return tokens
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        logger.error(f"[HISTORY] Failed to load purchase history: {e}")
        return set()


def was_token_purchased(mint: str) -> bool:
    """Check if token was ever purchased.
    
    Args:
        mint: Token mint address
        
    Returns:
        True if token was purchased before, False otherwise.
    """
    history = load_purchase_history()
    return mint in history


def add_to_purchase_history(
    mint: str,
    symbol: str,
    bot_name: str = "unknown",
    platform: str = "unknown",
    price: float = 0.0,
    amount: float = 0.0,
) -> bool:
    """Add token to purchase history.
    
    Args:
        mint: Token mint address
        symbol: Token symbol
        bot_name: Name of bot that made the purchase
        platform: Platform where token was bought
        price: Entry price
        amount: Amount of tokens bought
        
    Returns:
        True if added successfully, False otherwise.
    """
    _ensure_data_dir()

    try:
        # Load existing data
        data = {"purchased_tokens": {}}
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        # Add new token
        if "purchased_tokens" not in data:
            data["purchased_tokens"] = {}

        data["purchased_tokens"][mint] = {
            "symbol": symbol,
            "bot_name": bot_name,
            "platform": platform,
            "price": price,
            "amount": amount,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Save with exclusive lock
        with open(HISTORY_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        logger.warning(
            f"[HISTORY] Added {symbol} ({mint[:8]}...) to purchase history "
            f"(total: {len(data['purchased_tokens'])} tokens)"
        )
        return True

    except Exception as e:
        logger.error(f"[HISTORY] Failed to add token to history: {e}")
        return False


def get_purchase_history_stats() -> dict:
    """Get statistics about purchase history."""
    _ensure_data_dir()

    if not HISTORY_FILE.exists():
        return {"total_tokens": 0, "file_exists": False}

    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
            tokens = data.get("purchased_tokens", {})
            return {
                "total_tokens": len(tokens),
                "file_exists": True,
                "file_path": str(HISTORY_FILE),
            }
    except Exception as e:
        return {"total_tokens": 0, "file_exists": True, "error": str(e)}
