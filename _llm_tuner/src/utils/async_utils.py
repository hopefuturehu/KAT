"""Async utilities — timeouts, retries."""

import asyncio
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")


async def with_timeout(coro: Awaitable[T], timeout: float, message: str = "timed out") -> T:
    """Run a coroutine with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(message)


async def retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
) -> T:
    """Retry an async function with exponential backoff."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (backoff ** attempt)
                await asyncio.sleep(delay)
    raise last_error  # type: ignore
