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

from .client import BASE_URL, OpenAlexClient, OpenAlexError
from .scihub import SciHubClient

CASSETTE_DIR = Path("tests/cassettes")
_STRIP_PARAMS = {"mailto", "api_key"}  # politeness/auth never part of the key
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_STORED_HEADERS = {"content-type", "retry-after", "location"}
_INLINE_BODY_MAX = 48_000  # larger payloads land in deduped body files


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

    @property
    def work_store(self) -> WorkStore:
        if not hasattr(self, "_work_store"):
            self._work_store = WorkStore(self.path.parent / self.path.stem / "works")
        return self._work_store

    def store_works(self, content: bytes, request: httpx.Request | None = None) -> None:
        """Upsert result records; tombstone ids a by-id answer omitted."""
        try:
            payload = json.loads(content)
        except ValueError:
            return
        if isinstance(payload, dict) and payload.get("id", "").startswith(
            "https://openalex.org/"
        ) and "results" not in payload:  # singleton shape
            self.work_store.upsert(payload)
            return
        results = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(results, list):
            for record in results:
                if isinstance(record, dict) and record.get("id", "").startswith("https://openalex.org/"):
                    self.work_store.upsert(record)
            meta = payload.get("meta") or {}
            if request is not None and meta.get("next") in (None, False):
                filt = dict(request.url.params).get("filter", "")
                kind, _, raw = filt.partition(":")
                one = filt.count(":") == 1 and "+" not in filt
                wanted = raw.split("|") if one and kind.replace("_", "") == "openalexid" else []
                got = {
                    str(r.get("id", "")).rsplit("/", 1)[-1]
                    for r in results if isinstance(r, dict)
                }
                if wanted and len(got) <= len(wanted):
                    for wid in wanted:
                        if wid not in got:
                            self.work_store.mark_dead(wid)

    def transport(self, *, recorder: httpx.Client | None = None) -> httpx.BaseTransport:
        """Replay by default; pass a live ``recorder`` client to record fresh."""
        if recorder is not None:
            return _Recorder(recorder, self)
        return _Replayer(self)


class WorkStore:
    """Per-work content-addressed records: batch shape stops mattering.

    Cassettes keyed on full batch URLs drift whenever chunking or select
    fields change; the store keys on the work itself and synthesizes any
    ``/works?filter=openalex_id:...`` / ``doi:...`` response from it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index_file = root / "_doi-index.json"
        self._index: dict[str, str] | None = None

    def _load_index(self) -> dict[str, str]:
        if self._index is None:
            if self._index_file.exists():
                self._index = json.loads(self._index_file.read_text(encoding="utf-8"))
            else:
                self._index = {}
        return self._index

    def path_for(self, work_id: str) -> Path:
        safe = "".join(c for c in work_id if c.isalnum() or c in "-_")
        return self.root / f"{safe}.json"

    def get(self, work_id: str) -> dict[str, Any] | None:
        f = self.path_for(work_id)
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        if self.path_for(work_id).with_suffix(".dead.json").exists():
            return {"id": f"https://openalex.org/{work_id}", "_dead": True}
        return None

    def mark_dead(self, work_id: str) -> None:
        """Absent from a by-id response for that exact id (404-shaped absence)."""
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        if self.get(work_id) is None:
            self.path_for(work_id).with_suffix(".dead.json").write_text("{}\n", encoding="utf-8")

    def get_by_doi(self, doi: str) -> dict[str, Any] | None:
        idx = self._load_index()
        work_id = idx.get(str(doi).lower())
        return self.get(work_id) if work_id else None

    def upsert(self, record: dict[str, Any]) -> None:
        work_id = str(record.get("id", "")).split("/")[-1]
        if not work_id:
            return
        merged = {**(self.get(work_id) or {}), **record}
        self.root.mkdir(parents=True, exist_ok=True)
        self.path_for(work_id).write_text(
            json.dumps(merged, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        doi = str(merged.get("doi") or "").removeprefix("https://doi.org/").lower()
        idx = self._load_index()
        if doi and idx.get(doi) != work_id:
            idx[doi] = work_id
            self._index_file.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")


def _synthesize_works(store: WorkStore, request: httpx.Request) -> httpx.Response | None:
    """Serve /works?filter=openalex_id:...|... or doi:... entirely from the store."""

    import urllib.parse as _up

    if request.url.host != "api.openalex.org":
        return None
    q = dict(request.url.params)
    path = _up.unquote(request.url.path)
    if path.startswith("/works/") and len(path) > len("/works/"):
        ident = path[len("/works/"):]
        if ident.lower().startswith("doi:"):
            rec = store.get_by_doi(ident[4:].lower())
        else:
            rec = store.get(ident.rsplit("/", 1)[-1])
        if rec is None:
            return None
        if rec.get("_dead"):
            return httpx.Response(
                404,
                headers={"content-type": "application/json"},
                content=b'{"error": "404 Not Found"}',
                request=request,
            )
        select = q.get("select")
        if select:
            fields = set(select.split(","))
            rec = {k: v for k, v in rec.items() if k in fields}
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(rec, sort_keys=True).encode("utf-8"),
            request=request,
        )
    if request.url.path != "/works":
        return None
    filt = q.get("filter", "")
    if filt.count(":") != 1 or "+" in filt:
        return None
    kind, _, raw = filt.partition(":")
    kind = kind.replace("_", "")
    if kind not in ("openalexid", "doi"):
        return None
    results, handled = [], True
    for value in raw.split("|"):
        rec = (
            store.get(value.rsplit("/", 1)[-1])
            if kind == "openalexid"
            else store.get_by_doi(value)
        )
        if rec is None:
            handled = False  # genuinely unknown work: fall through to generic
            break
        if rec.get("_dead"):
            continue  # by-id responses legitimately omit dead records
        results.append(rec)
    if not handled:
        return None
    select = q.get("select")
    if select:
        fields = set(select.split(","))
        results = [{k: r[k] for k in fields if k in r} for r in results]
    payload = json.dumps(
        {"meta": {"count": len(results), "next_url": None, "next": None}, "results": results},
        sort_keys=True,
    )
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=payload.encode("utf-8"),
        request=request,
    )


class _Replayer(httpx.BaseTransport):
    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        served = _synthesize_works(self._cassette.work_store, request)
        if served is not None:
            return served
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
        if response.status_code in _TRANSIENT_STATUSES:
            # persisting a 429/5xx poisons the cassette: the retry re-reads the
            # saved entry instead of going live again
            self._interactions.pop(key, None)
            self.save()
            if b"Insufficient budget" in response.content:
                err = OpenAlexError(
                    "OpenAlex credit budget exhausted for this IP (resets at "
                    "midnight UTC); resume recording later -- recorded "
                    "entries are kept"
                )
                err.reason = "rate_limited"
                raise err
            return response
        saved = response
        headers = {h: saved.headers[h] for h in saved.headers if h.lower() in _STORED_HEADERS}
        self._interactions[key] = {
            "status": saved.status_code,
            "headers": headers,
            **self._cassette.store_body(key, saved.content),
        }
        self._cassette.store_works(saved.content, request=request)
        if saved.status_code == 404 and request.url.path.startswith("/works/"):
            ident = request.url.path[len("/works/") :]
            if not ident.lower().startswith("doi:"):
                self._cassette.work_store.mark_dead(ident.rsplit("/", 1)[-1])
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
    """Replaying OpenAlex client by default; ``record=True`` hits the live API.

    Record mode sleeps for real (no_sleep would burst past OpenAlex's flood
    control) and honors ``OPENALEX_MAILTO`` for the polite pool.
    """
    import os
    import time

    cassette = Cassette(name)
    if record:
        live = httpx.Client(timeout=30.0)
        transport: httpx.BaseTransport = cassette.transport(recorder=live)
    else:
        transport = cassette.transport()
    http = httpx.Client(base_url=BASE_URL, transport=transport)
    return OpenAlexClient(
        mailto=os.environ.get("OPENALEX_MAILTO", "cassette@history-graph.local"),
        http=http,
        clock=time.monotonic if record else zero_clock,
        sleep=time.sleep if record else no_sleep,
    )


def s2_client(name: str = "semanticscholar", *, record: bool = False):
    """Semantic Scholar client bound to its own cassette."""
    from .s2 import BASE_URL as S2_BASE
    from .s2 import SemanticScholarClient

    cassette = Cassette(name)
    if record:
        live = httpx.Client(timeout=30.0)
        transport: httpx.BaseTransport = cassette.transport(recorder=live)
    else:
        transport = cassette.transport()
    http = httpx.Client(base_url=S2_BASE, transport=transport)
    return SemanticScholarClient(http=http, clock=zero_clock, sleep=no_sleep, min_interval_s=0.0)


def patent_client(name: str = "patents", *, record: bool = False):
    """Google Patents client bound to a cassette (never sleeping)."""
    from .uspto import BASE_URL as GP_BASE
    from .uspto import PatentSearchClient

    cassette = Cassette(name)
    if record:
        live = httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        transport: httpx.BaseTransport = cassette.transport(recorder=live)
    else:
        transport = cassette.transport()
    http = httpx.Client(base_url=GP_BASE, transport=transport)
    return PatentSearchClient(http=http, clock=zero_clock, sleep=no_sleep, min_interval_s=0.0)


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
