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


def test_reconstruct_abstract_from_inverted_index() -> None:
    from history_graph.report import reconstruct_abstract

    inverted = {
        "research": [0, 4],
        "paper": [1, 5],
        "about": [2],
        "history": [3],
    }
    assert reconstruct_abstract(inverted) == "research paper about history research paper"
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


def test_build_fulltext_extracts_oa_links(tmp_path: Path) -> None:
    _write_ndjson(tmp_path / "events.jsonl", [_event("a", 2000, "paper")])
    oa_paper = {
        "id": "oa-paper",
        "openalex_id": "https://openalex.org/WOA",
        "resolved_via": "doi",
        "doi": "x",
        "title": "OA Paper",
        "publication_year": 2000,
        "type": "article",
        "cited_by_count": 5,
        "referenced_works": [],
        "authorships": [],
        "funders": [],
        "primary_location": {},
"best_oa_location": {
            "pdf_url": "https://example.org/oa.pdf",
            "landing_page_url": "https://doi.org/10.1/x",
            "license": "cc-by",
        },
        "open_access": {"is_oa": True, "oa_status": "hybrid", "oa_url": "https://example.org/oa.pdf"},
        "abstract_inverted_index": {"data": [1], "abstract": [0]},
    }
    closed_paper = {
        "id": "closed-paper",
        "openalex_id": "https://openalex.org/WCL",
        "resolved_via": "doi",
        "doi": "y",
        "title": "Closed Paper",
        "publication_year": 2001,
        "type": "article",
        "cited_by_count": 1,
        "referenced_works": [],
        "authorships": [],
        "funders": [],
        "primary_location": {},
        "best_oa_location": None,
        "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None},
    }
    _write_ndjson(tmp_path / "papers.jsonl", [oa_paper, closed_paper])

    run_report(tmp_path)

    fulltext = pl.read_csv(tmp_path / "paper_fulltext.csv")
    assert fulltext.height == 2
    oa_row = fulltext.filter(pl.col("paper_id") == "oa-paper")
    assert oa_row["pdf_url"][0] == "https://example.org/oa.pdf"
    assert oa_row["license"][0] == "cc-by"
    closed_row = fulltext.filter(pl.col("paper_id") == "closed-paper")
    assert not bool(closed_row["is_oa"][0])

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "1/2 thread papers have a lawful free full text" in report

    abstracts = pl.read_csv(tmp_path / "paper_abstracts.csv")
    oa_abstract = abstracts.filter(pl.col("paper_id") == "oa-paper")
    assert oa_abstract["abstract"][0] == "abstract data"
    assert "1/2 thread papers have a free abstract" in report


def test_build_relations_extracts_orgs_funders_and_venue(tmp_path: Path) -> None:
    _write_ndjson(tmp_path / "events.jsonl", [_event("a", 2000, "paper")])
    _write_ndjson(
        tmp_path / "papers.jsonl",
        [
            {
                "id": "paper-a",
                "openalex_id": "https://openalex.org/WA",
                "resolved_via": "doi",
                "doi": "x",
                "title": "Paper A",
                "publication_year": 2000,
                "type": "article",
                "cited_by_count": 5,
                "referenced_works": [],
                "authorships": [
                    {
                        "author": {"display_name": "Jane Doe"},
                        "institutions": [
                            {"display_name": "DeepMind", "country_code": "GB", "type": "company"}
                        ],
                    }
                ],
                "funders": [{"display_name": "DeepMind", "ror": "https://ror.org/00971b260"}],
                "primary_location": {
                    "source": {
                        "display_name": "Nature",
                        "host_organization_name": "Springer Nature",
                    }
                },
            }
        ],
    )

    run_report(tmp_path)

    institutions = pl.read_csv(tmp_path / "paper_institutions.csv")
    assert institutions.height == 1
    assert institutions["institution"][0] == "DeepMind"
    assert institutions["type"][0] == "company"
    assert institutions["country_code"][0] == "GB"

    funders = pl.read_csv(tmp_path / "paper_funders.csv")
    assert funders["funder"][0] == "DeepMind"
    assert str(funders["ror"][0]).endswith("00971b260")

    venues = pl.read_csv(tmp_path / "paper_venue.csv")
    assert venues["venue"][0] == "Nature"

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Paper -> Venue" in report
    assert "## Paper -> Funders" in report
    assert "## Paper -> Institutions" in report
