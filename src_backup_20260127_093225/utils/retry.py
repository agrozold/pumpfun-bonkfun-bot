"""
Retry utilities with exponential backoff and jitter.
For resilient network operations.
"""

import asyncio
import random
import logging
from functools import wraps
from typing import Callable, Type, Tuple, Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Classification of errors for retry policy."""
    RATE_LIMIT = "rate_limit"       # 429, quota exceeded
    TIMEOUT = "timeout"             # Connection/read timeout
    TRANSIENT = "transient"         # 5xx, temporary failures
    BLOCKHASH = "blockhash"         # BlockhashNotFound
    AUTH = "auth"                   # 401, 403 - don't retry
    CLIENT = "client"               # 4xx - don't retry
    UNKNOWN = "unknown"


# Default retry policies by error category
DEFAULT_POLICIES = {
    ErrorCategory.RATE_LIMIT: {"max_attempts": 5, "base_delay": 2.0, "max_delay": 60.0},
    ErrorCategory.TIMEOUT: {"max_attempts": 3, "base_delay": 1.0, "max_delay": 10.0},
    ErrorCategory.TRANSIENT: {"max_attempts": 4, "base_delay": 1.0, "max_delay": 30.0},
    ErrorCategory.BLOCKHASH: {"max_attempts": 3, "base_delay": 0.5, "max_delay": 5.0},
    ErrorCategory.AUTH: {"max_attempts": 1, "base_delay": 0, "max_delay": 0},  # No retry
    ErrorCategory.CLIENT: {"max_attempts": 1, "base_delay": 0, "max_delay": 0},  # No retry
    ErrorCategory.UNKNOWN: {"max_attempts": 2, "base_delay": 1.0, "max_delay": 10.0},
}


def classify_error(error: Exception) -> ErrorCategory:
    """Classify an error for retry policy selection."""
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()
    
    # Rate limiting
    if "429" in error_str or "rate" in error_str or "too many" in error_str or "quota" in error_str:
        return ErrorCategory.RATE_LIMIT
    
    # Timeout
    if "timeout" in error_str or "timeout" in error_type or "timed out" in error_str:
        return ErrorCategory.TIMEOUT
    
    # Blockhash
    if "blockhash" in error_str or "block hash" in error_str:
        return ErrorCategory.BLOCKHASH
    
    # Auth errors
    if "401" in error_str or "403" in error_str or "unauthorized" in error_str or "forbidden" in error_str:
        return ErrorCategory.AUTH
    
    # Server errors (transient)
    if "500" in error_str or "502" in error_str or "503" in error_str or "504" in error_str:
        return ErrorCategory.TRANSIENT
    
    # Client errors (don't retry)
    if "400" in error_str or "404" in error_str:
        return ErrorCategory.CLIENT
    
    # Connection errors (transient)
    if "connection" in error_str or "connect" in error_type:
        return ErrorCategory.TRANSIENT
    
    return ErrorCategory.UNKNOWN


def calculate_delay(attempt: int, base_delay: float, max_delay: float, jitter: bool = True) -> float:
    """Calculate delay with exponential backoff and optional jitter."""
    # Exponential: base_delay * 2^attempt
    delay = base_delay * (2 ** attempt)
    
    # Cap at max_delay
    delay = min(delay, max_delay)
    
    # Add jitter (±25%)
    if jitter and delay > 0:
        jitter_range = delay * 0.25
        delay = delay + random.uniform(-jitter_range, jitter_range)
    
    return max(0, delay)


async def retry_with_backoff(
    func: Callable,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[int, float, Exception], None]] = None,
    classify: bool = True,
) -> Any:
    """
    Execute async function with exponential backoff retry.
    
    Args:
        func: Async callable to execute
        max_attempts: Maximum number of attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        retryable_exceptions: Tuple of exception types to retry
        on_retry: Optional callback(attempt, delay, exception) on each retry
        classify: If True, use error classification to adjust retry policy
    
    Returns:
        Result of func()
    
    Raises:
        Last exception if all retries fail
    """
    last_exception = None
    
    for attempt in range(max_attempts):
        try:
            return await func()
        except retryable_exceptions as e:
            last_exception = e
            
            # Classify error and get policy
            if classify:
                category = classify_error(e)
                policy = DEFAULT_POLICIES.get(category, DEFAULT_POLICIES[ErrorCategory.UNKNOWN])
                
                # Don't retry AUTH/CLIENT errors
                if category in (ErrorCategory.AUTH, ErrorCategory.CLIENT):
                    logger.warning(f"Non-retryable error ({category.value}): {e}")
                    raise
                
                # Use category-specific limits
                effective_max = min(max_attempts, policy["max_attempts"])
                effective_base = policy["base_delay"]
                effective_max_delay = policy["max_delay"]
            else:
                effective_max = max_attempts
                effective_base = base_delay
                effective_max_delay = max_delay
            
            # Check if we should retry
            if attempt >= effective_max - 1:
                logger.warning(f"All {attempt + 1} attempts failed: {e}")
                raise
            
            # Calculate delay
            delay = calculate_delay(attempt, effective_base, effective_max_delay)
            
            # Log and callback
            logger.info(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s...")
            if on_retry:
                on_retry(attempt + 1, delay, e)
            
            # Wait before retry
            await asyncio.sleep(delay)
    
    # Should not reach here, but just in case
    if last_exception:
        raise last_exception


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
):
    """
    Decorator for async functions with retry logic.
    
    Usage:
        @with_retry(max_attempts=5, base_delay=0.5)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            async def call():
                return await func(*args, **kwargs)
            
            return await retry_with_backoff(
                call,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                retryable_exceptions=retryable_exceptions,
            )
        return wrapper
    return decorator


# === Convenience functions for common cases ===

async def retry_rpc_call(func: Callable, max_attempts: int = 3) -> Any:
    """Retry RPC calls with appropriate backoff."""
    return await retry_with_backoff(
        func,
        max_attempts=max_attempts,
        base_delay=0.5,
        max_delay=10.0,
        classify=True,
    )


async def retry_send_transaction(func: Callable, max_attempts: int = 5) -> Any:
    """Retry transaction sending with longer delays."""
    return await retry_with_backoff(
        func,
        max_attempts=max_attempts,
        base_delay=1.0,
        max_delay=30.0,
        classify=True,
    )
