"""Redis sync - периодически синхронизирует данные с Redis."""

import asyncio
import time
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    from core.redis_cache import (
        save_pending_token, get_all_pending_tokens, cleanup_old_pending,
        save_pump_signal, get_all_signals,
        mark_token_purchased, get_all_purchased,
        cache_stats
    )
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class RedisSync:
    """Синхронизирует данные трейдера с Redis."""

    def __init__(self, trader):
        self.trader = trader
        self.sync_interval = 5  # секунд
        self.running = False

    async def start(self):
        """Запуск синхронизации."""
        if not REDIS_AVAILABLE:
            logger.warning("[REDIS_SYNC] Redis not available, sync disabled")
            return

        self.running = True
        logger.info("[REDIS_SYNC] Started")

        # Загружаем данные из Redis при старте
        await self._load_from_redis()

        # Периодическая синхронизация
        while self.running:
            try:
                await self._sync_to_redis()
                await asyncio.sleep(self.sync_interval)
            except Exception as e:
                logger.error(f"[REDIS_SYNC] Error: {e}")
                await asyncio.sleep(10)

    def stop(self):
        """Остановка синхронизации."""
        self.running = False
        logger.info("[REDIS_SYNC] Stopped")

    async def _load_from_redis(self):
        """Загрузка данных из Redis при старте."""
        try:
            # Загружаем pending tokens
            pending = get_all_pending_tokens()
            logger.info(f"[REDIS_SYNC] Loaded {len(pending)} pending tokens from Redis")

            # Загружаем сигналы
            signals = get_all_signals()
            logger.info(f"[REDIS_SYNC] Loaded {len(signals)} signals from Redis")

            # Загружаем купленные
            purchased = get_all_purchased()
            logger.info(f"[REDIS_SYNC] Loaded {len(purchased)} purchased tokens from Redis")

        except Exception as e:
            logger.error(f"[REDIS_SYNC] Load error: {e}")

    async def _sync_to_redis(self):
        """Синхронизация данных в Redis."""
        try:
            # Сохраняем pending tokens
            if hasattr(self.trader, 'pending_tokens'):
                for mint, token_info in self.trader.pending_tokens.items():
                    symbol = token_info.symbol if hasattr(token_info, 'symbol') else 'unknown'
                    save_pending_token(mint, {
                        "symbol": symbol,
                        "time": time.time()
                    })

            # Сохраняем сигналы
            if hasattr(self.trader, 'pump_signals'):
                for mint, patterns in self.trader.pump_signals.items():
                    save_pump_signal(mint, {
                        "patterns": patterns,
                        "time": time.time()
                    })

            # Очищаем старые pending (>10 мин)
            removed = cleanup_old_pending(600)
            if removed > 0:
                logger.info(f"[REDIS_SYNC] Cleaned up {removed} old pending tokens")

        except Exception as e:
            logger.debug(f"[REDIS_SYNC] Sync error: {e}")


def log_redis_stats():
    """Логирует статистику Redis."""
    if not REDIS_AVAILABLE:
        return
    stats = cache_stats()
    logger.info(f"[REDIS] Stats: {stats}")
