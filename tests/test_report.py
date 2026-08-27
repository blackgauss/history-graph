"""Report computation tests over fabricated thread outputs."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from history_graph.report import CURRENT_YEAR, run_report


def _write_ndjson(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _event(entry_id: str, year: int, kind: str, sub: str | None = None) -> dict:
    return {
        "id": entry_id,
        "date": str(year),
        "year": year,
        "kind": kind,
        "sub": sub,
        "title": f"Title {entry_id}",
        "who": None,
        "related": [],
        "notes": None,
    }


def test_run_report_computes_impact_edges_and_gaps(tmp_path: Path) -> None:
    _write_ndjson(
        tmp_path / "events.jsonl",
        [
            _event("a", 2000, "paper"),
            _event("b", 2005, "tech"),
            _event("c", 2020, "business", "founded"),
        ],
    )
    _write_ndjson(
        tmp_path / "papers.jsonl",
        [
            {
                "id": "paper-a",
                "openalex_id": "https://openalex.org/WA",
                "resolved_via": "doi",
                "doi": "https://doi.org/10.1/a",
                "title": "Paper A",
                "publication_year": 2000,
                "type": "article",
                "cited_by_count": 100,
                "referenced_works": ["https://openalex.org/WB", "https://openalex.org/WX"],
                "authorships": [],
            },
            {
                "id": "paper-b",
                "openalex_id": "https://openalex.org/WB",
                "resolved_via": "title",
                "doi": None,
                "title": "Paper B",
                "publication_year": 2010,
                "type": "article",
                "cited_by_count": 40,
                "referenced_works": ["https://openalex.org/WZ"],
                "authorships": [],
            },
        ],
    )

    report_path = run_report(tmp_path)

    impact = pl.read_csv(tmp_path / "paper_impact.csv")
    top = impact.head(1)
    expected_rate = round(100 / (CURRENT_YEAR - 2000 + 1), 1)
    assert top["entry_id"][0] == "paper-a"
    assert top["citations_per_year"][0] == expected_rate

    edges = pl.read_csv(tmp_path / "internal_citations.csv")
    assert edges.height == 1
    assert edges["citing_entry"][0] == "paper-a"
    assert edges["cited_entry"][0] == "paper-b"

    events = pl.read_csv(tmp_path / "events.csv")
    assert events.height == 3
    assert events["date"].to_list() == [2000, 2005, 2020]

    report = report_path.read_text(encoding="utf-8")
    assert "Timeline entries: **3**" in report
    assert "Internal citation edges among thread papers: **1**" in report
    assert "| Paper A |" in report or "Paper A | Paper B |" in report


def test_run_report_handles_no_internal_citations(tmp_path: Path) -> None:
    _write_ndjson(
        tmp_path / "events.jsonl",
        [_event("a", 2000, "paper")],
    )
    _write_ndjson(
        tmp_path / "papers.jsonl",
        [
            {
                "id": "solo",
                "openalex_id": "https://openalex.org/W1",
                "resolved_via": "doi",
                "doi": "x",
                "title": "Solo",
                "publication_year": 2000,
                "type": "article",
                "cited_by_count": 5,
                "referenced_works": ["https://openalex.org/W9"],
                "authorships": [],
            }
        ],
    )

    run_report(tmp_path)

    edges = pl.read_csv(tmp_path / "internal_citations.csv")
    assert edges.height == 0
    assert "None found." in (tmp_path / "report.md").read_text(encoding="utf-8")
