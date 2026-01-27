import json
import redis
import logging
from pathlib import Path
from datetime import datetime
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

REDIS_CLIENT = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

def save_positions_to_redis(positions: list) -> bool:
    """Save all positions to Redis with TTL"""
    try:
        if not positions:
            logger.info("[REDIS] No positions to save")
            return True

        for pos in positions:
            mint_str = str(pos.mint)

            pos_dict = {
                "mint": mint_str,
                "symbol": pos.symbol,
                "entry_price": float(pos.entry_price),
                "quantity": float(pos.quantity),
                "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
                "platform": pos.platform,
                "is_active": pos.is_active,
                "take_profit_price": float(pos.take_profit_price) if pos.take_profit_price else None,
                "stop_loss_price": float(pos.stop_loss_price) if pos.stop_loss_price else None,
                "max_hold_time": pos.max_hold_time,
                "bonding_curve": str(pos.bonding_curve) if pos.bonding_curve else None,
                "tsl_enabled": pos.tsl_enabled,
                "tsl_activation_pct": float(pos.tsl_activation_pct),
                "tsl_trail_pct": float(pos.tsl_trail_pct),
                "tsl_sell_pct": float(pos.tsl_sell_pct),
                "tsl_active": pos.tsl_active,
                "high_water_mark": float(pos.high_water_mark) if pos.high_water_mark else None,
                "tsl_trigger_price": float(pos.tsl_trigger_price) if pos.tsl_trigger_price else None,
            }

            redis_key = f"position:{mint_str}"
            REDIS_CLIENT.setex(redis_key, 7 * 24 * 3600, json.dumps(pos_dict))
            logger.debug(f"[REDIS] Saved position {mint_str}")

        position_mints = [str(pos.mint) for pos in positions]
        REDIS_CLIENT.setex("positions:all", 7 * 24 * 3600, json.dumps(position_mints))

        logger.warning(f"[REDIS] Saved {len(positions)} positions to Redis")
        REDIS_CLIENT.bgsave()
        return True

    except Exception as e:
        logger.error(f"[REDIS] Failed to save positions: {e}")
        return False

def load_positions_from_redis() -> list:
    """Load all positions from Redis"""
    try:
        position_mints_str = REDIS_CLIENT.get("positions:all")
        if not position_mints_str:
            logger.info("[REDIS] No positions in Redis")
            return []

        position_mints = json.loads(position_mints_str)
        positions = []

        for mint_str in position_mints:
            redis_key = f"position:{mint_str}"
            pos_str = REDIS_CLIENT.get(redis_key)

            if pos_str:
                pos_dict = json.loads(pos_str)
                from src.trading.position import Position

                pos = Position(
                    mint=Pubkey.from_string(pos_dict["mint"]),
                    symbol=pos_dict["symbol"],
                    entry_price=pos_dict["entry_price"],
                    quantity=pos_dict["quantity"],
                    entry_time=datetime.fromisoformat(pos_dict["entry_time"]) if pos_dict.get("entry_time") else None,
                    platform=pos_dict.get("platform"),
                    is_active=pos_dict.get("is_active", True),
                    bonding_curve=pos_dict.get("bonding_curve"),
                )

                pos.take_profit_price = pos_dict.get("take_profit_price")
                pos.stop_loss_price = pos_dict.get("stop_loss_price")
                pos.max_hold_time = pos_dict.get("max_hold_time")

                pos.tsl_enabled = pos_dict.get("tsl_enabled", False)
                pos.tsl_activation_pct = pos_dict.get("tsl_activation_pct", 0.20)
                pos.tsl_trail_pct = pos_dict.get("tsl_trail_pct", 0.10)
                pos.tsl_sell_pct = pos_dict.get("tsl_sell_pct", 0.50)
                pos.tsl_active = pos_dict.get("tsl_active", False)
                pos.high_water_mark = pos_dict.get("high_water_mark")
                pos.tsl_trigger_price = pos_dict.get("tsl_trigger_price")

                positions.append(pos)

        logger.warning(f"[REDIS] Loaded {len(positions)} positions from Redis")
        return positions

    except Exception as e:
        logger.error(f"[REDIS] Failed to load positions from Redis: {e}")
        return []
