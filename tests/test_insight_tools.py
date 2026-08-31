"""Offline tests for path finding, insight scoring, and S2 contexts."""

import json
from pathlib import Path
from typing import Any

import httpx

from history_graph import s2
from history_graph.insight import concept_gap, summarize_path
from history_graph.paths import citation_path


def W(wid, cites=(), cites_by=(), gap=0.0, title=None):
    return {
        "id": wid,
        "doi": f"https://doi.org/10.1/{wid.lower()}",
        "title": title or f"Work {wid}",
        "publication_year": 2000 + int(wid[-1]) * 5,
        "cited_by_count": len(cites_by) * 10 + 5,
        "referenced_works": list(cites),
    }


# chain: A <- B <- C <- D  (each cites its predecessor)
CHAIN = {w["id"]: w for w in [
    W("W1", cites=(), cites_by=("W2",)),
    W("W2", cites=("W1",), cites_by=("W3",)),
    W("W3", cites=("W2",), cites_by=("W4",)),
    W("W4", cites=("W3",), cites_by=()),
]}


class FakeGraphClient:
    def __init__(self, works):
        self.works = works

    def get_work_by_doi(self, doi):
        tail = doi.split("/")[-1]
        return next((w for w in self.works.values() if str(w.get("doi", "")).endswith(tail)), None)

    def get_work_by_title(self, title):
        return next((w for w in self.works.values() if w.get("title") == title), None)

    def get_works(self, ids, select=None):
        return iter([self.works[i] for i in ids if i in self.works])

    def iter_citing_works(self, wid, select=None, max_pages=None, per_page=None):
        return iter([w for w in self.works.values() if wid in w.get("referenced_works", [])])


def test_finds_short_path_across_chain(tmp_path: Path):
    out = citation_path(FakeGraphClient(CHAIN), "W1", "W4", cache_dir=tmp_path)
    assert out["found"]
    assert out["paths"][0]["ids"] in (["W1", "W2", "W3", "W4"], ["W4", "W3", "W2", "W1"])
    assert out["explored"] < 8


def test_path_is_deterministic_across_runs(tmp_path: Path):
    a = citation_path(FakeGraphClient(CHAIN), "W2", "W4", cache_dir=tmp_path / "a")
    b = citation_path(FakeGraphClient(CHAIN), "W2", "W4", cache_dir=tmp_path / "b")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_budget_exhaustion_reports_not_fabricates(tmp_path: Path):
    hub = {f"H{i}": W(f"W{i}") for i in range(1, 40)}
    works = {**CHAIN, **hub}
    out = citation_path(FakeGraphClient(works), "W1", "ZZ", cache_dir=tmp_path, max_nodes=4)
    assert not out["found"] and "budget" in out["reason"] or "unresolved" in out["reason"]


def test_bridge_node_flagged(tmp_path: Path):
    # W1 <- W2 <- {W3, W4} <- W5: two 3-hop routes sharing the W2 bridge
    works = {w["id"]: w for w in [
        W("W1"),
        W("W2", cites=("W1",)),
        W("W3", cites=("W2",)),
        W("W4", cites=("W2",)),
        W("W5", cites=("W4", "W3")),
    ]}
    out = citation_path(FakeGraphClient(works), "W1", "W5", cache_dir=tmp_path, max_paths=3)
    assert out["found"]
    assert len(out["paths"]) == 2
    assert out["bridges"] and out["bridges"][0]["id"] == "W2"


def test_concept_gap_semantics():
    lm = [{"display_name": "logic", "score": 0.9}, {"display_name": "math", "score": 0.5}]
    a = {"concepts": lm}
    b = {"concepts": list(lm)}
    c = {"concepts": [{"display_name": "particle physics", "score": 0.9}]}
    assert concept_gap(a, b) == 0.0
    assert concept_gap(a, c) > 0.8
    assert concept_gap(a, {"concepts": []}) == 0.4  # neutral, not zero


def test_summarize_path_flags_surprising_hops():
    nodes = [
        {"concepts": [{"display_name": "neural networks", "score": 0.9}]},
        {"concepts": [{"display_name": "gradient boosting", "score": 0.9}]},
        {"concepts": [{"display_name": "click prediction", "score": 0.9}]},
    ]
    out = summarize_path(nodes)
    assert len(out["hop_gaps"]) == 2 and out["score"] > 0.5


# ------------------------------------------------------------------ S2


def s2_client_with(responses: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in responses.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "unknown"})

    http = httpx.Client(base_url=s2.BASE_URL, transport=httpx.MockTransport(handler))
    return s2.SemanticScholarClient(http=http, sleep=lambda s: None, clock=lambda: 0.0)


def test_s2_builds_upon_intent_from_context():
    client = s2_client_with({
        "/paper/doi:10.1/b/citations": {
            "data": [{
                "context": "Following the method introduced in the factorization"
                " machine paper [12]",
                "intents": [],
                "isInText": True,
                "citedPaper": {"paperId": "x", "externalIds": {"DOI": "10.1/a"}},
            }],
            "next": None,
        }
    })
    out = client.citation_context("10.1/b", "10.1/a")
    assert out["intent"] in {"builds-upon", "method", "used"}
    assert out["in_text"] is True


def test_s2_404_is_unknown_not_empty():
    client = s2_client_with({})
    assert client.citation_context("10.1/nope", "10.1/a") is None


def test_s2_to_s2_id_forms():
    assert s2.SemanticScholarClient.to_s2_id("10.1038/x") == "doi:10.1038/x"
    assert s2.SemanticScholarClient.to_s2_id("ARXIV:1706.03762") == "ARXIV:1706.03762"
    assert s2.SemanticScholarClient.to_s2_id("https://doi.org/10.1/x") == "doi:10.1/x"

