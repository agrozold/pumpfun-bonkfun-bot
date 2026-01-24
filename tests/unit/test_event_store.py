"""Тесты для Event Store"""
import pytest
import asyncio
import tempfile
import shutil
from pathlib import Path

from src.trading.event_store import (
    Event, EventType, EventStore, emit_event, get_event_store
)


@pytest.fixture
def temp_dir():
    """Временная директория для тестов"""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def event_store(temp_dir):
    """EventStore для тестов"""
    return EventStore(base_dir=temp_dir)


def test_event_creation():
    """Тест создания события"""
    event = Event.create(
        EventType.BUY_INITIATED,
        position_id='pos123',
        mint='mint456',
        trace_id='trace789',
        amount_sol=0.05
    )
    
    assert event.event_id is not None
    assert len(event.event_id) == 12
    assert event.event_type == EventType.BUY_INITIATED
    assert event.position_id == 'pos123'
    assert event.mint == 'mint456'
    assert event.data['amount_sol'] == 0.05


def test_event_serialization():
    """Тест сериализации события"""
    event = Event.create(
        EventType.SELL_CONFIRMED,
        position_id='pos123',
        mint='mint456',
        sol_amount=0.1
    )
    
    data = event.to_dict()
    restored = Event.from_dict(data)
    
    assert restored.event_id == event.event_id
    assert restored.event_type == event.event_type
    assert restored.position_id == event.position_id


@pytest.mark.asyncio
async def test_event_store_append(event_store):
    """Тест добавления событий"""
    event = Event.create(
        EventType.POSITION_CREATED,
        position_id='test_pos',
        mint='test_mint'
    )
    
    await event_store.append(event)
    await event_store.flush()
    
    # Проверяем что файл создан
    files = list(Path(event_store.base_dir).glob('*.jsonl'))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_event_store_replay(event_store):
    """Тест восстановления состояния"""
    position_id = 'replay_test'
    
    # Создаём последовательность событий
    await event_store.append(Event.create(
        EventType.POSITION_CREATED,
        position_id=position_id,
        mint='test_mint'
    ))
    await event_store.append(Event.create(
        EventType.BUY_CONFIRMED,
        position_id=position_id,
        mint='test_mint',
        sol_amount=0.05,
        tokens=1000000
    ))
    await event_store.flush()
    
    # Восстанавливаем
    state = await event_store.replay_position(position_id)
    
    assert state['position_id'] == position_id
    assert state['state'] == 'open'
    assert state['buy_amount_sol'] == 0.05


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
