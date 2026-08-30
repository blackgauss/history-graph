"""Opt-in instrumentation for pipeline stages and HTTP calls.

Two environment switches, off by default and side-effect free when off:

- ``HG_PROFILE=1``: accumulate wall time and call counts per named span and
  dump them with :func:`write_profile` (used by ``scripts/profile_pipeline.py``).
- ``HG_BREAK=stage.thread,http.scihub`` (or ``*``): drop into ``breakpoint()``
  when a named span is entered. Combine with ``PYTHONBREAKPOINT=ipdb.set_trace``
  or ``uv run python -m pdb`` as preferred.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

F = TypeVar("F", bound=Callable[..., object])

_PROFILE = bool(os.environ.get("HG_PROFILE"))
_BREAK_SPANS = {s.strip() for s in os.environ.get("HG_BREAK", "").split(",") if s.strip()}

_seconds: dict[str, float] = defaultdict(float)
_calls: dict[str, int] = defaultdict(int)


def profiling_enabled() -> bool:
    return _PROFILE


def break_requested(span: str) -> bool:
    return "*" in _BREAK_SPANS or span in _BREAK_SPANS


@contextmanager
def instrument(span: str) -> Iterator[None]:
    """Wrap a named unit of work; zero-cost unless HG_PROFILE/HG_BREAK are set."""
    if break_requested(span):
        breakpoint()
    if not _PROFILE:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        _calls[span] += 1
        _seconds[span] += time.perf_counter() - started


def instrumented(span: str) -> Callable[[F], F]:
    """Decorator form of :func:`instrument` for whole functions/stages."""

    def wrap(fn: F) -> F:
        @functools.wraps(fn)
        def inner(*args: object, **kwargs: object) -> object:
            with instrument(span):
                return fn(*args, **kwargs)

        return inner  # type: ignore[return-value]

    return wrap


def profile_summary() -> dict[str, dict[str, float]]:
    return {
        span: {"seconds": round(secs, 3), "calls": _calls[span]}
        for span, secs in _seconds.items()
    }


def write_profile(path: Path) -> dict[str, dict[str, float]]:
    summary = profile_summary()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary
