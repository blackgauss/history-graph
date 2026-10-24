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
    monkeypatch.setenv("HG_ALLOW_CURATED_WRITES", "1")  # tests play the human
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


def test_curated_writes_are_human_gated(workspace: Path, monkeypatch) -> None:
    call(
        "propose_event",
        entry={"id": "gated-1991", "date": "1991", "kind": "paper", "title": "Gated"},
        evidence="unit test",
    )
    monkeypatch.delenv("HG_ALLOW_CURATED_WRITES")
    refused = call("apply_proposals", repro=False)
    assert refused["applied"] is False and "human" in refused["hint"]
    assert call("list_proposals")["events"], "proposal must stay quarantined"
    assert call("promote_thread", slug="whatever", repro=False)["applied"] is False


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


def test_references_of_reports_partial_resolution_and_truncation(monkeypatch) -> None:
    work = {
        "id": "https://openalex.org/W0",
        "title": "Source work",
        "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"],
    }

    class OA:
        def get_works(self, ids):
            if ids == [work["id"]]:
                return iter([work])
            return iter([{"id": ids[0], "title": "Resolved reference"}])

    monkeypatch.setattr(mcp_server, "_clients", {"openalex": OA()})
    result = call("references_of", openalex_id=work["id"], limit=2)

    assert result["status"] == "partial"
    assert result["reference_count"] == 3 and result["returned_count"] == 1
    assert result["unresolved_reference_ids"] == ["https://openalex.org/W2"]
    assert result["truncated"] is True and result["complete"] is False


def test_citing_works_returns_explicit_partial_cursor(monkeypatch) -> None:
    class OA:
        def iter_pages(self, path, params, **kwargs):
            assert path == "/works" and params == {"filter": "cites:W0"}
            assert kwargs["cursor"] == "*" and kwargs["per_page"] == 2
            return iter([{
                "meta": {"count": 3},
                "results": [{"id": "W1"}, {"id": "W2"}],
                "next_cursor": "cursor-next",
            }])

    monkeypatch.setattr(mcp_server, "_clients", {"openalex": OA()})
    result = call("citing_works", openalex_id="W0", per_page=2)

    assert result["status"] == "partial"
    assert result["direction"] == "cited_by"
    assert result["total_available"] == 3 and result["returned_count"] == 2
    assert result["next_cursor"] == "cursor-next" and result["complete"] is False


def test_opencitations_edges_are_source_labeled_and_bounded(monkeypatch) -> None:
    class OC:
        def references(self, doi, **kwargs):
            assert doi == "10.1126/science.1225829"
            assert kwargs == {"creation_after": None, "creation_before": None}
            return [
                {
                    "oci": "1-2",
                    "citing": "doi:10.1126/science.1225829",
                    "cited": "openalex:W2 doi:10.1/ref",
                },
                {"oci": "1-3", "citing": "doi:10.1126/science.1225829", "cited": "doi:10.1/other"},
            ]

    monkeypatch.setattr(mcp_server, "_clients", {"opencitations": OC()})
    result = call(
        "opencitations_references", doi="https://doi.org/10.1126/science.1225829", limit=1
    )

    assert result["status"] == "partial"
    assert result["source"] == "OpenCitations Index v2"
    assert result["direction"] == "references"
    assert result["total_available"] == 2 and result["returned_count"] == 1
    assert result["edges"][0]["cited"]["openalex_id"] == "https://openalex.org/W2"


def test_citation_source_cross_check_reports_differences(monkeypatch) -> None:
    work = {
        "id": "https://openalex.org/W0",
        "doi": "https://doi.org/10.1126/science.1225829",
        "title": "Source",
        "referenced_works": ["W1", "W2"],
        "cited_by_count": 100,
    }

    class OA:
        def get_work_by_doi(self, doi):
            return work

        def get_works(self, ids):
            return iter([
                {"id": "W1", "doi": "https://doi.org/10.1/shared"},
                {"id": "W2", "doi": "https://doi.org/10.1/openalex-only"},
            ])

    class OC:
        def references(self, doi):
            return [
                {"cited": "doi:10.1/shared"},
                {"cited": "doi:10.1/opencitations-only"},
            ]

        def citation_count(self, doi):
            return 98

    monkeypatch.setattr(mcp_server, "_clients", {"openalex": OA(), "opencitations": OC()})
    result = call("citation_source_cross_check", doi="10.1126/science.1225829")

    assert result["status"] == "complete"
    assert result["reference_doi_comparison"]["shared_count"] == 1
    assert result["reference_doi_comparison"]["openalex_only_dois"] == ["10.1/openalex-only"]
    assert result["reference_doi_comparison"]["opencitations_only_dois"] == [
        "10.1/opencitations-only"
    ]
    assert result["opencitations"]["cited_by_count"] == 98


def test_cap_keeps_truncated_output_valid_json() -> None:
    result = json.loads(mcp_server._cap({"body": "x" * (mcp_server.MAX_CHARS + 10)}))
    assert result["truncated"] is True
    assert result["original_chars"] > mcp_server.MAX_CHARS
    assert len(result["preview"]) == mcp_server.MAX_CHARS // 3


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
    assert hit["hits"][0]["page"] >= 1
    assert hit["hits"][0]["source"] == "local_cache"


def test_inspect_document_fetches_oa_text_and_returns_page_citations(
    tmp_path: Path, monkeypatch
) -> None:
    doi = "10.9999/open-access-paper"
    work = {
        "id": "https://openalex.org/W123",
        "doi": f"https://doi.org/{doi}",
        "title": "Open Access Paper",
        "publication_year": 2025,
        "abstract_inverted_index": {"An": [0], "abstract.": [1]},
        "best_oa_location": {
            "pdf_url": "https://repository.example/paper.pdf",
            "landing_page_url": "https://repository.example/paper",
            "license": "cc-by",
        },
        "locations": [],
    }

    class OA:
        def get_work_by_doi(self, requested_doi):
            assert requested_doi == doi
            return work

    class Pdf:
        def fetch(self, url):
            assert url == "https://repository.example/paper.pdf"
            return b"%PDF-1.7 test", url

    def extract(pdf_dir: Path):
        text_dir = pdf_dir / "text"
        text_dir.mkdir(exist_ok=True)
        (text_dir / f"{doi_to_filename(doi).removesuffix('.pdf')}.txt").write_text(
            "Page one abstract.\fPage two contains the distinctive method phrase.\f",
            encoding="utf-8",
        )
        return {"extracted": 1, "skipped_missing_binary": False}

    monkeypatch.setenv("HG_OPEN_ACCESS_PDFS_DIR", str(tmp_path / "oa"))
    monkeypatch.setattr(
        mcp_server, "_clients", {"openalex": OA(), "open_access_pdf": Pdf()}
    )
    monkeypatch.setattr(mcp_server, "extract_pdf_texts", extract)

    result = call("inspect_document", doi=doi, query="distinctive method", limit=3)

    assert result["status"] == "complete"
    assert result["title"] == "Open Access Paper"
    assert result["source"]["access"] == "open_access"
    assert result["hits"][0]["page"] == 2
    assert "distinctive method" in result["hits"][0]["excerpt"]


def test_inspect_document_reports_missing_full_text_and_keeps_abstract(
    tmp_path: Path, monkeypatch
) -> None:
    doi = "10.9999/abstract-only"
    work = {
        "id": "https://openalex.org/W124",
        "doi": f"https://doi.org/{doi}",
        "title": "Abstract Only",
        "publication_year": 2022,
        "abstract_inverted_index": {"A": [0], "public": [1], "abstract": [2]},
        "best_oa_location": None,
        "locations": [],
    }

    class OA:
        def get_work_by_doi(self, requested_doi):
            return work

    monkeypatch.setenv("HG_OPEN_ACCESS_PDFS_DIR", str(tmp_path / "oa"))
    monkeypatch.setenv("HG_PDFS_DIR", str(tmp_path / "legacy-pdfs"))
    monkeypatch.setattr(mcp_server, "_clients", {"openalex": OA()})
    result = call("inspect_document", doi=doi)

    assert result["status"] == "full_text_unavailable"
    assert result["abstract"] == "A public abstract"


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
        "opencitations_references", "opencitations_citations", "citation_source_cross_check",
        "fetch_open_access_pdf", "inspect_document",
    } <= names
    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == names
