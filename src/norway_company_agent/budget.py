"""Batch-wide request and wall-clock governor.

Every outbound HTTP attempt (including redirects and retries) must call ``take`` first, so the
evaluator's 2,000-request cap can never be exceeded. Once the cap or deadline is reached the caller
receives ``BudgetExhausted`` and records an explicit terminal state instead of fetching.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from collections import Counter
from typing import Any


class BudgetExhausted(RuntimeError):
    """Raised when the request cap or wall-clock deadline would be exceeded."""


class RequestBudget:
    def __init__(self, max_requests: int = 1900, deadline_seconds: float | None = None, reserve: int = 0) -> None:
        self.max_requests = max_requests
        self.reserve = reserve
        self.started = time.monotonic()
        self.deadline = self.started + deadline_seconds if deadline_seconds else None
        self._used = 0
        self._by_host: Counter[str] = Counter()
        self._by_purpose: Counter[str] = Counter()
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.max_requests - self._used

    def time_left(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def take(self, url: str, *, purpose: str = "other", essential: bool = False) -> None:
        """Consume one request. Non-essential requests may not dip into the reserve."""
        if self.expired():
            raise BudgetExhausted("wall-clock deadline reached")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        with self._lock:
            limit = self.max_requests if essential else self.max_requests - self.reserve
            if self._used + 1 > limit:
                raise BudgetExhausted(f"request cap reached ({self._used}/{self.max_requests})")
            self._used += 1
            self._by_host[host] += 1
            self._by_purpose[purpose] += 1

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_requests": self.max_requests,
                "requests_used": self._used,
                "elapsed_seconds": round(time.monotonic() - self.started, 1),
                "by_purpose": dict(self._by_purpose.most_common()),
                "top_hosts": dict(self._by_host.most_common(15)),
            }


class _Unlimited(RequestBudget):
    """Default for library use and tests: counts but never refuses."""

    def __init__(self) -> None:
        super().__init__(max_requests=10**12)


_active: RequestBudget = _Unlimited()


def active_budget() -> RequestBudget:
    return _active


def set_active_budget(budget: RequestBudget | None) -> RequestBudget:
    global _active
    _active = budget or _Unlimited()
    return _active
