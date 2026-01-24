"""Тесты для Trading Limits"""
import pytest
import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path

from src.trading.trading_limits import (
    TradingLimits, LimitsTracker, AutoSweepConfig, AutoSweeper
)


@pytest.fixture
def limits():
    return TradingLimits(
        max_buy_amount_sol=Decimal('0.1'),
        min_buy_amount_sol=Decimal('0.01'),
        max_trades_per_hour=5,
        max_trades_per_day=20
    )


@pytest.fixture
def tracker(limits, tmp_path):
    return LimitsTracker(
        limits=limits,
        persistence_file=str(tmp_path / 'limits.json')
    )


@pytest.mark.asyncio
async def test_can_execute_trade_basic(tracker):
    """Тест базовой проверки лимитов"""
    can_trade, reason = await tracker.can_execute_trade(
        'buy', Decimal('0.05'), 'test_mint'
    )
    assert can_trade
    assert reason == 'OK'


@pytest.mark.asyncio
async def test_amount_below_minimum(tracker):
    """Тест минимальной суммы"""
    can_trade, reason = await tracker.can_execute_trade(
        'buy', Decimal('0.001'), 'test_mint'
    )
    assert not can_trade
    assert 'below minimum' in reason


@pytest.mark.asyncio
async def test_amount_above_maximum(tracker):
    """Тест максимальной суммы"""
    can_trade, reason = await tracker.can_execute_trade(
        'buy', Decimal('1.0'), 'test_mint'
    )
    assert not can_trade
    assert 'exceeds maximum' in reason


@pytest.mark.asyncio
async def test_hourly_limit(tracker):
    """Тест часового лимита"""
    # Записываем 5 сделок (лимит)
    for i in range(5):
        await tracker.record_trade('buy', f'mint_{i}', Decimal('0.01'), True)
    
    # 6-я должна быть отклонена
    can_trade, reason = await tracker.can_execute_trade(
        'buy', Decimal('0.01'), 'test_mint'
    )
    assert not can_trade
    assert 'Hourly trade limit' in reason


@pytest.mark.asyncio
async def test_stats(tracker):
    """Тест статистики"""
    await tracker.record_trade('buy', 'mint1', Decimal('0.05'), True)
    await tracker.record_trade('sell', 'mint1', Decimal('0.06'), True, Decimal('0.01'))
    
    stats = tracker.get_stats()
    
    assert stats['hourly_trades'] == 2
    assert stats['daily_trades'] == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
