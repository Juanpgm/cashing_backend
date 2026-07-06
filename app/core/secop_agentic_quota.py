"""In-memory sliding-window rate limiter for the manual SECOP agentic trigger.

A simple per-user, per-process limiter. We keep timestamps for each user in a
deque and prune entries older than the window before each check.

This is sufficient for low-throughput, user-initiated actions (≤20/hour). For
higher throughput we'd switch to Redis, but that's overkill here.

Usage::

    from app.core.secop_agentic_quota import enforce_agentic_quota
    enforce_agentic_quota(user_id)
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.core.config import settings
from app.core.exceptions import RateLimitExceededError

_WINDOW_SECONDS = 3600  # 1 hour
_HITS: dict[str, deque[float]] = defaultdict(deque)
_LOCK = threading.Lock()


def _prune(user_id: str, now: float) -> deque[float]:
    dq = _HITS[user_id]
    cutoff = now - _WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()
    return dq


def enforce_agentic_quota(user_id: str) -> None:
    """Raise :class:`RateLimitExceededError` if the user has exceeded the quota.

    Otherwise records the current timestamp.
    """
    limit = settings.SECOP_AGENTIC_HOURLY_LIMIT
    now = time.time()
    with _LOCK:
        dq = _prune(user_id, now)
        if len(dq) >= limit:
            retry_in = int(_WINDOW_SECONDS - (now - dq[0]))
            raise RateLimitExceededError(
                f"Has alcanzado el límite de {limit} exploraciones agénticas por hora. "
                f"Inténtalo de nuevo en ~{max(retry_in, 60)//60} min."
            )
        dq.append(now)


def remaining(user_id: str) -> int:
    """Return how many agentic calls the user has left in the current window."""
    limit = settings.SECOP_AGENTIC_HOURLY_LIMIT
    with _LOCK:
        dq = _prune(user_id, time.time())
        return max(limit - len(dq), 0)


def _reset() -> None:
    """Test helper — clear all counters."""
    with _LOCK:
        _HITS.clear()
