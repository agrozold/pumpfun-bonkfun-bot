"""Redis cache for sharing data between bots with persistence."""

import json
import redis
from typing import Any
from utils.logger import get_logger

logger = get_logger(__name__)

_redis_client = None

# TTL настройки (секунды) - 0 = бессрочно
TTL_RPC_CACHE = 3600         # RPC данные - 1 час (immutable data)
TTL_DEXSCREENER = 60         # Dexscreener - 1 минута
TTL_TOKEN_SCORE = 30         # Score токена - 30 сек
TTL_PENDING_TOKEN = 0        # Pending - бессрочно (удаляем вручную)
TTL_PUMP_SIGNAL = 600        # Сигналы - 10 минут
TTL_PURCHASED = 0            # Купленные - бессрочно
TTL_CREATOR_REP = 3600       # Репутация создателя - 1 час


def get_redis() -> redis.Redis | None:
    """Get Redis connection (singleton)."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
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
    """Set to Redis cache with TTL (0 = no expiry)."""
    r = get_redis()
    if r is None:
        return False
    try:
        if ttl > 0:
            r.setex(key, ttl, json.dumps(value))
        else:
            r.set(key, json.dumps(value))
        return True
    except Exception as e:
        logger.debug(f"[REDIS] Set error: {e}")
    return False


def cache_delete(key: str) -> bool:
    """Delete from Redis cache."""
    r = get_redis()
    if r is None:
        return False
    try:
        r.delete(key)
        return True
    except Exception as e:
        logger.debug(f"[REDIS] Delete error: {e}")
    return False


def cache_keys(pattern: str) -> list[str]:
    """Get keys matching pattern."""
    r = get_redis()
    if r is None:
        return []
    try:
        return r.keys(pattern)
    except Exception:
        return []


# ============================================
# PENDING TOKENS (waiting for signals)
# ============================================
def save_pending_token(mint: str, token_data: dict) -> bool:
    """Save token to pending queue (persistent)."""
    token_data['saved_at'] = __import__('time').time()
    return cache_set(f"pending:{mint}", token_data, TTL_PENDING_TOKEN)


def get_pending_token(mint: str) -> dict | None:
    """Get pending token by mint."""
    return cache_get(f"pending:{mint}")


def remove_pending_token(mint: str) -> bool:
    """Remove token from pending."""
    return cache_delete(f"pending:{mint}")


def get_all_pending_tokens() -> dict[str, dict]:
    """Get all pending tokens."""
    result = {}
    for key in cache_keys("pending:*"):
        mint = key.replace("pending:", "")
        data = cache_get(key)
        if data:
            result[mint] = data
    return result


def count_pending_tokens() -> int:
    """Count pending tokens."""
    return len(cache_keys("pending:*"))


def cleanup_old_pending(max_age_seconds: int = 600) -> int:
    """Remove pending tokens older than max_age. Returns count removed."""
    import time
    now = time.time()
    removed = 0
    for key in cache_keys("pending:*"):
        data = cache_get(key)
        if data and data.get('saved_at', 0) < now - max_age_seconds:
            cache_delete(key)
            removed += 1
    return removed


# ============================================
# PUMP SIGNALS (detected patterns)
# ============================================
def save_pump_signal(mint: str, signal_data: dict) -> bool:
    """Save detected pump signal."""
    signal_data['detected_at'] = __import__('time').time()
    return cache_set(f"signal:{mint}", signal_data, TTL_PUMP_SIGNAL)


def get_pump_signal(mint: str) -> dict | None:
    """Get pump signal for token."""
    return cache_get(f"signal:{mint}")


def get_all_signals() -> dict[str, dict]:
    """Get all active signals."""
    result = {}
    for key in cache_keys("signal:*"):
        mint = key.replace("signal:", "")
        data = cache_get(key)
        if data:
            result[mint] = data
    return result


# ============================================
# PURCHASED TOKENS (cross-bot deduplication)
# ============================================
def mark_token_purchased(mint: str, data: dict = None) -> bool:
    """Mark token as purchased (prevents duplicate buys across bots)."""
    import time
    purchase_data = data or {}
    purchase_data['purchased_at'] = time.time()
    return cache_set(f"bought:{mint}", purchase_data, TTL_PURCHASED)


def is_token_purchased(mint: str) -> bool:
    """Check if token was already purchased by any bot."""
    return cache_get(f"bought:{mint}") is not None


def get_purchased_token(mint: str) -> dict | None:
    """Get purchase info."""
    return cache_get(f"bought:{mint}")


def get_all_purchased() -> dict[str, dict]:
    """Get all purchased tokens."""
    result = {}
    for key in cache_keys("bought:*"):
        mint = key.replace("bought:", "")
        data = cache_get(key)
        if data:
            result[mint] = data
    return result


def count_purchased() -> int:
    """Count purchased tokens."""
    return len(cache_keys("bought:*"))


# ============================================
# DEXSCREENER CACHE
# ============================================
def save_dex_data(mint: str, data: dict) -> bool:
    """Cache Dexscreener data."""
    return cache_set(f"dex:{mint}", data, TTL_DEXSCREENER)


def get_dex_data(mint: str) -> dict | None:
    """Get cached Dexscreener data."""
    return cache_get(f"dex:{mint}")


# ============================================
# TOKEN SCORES CACHE
# ============================================
def save_token_score(mint: str, score_data: dict) -> bool:
    """Cache token score."""
    return cache_set(f"score:{mint}", score_data, TTL_TOKEN_SCORE)


def get_token_score(mint: str) -> dict | None:
    """Get cached token score."""
    return cache_get(f"score:{mint}")


# ============================================
# CREATOR REPUTATION CACHE
# ============================================
def save_creator_rep(creator: str, rep_data: dict) -> bool:
    """Cache creator reputation."""
    return cache_set(f"creator:{creator}", rep_data, TTL_CREATOR_REP)


def get_creator_rep(creator: str) -> dict | None:
    """Get cached creator reputation."""
    return cache_get(f"creator:{creator}")


# ============================================
# STATS
# ============================================
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
            "total_keys": r.dbsize(),
            "pending": count_pending_tokens(),
            "purchased": count_purchased(),
            "signals": len(cache_keys("signal:*")),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
