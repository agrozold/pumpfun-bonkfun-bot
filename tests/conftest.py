"""
Pytest fixtures for pumpfun-bonkfun-bot tests
"""
import pytest
import asyncio
import os
from unittest.mock import MagicMock, AsyncMock

# Отключаем реальные подключения в тестах
os.environ['TESTING'] = '1'
os.environ['AI_AGENT_MODE'] = '0'


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_rpc_client():
    """Mock Solana RPC client"""
    client = MagicMock()
    client.get_latest_blockhash = AsyncMock(return_value={
        'value': {'blockhash': 'test_blockhash_123', 'lastValidBlockHeight': 100000}
    })
    client.send_transaction = AsyncMock(return_value={'result': 'test_signature_abc'})
    client.get_balance = AsyncMock(return_value={'value': 1_000_000_000})  # 1 SOL
    return client


@pytest.fixture
def mock_redis():
    """Mock Redis client"""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.exists = AsyncMock(return_value=0)
    return redis


@pytest.fixture
def sample_token_data():
    """Sample token data for tests"""
    return {
        'mint': 'TestMint123456789abcdefghijk',
        'name': 'Test Token',
        'symbol': 'TEST',
        'creator': 'Creator123456789abcdefghijk',
        'uri': 'https://example.com/metadata.json',
        'slot': 250000000
    }


@pytest.fixture
def sample_position_data():
    """Sample position data"""
    return {
        'mint': 'TestMint123456789abcdefghijk',
        'token_amount': 1000000,
        'sol_spent': 0.01,
        'buy_price': 0.00001,
        'platform': 'pump_fun',
        'is_active': True
    }
