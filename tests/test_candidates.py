"""Candidate thread lifecycle: propose -> add -> test -> grow -> promote."""

from pathlib import Path
from typing import Any

import pytest

from history_graph import candidates, mcp_server

W = {
    "W1": {  # 1847 ancestor
        "id": "W1", "doi": "https://doi.org/10.1/a", "title": "The Principles",
        "publication_year": 1847, "cited_by_count": 100, "referenced_works": ["W0"],
        "authorships": [{"author": {"display_name": "George A"}}],
    },
    "W2": {  # 1938 cites W1 (the good edge)
        "id": "W2", "doi": "https://doi.org/10.2/b", "title": "A Theory",
        "publication_year": 1938, "cited_by_count": 900, "referenced_works": ["W1", "W9"],
        "authorships": [{"author": {"display_name": "Claude B"}}],
    },
    "W3": {  # 1950 does NOT cite W1 (metadata-blind pair)
        "id": "W3", "doi": "https://doi.org/10.3/c", "title": "Unlinked Later",
        "publication_year": 1950, "cited_by_count": 10, "referenced_works": ["W7"],
        "authorships": [{"author": {"display_name": "Ada C"}}],
    },
    "W9": {"id": "W9", "doi": "https://doi.org/10.9/z", "title": "Shared Ancestor",
           "publication_year": 1900, "cited_by_count": 5},
    "W90": {"id": "W90", "doi": "https://doi.org/10.90/n", "title": "Hot New Citer",
            "publication_year": 2010, "cited_by_count": 50},
}


class FakeClient:
    def __init__(self):
        self.calls: list[Any] = []

    def get_work_by_doi(self, doi):
        self.calls.append(("doi", doi))
        return next((w for w in W.values() if w.get("doi", "").endswith(doi)), None)

    def get_work_by_title(self, title):
        return next((w for w in W.values() if w.get("title") == title), None)

    def get_works(self, ids):
        return iter(
            [
                {
                    "id": wid,
                    "doi": W.get(wid, {}).get("doi"),
                    "title": W.get(wid, {}).get("title"),
                    "publication_year": W.get(wid, {}).get("publication_year"),
                    "cited_by_count": W.get(wid, {}).get("cited_by_count"),
                }
                for wid in ids
            ]
        )

    def iter_citing_works(self, wid, max_pages=None):
        if wid == "W2":
            return iter([W["W90"]])
        return iter([])


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("HG_CANDIDATES_DIR", str(tmp_path / "candidates"))
    monkeypatch.setenv("HG_THREAD_YAML", str(tmp_path / "curated.yaml"))
    (tmp_path / "curated.yaml").write_text(
        "# curated thread\nentries:\n"
        '  - id: old_entry\n    date: "1900"\n    kind: paper\n    title: Old\n'
    )
    d = tmp_path / "candidates"
    d.mkdir()
    return {"dir": d, "yaml": tmp_path / "curated.yaml", "client": FakeClient()}


def test_propose_add_test_grow_promote(env):
    proposed = candidates.propose_thread(
        env["client"], "lineage", "A -> B", ["10.1/a", "10.2/b", "10.3/c"],
        candidate_dir=env["dir"],
    )
    assert len(proposed["entries"]) == 3

    added = candidates.add_entries(
        "lineage",
        [{"id": "w3-link", "date": "1950", "kind": "paper", "title": "Unlinked Later",
          "refs": {"doi": "10.3/c"}, "related": ["missing_id"]}],
        candidate_dir=env["dir"],
    )
    assert added["added"] or added["skipped"]  # idempotent-ish

    score = candidates.score_thread(env["client"], "lineage", candidate_dir=env["dir"])
    assert score["resolved"] >= 3
    kinds = {e["kind"] for e in score["edges"]}
    # curated edges carry authorship/related links; support kinds are computable
    assert kinds <= {"cites-backwards", "co-cited", "metadata-blind", "unresolved"}
    assert score["verdict"] in {"sound", "gaps"}

    suggestions = candidates.frontier(env["client"], "lineage", candidate_dir=env["dir"])
    assert any(s["openalex_id"] == "W90" for s in suggestions)  # cites W2
    ranked = [s["openalex_id"] for s in suggestions]
    assert ranked[0] == "W90"  # only candidate present

    promoted = candidates.promote_thread(
        "lineage", candidate_dir=env["dir"], thread_yaml=env["yaml"]
    )
    text = env["yaml"].read_text()
    assert "# curated thread" in text and 'title: "The Principles"' in text
    thread = candidates.load_thread("lineage", env["dir"])
    assert set(promoted["promoted"]) <= {e.id for e in thread["entries"]}


def test_promote_skips_existing_ids(env):
    candidates.propose_thread(env["client"], "s", "c", ["10.1/a"], candidate_dir=env["dir"])
    # seed a curated entry with the same id the candidate proposes
    entry = candidates.propose_thread(
        env["client"], "s2", "c", ["10.1/a"], candidate_dir=env["dir"]
    )["entries"][0]
    with env["yaml"].open("a") as handle:
        handle.write(f"\n  - id: {entry}\n    date: \"1847\"\n    kind: paper\n    title: dup\n")
    promote = candidates.promote_thread("s", candidate_dir=env["dir"], thread_yaml=env["yaml"])
    assert promote["promoted"] == [] and promote["skipped_existing"] == 1


def _call(name, **kwargs):
    fn = next(f for f in mcp_server.TOOLS if f.__name__ == name)
    return fn(**kwargs)


def test_mcp_thread_tools_roundtrip(env, monkeypatch):
    monkeypatch.setattr(mcp_server, "_clients", {"openalex": env["client"], "scihub": None})
    call = _call

    assert "lineage" in call("propose_thread", slug="Lineage", claim="A->B",
                             seed_dois=["10.1/a", "10.2/b"])
    assert "lineage" in call("list_candidate_threads")
    score = call("test_thread", slug="lineage")
    assert score != "error"
    assert "W90" in call("grow_thread", slug="lineage")
    assert "Hot New Citer" in call("grow_thread", slug="lineage")
    promoted = call("promote_thread", slug="lineage")
    assert "promoted" in promoted or "skipped" in promoted
    assert 'title: "A Theory"' in env["yaml"].read_text()


def test_unknown_thread_errors_cleanly(env):
    with pytest.raises(FileNotFoundError):
        candidates.score_thread(env["client"], "nope", candidate_dir=env["dir"])
