"""MCP server exposing the lineage graph for autonomous curation agents.

Run (stdio transport, for mounting into an agent):

    uv run python -m history_graph.mcp_server

Tools are grouped: inspect the curated graph (thread_status, list_events,
get_paper, *_edges), explore OpenAlex (resolve_doi, find_by_title,
references_of, citing_works), gather evidence (search_fulltext, fetch_pdf),
and propose additions (propose_seed, propose_event -> human-gated
apply_proposals).  Everything read-only is capped at ~12k chars per call;
nothing outside data/proposed/ is ever written by a proposal tool.

Data locations honor HG_THREAD_DIR / HG_PDFS_DIR / HG_PROPOSED_DIR /
HG_THREAD_YAML / HG_SEED_TXT, defaulting to the repo layout.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import OpenAlexClient
from .download import TEXT_DIR, doi_to_filename, extract_pdf_texts, normalize_doi, run_download
from .proposals import apply_proposals as _apply_proposals
from .proposals import list_proposals as _list_proposals
from .proposals import propose_event as _propose_event
from .proposals import propose_seed as _propose_seed
from .report import reconstruct_abstract
from .scihub import SciHubClient

MAX_CHARS = 12_000

TOOLS: list[Any] = []


def tool(fn: Any) -> Any:
    TOOLS.append(fn)
    return fn


def _thread_dir() -> Path:
    return Path(os.environ.get("HG_THREAD_DIR", "data/thread"))


def _pdfs_dir() -> Path:
    return Path(os.environ.get("HG_PDFS_DIR", "data/pdfs"))


def _proposed_dir() -> Path:
    return Path(os.environ.get("HG_PROPOSED_DIR", "data/proposed"))


def _thread_yaml() -> Path:
    return Path(os.environ.get("HG_THREAD_YAML", "data/seed/computing_thread.yaml"))


def _seed_txt() -> Path:
    return Path(os.environ.get("HG_SEED_TXT", "data/seed/dois.txt"))


def _cap(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=None, default=str)
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + "... [truncated; narrow the query]"
    return text


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


_clients: dict[str, Any] = {}


def _openalex() -> OpenAlexClient:
    if "openalex" not in _clients:
        _clients["openalex"] = OpenAlexClient(
            mailto=os.environ.get("OPENALEX_MAILTO", "history-graph@localhost")
        )
    return _clients["openalex"]


def _scihub() -> SciHubClient:
    if "scihub" not in _clients:
        _clients["scihub"] = SciHubClient()
    return _clients["scihub"]


def _compact_work(work: dict[str, Any]) -> dict[str, Any]:
    authors = [
        (a.get("author") or {}).get("display_name") for a in work.get("authorships") or []
    ]
    source = ((work.get("primary_location") or {}).get("source") or {})
    return {
        "openalex_id": work.get("id"),
        "doi": work.get("doi"),
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "type": work.get("type"),
        "cited_by": work.get("cited_by_count"),
        "authors": authors[:12],
        "author_count": len(authors),
        "venue": source.get("display_name"),
        "referenced_works": work.get("referenced_works"),
    }


# ---------------------------------------------------------------- inspect graph


@tool
def thread_status() -> str:
    """Manifests of the thread/pdf stages plus unresolved papers and gaps."""
    thread_dir = _thread_dir()
    out: dict[str, Any] = {}
    for name, path in (
        ("thread", thread_dir / "manifest.json"),
        ("pdfs", _pdfs_dir() / "manifest.json"),
    ):
        out[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    out["unresolved_papers"] = (out.get("thread") or {}).get("unresolved_ids", [])
    out["events"] = len(_read_jsonl(thread_dir / "events.jsonl"))
    out["papers"] = len(_read_jsonl(thread_dir / "papers.jsonl"))
    return _cap(out)


@tool
def list_events(
    contains: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 40,
) -> str:
    """Filter timeline events by substring/year range (ordered by date)."""
    rows = _read_jsonl(_thread_dir() / "events.jsonl")
    out = []
    for row in sorted(rows, key=lambda r: str(r.get("date", ""))):
        year = int(str(row.get("date", "0"))[:4])
        if year_from is not None and year < year_from:
            continue
        if year_to is not None and year > year_to:
            continue
        if contains and contains.lower() not in json.dumps(row, ensure_ascii=False).lower():
            continue
        out.append({k: row.get(k) for k in ("id", "date", "kind", "title", "who", "related")})
        if len(out) >= max(limit, 1):
            break
    return _cap({"count": sum(1 for _ in rows), "matched": len(out), "events": out})


@tool
def get_paper(entry_or_doi: str) -> str:
    """Everything the graph knows about one paper (entry id or DOI)."""
    needle = normalize_doi(entry_or_doi)
    papers = _read_jsonl(_thread_dir() / "papers.jsonl")
    for paper in papers:
        doi = normalize_doi(paper.get("doi") or "")
        if paper.get("id") != needle and doi != needle:
            continue
        entry_id = paper["id"]
        tables = {
            "impact": ("paper_impact.csv", "entry_id"),
            "funders": ("paper_funders.csv", "paper_id"),
            "institutions": ("paper_institutions.csv", "paper_id"),
            "venue": ("paper_venue.csv", "paper_id"),
            "fulltext": ("paper_fulltext.csv", "paper_id"),
        }
        joined = {
            name: [r for r in _read_csv(_thread_dir() / fn) if r.get(key) == entry_id]
            for name, (fn, key) in tables.items()
        }
        record = dict(paper)
        abstract = reconstruct_abstract(record.pop("abstract_inverted_index", None))
        compacted = _compact_work({"id": record.get("openalex_id"), **record})
        edges = [
            e
            for e in _read_csv(_thread_dir() / "internal_citations.csv")
            if entry_id in (e.get("citing_entry"), e.get("cited_entry"))
        ]
        return _cap(
            {
                **compacted,
                "entry_id": entry_id,
                "abstract": abstract,
                "resolved_via": record.get("resolved_via"),
                **joined,
                "internal_citations": edges,
            }
        )
    return _cap({"error": f"unknown paper {entry_or_doi!r}; try find_by_title"})


@tool
def list_citation_edges() -> str:
    """Citation edges between the thread's own papers."""
    return _cap(_read_csv(_thread_dir() / "internal_citations.csv"))


@tool
def list_curated_edges() -> str:
    """Curated `related` cross-links (conceptual/industrial lineage)."""
    return _cap(_read_csv(_thread_dir() / "curated_edges.csv"))


# ------------------------------------------------------------------- exploration


@tool
def resolve_doi(doi: str) -> str:
    """Look up a DOI on OpenAlex (compact work record)."""
    work = _openalex().get_work_by_doi(doi)
    return _cap(_compact_work(work) if work else {"found": False})


@tool
def find_by_title(title: str) -> str:
    """Best-effort OpenAlex lookup by title (for works without DOIs)."""
    work = _openalex().get_work_by_title(title)
    return _cap(_compact_work(work) if work else {"found": False})


@tool
def references_of(openalex_id: str, limit: int = 60) -> str:
    """Outbound references of a work, with cited records resolved in bulk."""
    work = next(iter(_openalex().get_works([openalex_id])), None)
    if work is None:
        return _cap({"error": f"no OpenAlex work {openalex_id}"})
    refs = (work.get("referenced_works") or [])[: max(limit, 1)]
    resolved = [_compact_work(r) for r in _openalex().get_works(refs)]
    return _cap(
        {
            "citing": _compact_work(work),
            "reference_count": len(work.get("referenced_works") or []),
            "references": resolved,
        }
    )


@tool
def citing_works(openalex_id: str, max_pages: int = 2) -> str:
    """Inbound citations (capped pages of 200)."""
    works = list(
        _openalex().iter_citing_works(openalex_id, max_pages=min(max_pages, 5))
    )
    return _cap({"count_returned": len(works), "works": [_compact_work(w) for w in works]})


# ---------------------------------------------------------------------- evidence


@tool
def search_fulltext(query: str, context_chars: int = 200, limit: int = 5) -> str:
    """Quote matches for `query` in extracted PDF full texts of the thread."""
    text_dir = _pdfs_dir() / TEXT_DIR
    titles = {
        doi_to_filename(p.get("doi") or ""): p.get("title")
        for p in _read_jsonl(_thread_dir() / "papers.jsonl")
    }
    corpus = sorted(text_dir.glob("*.txt")) if text_dir.exists() else []
    hits: list[dict[str, Any]] = []
    lowered = query.lower()
    for txt in corpus:
        body = txt.read_text(encoding="utf-8", errors="replace")
        pos = body.lower().find(lowered)
        if pos < 0:
            continue
        snippet = body[max(0, pos - context_chars // 2): pos + len(query) + context_chars // 2]
        hits.append(
            {
                "file": txt.name,
                "title": titles.get(txt.name),
                "excerpt": " ".join(snippet.split()),
            }
        )
        if len(hits) >= max(limit, 1):
            break
    return _cap({"query": query, "corpus_files": len(corpus), "hits": hits})


@tool
def fetch_pdf(doi: str) -> str:
    """Download a paper PDF via sci-hub now (each request waits >= 15s throttle).

    May return `failed` when mirrors captcha-wall or lack the paper; retrying
    later generally succeeds. Full text is extracted for search_fulltext."""
    pdf_dir = _pdfs_dir()
    tmp_seeds = pdf_dir / ".mcp-fetch-dois.txt"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    tmp_seeds.write_text(normalize_doi(doi) + "\n", encoding="utf-8")
    try:
        manifest = run_download(_scihub(), [tmp_seeds], pdf_dir)
        texts = extract_pdf_texts(pdf_dir)
    finally:
        tmp_seeds.unlink(missing_ok=True)
    key = normalize_doi(doi)
    text_path = pdf_dir / TEXT_DIR / (doi_to_filename(key) + ".txt")
    return _cap(
        {
            "status": "downloaded" if key in manifest["files"] else "failed",
            "failure": manifest["failures"].get(key),
            "texts_extracted_now": texts["extracted"],
            "text_extracted": text_path.exists(),
        }
    )


# -------------------------------------------------------------------- proposals


@tool
def propose_seed(doi: str, reason: str) -> str:
    """Quarantine a DOI the agent wants added as an ingest seed (needs approval)."""
    return _cap(_propose_seed(doi, reason, proposed_dir=_proposed_dir()))


@tool
def propose_event(entry: dict, evidence: str) -> str:
    """Quarantine a proposed timeline entry (schema-validated; requires evidence text).

    `entry` mirrors computing_thread.yaml: id/date/kind/title and optional
    who/refs{doi|title_search|patent_number}/related/notes."""
    if len(evidence.strip()) < 3:
        return _cap({"status": "rejected", "error": "evidence text is required"})
    return _cap(_propose_event(entry, evidence, proposed_dir=_proposed_dir()))


@tool
def list_proposals() -> str:
    """Show quarantined proposals awaiting approval."""
    return _cap(_list_proposals(proposed_dir=_proposed_dir()))


@tool
def apply_proposals(repro: bool = False) -> str:
    """Approve all proposals: append entries/seeds to curated inputs.

    With repro=True also runs `uv run dvc repro` afterwards (slow, network)."""
    summary = _apply_proposals(
        proposed_dir=_proposed_dir(), thread_yaml=_thread_yaml(), seed_txt=_seed_txt()
    )
    if repro:
        import subprocess

        done = subprocess.run(
            ["uv", "run", "dvc", "repro"], capture_output=True, text=True, timeout=1800
        )
        summary["repro"] = {
            "returncode": done.returncode,
            "tail": (done.stdout + done.stderr)[-800:],
        }
    return _cap(summary)


def build_server() -> MCPServer:
    server = MCPServer(
        name="history-graph",
        instructions=(
            "Academic lineage graph: curated computing thread (Timeline events, "
            "papers, citations), OpenAlex exploration, sci-hub full-text evidence, "
            "and quarantined proposals. Use list_events/references_of to find "
            "broken or missing lineage links, search_fulltext/fetch_pdf to ground "
            "claims in quotes, then propose_seed/propose_event. Never edit curated "
            "data directly; a human approves via apply_proposals."
        ),
    )
    for fn in TOOLS:
        server.tool()(fn)
    return server


def main() -> int:
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
