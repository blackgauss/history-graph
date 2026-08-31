"""Record/replay httpx transports for deterministic, offline scenario runs.

Record once against the live APIs, commit the cassette JSON, then every test
(and the profile/debug scripts, and the MCP tools) replays byte-identical
responses — no network, no sleeps (inject the fake clock/sleep here), no drift.
Request keys normalize away ``mailto`` and sort query params so politeness-pool
noise never misses.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from .client import BASE_URL, OpenAlexClient
from .scihub import SciHubClient

CASSETTE_DIR = Path("tests/cassettes")
_STRIP_PARAMS = {"mailto"}
_STORED_HEADERS = {"content-type", "retry-after", "location"}
_INLINE_BODY_MAX = 512_000  # bigger payloads are stored as siblings, not b64 blobs


def request_key(request: httpx.Request) -> str:
    params = sorted(
        (k, v) for k, v in request.url.params.multi_items() if k not in _STRIP_PARAMS
    )
    url = request.url.copy_with(params=params)
    return f"{request.method} {url}"


class Cassette:
    """A JSON interaction log keyed by canonicalized request."""

    def __init__(self, name: str, directory: Path | None = None) -> None:
        self.path = (directory or CASSETTE_DIR) / f"{name}.json"
        self._interactions: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._interactions is None:
            if not self.path.exists():
                raise FileNotFoundError(f"missing cassette {self.path}; record it first")
            self._interactions = json.loads(self.path.read_text(encoding="utf-8"))
        return self._interactions

    def read_body(self, key: str, entry: dict[str, Any]) -> bytes:
        if entry.get("body_file"):
            return (self.path.parent / entry["body_file"]).read_bytes()
        return base64.b64decode(entry["body_b64"])

    def store_body(self, key: str, content: bytes) -> dict[str, str]:
        if len(content) <= _INLINE_BODY_MAX:
            return {"body_b64": base64.b64encode(content).decode("ascii")}
        digest = hashlib.sha256(content).hexdigest()[:16]
        rel = f"{self.path.stem}/bodies/{digest}.bin"
        path = self.path.parent / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"body_file": rel}

    def transport(self, *, recorder: httpx.Client | None = None) -> httpx.BaseTransport:
        """Replay by default; pass a live ``recorder`` client to record fresh."""
        if recorder is not None:
            return _Recorder(recorder, self)
        return _Replayer(self)


class _Replayer(httpx.BaseTransport):
    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        try:
            entry = self._cassette._load()[key]
        except KeyError as exc:
            raise KeyError(f"no cassette entry for {key}") from exc
        return httpx.Response(
            entry["status"],
            headers=entry.get("headers") or None,
            content=self._cassette.read_body(key, entry),
            request=request,
        )


class _Recorder(httpx.BaseTransport):
    def __init__(self, live: httpx.Client, cassette: Cassette) -> None:
        self._live = live
        self._cassette = cassette
        try:
            self._interactions: dict[str, dict[str, Any]] = dict(self._cassette._load())
        except FileNotFoundError:
            self._interactions = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        response = self._live.send(request, follow_redirects=True)
        saved = response
        headers = {h: saved.headers[h] for h in saved.headers if h.lower() in _STORED_HEADERS}
        self._interactions[key] = {
            "status": saved.status_code,
            "headers": headers,
            **self._cassette.store_body(key, saved.content),
        }
        self.save()
        return response

    def save(self) -> Path:
        self._cassette.path.parent.mkdir(parents=True, exist_ok=True)
        self._cassette.path.write_text(
            json.dumps(self._interactions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self._cassette.path


def no_sleep(_seconds: float) -> None:
    """Stand-in clock.sleep for deterministic runs."""


def zero_clock() -> float:
    """Stand-in clock so throttle math sees no elapsed time."""
    return 0.0


def openalex_client(name: str = "openalex", *, record: bool = False) -> OpenAlexClient:
    """Replaying OpenAlex client by default; ``record=True`` hits the live API."""
    cassette = Cassette(name)
    if record:
        live = httpx.Client(timeout=30.0)
        transport: httpx.BaseTransport = cassette.transport(recorder=live)
    else:
        transport = cassette.transport()
    http = httpx.Client(base_url=BASE_URL, transport=transport)
    return OpenAlexClient(
        mailto="cassette@history-graph.local",
        http=http,
        clock=zero_clock,
        sleep=no_sleep,
    )


def scihub_client(name: str = "scihub", *, record: bool = False) -> SciHubClient:
    """Sci-Hub client bound to a cassette, never sleeping between requests."""
    cassette = Cassette(name)
    if record:
        from .scihub import USER_AGENT

        live = httpx.Client(
            timeout=60.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        transport: httpx.BaseTransport = cassette.transport(recorder=live)
    else:
        transport = cassette.transport()
    http = httpx.Client(transport=transport, follow_redirects=True)
    return SciHubClient(
        mirrors=("https://sci-hub.ru",),
        http=http,
        min_interval_s=0.0,
        clock=zero_clock,
        sleep=no_sleep,
    )
