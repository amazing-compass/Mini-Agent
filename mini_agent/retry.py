"""Elegant retry mechanism module

Provides decorators and utility functions to support retry logic for async functions.

Features:
- Supports exponential backoff strategy
- Configurable retry count and intervals
- Supports specifying retryable exception types
- Detailed logging
- Fully decoupled, non-invasive to business code
"""

import asyncio
import functools
import logging
from typing import Any, Callable, Optional, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryConfig:
    """Retry configuration class"""

    def __init__(
        self,
        enabled: bool = True,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        retryable_exceptions: Optional[tuple[Type[Exception], ...]] = None,
    ):
        """
        Args:
            enabled: Whether to enable retry mechanism
            max_retries: Maximum number of retries
            initial_delay: Initial delay time (seconds)
            max_delay: Maximum delay time (seconds)
            exponential_base: Exponential backoff base
            retryable_exceptions: Tuple of retryable exception types. When
                omitted defaults to `(TransientError,)` — only genuine
                network / 5xx hiccups retry. Auth, malformed-request, and
                capacity errors must NOT be retried (Phase 2 design §5.4).
                Pass `(Exception,)` explicitly to restore the old
                "retry everything" behavior.
        """
        self.enabled = enabled
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        if retryable_exceptions is None:
            # Resolve lazily to avoid a circular import between
            # mini_agent.retry and mini_agent.llm.ha.errors.
            from .llm.ha.errors import TransientError

            retryable_exceptions = (TransientError,)
        self.retryable_exceptions = retryable_exceptions

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay time (exponential backoff)

        Args:
            attempt: Current attempt number (starting from 0)

        Returns:
            Delay time (seconds)
        """
        delay = self.initial_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)


class RetryExhaustedError(Exception):
    """Retry exhausted exception"""

    def __init__(self, last_exception: Exception, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(f"Retry failed after {attempts} attempts. Last error: {str(last_exception)}")


def async_retry(
    config: RetryConfig | None = None,
    on_retry: Callable[[Exception, int], None] | None = None,
    should_retry: Callable[[Exception], bool] | None = None,
) -> Callable:
    """Async function retry decorator.

    Args:
        config: Retry configuration object, uses default config if None.
        on_retry: Callback invoked before each retry; receives exception and
            the current attempt number.
        should_retry: Optional predicate consulted AFTER `retryable_exceptions`
            narrows the type. Return False to short-circuit the retry loop
            (e.g. for auth/malformed-request errors that aren't worth
            retrying on the same node). This keeps classification policy
            out of `retry.py` while letting callers plug it in. If None,
            every caught exception is treated as retryable.

    Returns:
        Decorator function.

    Example:
        ```python
        @async_retry(
            RetryConfig(max_retries=3, initial_delay=1.0),
            should_retry=lambda e: is_retryable(classify_error(e)),
        )
        async def call_api():
            ...
        ```
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(config.max_retries + 1):
                try:
                    # Try to execute function
                    return await func(*args, **kwargs)

                except config.retryable_exceptions as e:
                    last_exception = e

                    # Classification gate: let callers veto retries for
                    # errors that will clearly fail the same way next time
                    # (auth, malformed schema, context overflow, ...).
                    if should_retry is not None and not should_retry(e):
                        logger.info(
                            f"Function {func.__name__} raised {type(e).__name__}; "
                            f"classified as non-retryable, raising immediately"
                        )
                        raise

                    # If this is the last attempt, don't retry
                    if attempt >= config.max_retries:
                        logger.error(f"Function {func.__name__} retry failed, reached maximum retry count {config.max_retries}")
                        raise RetryExhaustedError(e, attempt + 1)

                    # Calculate delay time
                    delay = config.calculate_delay(attempt)

                    # Log
                    logger.warning(
                        f"Function {func.__name__} call {attempt + 1} failed: {str(e)}, "
                        f"retrying attempt {attempt + 2} after {delay:.2f} seconds"
                    )

                    # Call callback function
                    if on_retry:
                        on_retry(e, attempt + 1)

                    # Wait before retry
                    await asyncio.sleep(delay)

            # Should not reach here in theory
            if last_exception:
                raise last_exception
            raise Exception("Unknown error")

        return wrapper

    return decorator
