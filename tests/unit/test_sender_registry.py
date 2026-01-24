"""Unit tests for SenderRegistry"""
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from src.core.sender import SendStatus, SendResult, SendProvider
from src.core.sender_registry import SenderRegistry, SendStrategy


class MockProvider:
    """Mock send provider for testing"""
    
    def __init__(self, name: str, priority: int, should_succeed: bool = True, latency: float = 0.01):
        self.name = name
        self.priority = priority
        self._should_succeed = should_succeed
        self._latency = latency
        self._healthy = True
        self._send_count = 0
    
    async def send(self, tx_bytes: bytes, trace_id: str = None) -> SendResult:
        self._send_count += 1
        await asyncio.sleep(self._latency)
        
        if self._should_succeed:
            return SendResult(
                status=SendStatus.SUCCESS,
                signature='sig_' + self.name,
                provider=self.name,
                latency_ms=self._latency * 1000
            )
        else:
            return SendResult(
                status=SendStatus.FAILED,
                provider=self.name,
                error='Mock failure'
            )
    
    async def confirm(self, signature: str, timeout: float = 30.0):
        return {'confirmed': True}
    
    def is_healthy(self) -> bool:
        return self._healthy
    
    async def close(self) -> None:
        pass


class TestSenderRegistry:
    """Tests for SenderRegistry"""
    
    @pytest.fixture
    def registry(self):
        return SenderRegistry(strategy=SendStrategy.FALLBACK)
    
    @pytest.fixture
    def providers(self):
        return [
            MockProvider('helius', priority=1, should_succeed=True),
            MockProvider('jito', priority=2, should_succeed=True),
            MockProvider('quicknode', priority=3, should_succeed=True)
        ]
    
    def test_register_provider(self, registry, providers):
        for p in providers:
            registry.register(p)
        
        active = registry.get_active_providers()
        assert len(active) == 3
        assert active[0].name == 'helius'  # Sorted by priority
    
    def test_disable_enable_provider(self, registry, providers):
        for p in providers:
            registry.register(p)
        
        registry.disable('helius')
        active = registry.get_active_providers()
        assert len(active) == 2
        assert all(p.name != 'helius' for p in active)
        
        registry.enable('helius')
        active = registry.get_active_providers()
        assert len(active) == 3
    
    @pytest.mark.asyncio
    async def test_fallback_strategy(self, registry):
        p1 = MockProvider('first', priority=1, should_succeed=True)
        p2 = MockProvider('second', priority=2, should_succeed=True)
        
        registry.register(p1)
        registry.register(p2)
        
        result = await registry.send(b'test_tx', strategy=SendStrategy.FALLBACK)
        
        assert result.is_success
        assert result.provider == 'first'
        assert p1._send_count == 1
        assert p2._send_count == 0  # Not called because first succeeded
    
    @pytest.mark.asyncio
    async def test_fallback_to_second(self, registry):
        p1 = MockProvider('first', priority=1, should_succeed=False)
        p2 = MockProvider('second', priority=2, should_succeed=True)
        
        registry.register(p1)
        registry.register(p2)
        
        result = await registry.send(b'test_tx', strategy=SendStrategy.FALLBACK)
        
        assert result.is_success
        assert result.provider == 'second'
        assert p1._send_count == 1
        assert p2._send_count == 1
    
    @pytest.mark.asyncio
    async def test_race_strategy(self, registry):
        p1 = MockProvider('slow', priority=1, should_succeed=True, latency=0.1)
        p2 = MockProvider('fast', priority=2, should_succeed=True, latency=0.01)
        
        registry.register(p1)
        registry.register(p2)
        
        result = await registry.send(b'test_tx', strategy=SendStrategy.RACE)
        
        assert result.is_success
        # Fast provider should win
        assert result.provider == 'fast'
    
    @pytest.mark.asyncio
    async def test_no_providers_error(self, registry):
        result = await registry.send(b'test_tx')
        
        assert not result.is_success
        assert 'No active providers' in result.error
    
    @pytest.mark.asyncio
    async def test_all_fail(self, registry):
        p1 = MockProvider('first', priority=1, should_succeed=False)
        p2 = MockProvider('second', priority=2, should_succeed=False)
        
        registry.register(p1)
        registry.register(p2)
        
        result = await registry.send(b'test_tx', strategy=SendStrategy.FALLBACK)
        
        assert not result.is_success
    
    @pytest.mark.asyncio
    async def test_unhealthy_provider_skipped(self, registry):
        p1 = MockProvider('unhealthy', priority=1, should_succeed=True)
        p1._healthy = False
        p2 = MockProvider('healthy', priority=2, should_succeed=True)
        
        registry.register(p1)
        registry.register(p2)
        
        active = registry.get_active_providers()
        assert len(active) == 1
        assert active[0].name == 'healthy'
