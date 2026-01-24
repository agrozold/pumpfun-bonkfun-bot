"""
Circuit Breaker и Retry с Exponential Backoff.
Защита от каскадных отказов и автоматическое восстановление.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Any, TypeVar, Generic
from enum import Enum
from functools import wraps

logger = logging.getLogger(__name__)

T = TypeVar('T')


class CircuitState(Enum):
    """Состояния Circuit Breaker"""
    CLOSED = "closed"      # Нормальная работа
    OPEN = "open"          # Отказ, блокируем запросы
    HALF_OPEN = "half_open"  # Пробуем восстановиться


@dataclass
class CircuitBreakerConfig:
    """Конфигурация Circuit Breaker"""
    failure_threshold: int = 5          # Порог ошибок для открытия
    success_threshold: int = 2          # Успехов для закрытия из half-open
    timeout_seconds: float = 30.0       # Время в open до перехода в half-open
    half_open_max_calls: int = 3        # Максимум вызовов в half-open


@dataclass
class CircuitBreakerStats:
    """Статистика Circuit Breaker"""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_changes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None


class CircuitBreaker:
    """
    Circuit Breaker pattern implementation.

    Использование:
        cb = CircuitBreaker("rpc_client")

        @cb.protect
        async def call_rpc():
            ...

        # или
        result = await cb.call(call_rpc)
    """

    def __init__(self, name: str, config: CircuitBreakerConfig = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
        self._stats = CircuitBreakerStats()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    async def _check_state(self) -> bool:
        """
        Проверить состояние и вернуть True если можно выполнять запрос.
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Проверяем timeout
                if self._last_failure_time:
                    elapsed = time.monotonic() - self._last_failure_time
                    if elapsed >= self.config.timeout_seconds:
                        self._transition_to(CircuitState.HALF_OPEN)
                        return True

                self._stats.rejected_calls += 1
                return False

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

            return False

    async def _record_success(self) -> None:
        """Записать успешный вызов"""
        async with self._lock:
            self._stats.total_calls += 1
            self._stats.successful_calls += 1
            self._stats.last_success_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            else:
                self._failure_count = 0

    async def _record_failure(self, error: Exception) -> None:
        """Записать неуспешный вызов"""
        async with self._lock:
            self._stats.total_calls += 1
            self._stats.failed_calls += 1
            self._stats.last_failure_time = time.monotonic()
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
            else:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Переход в новое состояние"""
        if new_state == self._state:
            return

        old_state = self._state
        self._state = new_state
        self._stats.state_changes += 1

        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0
            self._half_open_calls = 0

        logger.warning(f"Circuit Breaker '{self.name}': {old_state.value} -> {new_state.value}")

    async def call(self, func: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """Выполнить вызов через Circuit Breaker"""
        if not await self._check_state():
            raise CircuitBreakerOpenError(f"Circuit Breaker '{self.name}' is open")

        try:
            result = await func(*args, **kwargs)
            await self._record_success()
            return result
        except Exception as e:
            await self._record_failure(e)
            raise

    def protect(self, func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """Декоратор для защиты функции"""
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await self.call(func, *args, **kwargs)
        return wrapper

    def get_stats(self) -> dict:
        """Получить статистику"""
        return {
            'name': self.name,
            'state': self._state.value,
            'failure_count': self._failure_count,
            'success_count': self._success_count,
            **{k: v for k, v in self._stats.__dict__.items()}
        }

    def reset(self) -> None:
        """Сбросить состояние"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        logger.info(f"Circuit Breaker '{self.name}' reset")


class CircuitBreakerOpenError(Exception):
    """Исключение когда Circuit Breaker открыт"""
    pass


@dataclass
class RetryConfig:
    """Конфигурация retry"""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple = (Exception,)


async def retry_with_backoff(
    func: Callable[..., Awaitable[T]],
    config: RetryConfig = None,
    *args,
    **kwargs
) -> T:
    """
    Выполнить функцию с retry и exponential backoff.

    Использование:
        result = await retry_with_backoff(
            call_api,
            RetryConfig(max_attempts=5),
            arg1, arg2
        )
    """
    config = config or RetryConfig()
    last_exception = None

    for attempt in range(1, config.max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except config.retryable_exceptions as e:
            last_exception = e

            if attempt == config.max_attempts:
                logger.error(f"All {config.max_attempts} attempts failed: {e}")
                raise

            # Вычисляем задержку: base * (exponential_base ^ attempt)
            delay = min(
                config.base_delay * (config.exponential_base ** (attempt - 1)),
                config.max_delay
            )

            # Добавляем jitter
            if config.jitter:
                import random
                delay = delay * (0.5 + random.random())

            logger.warning(f"Attempt {attempt} failed: {e}. Retrying in {delay:.2f}s...")
            await asyncio.sleep(delay)

    raise last_exception


def with_retry(config: RetryConfig = None):
    """Декоратор для retry"""
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await retry_with_backoff(func, config, *args, **kwargs)
        return wrapper
    return decorator


class ServiceHealthChecker:
    """
    Проверка здоровья сервисов.

    Использование:
        checker = ServiceHealthChecker()
        checker.register("rpc", check_rpc_health)
        checker.register("redis", check_redis_health)

        status = await checker.check_all()
    """

    def __init__(self):
        self._checks: dict[str, Callable[[], Awaitable[bool]]] = {}
        self._last_results: dict[str, bool] = {}
        self._last_check_time: Optional[float] = None

    def register(self, name: str, check_func: Callable[[], Awaitable[bool]]) -> None:
        """Зарегистрировать проверку здоровья"""
        self._checks[name] = check_func

    async def check(self, name: str) -> bool:
        """Проверить конкретный сервис"""
        if name not in self._checks:
            return False

        try:
            result = await asyncio.wait_for(self._checks[name](), timeout=10.0)
            self._last_results[name] = result
            return result
        except asyncio.TimeoutError:
            logger.error(f"Health check '{name}' timed out")
            self._last_results[name] = False
            return False
        except Exception as e:
            logger.error(f"Health check '{name}' failed: {e}")
            self._last_results[name] = False
            return False

    async def check_all(self) -> dict[str, bool]:
        """Проверить все сервисы"""
        results = {}

        tasks = {name: self.check(name) for name in self._checks}

        for name, coro in tasks.items():
            results[name] = await coro

        self._last_check_time = time.monotonic()
        return results

    def is_healthy(self) -> bool:
        """Все сервисы здоровы?"""
        return all(self._last_results.values()) if self._last_results else False

    def get_status(self) -> dict:
        """Получить статус"""
        return {
            'healthy': self.is_healthy(),
            'services': dict(self._last_results),
            'last_check': self._last_check_time
        }


# Глобальные Circuit Breakers
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, config: CircuitBreakerConfig = None) -> CircuitBreaker:
    """Получить или создать Circuit Breaker"""
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name, config)
    return _circuit_breakers[name]


def get_all_circuit_breakers_stats() -> dict:
    """Получить статистику всех Circuit Breakers"""
    return {name: cb.get_stats() for name, cb in _circuit_breakers.items()}
