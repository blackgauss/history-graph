"""Derive meaningful views from an ingested thread.

Builds, via polars:
- paper_impact.csv         per-paper citation impact metrics
- internal_citations.csv   citation edges among the thread's own papers
- events.csv               normalized chronological timeline
- report.md                human-readable summary of all three

Usage: python -m history_graph.report [--raw-dir data/thread]
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

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
    exploded = papers.select(pl.col("id"), pl.col("referenced_works")).explode("referenced_works")
    edges = (
        exploded.join(id_to_entry, left_on="referenced_works", right_on="openalex_id", how="inner")
        .rename({"id": "citing_entry", "id_right": "cited_entry"})
        .select("citing_entry", "cited_entry")
        .unique()
        .sort(["citing_entry", "cited_entry"])
    )
    titles = dict(papers.select("id", "title").iter_rows())
    return edges, titles


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
) -> Path:
    events.select("date", "kind", "sub", "title", "who").write_csv(raw_dir / "events.csv")
    impact.write_csv(raw_dir / "paper_impact.csv")
    edges.write_csv(raw_dir / "internal_citations.csv")

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
    gaps = build_gap_stats(events)
    return write_report(raw_dir, events, impact, edges, edge_titles, gaps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="history-graph.report", description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/thread"))
    args = parser.parse_args(argv)
    report_path = run_report(args.raw_dir)
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
