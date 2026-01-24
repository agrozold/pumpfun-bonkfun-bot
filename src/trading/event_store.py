"""
Event Sourcing для позиций.
Все изменения состояния записываются как события.
Позволяет восстановить состояние и провести аудит.
"""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable, Awaitable
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Типы событий позиции"""
    POSITION_CREATED = "position_created"
    POSITION_CLOSED = "position_closed"
    POSITION_FAILED = "position_failed"
    BUY_INITIATED = "buy_initiated"
    BUY_TX_SENT = "buy_tx_sent"
    BUY_CONFIRMED = "buy_confirmed"
    BUY_FAILED = "buy_failed"
    SELL_INITIATED = "sell_initiated"
    SELL_TX_SENT = "sell_tx_sent"
    SELL_CONFIRMED = "sell_confirmed"
    SELL_FAILED = "sell_failed"
    PRICE_UPDATED = "price_updated"
    TAKE_PROFIT_TRIGGERED = "take_profit_triggered"
    STOP_LOSS_TRIGGERED = "stop_loss_triggered"
    MOON_BAG_CREATED = "moon_bag_created"
    STATE_CHANGED = "state_changed"
    ERROR_OCCURRED = "error_occurred"
    RETRY_ATTEMPTED = "retry_attempted"


@dataclass
class Event:
    """Базовое событие"""
    event_id: str
    event_type: EventType
    timestamp: str
    position_id: str
    mint: str
    data: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        event_type: EventType,
        position_id: str,
        mint: str,
        trace_id: str = None,
        **data
    ) -> 'Event':
        import uuid
        return cls(
            event_id=str(uuid.uuid4())[:12],
            event_type=event_type,
            timestamp=datetime.utcnow().isoformat() + 'Z',
            position_id=position_id,
            mint=mint,
            data=data,
            trace_id=trace_id
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'event_id': self.event_id,
            'event_type': self.event_type.value,
            'timestamp': self.timestamp,
            'position_id': self.position_id,
            'mint': self.mint,
            'data': self.data,
            'trace_id': self.trace_id
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Event':
        return cls(
            event_id=d['event_id'],
            event_type=EventType(d['event_type']),
            timestamp=d['timestamp'],
            position_id=d['position_id'],
            mint=d['mint'],
            data=d.get('data', {}),
            trace_id=d.get('trace_id')
        )


class EventStore:
    """
    Хранилище событий с JSONL persistence.

    Использование:
        store = EventStore('data/events')
        await store.append(event)
        events = await store.get_events_for_position(position_id)
    """

    def __init__(self, base_dir: str = 'data/events'):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._buffer: List[Event] = []
        self._buffer_size = 10
        self._lock = asyncio.Lock()
        self._subscribers: List[Callable[[Event], Awaitable[None]]] = []

    def _get_file_path(self, date: str = None) -> Path:
        """Получить путь к файлу событий по дате"""
        if date is None:
            date = datetime.utcnow().strftime('%Y-%m-%d')
        return self.base_dir / f'events_{date}.jsonl'

    async def append(self, event: Event) -> None:
        """Добавить событие"""
        async with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._buffer_size:
                await self._flush_unlocked()

        # Уведомить подписчиков
        for subscriber in self._subscribers:
            try:
                await subscriber(event)
            except Exception as e:
                logger.error(f"Event subscriber error: {e}")

    async def append_many(self, events: List[Event]) -> None:
        """Добавить несколько событий"""
        for event in events:
            await self.append(event)

    async def flush(self) -> None:
        """Принудительный сброс буфера"""
        async with self._lock:
            await self._flush_unlocked()

    async def _flush_unlocked(self) -> None:
        """Сброс буфера (вызывать внутри lock)"""
        if not self._buffer:
            return

        filepath = self._get_file_path()
        lines = [json.dumps(e.to_dict(), ensure_ascii=False) + '\n' for e in self._buffer]

        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.writelines(lines)
            self._buffer.clear()
        except Exception as e:
            logger.error(f"Failed to flush events: {e}")

    async def get_events_for_position(self, position_id: str, days: int = 7) -> List[Event]:
        """Получить все события для позиции за последние N дней"""
        events = []

        for i in range(days):
            date = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            filepath = self._get_file_path(date)

            if not filepath.exists():
                continue

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            data = json.loads(line.strip())
                            if data.get('position_id') == position_id:
                                events.append(Event.from_dict(data))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.error(f"Error reading events file {filepath}: {e}")

        return sorted(events, key=lambda e: e.timestamp)

    async def get_events_for_mint(self, mint: str, days: int = 7) -> List[Event]:
        """Получить все события для mint за последние N дней"""
        events = []

        for i in range(days):
            date = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            filepath = self._get_file_path(date)

            if not filepath.exists():
                continue

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            data = json.loads(line.strip())
                            if data.get('mint') == mint:
                                events.append(Event.from_dict(data))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.error(f"Error reading events file {filepath}: {e}")

        return sorted(events, key=lambda e: e.timestamp)

    async def get_recent_events(self, limit: int = 100) -> List[Event]:
        """Получить последние N событий"""
        events = []
        filepath = self._get_file_path()

        if filepath.exists():
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    for line in lines[-limit:]:
                        try:
                            data = json.loads(line.strip())
                            events.append(Event.from_dict(data))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.error(f"Error reading recent events: {e}")

        return events

    def subscribe(self, callback: Callable[[Event], Awaitable[None]]) -> None:
        """Подписаться на события"""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Event], Awaitable[None]]) -> None:
        """Отписаться от событий"""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def replay_position(self, position_id: str) -> Dict[str, Any]:
        """
        Восстановить состояние позиции из событий.
        Возвращает словарь с текущим состоянием.
        """
        events = await self.get_events_for_position(position_id)

        if not events:
            return {}

        state = {
            'position_id': position_id,
            'mint': events[0].mint,
            'created_at': events[0].timestamp,
            'state': 'unknown',
            'buy_signature': None,
            'sell_signature': None,
            'buy_amount_sol': None,
            'sell_amount_sol': None,
            'tokens_bought': None,
            'tokens_sold': None,
            'pnl_sol': None,
            'events_count': len(events),
            'last_event': events[-1].timestamp
        }

        for event in events:
            if event.event_type == EventType.POSITION_CREATED:
                state['state'] = 'created'
                state.update(event.data)

            elif event.event_type == EventType.BUY_TX_SENT:
                state['buy_signature'] = event.data.get('signature')
                state['state'] = 'pending_buy'

            elif event.event_type == EventType.BUY_CONFIRMED:
                state['state'] = 'open'
                state['buy_amount_sol'] = event.data.get('sol_amount')
                state['tokens_bought'] = event.data.get('tokens')

            elif event.event_type == EventType.BUY_FAILED:
                state['state'] = 'failed'
                state['fail_reason'] = event.data.get('reason')

            elif event.event_type == EventType.SELL_TX_SENT:
                state['sell_signature'] = event.data.get('signature')
                state['state'] = 'pending_sell'

            elif event.event_type == EventType.SELL_CONFIRMED:
                state['state'] = 'closed'
                state['sell_amount_sol'] = event.data.get('sol_amount')
                state['tokens_sold'] = event.data.get('tokens')
                if state['buy_amount_sol'] and state['sell_amount_sol']:
                    state['pnl_sol'] = state['sell_amount_sol'] - state['buy_amount_sol']

            elif event.event_type == EventType.POSITION_CLOSED:
                state['state'] = 'closed'

            elif event.event_type == EventType.POSITION_FAILED:
                state['state'] = 'failed'

        return state

    async def get_stats(self, days: int = 1) -> Dict[str, Any]:
        """Получить статистику событий"""
        stats = {
            'total_events': 0,
            'events_by_type': {},
            'unique_positions': set(),
            'unique_mints': set()
        }

        for i in range(days):
            date = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            filepath = self._get_file_path(date)

            if not filepath.exists():
                continue

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            data = json.loads(line.strip())
                            stats['total_events'] += 1

                            event_type = data.get('event_type', 'unknown')
                            stats['events_by_type'][event_type] = stats['events_by_type'].get(event_type, 0) + 1

                            if data.get('position_id'):
                                stats['unique_positions'].add(data['position_id'])
                            if data.get('mint'):
                                stats['unique_mints'].add(data['mint'])
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.error(f"Error reading stats: {e}")

        stats['unique_positions'] = len(stats['unique_positions'])
        stats['unique_mints'] = len(stats['unique_mints'])

        return stats


# Добавить импорт timedelta
from datetime import timedelta


# Глобальный экземпляр
_event_store: Optional[EventStore] = None


def get_event_store() -> EventStore:
    """Получить глобальный EventStore"""
    global _event_store
    if _event_store is None:
        _event_store = EventStore()
    return _event_store


async def emit_event(
    event_type: EventType,
    position_id: str,
    mint: str,
    trace_id: str = None,
    **data
) -> Event:
    """Удобная функция для создания и сохранения события"""
    event = Event.create(event_type, position_id, mint, trace_id, **data)
    await get_event_store().append(event)
    logger.debug(f"Event emitted: {event_type.value} for {position_id[:8]}...")
    return event
