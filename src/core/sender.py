"""
Sender Protocol - унифицированный интерфейс отправки транзакций
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol
from enum import Enum


class SendStatus(Enum):
    SUCCESS = 'success'
    FAILED = 'failed'
    TIMEOUT = 'timeout'
    RATE_LIMITED = 'rate_limited'


@dataclass
class SendResult:
    """Результат отправки транзакции"""
    status: SendStatus
    signature: Optional[str] = None
    provider: str = ''
    latency_ms: float = 0.0
    error: Optional[str] = None
    slot: Optional[int] = None

    @property
    def is_success(self) -> bool:
        return self.status == SendStatus.SUCCESS


@dataclass
class ConfirmResult:
    """Результат подтверждения транзакции"""
    confirmed: bool
    slot: Optional[int] = None
    error: Optional[str] = None
    confirmations: int = 0


class SendProvider(Protocol):
    """Протокол провайдера отправки"""

    name: str
    priority: int

    async def send(self, tx_bytes: bytes, trace_id: str = None) -> SendResult:
        """Отправить транзакцию"""
        ...

    async def confirm(self, signature: str, timeout: float = 30.0) -> ConfirmResult:
        """Подтвердить транзакцию"""
        ...

    def is_healthy(self) -> bool:
        """Проверка здоровья провайдера"""
        ...

    async def close(self) -> None:
        """Закрыть соединения"""
        ...
