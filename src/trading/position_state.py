"""
PositionState - State Machine для позиций
Явные состояния и валидация переходов
"""

from enum import Enum
from typing import Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class PositionState(Enum):
    """Состояния позиции"""
    PENDING_BUY = "pending_buy"       # TX покупки отправлена
    OPENING = "opening"               # Buy confirmed, инициализация
    OPEN = "open"                     # Активный мониторинг
    PARTIALLY_FILLED = "partial"      # Частичное исполнение
    PENDING_SELL = "pending_sell"     # Sell TX отправлена
    CLOSING = "closing"               # Sell confirmed, cleanup
    CLOSED = "closed"                 # Финальное состояние
    FAILED = "failed"                 # Ошибка


class InvalidStateTransitionError(Exception):
    """Недопустимый переход состояния"""
    pass


# Валидные переходы состояний
VALID_TRANSITIONS: dict[PositionState, Set[PositionState]] = {
    PositionState.PENDING_BUY: {
        PositionState.OPENING,
        PositionState.FAILED
    },
    PositionState.OPENING: {
        PositionState.OPEN,
        PositionState.FAILED
    },
    PositionState.OPEN: {
        PositionState.PENDING_SELL,
        PositionState.PARTIALLY_FILLED,
        PositionState.FAILED
    },
    PositionState.PARTIALLY_FILLED: {
        PositionState.PENDING_SELL,
        PositionState.OPEN,
        PositionState.FAILED
    },
    PositionState.PENDING_SELL: {
        PositionState.CLOSING,
        PositionState.OPEN,  # Если sell не прошёл
        PositionState.PARTIALLY_FILLED,
        PositionState.FAILED
    },
    PositionState.CLOSING: {
        PositionState.CLOSED,
        PositionState.FAILED
    },
    PositionState.CLOSED: set(),  # Финальное состояние
    PositionState.FAILED: set()   # Финальное состояние
}


@dataclass
class StateTransition:
    """Запись о переходе состояния"""
    from_state: Optional[PositionState]
    to_state: PositionState
    timestamp: datetime
    reason: str = ""
    trace_id: Optional[str] = None


@dataclass
class StateMachine:
    """
    State Machine для управления состоянием позиции.
    
    Использование:
        sm = StateMachine(initial_state=PositionState.PENDING_BUY)
        sm.transition_to(PositionState.OPENING, reason="buy confirmed")
    """

    current_state: PositionState
    history: list = field(default_factory=list)

    def __post_init__(self):
        # Записываем начальное состояние
        self.history.append(StateTransition(
            from_state=None,
            to_state=self.current_state,
            timestamp=datetime.utcnow(),
            reason="initial"
        ))

    def can_transition_to(self, new_state: PositionState) -> bool:
        """Проверить возможность перехода"""
        valid_next = VALID_TRANSITIONS.get(self.current_state, set())
        return new_state in valid_next

    def transition_to(
        self,
        new_state: PositionState,
        reason: str = "",
        trace_id: str = None,
        force: bool = False
    ) -> None:
        """
        Выполнить переход в новое состояние.
        
        Args:
            new_state: Целевое состояние
            reason: Причина перехода
            trace_id: ID трейса
            force: Принудительный переход (без валидации)
            
        Raises:
            InvalidStateTransitionError: При недопустимом переходе
        """
        if not force and not self.can_transition_to(new_state):
            raise InvalidStateTransitionError(
                f"Invalid transition: {self.current_state.value} -> {new_state.value}"
            )

        old_state = self.current_state
        self.current_state = new_state

        transition = StateTransition(
            from_state=old_state,
            to_state=new_state,
            timestamp=datetime.utcnow(),
            reason=reason,
            trace_id=trace_id
        )
        self.history.append(transition)

        logger.info(
            f"State transition: {old_state.value} -> {new_state.value} "
            f"(reason: {reason})"
        )

    @property
    def is_final(self) -> bool:
        """Проверка финального состояния"""
        return self.current_state in {PositionState.CLOSED, PositionState.FAILED}

    @property
    def is_active(self) -> bool:
        """Проверка активности (для обратной совместимости)"""
        return self.current_state in {
            PositionState.PENDING_BUY,
            PositionState.OPENING,
            PositionState.OPEN,
            PositionState.PARTIALLY_FILLED,
            PositionState.PENDING_SELL,
            PositionState.CLOSING
        }

    def to_dict(self) -> dict:
        """Сериализация для JSON"""
        return {
            'current_state': self.current_state.value,
            'history': [
                {
                    'from': t.from_state.value if t.from_state else None,
                    'to': t.to_state.value,
                    'timestamp': t.timestamp.isoformat(),
                    'reason': t.reason,
                    'trace_id': t.trace_id
                }
                for t in self.history
            ]
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'StateMachine':
        """Десериализация из JSON"""
        state_str = data.get('current_state', 'open')

        # Миграция: is_active -> state
        if 'current_state' not in data and 'is_active' in data:
            state_str = 'open' if data['is_active'] else 'closed'

        current = PositionState(state_str)
        sm = cls(current_state=current)

        # Восстановление истории
        if 'history' in data:
            sm.history = []
            for t in data['history']:
                sm.history.append(StateTransition(
                    from_state=PositionState(t['from']) if t.get('from') else None,
                    to_state=PositionState(t['to']),
                    timestamp=datetime.fromisoformat(t['timestamp']),
                    reason=t.get('reason', ''),
                    trace_id=t.get('trace_id')
                ))

        return sm
