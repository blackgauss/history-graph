"""Shared HTTP outcome vocabulary -- the "can't tell" fix.

Every source (OpenAlex, Semantic Scholar, Google Patents, Sci-Hub) previously
had its own exception family, retry stack and failure conventions (sentinel
dicts, bare HTTPStatusError escaping after retries, captchas posing as data).
Callers therefore sniffed `except Exception` and guessed. This module gives
one contract:

- absence is a normal return (``None`` / ``[]``) -- the source positively
  answered "not here";
- anything inconclusive raises :class:`CanNotTell` with a machine-readable
  ``reason`` (``rate_limited`` / ``bot_walled`` / ``server_error`` /
  ``bad_format`` / ``transport_error``), so callers can say "probe_error"
  instead of silently recording absence;
- one retry policy and one throttle helper for all sources.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_BUDGET_MARKER = b"Insufficient budget"


class CanNotTell(RuntimeError):
    """The source could not answer; the answer is UNKNOWN, not empty/absent."""

    reason = "unknown"


def is_probe_error(exc: BaseException) -> bool:
    """True when the failure means 'no answer' (retry later), not 'not found'."""
    return isinstance(exc, CanNotTell)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in TRANSIENT_STATUSES
    )


def _is_mirrored_transient(exc: BaseException) -> bool:
    """Mirror-rotation contexts must NOT retry ConnectError (dead host = next mirror)."""
    return is_transient(exc) and not isinstance(exc, httpx.ConnectError)


retry_mirrors = retry(
    retry=retry_if_exception(_is_mirrored_transient),
    wait=wait_exponential(multiplier=0.5, max=8),
    stop=stop_after_attempt(5),
    reraise=True,
)

retry_transient = retry(
    retry=retry_if_exception(is_transient),
    wait=wait_exponential(multiplier=0.5, max=8),
    stop=stop_after_attempt(5),
    reraise=True,
)


class Throttle:
    """Pace requests; injectable clock/sleep keep tests deterministic."""

    def __init__(self, interval_s: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._last = 0.0

    def wait(self) -> None:
        pause = self._interval - (self._clock() - self._last)
        if pause > 0:
            self._sleep(pause)
        self._last = self._clock()


def map_status(status: int, body: bytes = b"") -> CanNotTell:
    """Map a failed (still-persisting) status to a typed outcome."""
    if status in (429, 408) or RETRYABLE_BUDGET_MARKER in body:
        err = CanNotTell("rate limited (possibly a credit budget; resets over time)")
        err.reason = "rate_limited"
        return err
    if status in (500, 502, 503, 504):
        err = CanNotTell(f"server error {status}")
        err.reason = "server_error"
        return err
    err = CanNotTell(f"status {status}")
    err.reason = "server_error"
    return err


def http_status_outcome(cls, exc: BaseException) -> CanNotTell:
    """Wrap an escaping httpx failure as the source's typed CanNotTell."""
    if isinstance(exc, httpx.HTTPStatusError):
        mapped = map_status(exc.response.status_code, exc.response.content)
        err = cls(str(mapped))
        err.reason = mapped.reason
        return err
    err = cls(f"transport error: {type(exc).__name__}")
    err.reason = "transport_error"
    return err


def bad_format_error(cls, path: str) -> CanNotTell:
    err = cls(f"non-JSON body from {path}")
    err.reason = "bad_format"
    return err


def parse_json(body: bytes, *, source: str, path: str) -> Any:
    import json

    try:
        return json.loads(body)
    except ValueError as exc:
        err = CanNotTell(f"non-JSON body from {source} {path}")
        err.reason = "bad_format"
        raise err from exc
