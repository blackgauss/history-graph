"""Tests for the cassette replay transport and observability helpers."""

from __future__ import annotations

from collections import defaultdict

import httpx
import pytest

from history_graph import observability
from history_graph.client import OpenAlexClient, OpenAlexError
from history_graph.scihub import SciHubClient
from history_graph.testing import Cassette, no_sleep, request_key, zero_clock


def _cassette_run(tmp_path, source_handler):
    source = httpx.Client(transport=httpx.MockTransport(source_handler))
    recorder = Cassette("unit", directory=tmp_path)
    client = httpx.Client(
        base_url="https://live.test",
        transport=recorder.transport(recorder=source),
    )
    first = client.get("/works", params={"filter": "doi:10.1/x", "mailto": "a@b.c"})
    second = client.get("/works", params={"filter": "doi:10.1/x", "mailto": "other@d.e"})
    replay = httpx.Client(
        base_url="https://live.test",
        transport=Cassette("unit", directory=tmp_path).transport(),
    )
    replayed = replay.get("/works", params={"filter": "doi:10.1/x", "mailto": "zzz@q.r"})
    return first, second, replayed


def test_replay_roundtrip_and_mailto_normalization(tmp_path) -> None:
    def live(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": str(request.url)})

    first, second, replayed = _cassette_run(tmp_path, live)

    assert replayed.status_code == 200 and replayed.json()["ok"] is True
    keys = {request_key(r.request) for r in (first, second, replayed)}
    assert len(keys) == 1
    key = keys.pop()
    assert "mailto" in str(first.url) and "mailto" not in key


def test_replay_missing_interaction_raises_clearly(tmp_path) -> None:
    live = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x")))
    Cassette("miss", directory=tmp_path).transport(recorder=live).handle_request(
        httpx.Request("GET", "https://live.test/known")
    )
    replayer = Cassette("miss", directory=tmp_path).transport()
    with pytest.raises(KeyError, match="known"):
        replayer.handle_request(httpx.Request("GET", "https://live.test/unknown"))


def test_large_bodies_go_to_sibling_files(tmp_path) -> None:
    big = b"x" * 600_000
    live = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=big)))
    Cassette("big", directory=tmp_path).transport(recorder=live).handle_request(
        httpx.Request("GET", "https://live.test/pdf")
    )
    body = httpx.Client(transport=Cassette("big", directory=tmp_path).transport()).get(
        "https://live.test/pdf"
    )
    assert body.content == big
    assert list((tmp_path / "big" / "bodies").glob("*.bin"))


def test_instrument_passes_through_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_PROFILE", True)
    monkeypatch.setattr(observability, "_seconds", defaultdict(float))
    monkeypatch.setattr(observability, "_calls", defaultdict(int))

    with observability.instrument("unit.span"):
        value = 42

    assert value == 42
    summary = observability.profile_summary()
    assert summary["unit.span"]["calls"] == 1
    assert summary["unit.span"]["seconds"] == pytest.approx(0, abs=1)


def test_break_requested_respects_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_BREAK_SPANS", {"stage.thread"})
    assert observability.break_requested("stage.thread")
    assert not observability.break_requested("stage.report")
    monkeypatch.setattr(observability, "_BREAK_SPANS", {"*"})
    assert observability.break_requested("anything")


def test_clock_injection_primitives() -> None:
    assert zero_clock() == 0.0
    assert no_sleep(99.0) is None


class _Always400(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, request=request)


def test_openalex_client_uses_injected_sleep_for_throttle() -> None:
    slept: list[float] = []
    ticks = iter(range(0, 1000))
    client = OpenAlexClient(
        http=httpx.Client(base_url="https://oa.test", transport=_Always400()),
        clock=lambda: float(next(ticks)),
        sleep=slept.append,
    )
    with pytest.raises(OpenAlexError):
        client._request("/works", {})
    assert slept and slept[0] <= 0.2  # 0.12s polite-pool gap, never real sleeping


def test_scihub_client_uses_injected_sleep_for_throttle() -> None:
    slept: list[float] = []
    client = SciHubClient(
        mirrors=("https://s.test",),
        http=httpx.Client(transport=_Always400()),
        clock=lambda: 0.0,
        sleep=slept.append,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client._get("https://s.test/10.1/x")
    assert slept == [pytest.approx(15.0)]  # 15s sci-hub policy via injected sleep only


def test_work_store_synthesis_select_slice_and_dead_ids(tmp_path) -> None:
    import urllib.parse as up

    import httpx

    from history_graph.testing import WorkStore, _synthesize_works

    store = WorkStore(tmp_path)
    store.upsert({"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a",
                  "cited_by_count": 5, "referenced_works": ["W0"]})
    store.mark_dead("W9")

    def synth(filter_: str, select: str | None = None) -> object:
        params = {"filter": filter_, "cursor": "*"}
        if select:
            params["select"] = select
        req = httpx.Request("GET", f"https://api.openalex.org/works?{up.urlencode(params)}")
        return _synthesize_works(store, req)

    out = synth("openalex_id:W1|W9")  # dead id omitted from results, batch still served
    import json
    payload = json.loads(out.content)
    assert [r["id"] for r in payload["results"]] == ["https://openalex.org/W1"]

    sliced = synth("openalex_id:W1", "id,cited_by_count")
    assert json.loads(sliced.content)["results"] == [
        {"id": "https://openalex.org/W1", "cited_by_count": 5}
    ]
    assert synth("doi:10.1/A") is not None         # doi index, case-insensitive
    assert synth("openalex_id:W9") is not None     # all-dead batch: served empty
    assert synth("openalex_id:W1|W42") is None     # unknown work -> fall through
    assert synth("citing_and_cited_by:W1") is None  # other filters stay generic
