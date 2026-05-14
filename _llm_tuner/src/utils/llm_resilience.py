"""LLM API resilience: retry, circuit breaker, rate limiting, and JSON extraction.

Provides a layered defence for LLM API calls so transient failures don't abort
an experiment, and malformed LLM outputs are handled gracefully.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, TypeVar

from src.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# ── Retryable error detection ──────────────────────────────────────────────

RETRYABLE_STATUS_CODES: set[int] = {429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    asyncio.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)
NON_RETRYABLE_KEYWORDS = (
    "invalid api key",
    "authentication",
    "unauthorized",
    "insufficient_quota",
    "account",
    "billing",
    "model not found",
    "does not exist",
)


def is_retryable(exception: Exception) -> bool:
    """Determine whether an exception from an LLM call is worth retrying."""
    msg = str(exception).lower()

    for keyword in NON_RETRYABLE_KEYWORDS:
        if keyword in msg:
            return False

    if isinstance(exception, RETRYABLE_EXCEPTIONS):
        return True

    # OpenAI / Anthropic SDKs typically wrap HTTP errors in specific types.
    # Check for status-code-like patterns in the error string.
    status_match = re.search(r"status (?:code )?(\d{3})", msg)
    if status_match:
        return int(status_match.group(1)) in RETRYABLE_STATUS_CODES

    return True  # default: retry unknown errors once


# ── Exponential backoff with jitter ────────────────────────────────────────


def backoff_delay(attempt: int, base: float = 1.0, max_delay: float = 60.0) -> float:
    """Exponential backoff with full jitter.

    attempt  0 →     0 –   2 s
    attempt  1 →     0 –   4 s
    attempt  2 →     0 –   8 s
    attempt  3 →     0 –  16 s
    attempt  4 →     0 –  32 s
    attempt  5+→     0 –  60 s  (capped)
    """
    return random.uniform(0, min(base * (2 ** attempt), max_delay))


async def async_retry(
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    **kwargs: Any,
) -> T:
    """Call *fn(*args, **kwargs)* with retry + exponential backoff.

    Returns the result on success or raises the last exception after exhausting
    retries.  Non-retryable errors are re-raised immediately.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                raise

            if attempt == max_retries:
                logger.error(
                    "llm call exhausted retries",
                    attempts=attempt + 1,
                    error=str(exc)[:200],
                )
                raise

            delay = backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "llm call failed, retrying",
                attempt=attempt + 1,
                next_delay=f"{delay:.1f}s",
                error=str(exc)[:150],
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


# ── Rate limiter (token bucket) ────────────────────────────────────────────


@dataclass
class RateLimiter:
    """Simple token-bucket rate limiter, safe for single-threaded async use."""

    max_tokens: float = 10.0
    refill_rate: float = 1.0  # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False, default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self._tokens = self.max_tokens

    async def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            wait = (1.0 - self._tokens) / self.refill_rate
            await asyncio.sleep(wait)


# ── Circuit breaker ─────────────────────────────────────────────────────────


class CircuitBreaker:
    """Prevents cascading failures by opening after consecutive errors.

    States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (probing)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max

        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "closed"  # closed | open | half_open
        self._half_open_attempts: int = 0

    @property
    def state(self) -> str:
        return self._state

    def _transition(self) -> None:
        """Advance state machine based on time and failure count."""
        now = time.monotonic()

        if self._state == "open":
            if now - self._last_failure_time >= self.recovery_timeout:
                self._state = "half_open"
                self._half_open_attempts = 0
                logger.info("circuit breaker → half_open")

        elif self._state == "half_open":
            if self._half_open_attempts >= self.half_open_max:
                return  # still probing

    async def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute *fn* with circuit-breaker protection."""
        self._transition()

        if self._state == "open":
            raise CircuitBreakerOpenError(
                f"Circuit breaker open — {self._failure_count} failures, "
                f"retry in {self.recovery_timeout - (time.monotonic() - self._last_failure_time):.0f}s"
            )

        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            raise

    def _on_success(self) -> None:
        if self._state == "half_open":
            self._state = "closed"
            logger.info("circuit breaker → closed (recovered)")
        self._failure_count = 0

    def _on_failure(self, exc: Exception) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == "half_open":
            self._half_open_attempts += 1
            if self._half_open_attempts >= self.half_open_max:
                self._state = "open"

        elif self._state == "closed" and self._failure_count >= self.failure_threshold:
            self._state = "open"
            logger.error(
                "circuit breaker → open",
                failures=self._failure_count,
                last_error=str(exc)[:150],
            )


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""
    pass


# ── JSON extraction ─────────────────────────────────────────────────────────


# Matches ```json ... ``` or ``` ... ``` or bare { ... }
_JSON_RE = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```"  # fenced code block
    r"|(\{[\s\S]*\})",               # bare JSON object
)


def extract_json(text: str) -> Any:
    """Extract a JSON object from an LLM text response.

    Handles:
      - ```json { ... } ```
      - ``` { ... } ```
      - Bare { ... }
      - Leading/trailing commentary text

    Returns the parsed object or raises *ValueError*.
    """
    if not text or not text.strip():
        raise ValueError("empty LLM response")

    # Try direct parse first (clean output)
    trimmed = text.strip()
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        pass

    # Try extracting from code blocks
    for match in _JSON_RE.finditer(trimmed):
        block = match.group(1) or match.group(2)
        if block:
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                continue

    # Last resort: find the outermost { ... } pair
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = trimmed[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"no valid JSON found in LLM response (first 200 chars): {trimmed[:200]}")


def safe_extract_json(text: str, default: Any = None) -> Any:
    """Like *extract_json* but returns *default* on failure instead of raising."""
    try:
        return extract_json(text)
    except ValueError:
        logger.warning("failed to extract JSON from LLM response", preview=text[:200])
        return default


# ── Tool-call loop guard ────────────────────────────────────────────────────


class ToolCallLoopExceededError(Exception):
    """Raised when the LLM tool-call loop exceeds the maximum iterations."""
    pass


# ── Resilience manager (per-provider isolation) ───────────────────────────────


class ResilienceManager:
    """Manages per-provider rate limiter and circuit breaker instances.

    Unlike the old singleton pattern, each provider gets its own limiter
    and breaker so a DeepSeek outage does not trip the Anthropic breaker.
    """

    def __init__(
        self,
        default_rate_limit_rps: float = 5.0,
        default_failure_threshold: int = 5,
        default_recovery_timeout: float = 30.0,
    ):
        self._default_rps = default_rate_limit_rps
        self._default_failure = default_failure_threshold
        self._default_recovery = default_recovery_timeout
        self._limiters: dict[str, RateLimiter] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_limiter(self, provider: str, rate_limit_rps: float | None = None) -> RateLimiter:
        if provider not in self._limiters:
            rps = rate_limit_rps if rate_limit_rps is not None else self._default_rps
            self._limiters[provider] = RateLimiter(max_tokens=rps, refill_rate=rps)
        return self._limiters[provider]

    def get_breaker(
        self,
        provider: str,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
    ) -> CircuitBreaker:
        if provider not in self._breakers:
            ft = failure_threshold if failure_threshold is not None else self._default_failure
            rt = recovery_timeout if recovery_timeout is not None else self._default_recovery
            self._breakers[provider] = CircuitBreaker(
                failure_threshold=ft, recovery_timeout=rt
            )
        return self._breakers[provider]

    async def acquire(self, provider: str, rate_limit_rps: float | None = None) -> None:
        """Acquire a rate-limit token for *provider*."""
        await self.get_limiter(provider, rate_limit_rps).acquire()

    async def call(
        self,
        provider: str,
        fn: Callable[..., T],
        *args: Any,
        rate_limit_rps: float | None = None,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        """Execute *fn* with per-provider circuit breaker protection."""
        breaker = self.get_breaker(provider, failure_threshold, recovery_timeout)

        async def _wrapped():
            await self.acquire(provider, rate_limit_rps)
            return await fn(*args, **kwargs)

        return await breaker.call(_wrapped)
