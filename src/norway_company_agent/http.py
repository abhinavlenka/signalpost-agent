from __future__ import annotations

import json
import hashlib
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .budget import BudgetExhausted, active_budget

USER_AGENT = "signalpost-agent/1.0 (+https://builderr.ai/challenges/signalpost)"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None
    attempts: int = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _BudgetedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Every redirect hop is an outbound request and counts against the batch budget."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        active_budget().take(newurl, purpose="redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_BudgetedRedirectHandler())


def fetch_json(
    url: str,
    *,
    timeout: float = 20.0,
    attempts: int = 3,
    headers: dict[str, str] | None = None,
    purpose: str = "json",
    backoff: float = 0.6,
) -> FetchResult:
    return _fetch(url, parse=json.loads, accept="application/json", timeout=timeout, attempts=attempts, headers=headers, purpose=purpose, backoff=backoff)


def fetch_text(
    url: str,
    *,
    timeout: float = 20.0,
    attempts: int = 3,
    headers: dict[str, str] | None = None,
    purpose: str = "text",
    backoff: float = 0.6,
) -> FetchResult:
    return _fetch(url, parse=lambda raw: raw.decode("utf-8", errors="replace"), accept="*/*", timeout=timeout, attempts=attempts, headers=headers, purpose=purpose, backoff=backoff)


def _fetch(
    url: str,
    *,
    parse: Callable[[bytes], Any],
    accept: str,
    timeout: float,
    attempts: int,
    headers: dict[str, str] | None,
    purpose: str,
    backoff: float,
) -> FetchResult:
    last_error = "request failed"
    last_status = 0
    for attempt in range(attempts):
        try:
            active_budget().take(url, purpose=purpose)
        except BudgetExhausted as exc:
            return FetchResult(url, 0, 0, 0, error=f"budget_exhausted: {exc}", retrieved_at=_utc_now(), attempts=attempt)
        started = time.monotonic()
        request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT, **(headers or {})})
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                raw = response.read()
                elapsed = int((time.monotonic() - started) * 1000)
                return FetchResult(url, response.status, elapsed, len(raw), parse(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now(), attempts=attempt + 1)
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read()
            if exc.code in {404, 410}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now(), attempts=attempt + 1)
            last_error = f"HTTP {exc.code}"
            last_status = exc.code
            if exc.code not in RETRYABLE_STATUS:
                break
        except BudgetExhausted as exc:
            return FetchResult(url, 0, 0, 0, error=f"budget_exhausted: {exc}", retrieved_at=_utc_now(), attempts=attempt + 1)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        if attempt + 1 < attempts:
            time.sleep(backoff * (2**attempt) + random.uniform(0, backoff))
    return FetchResult(url, last_status, 0, 0, error=last_error, retrieved_at=_utc_now(), attempts=attempts)
