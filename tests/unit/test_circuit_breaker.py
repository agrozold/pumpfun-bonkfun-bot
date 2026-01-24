"""Тесты для Circuit Breaker"""
import pytest
import asyncio

from src.core.circuit_breaker import (
    CircuitBreaker, CircuitBreakerConfig, CircuitState,
    CircuitBreakerOpenError, retry_with_backoff, RetryConfig
)


@pytest.fixture
def circuit_breaker():
    config = CircuitBreakerConfig(
        failure_threshold=3,
        success_threshold=2,
        timeout_seconds=1.0
    )
    return CircuitBreaker("test", config)


@pytest.mark.asyncio
async def test_circuit_breaker_closed_on_success(circuit_breaker):
    """Тест: CB остаётся закрытым при успехах"""
    async def success_func():
        return "ok"
    
    result = await circuit_breaker.call(success_func)
    
    assert result == "ok"
    assert circuit_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_opens_on_failures(circuit_breaker):
    """Тест: CB открывается после N ошибок"""
    async def failing_func():
        raise ValueError("error")
    
    for _ in range(3):
        with pytest.raises(ValueError):
            await circuit_breaker.call(failing_func)
    
    assert circuit_breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_rejects_when_open(circuit_breaker):
    """Тест: CB отклоняет запросы в открытом состоянии"""
    async def failing_func():
        raise ValueError("error")
    
    # Открываем CB
    for _ in range(3):
        with pytest.raises(ValueError):
            await circuit_breaker.call(failing_func)
    
    # Теперь должен отклонять
    async def success_func():
        return "ok"
    
    with pytest.raises(CircuitBreakerOpenError):
        await circuit_breaker.call(success_func)


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_after_timeout(circuit_breaker):
    """Тест: CB переходит в half-open после таймаута"""
    async def failing_func():
        raise ValueError("error")
    
    # Открываем CB
    for _ in range(3):
        with pytest.raises(ValueError):
            await circuit_breaker.call(failing_func)
    
    # Ждём таймаут
    await asyncio.sleep(1.1)
    
    # Теперь должен быть half-open и пропустить запрос
    async def success_func():
        return "ok"
    
    result = await circuit_breaker.call(success_func)
    assert result == "ok"


@pytest.mark.asyncio
async def test_retry_with_backoff_success():
    """Тест: retry успешен с первой попытки"""
    call_count = 0
    
    async def func():
        nonlocal call_count
        call_count += 1
        return "ok"
    
    config = RetryConfig(max_attempts=3, base_delay=0.1)
    result = await retry_with_backoff(func, config)
    
    assert result == "ok"
    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_with_backoff_eventual_success():
    """Тест: retry успешен после нескольких попыток"""
    call_count = 0
    
    async def func():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("error")
        return "ok"
    
    config = RetryConfig(max_attempts=5, base_delay=0.01)
    result = await retry_with_backoff(func, config)
    
    assert result == "ok"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_with_backoff_all_fail():
    """Тест: retry исчерпывает все попытки"""
    call_count = 0
    
    async def func():
        nonlocal call_count
        call_count += 1
        raise ValueError("error")
    
    config = RetryConfig(max_attempts=3, base_delay=0.01)
    
    with pytest.raises(ValueError):
        await retry_with_backoff(func, config)
    
    assert call_count == 3


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
