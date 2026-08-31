"""Candidate threads: agent-proposed, testable lineage hypotheses.

A candidate thread lives in ``data/candidates/<slug>.yaml`` (gitignored
working area) and can be:

- proposed from seed DOIs (seeds auto-resolve into initial paper entries),
- tested (deterministic scorecard: resolution, dangling links, chronology,
  citation support, evidence coverage),
- grown with frontier suggestions (works citing the thread's papers, and
  shared ancestors cited by two of its papers),
- promoted into the curated thread YAML once it passes, human-triggered.

All functions take an ``OpenAlexClient`` so tests/scripts can inject replay
clients, matching the rest of the package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .client import OpenAlexClient
from .models import ThreadEntry, parse_thread
from .proposals import thread_ids, yaml_block

DEFAULT_CANDIDATE_DIR = Path("data/candidates")
DEFAULT_CURATED_YAML = Path("data/seed/computing_thread.yaml")
_MAX_GAP_YEARS = 60  # bridge spanning more than this needs real evidence


def _suggest(work: dict[str, Any], reason: str, openalex_id: str | None = None) -> dict[str, Any]:
    return {
        "reason": reason,
        "openalex_id": openalex_id or work["id"],
        "doi": (work.get("doi") or "").removeprefix("https://doi.org/"),
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "cited_by": work.get("cited_by_count"),
    }


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "thread"


def thread_path(slug: str, directory: Path = DEFAULT_CANDIDATE_DIR) -> Path:
    return directory / f"{slug}.yaml"


def _candidate_yaml(slug: str, claim: str, entries: list[dict[str, Any]]) -> str:
    header = f"# candidate thread: {slug}\n# claim: {claim}\nentries:\n"
    return header + "".join(yaml_block(e, "") + "\n" for e in entries)


def load_thread(slug: str, directory: Path = DEFAULT_CANDIDATE_DIR) -> dict[str, Any]:
    path = thread_path(slug, directory)
    if not path.exists():
        raise FileNotFoundError(f"no candidate thread {slug!r} at {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = parse_thread({"entries": doc.get("entries", [])})
    claim = doc.get("claim", "")
    if isinstance(claim, str) and claim.startswith("# candidate"):
        claim = ""
    return {"slug": slug, "claim": claim, "entries": entries}


def resolve_entries(
    client: OpenAlexClient,
    entries: list[ThreadEntry],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Resolve thread entries to OpenAlex works; returns ({id: work}, unresolved)."""
    resolved: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for entry in entries:
        work = None
        if entry.refs.doi:
            work = client.get_work_by_doi(entry.refs.doi)
        if work is None and entry.refs.title_search:
            work = client.get_work_by_title(entry.refs.title_search)
        if work is None:
            unresolved.append(entry.id)
        else:
            resolved[entry.id] = work
    return resolved, unresolved


def propose_thread(
    client: OpenAlexClient,
    slug: str,
    claim: str,
    seed_dois: list[str],
    *,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
) -> dict[str, Any]:
    """Bootstrap a candidate thread from seed DOIs; seeds become paper entries."""
    entries: list[dict[str, Any]] = []
    for doi in seed_dois:
        doi = doi.strip().lower().removeprefix("https://doi.org/")
        work = client.get_work_by_doi(doi)
        if work is None:
            continue
        author = ((work.get("authorships") or [{}])[0].get("author") or {}).get("display_name")
        entry = {
            "id": slugify(
                f"{(author or 'anon').split()[-1]}-{work.get('title', '')[:24]}"
                f"-{work.get('publication_year')}"
            ),
            "date": str(work.get("publication_year") or "0000"),
            "kind": "paper",
            "title": work.get("title"),
            "who": author,
            "refs": {"doi": doi},
        }
        entries.append(entry)
    parse_thread({"entries": entries})  # validate before writing
    path = thread_path(slug, candidate_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_candidate_yaml(slug, claim, entries), encoding="utf-8")
    return {"slug": slug, "entries": [e["id"] for e in entries], "path": str(path)}


def add_entries(
    slug: str,
    entries: list[dict[str, Any]],
    *,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
) -> dict[str, Any]:
    """Append validated entries to a candidate thread (dedup by id)."""
    thread = load_thread(slug, candidate_dir)
    existing = {e.id for e in thread["entries"]}
    additions = [e for e in entries if e.get("id") not in existing]
    parse_thread({"entries": additions})
    merged = [e.model_dump(exclude_none=True, exclude_defaults=True) for e in thread["entries"]]
    merged.extend(additions)
    (candidate_dir / f"{slug}.yaml").write_text(
        _candidate_yaml(slug, thread["claim"], merged), encoding="utf-8"
    )
    added_ids = [e["id"] for e in additions]
    return {"slug": slug, "added": added_ids, "skipped": len(entries) - len(added_ids)}


def list_threads(directory: Path = DEFAULT_CANDIDATE_DIR) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.yaml")) if directory.exists() else []


def score_thread(
    client: OpenAlexClient,
    slug: str,
    *,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
) -> dict[str, Any]:
    """Deterministic test battery for a candidate thread; never mutates anything."""
    doc = yaml.safe_load(thread_path(slug, candidate_dir).read_text(encoding="utf-8"))
    entries = parse_thread({"entries": doc.get("entries", [])})
    resolved, unresolved = resolve_entries(client, entries)
    by_year = sorted(entries, key=lambda e: e.year)

    issues: list[str] = [f"unresolved: {i}" for i in unresolved]
    ids = {e.id for e in entries}
    issues += [
        f"dangling related: {e.id}->{r}" for e in entries for r in e.related if r not in ids
    ]
    issues += [
        f"date gap {b.year - a.year}y between {a.id} and {b.id}"
        for a, b in zip(by_year, by_year[1:], strict=False)
        if b.year - a.year > _MAX_GAP_YEARS
    ]

    def pair_support(a: ThreadEntry, b: ThreadEntry) -> str:
        """How does OpenAlex metadata back (or fail to back) the a -> b link?"""
        wa, wb = resolved.get(a.id), resolved.get(b.id)
        if wa is None or wb is None:
            return "unresolved"
        if wa.get("id") in (wb.get("referenced_works") or []):
            return "cites-backwards"  # later work cites the earlier one
        if shared_refs(wa, wb):
            return "co-cited"  # common ancestors cited by both
        return "metadata-blind"  # graph cannot see the link (or the link is wrong)

    declared = [(a, b) for a in entries for b in entries if b.id in a.related]
    edges = [{"from": a.id, "to": b.id, "kind": pair_support(a, b)} for a, b in declared]
    issues += [
        f"backwards chronology: {a.id} ({a.year}) -> {b.id} ({b.year})"
        for a, b in declared
        if b.year < a.year
    ]
    supported = sum(e["kind"] in {"cites-backwards", "co-cited"} for e in edges)
    metadata_blind = sum(e["kind"] == "metadata-blind" for e in edges)
    co_cited = sum(e["kind"] == "co-cited" for e in edges)
    evidence = sum(
        1 for e in entries
        if (e.notes and "evidence:" in e.notes) or e.refs.doi or e.refs.title_search
    )
    return {
        "slug": slug,
        "entries": len(entries),
        "resolved": len(resolved),
        "unresolved": unresolved,
        "edges": edges,
        "supported": supported,
        "co_cited": co_cited,
        "metadata_blind": metadata_blind,
        "evidence_coverage": round(evidence / max(len(entries), 1), 2),
        "date_span": by_year[-1].year - by_year[0].year if entries else 0,
        "verdict": "sound" if not issues else "gaps",
        "issues": issues,
    }


def shared_refs(wa: dict[str, Any], wb: dict[str, Any]) -> set[str]:
    return set(wa.get("referenced_works") or []) & set(wb.get("referenced_works") or [])


def frontier(
    client: OpenAlexClient,
    slug: str,
    *,
    limit: int = 8,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
) -> list[dict[str, Any]]:
    """Suggest where to grow next: shared ancestors (best), then inbound cites."""
    doc = yaml.safe_load(thread_path(slug, candidate_dir).read_text(encoding="utf-8"))
    entries = parse_thread({"entries": doc.get("entries", [])})
    resolved, _ = resolve_entries(client, entries)
    mine = {w["id"] for w in resolved.values()}

    suggestions: list[dict[str, Any]] = []
    works: dict[str, str] = {}  # openalex id -> first entry id citing it (dedup)
    for entry_id, work in sorted(resolved.items()):
        works.setdefault(work["id"], entry_id)
    wid_list = sorted(works)
    for i, wid_a in enumerate(wid_list):
        for wid_b in wid_list[i + 1:]:
            refs_a = set(resolved[works[wid_a]].get("referenced_works") or [])
            refs_b = set(resolved[works[wid_b]].get("referenced_works") or [])
            for ref in sorted((refs_a & refs_b) - mine)[:2]:
                parent = next(iter(client.get_works([ref])), None)
                if not parent:
                    continue
                suggestions.append(
                    _suggest(parent, f"cited by both {works[wid_a]} and {works[wid_b]}", ref)
                )
    for wid in wid_list:
        work = resolved[works[wid]]
        for citing in client.iter_citing_works(wid, max_pages=1):
            if citing["id"] not in mine:
                suggestions.append(
                    _suggest(citing, f"cites {slugify(str(work.get('title'))[:20])}")
                )
    deduped = {s["openalex_id"]: s for s in suggestions}
    ranked = sorted(
        deduped.values(),
        key=lambda s: (-("cited by" in s["reason"]), -(s.get("cited_by") or 0), str(s.get("year"))),
    )
    return ranked[: max(limit, 1)]


def promote_thread(
    slug: str,
    *,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    thread_yaml: Path = DEFAULT_CURATED_YAML,
) -> dict[str, Any]:
    """Append a candidate thread's entries to the curated thread (comment-preserving)."""
    thread = load_thread(slug, candidate_dir)
    existing = thread_ids(thread_yaml)
    blocks, added = [], []
    for entry in thread["entries"]:
        if entry.id in existing:
            continue
        blocks.append(yaml_block(entry.model_dump(exclude_none=True, exclude_defaults=True), ""))
        added.append(entry.id)
    if blocks:
        with thread_yaml.open("a", encoding="utf-8") as handle:
            handle.write("".join("\n" + block for block in blocks))
    return {
        "slug": slug,
        "promoted": added,
        "skipped_existing": len(thread["entries"]) - len(added),
    }
