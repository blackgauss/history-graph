"""Offline tests for the MCP lineage tools (plain function calls, no transport)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from history_graph import mcp_server
from history_graph.download import doi_to_filename, extract_pdf_texts
from history_graph.report import run_report
from history_graph.testing import openalex_client
from history_graph.thread import run_thread

SEEDS = Path("data/seed/computing_thread.yaml")
MINI_YAML = """\
# hand-written comment that must survive appends
entries:
  - id: seed-paper
    date: "1950"
    kind: paper
    title: "Seed Paper"
"""


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    raw_dir = tmp_path / "thread"
    client = openalex_client("openalex-thread")
    try:
        run_thread(client, SEEDS, raw_dir)
    finally:
        client.close()
    run_report(raw_dir)
    pdfs = tmp_path / "pdfs"
    seeded = tmp_path / "seed"
    seeded.mkdir()
    (seeded / "thread.yaml").write_text(MINI_YAML, encoding="utf-8")
    (seeded / "dois.txt").write_text("10.9999/existing  # curated\n", encoding="utf-8")
    monkeypatch.setenv("HG_THREAD_DIR", str(raw_dir))
    monkeypatch.setenv("HG_PDFS_DIR", str(pdfs))
    monkeypatch.setenv("HG_PROPOSED_DIR", str(tmp_path / "proposed"))
    monkeypatch.setenv("HG_THREAD_YAML", str(seeded / "thread.yaml"))
    monkeypatch.setenv("HG_SEED_TXT", str(seeded / "dois.txt"))
    return tmp_path


def call(name: str, **kwargs: Any) -> Any:
    fn = next(f for f in mcp_server.TOOLS if f.__name__ == name)
    return json.loads(fn(**kwargs))


# ------------------------------------------------------------------ graph reads


def test_thread_status_reports_counts(workspace: Path) -> None:
    status = call("thread_status")
    thread_dir = workspace / "thread"
    for key, name in (("papers", "papers.jsonl"), ("events", "events.jsonl")):
        lines = (thread_dir / name).read_text(encoding="utf-8").splitlines()
        assert status[key] == len([x for x in lines if x.strip()])
    assert status["papers"] >= 11 and status["events"] >= 41
    assert status["unresolved_papers"] == []


def test_list_events_filters_by_year_and_text(workspace: Path) -> None:
    early = call("list_events", year_to=1900)
    assert early["matched"] >= 1 and all(e["date"][:4] <= "1900" for e in early["events"])
    hits = call("list_events", contains="transistor")
    assert hits["matched"] >= 1


def test_get_paper_joins_all_tables(workspace: Path) -> None:
    paper = call("get_paper", entry_or_doi="shannon-math-theory-comm-1948")
    assert "Mathematical Theory" in paper["title"]
    assert paper["impact"] and paper["fulltext"]
    assert paper["abstract"]


def test_get_paper_unknown(workspace: Path) -> None:
    assert "error" in call("get_paper", entry_or_doi="nothing-here")


def test_edges_tools(workspace: Path) -> None:
    cit = call("list_citation_edges")
    assert len(cit) >= 1 and {"citing_entry", "cited_entry"} <= set(cit[0])
    curated = call("list_curated_edges")
    assert len(curated) >= 1 and {"source", "target"} <= set(curated[0])


# ---------------------------------------------------------------- fulltext/evidence


def test_search_fulltext_quotes_cassette_pdf(workspace: Path) -> None:
    pdfs = workspace / "pdfs"
    pdfs.mkdir()
    body = next(Path("tests/cassettes/scihub/bodies").glob("*.bin"))
    name = doi_to_filename("10.1145/3065386")
    shutil.copy(body, pdfs / (name + ".pdf"))
    result = extract_pdf_texts(pdfs)
    if result["skipped_missing_binary"]:
        pytest.skip("pdftotext not installed")
    assert result["extracted"] == 1
    hit = call("search_fulltext", query="convolutional")
    assert hit["corpus_files"] == 1 and hit["hits"]
    assert "convolutional" in hit["hits"][0]["excerpt"].lower()
    assert {"file", "excerpt", "title"} <= set(hit["hits"][0])


# ----------------------------------------------------------------------- proposals


def test_proposal_flow_end_to_end(workspace: Path) -> None:
    seed = call("propose_seed", doi="https://doi.org/10.1000/New", reason="missing link")
    assert seed["status"] == "proposed"
    assert call("propose_seed", doi="10.1000/NEW", reason="again")["status"] == "duplicate"

    entry = {
        "id": "new-link-1960",
        "date": "1960",
        "kind": "paper",
        "title": "The Missing Link",
        "refs": {"doi": "10.1000/New"},
    }
    res = call("propose_event", entry=entry, evidence="Krizhevsky writes 'following ...'")
    assert res["status"] == "proposed"
    assert call("propose_event", entry=entry, evidence="")["status"] == "rejected"

    listing = call("list_proposals")
    assert [s["doi"] for s in listing["seeds"]] == ["10.1000/new"]
    assert listing["events"][0]["entry"]["id"] == "new-link-1960"

    applied = call("apply_proposals", repro=False)
    assert applied["events_added"] == ["new-link-1960"]
    assert applied["seeds_added"] == ["10.1000/new"]

    yaml_text = (workspace / "seed/thread.yaml").read_text()
    assert "hand-written comment" in yaml_text  # comments preserved
    assert "new-link-1960" in yaml_text
    assert "10.1000/new" in (workspace / "seed/dois.txt").read_text()
    assert call("list_proposals") == {"seeds": [], "events": []}


def test_apply_rejects_bad_entry_schema(workspace: Path) -> None:
    with pytest.raises(Exception, match="kind"):
        call(
            "propose_event",
            entry={"id": "x", "date": "1999", "kind": "vibe", "title": "T"},
            evidence="something",
        )


# --------------------------------------------------------------------------- server


def test_server_builds_with_all_tools() -> None:
    names = {fn.__name__ for fn in mcp_server.TOOLS}
    assert {
        "thread_status", "list_events", "get_paper", "list_citation_edges",
        "list_curated_edges", "resolve_doi", "find_by_title", "references_of",
        "citing_works", "search_fulltext", "fetch_pdf", "propose_seed",
        "propose_event", "list_proposals", "apply_proposals",
    } <= names
    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == names
