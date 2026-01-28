"""
Redis State Manager - Single Source of Truth for positions.
Replaces positions.json with Redis Hash maps.
Provides atomic operations to prevent race conditions.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Redis keys
POSITIONS_KEY = "whale:positions"
PROCESSED_TX_KEY = "whale:processed_tx"
BUYING_LOCK_KEY = "whale:buying"
BOT_LOCK_KEY = "whale:bot_lock"

# TTL constants
TX_TTL_SECONDS = 3600
BUYING_TTL_SECONDS = 60


class RedisStateManager:
    """Thread-safe state manager using Redis as single source of truth."""
    
    _instance: Optional["RedisStateManager"] = None
    _lock = asyncio.Lock()
    
    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._connected = False
        
    @classmethod
    async def get_instance(cls, redis_url: str = "redis://localhost:6379/0") -> "RedisStateManager":
        """Get singleton instance."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls(redis_url)
                await cls._instance.connect()
            return cls._instance
    
    async def connect(self) -> bool:
        """Connect to Redis."""
        try:
            self._redis = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
            )
            await self._redis.ping()
            self._connected = True
            logger.info("[REDIS] Connected successfully")
            return True
        except Exception as e:
            logger.error(f"[REDIS] Connection failed: {e}")
            self._connected = False
            return False
    
    async def is_connected(self) -> bool:
        """Check Redis connection."""
        if not self._redis:
            return False
        try:
            await self._redis.ping()
            return True
        except:
            return False
    
    # ==================== POSITIONS ====================
    
    async def save_position(self, mint: str, position_dict: dict) -> bool:
        """Save single position atomically."""
        if not self._connected:
            return False
        try:
            position_json = json.dumps(position_dict)
            await self._redis.hset(POSITIONS_KEY, mint, position_json)
            logger.info(f"[REDIS] Saved position: {mint[:16]}...")
            return True
        except Exception as e:
            logger.error(f"[REDIS] save_position failed: {e}")
            return False
    
    async def get_position(self, mint: str) -> Optional[dict]:
        """Get single position by mint."""
        if not self._connected:
            return None
        try:
            data = await self._redis.hget(POSITIONS_KEY, mint)
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.error(f"[REDIS] get_position failed: {e}")
            return None
    
    async def get_all_positions(self) -> list[dict]:
        """Get all active positions."""
        if not self._connected:
            return []
        try:
            data = await self._redis.hgetall(POSITIONS_KEY)
            positions = []
            for mint, pos_json in data.items():
                try:
                    pos = json.loads(pos_json)
                    if pos.get("is_active", True):
                        positions.append(pos)
                except json.JSONDecodeError:
                    logger.warning(f"[REDIS] Invalid JSON for {mint}")
            return positions
        except Exception as e:
            logger.error(f"[REDIS] get_all_positions failed: {e}")
            return []
    
    async def remove_position(self, mint: str) -> bool:
        """Remove position by mint (atomic HDEL)."""
        if not self._connected:
            return False
        try:
            await self._redis.hdel(POSITIONS_KEY, mint)
            logger.info(f"[REDIS] Removed position: {mint[:16]}...")
            return True
        except Exception as e:
            logger.error(f"[REDIS] remove_position failed: {e}")
            return False
    
    async def position_exists(self, mint: str) -> bool:
        """Check if position exists (fast HEXISTS)."""
        if not self._connected:
            return False
        try:
            return await self._redis.hexists(POSITIONS_KEY, mint)
        except:
            return False
    
    async def get_positions_count(self) -> int:
        """Get count of positions (fast HLEN)."""
        if not self._connected:
            return 0
        try:
            return await self._redis.hlen(POSITIONS_KEY)
        except:
            return 0
    
    # ==================== IDEMPOTENCY (TX DEDUP) ====================
    
    async def is_tx_processed(self, tx_signature: str) -> bool:
        """Check if transaction was already processed."""
        if not self._connected:
            return False
        try:
            return await self._redis.sismember(PROCESSED_TX_KEY, tx_signature)
        except:
            return False
    
    async def mark_tx_processed(self, tx_signature: str) -> bool:
        """Mark transaction as processed (with TTL)."""
        if not self._connected:
            return False
        try:
            await self._redis.sadd(PROCESSED_TX_KEY, tx_signature)
            await self._redis.expire(PROCESSED_TX_KEY, TX_TTL_SECONDS)
            return True
        except Exception as e:
            logger.error(f"[REDIS] mark_tx_processed failed: {e}")
            return False
    
    # ==================== BUYING LOCK (ANTI-DUPLICATE) ====================
    
    async def try_acquire_buy_lock(self, mint: str, ttl: int = BUYING_TTL_SECONDS) -> bool:
        """Try to acquire exclusive buy lock for mint."""
        if not self._connected:
            return True
        try:
            lock_key = f"{BUYING_LOCK_KEY}:{mint}"
            result = await self._redis.set(lock_key, "1", nx=True, ex=ttl)
            if result:
                logger.info(f"[REDIS] Acquired buy lock: {mint[:16]}...")
                return True
            else:
                logger.info(f"[REDIS] Buy lock exists: {mint[:16]}...")
                return False
        except Exception as e:
            logger.error(f"[REDIS] try_acquire_buy_lock failed: {e}")
            return True
    
    async def release_buy_lock(self, mint: str) -> None:
        """Release buy lock after buy completes or fails."""
        if not self._connected:
            return
        try:
            lock_key = f"{BUYING_LOCK_KEY}:{mint}"
            await self._redis.delete(lock_key)
            logger.info(f"[REDIS] Released buy lock: {mint[:16]}...")
        except Exception as e:
            logger.error(f"[REDIS] release_buy_lock failed: {e}")
    
    async def is_being_bought(self, mint: str) -> bool:
        """Check if mint is currently being bought."""
        if not self._connected:
            return False
        try:
            lock_key = f"{BUYING_LOCK_KEY}:{mint}"
            return await self._redis.exists(lock_key) > 0
        except:
            return False
    
    # ==================== CLEANUP ====================
    
    async def clear_stale_locks(self) -> int:
        """Clear all buying locks (use on startup)."""
        if not self._connected:
            return 0
        try:
            pattern = f"{BUYING_LOCK_KEY}:*"
            keys = []
            async for key in self._redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                await self._redis.delete(*keys)
                logger.info(f"[REDIS] Cleared {len(keys)} stale buy locks")
            return len(keys)
        except Exception as e:
            logger.error(f"[REDIS] clear_stale_locks failed: {e}")
            return 0
    
    # ==================== MIGRATION HELPERS ====================
    
    async def import_from_json(self, json_path: str = "positions.json") -> int:
        """Import positions from JSON file to Redis."""
        import os
        if not os.path.exists(json_path):
            logger.info(f"[REDIS] No JSON file to import: {json_path}")
            return 0
        
        try:
            with open(json_path) as f:
                positions = json.load(f)
            
            if not positions:
                return 0
            
            count = 0
            for pos in positions:
                mint = pos.get("mint", "")
                if mint and pos.get("is_active", True):
                    await self.save_position(mint, pos)
                    count += 1
            
            logger.info(f"[REDIS] Imported {count} positions from {json_path}")
            return count
        except Exception as e:
            logger.error(f"[REDIS] import_from_json failed: {e}")
            return 0
    
    async def export_to_json(self, json_path: str = "positions.json") -> int:
        """Export positions from Redis to JSON file."""
        try:
            positions = await self.get_all_positions()
            with open(json_path, "w") as f:
                json.dump(positions, f, indent=2)
            logger.info(f"[REDIS] Exported {len(positions)} positions to {json_path}")
            return len(positions)
        except Exception as e:
            logger.error(f"[REDIS] export_to_json failed: {e}")
            return 0


# ==================== SINGLETON ACCESS ====================

_state_manager: Optional[RedisStateManager] = None


async def get_redis_state() -> RedisStateManager:
    """Get singleton RedisStateManager instance."""
    global _state_manager
    if _state_manager is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _state_manager = await RedisStateManager.get_instance(redis_url)
    return _state_manager


async def init_redis_state() -> bool:
    """Initialize Redis state manager and migrate from JSON."""
    try:
        state = await get_redis_state()
        if not await state.is_connected():
            logger.error("[REDIS] Failed to connect!")
            return False
        
        await state.clear_stale_locks()
        
        count = await state.get_positions_count()
        if count == 0:
            imported = await state.import_from_json()
            logger.info(f"[REDIS] Migrated {imported} positions from JSON")
        else:
            logger.info(f"[REDIS] Found {count} existing positions")
        
        return True
    except Exception as e:
        logger.error(f"[REDIS] init_redis_state failed: {e}")
        return False
