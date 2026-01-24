"""Unit tests for DedupStore"""
import pytest
import asyncio
from trading.dedup_store import (
    TokenStatus,
    SQLiteDedupStore,
    RedisDedupStore,
)


class TestTokenStatus:
    def test_all_statuses(self):
        statuses = [s.value for s in TokenStatus]
        assert "seen" in statuses
        assert "buying" in statuses
        assert "bought" in statuses
        assert "failed" in statuses


class TestSQLiteDedupStore:
    @pytest.fixture
    def store(self, tmp_path):
        return SQLiteDedupStore(db_path=str(tmp_path / "test_dedup.db"))
    
    @pytest.mark.asyncio
    async def test_try_acquire(self, store):
        mint = "TestMint123"
        
        # Первый захват должен успеть
        result = await store.try_acquire(mint, "bot1")
        assert result is True
        
        # Второй захват того же токена должен провалиться
        result = await store.try_acquire(mint, "bot2")
        assert result is False
    
    @pytest.mark.asyncio
    async def test_mark_bought(self, store):
        mint = "TestMint456"
        
        await store.try_acquire(mint, "bot1")
        await store.mark_bought(mint, "bot1")
        
        # После bought нельзя захватить
        result = await store.try_acquire(mint, "bot2")
        assert result is False
        
        status = await store.get_status(mint)
        assert status == TokenStatus.BOUGHT
    
    @pytest.mark.asyncio
    async def test_release(self, store):
        mint = "TestMint789"
        
        await store.try_acquire(mint, "bot1")
        await store.release(mint)
        
        # После release можно захватить
        result = await store.try_acquire(mint, "bot2")
        assert result is True
    
    @pytest.mark.asyncio
    async def test_is_processed(self, store):
        mint = "TestMintABC"
        
        assert await store.is_processed(mint) is False
        
        await store.try_acquire(mint, "bot1")
        assert await store.is_processed(mint) is True
