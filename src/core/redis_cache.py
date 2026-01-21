"""Redis cache for sharing data between bots."""

import json
import redis
from typing import Any
from utils.logger import get_logger

logger = get_logger(__name__)

_redis_client = None

def get_redis() -> redis.Redis:
    """Get Redis connection (singleton)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        try:
            _redis_client.ping()
            logger.info("[REDIS] Connected to Redis")
        except redis.ConnectionError:
            logger.warning("[REDIS] Failed to connect, running without cache")
            _redis_client = None
    return _redis_client

def cache_get(key: str) -> Any:
    """Get from Redis cache."""
    r = get_redis()
    if r is None:
        return None
    try:
        data = r.get(key)
        if data:
            return json.loads(data)
    except Exception as e:
        logger.debug(f"[REDIS] Get error: {e}")
    return None

def cache_set(key: str, value: Any, ttl: int = 60) -> bool:
    """Set to Redis cache with TTL."""
    r = get_redis()
    if r is None:
        return False
    try:
        r.setex(key, ttl, json.dumps(value))
        return True
    except Exception as e:
        logger.debug(f"[REDIS] Set error: {e}")
    return False

def cache_stats() -> dict:
    """Get cache stats."""
    r = get_redis()
    if r is None:
        return {"status": "disconnected"}
    try:
        info = r.info("stats")
        return {
            "status": "connected",
            "hits": info.get("keyspace_hits", 0),
            "misses": info.get("keyspace_misses", 0),
            "keys": r.dbsize(),
        }
    except:
        return {"status": "error"}
