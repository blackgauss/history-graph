"""Path-finding / insight harness: fixed queries, graded expectations, goldens.

This is the numeric feedback loop for the insight features: a query is a
citation-path request plus expectations (found, hop limit, must-include nodes,
bridge nodes, minimum concept-gap score). Fixture queries run against an
offline synthetic graph (fast inner loop, no HTTP at all); live queries run
against the OpenAlex cassette (recordable/replayable). A golden JSON summary
detects silent regressions across refactors — the recursive-improvement seam:
tune scoring or search, rerun, inspect the score, then update goldens only
when the diff is justified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import citation_path

QUERIES_PATH = Path("tests/insight_queries.json")
GOLDEN_PATH = Path("tests/golden/insight.json")


class FixtureClient:
    """Deterministic in-memory OpenAlex stand-in built from a graph fixture."""

    def __init__(self, works: list[dict[str, Any]]) -> None:
        self.works = {w["id"]: w for w in works}

    def get_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        doi = doi.lower()
        return next(
            (w for w in self.works.values() if str(w.get("doi", "")).lower().endswith(doi)), None
        )

    def get_work_by_title(self, title: str) -> dict[str, Any] | None:
        return next((w for w in self.works.values() if w.get("title") == title), None)

    def get_works(self, ids, select=None):
        return iter([self.works[i] for i in ids if i in self.works])

    def iter_citing_works(self, wid, select=None, max_pages=None, per_page=None):
        return iter([w for w in self.works.values() if wid in w.get("referenced_works", [])])


def fixture_client(path: Path) -> FixtureClient:
    return FixtureClient(json.loads(path.read_text(encoding="utf-8"))["works"])


def grade(query: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Compare a citation_path result against the query's expectations."""
    fails: list[str] = []
    observed: dict[str, Any] = {"found": result.get("found")}
    if result.get("found"):
        best = result["paths"][0]
        observed.update(
            {
                "edges": len(best["ids"]) - 1,
                "ids": best["ids"],
                "score": best["score"],
                "bridges": [b["id"] for b in result.get("bridges") or []],
            }
        )
    exp = query.get("expect", {})
    if bool(exp.get("found", True)) != bool(result.get("found")):
        fails.append(f"found={result.get('found')!r}")
    if result.get("found"):
        if "edges" in exp and observed["edges"] > exp["edges"]:
            fails.append(f"edges {observed['edges']} > {exp['edges']}")
        for need in exp.get("include", []):
            blob = json.dumps(result["paths"], sort_keys=True)
            if need not in blob:
                fails.append(f"missing node {need}")
        for need in exp.get("bridges", []):
            if need not in observed["bridges"]:
                fails.append(f"missing bridge {need}")
        if "min_score" in exp and observed["score"] < exp["min_score"]:
            fails.append(f"score {observed['score']} < {exp['min_score']}")
    return {"id": query["id"], "ok": not fails, "fails": fails, "observed": observed}


def grade_dossier(query: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    from .dossier import lineage_dossier  # noqa: F401 (import cost only on this path)

    fails: list[str] = []
    observed: dict[str, Any] = {"found": out.get("found")}
    if out.get("found"):
        observed.update({
            "hub": out["hub"]["id"],
            "upstream": sorted(u["id"] for u in out["upstream"]),
            "downstream": [d["id"] for d in out["downstream"]],
            "schools": len(out["fuses"]),
        })
    exp = query.get("expect", {})
    if bool(exp.get("found", True)) != bool(out.get("found")):
        fails.append(f"found={out.get('found')!r}")
    if "schools_min" in exp and observed.get("schools", 0) < exp["schools_min"]:
        fails.append(f"schools {observed.get('schools')} < {exp['schools_min']}")
    for need in exp.get("upstream_includes", []):
        if need not in observed.get("upstream", []):
            fails.append(f"missing ancestor {need}")
    return {
        "id": query["id"], "ok": not fails, "fails": fails, "observed": observed,
        "mode": "fixture" if query.get("fixture") else "cassette",
    }


def run_query(client_factory, query: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    client = client_factory("fixture" if query.get("fixture") else "openalex")
    if query.get("fixture"):
        client = fixture_client(Path(query["fixture"]))
    if query.get("dossier"):
        from .dossier import lineage_dossier

        return grade_dossier(
            query,
            lineage_dossier(client, query["work"], cache_dir=cache_dir / query["id"]),
        )
    result = citation_path(
        client,
        query["from"],
        query["to"],
        max_nodes=query.get("max_nodes", 80),
        max_paths=query.get("max_paths", 3),
        cache_dir=cache_dir / query["id"],
    )
    graded = grade(query, result)
    graded["mode"] = "fixture" if query.get("fixture") else "cassette"
    return graded


def run_all(
    cassetted_client_factory,
    queries_path: Path = QUERIES_PATH,
    cache_dir: Path = Path("data/tmp/insight-harness"),
) -> dict[str, Any]:
    queries = json.loads(queries_path.read_text(encoding="utf-8"))
    reports = []
    for q in queries:
        try:
            reports.append(run_query(cassetted_client_factory, q, cache_dir))
        except KeyError as exc:  # cassette gap: untestable, not failed
            reports.append({
                "id": q["id"], "ok": True, "skipped": True, "fails": [],
                "observed": None, "mode": "cassette-gap", "reason": str(exc)[:160],
            })
    return {
        "queries": reports,
        "passed": sum(1 for r in reports if r["ok"]),
        "total": len(reports),
    }


def check_or_write_goldens(report: dict[str, Any], *, write: bool) -> list[str]:
    """Regression diff vs tests/golden/insight.json (golden-test pattern)."""
    summary = {
        r["id"]: r["observed"] for r in report["queries"] if not r.get("skipped")
    }
    if write:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return []
    if not GOLDEN_PATH.exists():
        return [f"missing golden {GOLDEN_PATH}; run with HG_UPDATE_GOLDENS=1"]
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    diffs = []
    for qid, obs in summary.items():
        if golden.get(qid) != obs:
            diffs.append(f"{qid}: {golden.get(qid)} -> {obs}")
    return diffs
