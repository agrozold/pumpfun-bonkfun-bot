"""
TraceContext - сквозная трассировка транзакций
Управление trace_id через contextvars для автоматической привязки к логам
"""

import uuid
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from datetime import datetime

# Context variable для хранения текущего trace
_current_trace: ContextVar[Optional['TraceContext']] = ContextVar('current_trace', default=None)


@dataclass
class TraceEvent:
    """Событие в рамках трейса"""
    stage: str                    # t0_signal, t1_build, t2_send, t3_first_seen, t4_finalized
    timestamp_mono: float         # time.monotonic() для расчёта длительностей
    timestamp_wall: str           # ISO8601 для записи
    data: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def now(cls, stage: str, **data) -> 'TraceEvent':
        return cls(
            stage=stage,
            timestamp_mono=time.monotonic(),
            timestamp_wall=datetime.utcnow().isoformat() + 'Z',
            data=data
        )


@dataclass
class TraceContext:
    """Контекст трассировки одной торговой операции"""
    trace_id: str
    trade_type: str               # 'buy' | 'sell'
    mint: str
    source: str                   # 'pumpportal' | 'shredstream' | 'logs'
    slot_detected: Optional[int] = None
    events: list = field(default_factory=list)
    
    # Временные метки (monotonic)
    t0: Optional[float] = None    # Сигнал получен
    t1: Optional[float] = None    # Построение TX завершено
    t2: Optional[float] = None    # TX отправлена
    t3: Optional[float] = None    # Первое появление в сети
    t4: Optional[float] = None    # Финализация
    
    # Результат
    signature: Optional[str] = None
    slot_landed: Optional[int] = None
    tx_index: Optional[int] = None
    outcome: Optional[str] = None  # 'success' | 'fail'
    fail_reason: Optional[str] = None
    
    @classmethod
    def start(cls, trade_type: str, mint: str, source: str, slot_detected: int = None) -> 'TraceContext':
        """Создать новый контекст трассировки"""
        ctx = cls(
            trace_id=str(uuid.uuid4())[:12],
            trade_type=trade_type,
            mint=mint,
            source=source,
            slot_detected=slot_detected,
            t0=time.monotonic()
        )
        ctx.add_event('t0_signal', source=source, slot=slot_detected)
        _current_trace.set(ctx)
        return ctx
    
    def add_event(self, stage: str, **data) -> None:
        """Добавить событие в трейс"""
        self.events.append(TraceEvent.now(stage, **data))
    
    def mark_build_complete(self, **details) -> None:
        """Отметить завершение построения TX"""
        self.t1 = time.monotonic()
        self.add_event('t1_build_complete', **details)
    
    def mark_sent(self, provider: str, signature: str = None) -> None:
        """Отметить отправку TX"""
        self.t2 = time.monotonic()
        self.signature = signature
        self.add_event('t2_send', provider=provider, signature=signature)
    
    def mark_first_seen(self, slot: int = None) -> None:
        """Отметить первое появление в сети"""
        self.t3 = time.monotonic()
        self.add_event('t3_first_seen', slot=slot)
    
    def mark_finalized(self, slot_landed: int = None, tx_index: int = None, success: bool = True, fail_reason: str = None) -> None:
        """Отметить финализацию"""
        self.t4 = time.monotonic()
        self.slot_landed = slot_landed
        self.tx_index = tx_index
        self.outcome = 'success' if success else 'fail'
        self.fail_reason = fail_reason
        self.add_event('t4_finalized', 
                      slot_landed=slot_landed, 
                      tx_index=tx_index,
                      success=success,
                      fail_reason=fail_reason)
    
    def finish(self) -> None:
        """Завершить трейс и очистить контекст"""
        _current_trace.set(None)
    
    @property
    def total_latency_ms(self) -> Optional[float]:
        """Общая задержка t4-t0 в миллисекундах"""
        if self.t0 and self.t4:
            return (self.t4 - self.t0) * 1000
        return None
    
    @property
    def build_latency_ms(self) -> Optional[float]:
        """Задержка построения t1-t0"""
        if self.t0 and self.t1:
            return (self.t1 - self.t0) * 1000
        return None
    
    @property
    def send_latency_ms(self) -> Optional[float]:
        """Задержка отправки t2-t1"""
        if self.t1 and self.t2:
            return (self.t2 - self.t1) * 1000
        return None
    
    @property
    def confirm_latency_ms(self) -> Optional[float]:
        """Задержка подтверждения t4-t2"""
        if self.t2 and self.t4:
            return (self.t4 - self.t2) * 1000
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Сериализация для JSONL"""
        return {
            'trace_id': self.trace_id,
            'trade_type': self.trade_type,
            'mint': self.mint,
            'source': self.source,
            'slot_detected': self.slot_detected,
            'signature': self.signature,
            'slot_landed': self.slot_landed,
            'tx_index': self.tx_index,
            'outcome': self.outcome,
            'fail_reason': self.fail_reason,
            'latency': {
                'total_ms': self.total_latency_ms,
                'build_ms': self.build_latency_ms,
                'send_ms': self.send_latency_ms,
                'confirm_ms': self.confirm_latency_ms
            },
            'events': [
                {
                    'stage': e.stage,
                    'timestamp': e.timestamp_wall,
                    'data': e.data
                }
                for e in self.events
            ]
        }


def get_current_trace() -> Optional[TraceContext]:
    """Получить текущий контекст трассировки"""
    return _current_trace.get()


def get_trace_id() -> Optional[str]:
    """Получить текущий trace_id (для логгера)"""
    ctx = _current_trace.get()
    return ctx.trace_id if ctx else None
