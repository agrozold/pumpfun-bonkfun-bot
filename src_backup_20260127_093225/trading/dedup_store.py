"""
Dedup Store - дедупликация токенов для мультипроцессного режима.
Поддерживает Redis (primary) и SQLite (fallback).
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Protocol
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class TokenStatus(Enum):
    """Статусы токена в lifecycle"""
    SEEN = "seen"              # Токен обнаружен
    PENDING_CHECK = "pending"  # Проверяется (scoring, dev check)
    BUY_INFLIGHT = "buying"    # Покупка в процессе
    BOUGHT = "bought"          # Куплен успешно
    FAILED = "failed"          # Неудача (не покупать повторно)
    SKIPPED = "skipped"        # Пропущен по фильтрам


@dataclass
class TokenState:
    """Состояние токена"""
    mint: str
    status: TokenStatus
    timestamp: float
    bot_name: str = ""
    reason: str = ""
    trace_id: str = ""


class DedupStore(Protocol):
    """Протокол для хранилища дедупликации"""
    
    async def try_acquire(self, mint: str, bot_name: str, ttl: int = 3600) -> bool:
        """Попытаться захватить токен для покупки. Возвращает True если успешно."""
        ...
    
    async def release(self, mint: str) -> None:
        """Освободить токен (при ошибке)"""
        ...
    
    async def mark_bought(self, mint: str, bot_name: str) -> None:
        """Отметить токен как купленный"""
        ...
    
    async def mark_failed(self, mint: str, reason: str) -> None:
        """Отметить токен как failed"""
        ...
    
    async def is_processed(self, mint: str) -> bool:
        """Проверить, обработан ли токен"""
        ...
    
    async def get_status(self, mint: str) -> Optional[TokenStatus]:
        """Получить статус токена"""
        ...


class RedisDedupStore:
    """
    Redis-based дедупликация с SETNX для атомарности.
    
    Ключи:
    - dedup:buying:{mint} - токен в процессе покупки (TTL 60s)
    - dedup:bought:{mint} - токен куплен (TTL 24h)
    - dedup:failed:{mint} - токен failed (TTL 1h)
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        key_prefix: str = "dedup",
        buying_ttl: int = 60,      # 60 секунд на покупку
        bought_ttl: int = 86400,   # 24 часа
        failed_ttl: int = 3600,    # 1 час
    ):
        self.host = host
        self.port = port
        self.db = db
        self.key_prefix = key_prefix
        self.buying_ttl = buying_ttl
        self.bought_ttl = bought_ttl
        self.failed_ttl = failed_ttl
        self._redis = None
        self._connected = False
    
    async def connect(self) -> bool:
        """Подключиться к Redis"""
        if self._connected:
            return True
        
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                decode_responses=True
            )
            # Проверяем соединение
            await self._redis.ping()
            self._connected = True
            logger.info(f"[DEDUP] Connected to Redis at {self.host}:{self.port}")
            return True
        except ImportError:
            logger.warning("[DEDUP] redis.asyncio not installed, falling back to sync redis")
            try:
                import redis
                self._redis = redis.Redis(
                    host=self.host,
                    port=self.port,
                    db=self.db,
                    decode_responses=True
                )
                self._redis.ping()
                self._connected = True
                logger.info(f"[DEDUP] Connected to Redis (sync) at {self.host}:{self.port}")
                return True
            except Exception as e:
                logger.error(f"[DEDUP] Redis connection failed: {e}")
                return False
        except Exception as e:
            logger.error(f"[DEDUP] Redis connection failed: {e}")
            return False
    
    def _key(self, status: str, mint: str) -> str:
        """Генерация ключа"""
        return f"{self.key_prefix}:{status}:{mint}"
    
    async def try_acquire(self, mint: str, bot_name: str, ttl: int = None) -> bool:
        """
        Атомарно попытаться захватить токен для покупки.
        Использует SETNX для race-free операции.
        """
        if not await self.connect():
            logger.warning(f"[DEDUP] Redis unavailable, allowing {mint[:8]}...")
            return True  # Fail open
        
        ttl = ttl or self.buying_ttl
        key = self._key("buying", mint)
        bought_key = self._key("bought", mint)
        
        try:
            # Проверяем, не куплен ли уже
            if await self._exists(bought_key):
                logger.info(f"[DEDUP] Token {mint[:8]}... already bought")
                return False
            
            # Пытаемся захватить (SETNX + EXPIRE)
            value = json.dumps({
                "bot": bot_name,
                "ts": time.time(),
                "status": TokenStatus.BUY_INFLIGHT.value
            })
            
            # SET key value NX EX ttl - атомарная операция
            result = await self._set_nx(key, value, ttl)
            
            if result:
                logger.info(f"[DEDUP] Acquired {mint[:8]}... for {bot_name}")
                return True
            else:
                # Кто-то уже захватил
                existing = await self._get(key)
                if existing:
                    data = json.loads(existing)
                    logger.info(f"[DEDUP] Token {mint[:8]}... already acquired by {data.get('bot', 'unknown')}")
                return False
                
        except Exception as e:
            logger.error(f"[DEDUP] try_acquire error: {e}")
            return True  # Fail open
    
    async def release(self, mint: str) -> None:
        """Освободить токен (при ошибке до покупки)"""
        if not self._connected:
            return
        
        key = self._key("buying", mint)
        try:
            await self._delete(key)
            logger.info(f"[DEDUP] Released {mint[:8]}...")
        except Exception as e:
            logger.error(f"[DEDUP] release error: {e}")
    
    async def mark_bought(self, mint: str, bot_name: str) -> None:
        """Отметить токен как купленный (перманентно)"""
        if not await self.connect():
            return
        
        buying_key = self._key("buying", mint)
        bought_key = self._key("bought", mint)
        
        try:
            value = json.dumps({
                "bot": bot_name,
                "ts": time.time(),
                "status": TokenStatus.BOUGHT.value
            })
            
            # Удаляем buying, добавляем bought
            await self._delete(buying_key)
            await self._set_ex(bought_key, value, self.bought_ttl)
            
            logger.info(f"[DEDUP] Marked {mint[:8]}... as BOUGHT by {bot_name}")
        except Exception as e:
            logger.error(f"[DEDUP] mark_bought error: {e}")
    
    async def mark_failed(self, mint: str, reason: str) -> None:
        """Отметить токен как failed"""
        if not await self.connect():
            return
        
        buying_key = self._key("buying", mint)
        failed_key = self._key("failed", mint)
        
        try:
            value = json.dumps({
                "reason": reason,
                "ts": time.time(),
                "status": TokenStatus.FAILED.value
            })
            
            await self._delete(buying_key)
            await self._set_ex(failed_key, value, self.failed_ttl)
            
            logger.info(f"[DEDUP] Marked {mint[:8]}... as FAILED: {reason}")
        except Exception as e:
            logger.error(f"[DEDUP] mark_failed error: {e}")
    
    async def is_processed(self, mint: str) -> bool:
        """Проверить, обработан ли токен (buying, bought, or failed)"""
        if not await self.connect():
            return False
        
        try:
            for status in ["buying", "bought", "failed"]:
                if await self._exists(self._key(status, mint)):
                    return True
            return False
        except Exception as e:
            logger.error(f"[DEDUP] is_processed error: {e}")
            return False
    
    async def get_status(self, mint: str) -> Optional[TokenStatus]:
        """Получить статус токена"""
        if not await self.connect():
            return None
        
        try:
            for status in ["bought", "buying", "failed"]:
                key = self._key(status, mint)
                if await self._exists(key):
                    return TokenStatus(status if status != "buying" else "buying")
            return None
        except Exception as e:
            logger.error(f"[DEDUP] get_status error: {e}")
            return None
    
    # === Redis операции (поддержка sync и async) ===
    
    async def _set_nx(self, key: str, value: str, ttl: int) -> bool:
        """SET key value NX EX ttl"""
        if asyncio.iscoroutinefunction(self._redis.set):
            return await self._redis.set(key, value, nx=True, ex=ttl)
        return self._redis.set(key, value, nx=True, ex=ttl)
    
    async def _set_ex(self, key: str, value: str, ttl: int) -> None:
        """SET key value EX ttl"""
        if asyncio.iscoroutinefunction(self._redis.set):
            await self._redis.set(key, value, ex=ttl)
        else:
            self._redis.set(key, value, ex=ttl)
    
    async def _get(self, key: str) -> Optional[str]:
        """GET key"""
        if asyncio.iscoroutinefunction(self._redis.get):
            return await self._redis.get(key)
        return self._redis.get(key)
    
    async def _exists(self, key: str) -> bool:
        """EXISTS key"""
        if asyncio.iscoroutinefunction(self._redis.exists):
            return await self._redis.exists(key) > 0
        return self._redis.exists(key) > 0
    
    async def _delete(self, key: str) -> None:
        """DEL key"""
        if asyncio.iscoroutinefunction(self._redis.delete):
            await self._redis.delete(key)
        else:
            self._redis.delete(key)


class SQLiteDedupStore:
    """
    SQLite fallback для дедупликации.
    Использует file locking для мультипроцессности.
    """
    
    def __init__(self, db_path: str = "data/dedup.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False
    
    async def _init_db(self) -> None:
        """Инициализация БД"""
        if self._initialized:
            return
        
        import aiosqlite
        
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    mint TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    bot_name TEXT,
                    reason TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_status ON tokens(status)
            """)
            await db.commit()
        
        self._initialized = True
        logger.info(f"[DEDUP] SQLite initialized at {self.db_path}")
    
    async def try_acquire(self, mint: str, bot_name: str, ttl: int = 3600) -> bool:
        """Попытаться захватить токен"""
        await self._init_db()
        
        import aiosqlite
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                # Проверяем существующий статус
                cursor = await db.execute(
                    "SELECT status FROM tokens WHERE mint = ?",
                    (mint,)
                )
                row = await cursor.fetchone()
                
                if row:
                    status = row[0]
                    if status in ("bought", "buying"):
                        return False
                
                # Вставляем или обновляем
                now = time.time()
                await db.execute("""
                    INSERT INTO tokens (mint, status, bot_name, created_at, updated_at)
                    VALUES (?, 'buying', ?, ?, ?)
                    ON CONFLICT(mint) DO UPDATE SET
                        status = 'buying',
                        bot_name = ?,
                        updated_at = ?
                    WHERE status NOT IN ('bought', 'buying')
                """, (mint, bot_name, now, now, bot_name, now))
                
                await db.commit()
                
                # Проверяем что мы захватили
                cursor = await db.execute(
                    "SELECT bot_name FROM tokens WHERE mint = ? AND status = 'buying'",
                    (mint,)
                )
                row = await cursor.fetchone()
                
                if row and row[0] == bot_name:
                    logger.info(f"[DEDUP/SQLite] Acquired {mint[:8]}... for {bot_name}")
                    return True
                return False
                
        except Exception as e:
            logger.error(f"[DEDUP/SQLite] try_acquire error: {e}")
            return True  # Fail open
    
    async def release(self, mint: str) -> None:
        """Освободить токен"""
        await self._init_db()
        
        import aiosqlite
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    "DELETE FROM tokens WHERE mint = ? AND status = 'buying'",
                    (mint,)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[DEDUP/SQLite] release error: {e}")
    
    async def mark_bought(self, mint: str, bot_name: str) -> None:
        """Отметить как купленный"""
        await self._init_db()
        
        import aiosqlite
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute("""
                    INSERT INTO tokens (mint, status, bot_name, created_at, updated_at)
                    VALUES (?, 'bought', ?, ?, ?)
                    ON CONFLICT(mint) DO UPDATE SET
                        status = 'bought',
                        bot_name = ?,
                        updated_at = ?
                """, (mint, bot_name, time.time(), time.time(), bot_name, time.time()))
                await db.commit()
        except Exception as e:
            logger.error(f"[DEDUP/SQLite] mark_bought error: {e}")
    
    async def mark_failed(self, mint: str, reason: str) -> None:
        """Отметить как failed"""
        await self._init_db()
        
        import aiosqlite
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute("""
                    INSERT INTO tokens (mint, status, reason, created_at, updated_at)
                    VALUES (?, 'failed', ?, ?, ?)
                    ON CONFLICT(mint) DO UPDATE SET
                        status = 'failed',
                        reason = ?,
                        updated_at = ?
                """, (mint, reason, time.time(), time.time(), reason, time.time()))
                await db.commit()
        except Exception as e:
            logger.error(f"[DEDUP/SQLite] mark_failed error: {e}")
    
    async def is_processed(self, mint: str) -> bool:
        """Проверить обработан ли"""
        await self._init_db()
        
        import aiosqlite
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute(
                    "SELECT 1 FROM tokens WHERE mint = ?",
                    (mint,)
                )
                return await cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"[DEDUP/SQLite] is_processed error: {e}")
            return False
    
    async def get_status(self, mint: str) -> Optional[TokenStatus]:
        """Получить статус"""
        await self._init_db()
        
        import aiosqlite
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute(
                    "SELECT status FROM tokens WHERE mint = ?",
                    (mint,)
                )
                row = await cursor.fetchone()
                if row:
                    return TokenStatus(row[0])
                return None
        except Exception as e:
            logger.error(f"[DEDUP/SQLite] get_status error: {e}")
            return None


class DedupStoreFactory:
    """Фабрика для создания DedupStore"""
    
    @staticmethod
    async def create(
        backend: str = "redis",
        redis_host: str = "localhost",
        redis_port: int = 6379,
        sqlite_path: str = "data/dedup.db"
    ) -> DedupStore:
        """
        Создать DedupStore.
        
        Args:
            backend: "redis" или "sqlite"
            
        Returns:
            DedupStore instance
        """
        if backend == "redis":
            store = RedisDedupStore(host=redis_host, port=redis_port)
            if await store.connect():
                return store
            logger.warning("[DEDUP] Redis unavailable, falling back to SQLite")
        
        return SQLiteDedupStore(db_path=sqlite_path)


# === Глобальный instance ===

_store: Optional[DedupStore] = None


async def get_dedup_store() -> DedupStore:
    """Получить глобальный DedupStore"""
    global _store
    if _store is None:
        _store = await DedupStoreFactory.create()
    return _store


async def try_acquire_token(mint: str, bot_name: str) -> bool:
    """Удобная функция для захвата токена"""
    store = await get_dedup_store()
    return await store.try_acquire(mint, bot_name)


async def mark_token_bought(mint: str, bot_name: str) -> None:
    """Удобная функция для отметки покупки"""
    store = await get_dedup_store()
    await store.mark_bought(mint, bot_name)
