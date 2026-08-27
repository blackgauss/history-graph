"""Derive meaningful views from an ingested thread.

Builds, via polars:
- events.csv               normalized chronological timeline
- paper_impact.csv         per-paper citation impact metrics
- internal_citations.csv   citation edges among the thread's own papers
- paper_institutions.csv   paper -> institution edges (type, country)
- paper_funders.csv        paper -> funder edges (name, ROR)
- paper_venue.csv          paper -> journal/venue edges
- paper_fulltext.csv       paper -> lawful open-access full-text links
- report.md                human-readable summary

Usage: python -m history_graph.report [--raw-dir data/thread]
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

CURRENT_YEAR = date.today().year


def load_frames(raw_dir: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    events = pl.read_ndjson(raw_dir / "events.jsonl").sort("year")
    papers = pl.read_ndjson(raw_dir / "papers.jsonl")
    return events, papers


def build_impact(papers: pl.DataFrame) -> pl.DataFrame:
    return papers.select(
        pl.col("id").alias("entry_id"),
        pl.col("title"),
        pl.col("publication_year"),
        pl.col("cited_by_count"),
        (pl.col("cited_by_count") / (CURRENT_YEAR - pl.col("publication_year") + 1))
        .round(1)
        .alias("citations_per_year"),
    ).sort("cited_by_count", descending=True)


def build_internal_citations(
    papers: pl.DataFrame,
) -> tuple[pl.DataFrame, dict[str, str]]:
    """Edges where one thread paper cites another thread paper."""
    id_to_entry = papers.select(pl.col("openalex_id"), pl.col("id"))
    exploded = (
        papers.select(pl.col("id"), pl.col("referenced_works"))
        .explode("referenced_works")
        .with_columns(pl.col("referenced_works").cast(pl.Utf8))
        .drop_nulls()
    )
    edges = (
        exploded.join(id_to_entry, left_on="referenced_works", right_on="openalex_id", how="inner")
        .rename({"id": "citing_entry", "id_right": "cited_entry"})
        .select("citing_entry", "cited_entry")
        .unique()
        .sort(["citing_entry", "cited_entry"])
    )
    titles = dict(papers.select("id", "title").iter_rows())
    return edges, titles


def _paper_dicts(papers: pl.DataFrame) -> list[dict[str, Any]]:
    return papers.to_dicts()


def build_relations(papers: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Walk nested record fields into flat paper->org relation tables.

    Returns (institutions, funders, venues) frames.
    """
    inst_rows: list[dict[str, Any]] = []
    funder_rows: list[dict[str, Any]] = []
    venue_rows: list[dict[str, Any]] = []

    for record in _paper_dicts(papers):
        paper_id = record["id"]
        for authorship in record.get("authorships") or []:
            for institution in authorship.get("institutions") or []:
                inst_rows.append(
                    {
                        "paper_id": paper_id,
                        "author": authorship.get("author", {}).get("display_name"),
                        "institution": institution.get("display_name"),
                        "country_code": institution.get("country_code"),
                        "type": institution.get("type"),
                    }
                )
        for funder in record.get("funders") or []:
            funder_rows.append(
                {
                    "paper_id": paper_id,
                    "funder": funder.get("display_name"),
                    "ror": funder.get("ror"),
                }
            )
        source = (record.get("primary_location") or {}).get("source")
        venue_rows.append(
            {
                "paper_id": paper_id,
                "venue": source.get("display_name") if source else None,
                "publisher": source.get("host_organization_name") if source else None,
            }
        )

    institutions = (
        pl.DataFrame(inst_rows).unique().sort("paper_id")
        if inst_rows
        else pl.DataFrame(
            schema={
                "paper_id": pl.Utf8,
                "author": pl.Utf8,
                "institution": pl.Utf8,
                "country_code": pl.Utf8,
                "type": pl.Utf8,
            }
        )
    )
    funders = (
        pl.DataFrame(funder_rows).unique().sort("paper_id")
        if funder_rows
        else pl.DataFrame(schema={"paper_id": pl.Utf8, "funder": pl.Utf8, "ror": pl.Utf8})
    )
    venues = (
        pl.DataFrame(venue_rows).sort("paper_id")
        if venue_rows
        else pl.DataFrame(schema={"paper_id": pl.Utf8, "venue": pl.Utf8, "publisher": pl.Utf8})
    )
    return institutions, funders, venues


def build_fulltext(papers: pl.DataFrame) -> pl.DataFrame:
    """Extract lawful open-access full-text links per paper."""
    rows: list[dict[str, Any]] = []
    for record in _paper_dicts(papers):
        best = record.get("best_oa_location") or {}
        oa = record.get("open_access") or {}
        rows.append(
            {
                "paper_id": record["id"],
                "pdf_url": best.get("pdf_url") or oa.get("oa_url"),
                "landing_page_url": best.get("landing_page_url"),
                "license": best.get("license"),
                "is_oa": oa.get("is_oa"),
                "oa_status": oa.get("oa_status"),
            }
        )
    return (
        pl.DataFrame(rows)
        .with_columns(pl.col("is_oa").cast(pl.Boolean))
        .sort("paper_id")
        if rows
        else pl.DataFrame(
            schema={
                "paper_id": pl.Utf8,
                "pdf_url": pl.Utf8,
                "landing_page_url": pl.Utf8,
                "license": pl.Utf8,
                "is_oa": pl.Boolean,
                "oa_status": pl.Utf8,
            }
        )
    )


def build_gap_stats(events: pl.DataFrame) -> dict[str, float]:
    years = events.get_column("year")
    gaps = years.diff().drop_nulls()
    if gaps.len() == 0:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    return {
        "min": float(gaps.min()),
        "median": float(gaps.median()),
        "max": float(gaps.max()),
    }


def _markdown_table(df: pl.DataFrame, columns: list[str]) -> list[str]:
    rows = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in df.select(columns).iter_rows():
        cells = ["" if value is None else str(value) for value in row]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def write_report(
    raw_dir: Path,
    events: pl.DataFrame,
    impact: pl.DataFrame,
    edges: pl.DataFrame,
    edge_titles: dict[str, str],
    gaps: dict[str, float],
    institutions: pl.DataFrame,
    funders: pl.DataFrame,
    venues: pl.DataFrame,
    fulltext: pl.DataFrame,
) -> Path:
    events.select("date", "kind", "sub", "title", "who").write_csv(raw_dir / "events.csv")
    impact.write_csv(raw_dir / "paper_impact.csv")
    edges.write_csv(raw_dir / "internal_citations.csv")
    institutions.write_csv(raw_dir / "paper_institutions.csv")
    funders.write_csv(raw_dir / "paper_funders.csv")
    venues.write_csv(raw_dir / "paper_venue.csv")
    fulltext.write_csv(raw_dir / "paper_fulltext.csv")

    kind_counts = events["kind"].value_counts().sort("count", descending=True)
    kinds_summary = ", ".join(f"{kind}={count}" for kind, count in kind_counts.iter_rows())
    lines = [
        "# Computing thread report",
        "",
        f"- Timeline entries: **{events.height}** ({kinds_summary})",
        f"- Papers resolved: **{impact.height}**",
        f"- Internal citation edges among thread papers: **{edges.height}**",
        (
            f"- Milestone gap in years — min {gaps['min']:.0f}, "
            f"median {gaps['median']:.0f}, max {gaps['max']:.0f}"
        ),
        "",
        "## Paper impact",
        "",
        *_markdown_table(
            impact,
            ["entry_id", "publication_year", "cited_by_count", "citations_per_year"],
        ),
        "",
        "## Internal citations (thread paper -> thread paper)",
        "",
    ]
    if edges.height == 0:
        lines.append("_None found._")
    else:
        mapped = edges.with_columns(
            pl.col("citing_entry").replace_strict(edge_titles),
            pl.col("cited_entry").replace_strict(edge_titles),
        )
        lines += _markdown_table(mapped, ["citing_entry", "cited_entry"])

    lines += [
        "",
        "## Paper -> Venue",
        "",
        *_markdown_table(venues, ["paper_id", "venue", "publisher"]),
        "",
        "## Paper -> Funders (companies/orgs)",
        "",
        *_markdown_table(
            funders.select("paper_id", "funder", "ror").unique().sort("paper_id"),
            ["paper_id", "funder", "ror"],
        ),
        "",
        "## Paper -> Institutions (authors' affiliations)",
        "",
        *_markdown_table(
            institutions.select("paper_id", "institution", "country_code", "type")
            .unique()
            .sort("paper_id"),
            ["paper_id", "institution", "country_code", "type"],
        ),
        "",
        "## Paper -> Open-access full text (legal PDF links)",
        "",
    ]
    oa_count = int(fulltext.filter(pl.col("is_oa").fill_null(False)).height)
    lines.append(f"- {oa_count}/{fulltext.height} thread papers have a lawful free full text.")
    lines.append("")
    lines += _markdown_table(
        fulltext.select("paper_id", "pdf_url", "license", "oa_status"),
        ["paper_id", "pdf_url", "license", "oa_status"],
    )

    report_path = raw_dir / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run_report(raw_dir: Path) -> Path:
    events, papers = load_frames(raw_dir)
    papers = papers.with_columns(
        pl.col("publication_year").cast(pl.Int64),
        pl.col("cited_by_count").cast(pl.Int64),
    )
    impact = build_impact(papers)
    edges, edge_titles = build_internal_citations(papers)
    institutions, funders, venues = build_relations(papers)
    fulltext = build_fulltext(papers)
    gaps = build_gap_stats(events)
    return write_report(
        raw_dir,
        events,
        impact,
        edges,
        edge_titles,
        gaps,
        institutions,
        funders,
        venues,
        fulltext,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="history-graph.report", description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/thread"))
    args = parser.parse_args(argv)
    report_path = run_report(args.raw_dir)
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
