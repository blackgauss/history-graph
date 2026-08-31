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

from . import candidates
from .client import OpenAlexClient
from .download import TEXT_DIR, doi_to_filename, extract_pdf_texts, normalize_doi, run_download
from .http import dotenv_defaults
from .proposals import apply_proposals as _apply_proposals
from .proposals import list_proposals as _list_proposals
from .proposals import propose_event as _propose_event
from .proposals import propose_seed as _propose_seed
from .report import reconstruct_abstract
from .scihub import SciHubClient

MAX_CHARS = 12_000

# spawn-time env can be stale (opencode caches config); repo .env is authoritative
dotenv_defaults()

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


def _candidate_dir() -> Path:
    return Path(os.environ.get("HG_CANDIDATES_DIR", "data/candidates"))


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


def _cassette_mode() -> str:
    """Set HG_CASSETTES=1 to run all HTTP through tests/cassettes
    (HG_CASSETTE_MODE=record to extend them live instead of replaying)."""
    mode = os.environ.get("HG_CASSETTE_MODE")
    return mode or ("replay" if os.environ.get("HG_CASSETTES") else "")


def _openalex() -> OpenAlexClient:
    if "openalex" not in _clients:
        mode = _cassette_mode()
        if mode:
            from .testing import openalex_client

            _clients["openalex"] = openalex_client(record=mode == "record")
        else:
            _clients["openalex"] = OpenAlexClient(
                mailto=os.environ.get("OPENALEX_MAILTO", "history-graph@localhost"),
                api_key=os.environ.get("OPENALEX_API_KEY"),
            )
    return _clients["openalex"]


def _scihub() -> SciHubClient:
    if "scihub" not in _clients:
        mode = _cassette_mode()
        if mode:
            from .testing import scihub_client

            _clients["scihub"] = scihub_client(record=mode == "record")
        else:
            _clients["scihub"] = SciHubClient()
    return _clients["scihub"]


def _s2() -> Any:
    if "s2" not in _clients:
        mode = _cassette_mode()
        if mode:
            from .testing import s2_client

            _clients["s2"] = s2_client(record=mode == "record")
        else:
            from .s2 import SemanticScholarClient

            _clients["s2"] = SemanticScholarClient()
    return _clients["s2"]


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
        compact_source = {**record, "id": record.get("openalex_id")}
        compacted = _compact_work(compact_source)
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
        doi_to_filename(p.get("doi") or "").removesuffix(".pdf"): p.get("title")
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
                "title": titles.get(txt.stem),
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
    text_path = pdf_dir / TEXT_DIR / (doi_to_filename(key).removesuffix(".pdf") + ".txt")
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
def _curated_gate(tool: str) -> str | None:
    """Curated-input writes are human-gated: agents must propose, not edit.

    Returns a JSON refusal unless HG_ALLOW_CURATED_WRITES=1 in the server env
    (a human setting), so autonomous sessions cannot bless the seed or goldens."""
    if os.environ.get("HG_ALLOW_CURATED_WRITES") == "1":
        return None
    return _cap({
        "applied": False,
        "reason": "curated writes are human-gated",
        "tool": tool,
        "hint": "propose is enough; a human re-runs this with HG_ALLOW_CURATED_WRITES=1",
    })


@tool
def apply_proposals(repro: bool = False) -> str:
    """Approve all proposals: append entries/seeds to curated inputs.

    With repro=True also runs `uv run dvc repro` afterwards (slow, network)."""
    if gated := _curated_gate("apply_proposals"):
        return gated
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


# --------------------------------------------------------------------- threads


@tool
def propose_thread(slug: str, claim: str, seed_dois: list[str]) -> str:
    """Start a candidate thread (hypothesis) from seed DOIs; seeds become entries.

    Candidate threads live in data/candidates/ and never touch curated data."""
    return _cap(
        candidates.propose_thread(
            _openalex(), candidates.slugify(slug), claim, seed_dois,
            candidate_dir=_candidate_dir(),
        )
    )


@tool
def list_candidate_threads() -> str:
    """Names + claims of candidate threads on disk."""
    return _cap(candidates.list_threads(_candidate_dir()))


@tool
def test_thread(slug: str) -> str:
    """Deterministic scorecard: resolution, dangling links, chronology, gaps,

    per-edge support (cites-earlier / co-cited / needs-text / unresolved, with
    a bridge flag for links only full text can settle), verdict, issues."""
    return _cap(candidates.score_thread(_openalex(), slug, candidate_dir=_candidate_dir()))


@tool
def grow_thread(slug: str, limit: int = 8) -> str:
    """Rank suggestions for the next entry: shared cited ancestors, then citing works.

    Feed chosen ones back via add_thread_entries, then test_thread again."""
    return _cap(candidates.frontier(_openalex(), slug, limit=limit, candidate_dir=_candidate_dir()))


@tool
def add_thread_entries(slug: str, entries: list[dict]) -> str:
    """Append schema-validated entries to a candidate thread (dedup by id)."""
    return _cap(candidates.add_entries(slug, entries, candidate_dir=_candidate_dir()))


@tool
def promote_thread(slug: str, repro: bool = False) -> str:
    """Approve a candidate thread: append its entries to the curated thread YAML.

    With repro=True also runs `uv run dvc repro` afterwards (slow, network)."""
    if gated := _curated_gate("promote_thread"):
        return gated
    summary = candidates.promote_thread(
        slug, candidate_dir=_candidate_dir(), thread_yaml=_thread_yaml()
    )
    if repro:
        import subprocess

        done = subprocess.run(
            ["uv", "run", "dvc", "repro"], capture_output=True, text=True, timeout=1800
        )
        combined = done.stdout + done.stderr
        summary["repro"] = {"returncode": done.returncode, "tail": combined[-800:]}
    return _cap(summary)


def _patents() -> Any:
    if "patents" not in _clients:
        mode = _cassette_mode()
        if mode:
            from .testing import patent_client

            _clients["patents"] = patent_client(record=mode == "record")
        else:
            from .uspto import PatentSearchClient

            _clients["patents"] = PatentSearchClient()
    return _clients["patents"]


# ----------------------------------------------------------------------- insight


@tool
def citation_path(
    from_work: str, to_work: str, max_hops: int = 5, max_paths: int = 3
) -> str:
    """Short citation paths between two works (DOI, OpenAlex ID, or title).

    Bidirectional BFS over references and citations; ranks paths by concept
    gap, flags nodes present in every path (bridge nodes). Cost-bounded."""
    from .paths import citation_path as find_paths

    cache = Path(os.environ.get("HG_GRAPH_CACHE", "data/cache")) / "graph_cache.json"
    result = find_paths(
        _openalex(),
        from_work,
        to_work,
        max_nodes=max_hops * 40,
        max_paths=max_paths,
        cache_dir=cache.parent,
        cache_name=cache.name,
    )
    return _cap(result)


@tool
def search_patents(
    query: str,
    after_year: int | None = None,
    before_year: int | None = None,
    assignee: str | None = None,
    inventor: str | None = None,
    limit: int = 15,
) -> str:
    """Find patents (Google Patents full text, keyless).

    Priority-date window helps for pre-1960 prior art; use with
    propose_event(kind="patent", patent_no=...)."""
    from .uspto import PatentError

    try:
        found = _patents().search(
            query,
            after=after_year,
            before=before_year,
            assignee=assignee,
            inventor=inventor,
            limit=limit,
        )
    except PatentError as exc:
        return _cap({"error": str(exc)[:160]})
    return _cap(found)


@tool
def patent_links(publication_number: str, page: int = 0) -> str:
    """Citations around a patent: citing patents, plus (best-effort) academic

    works citing it — the patent<->paper bridge OpenAlex usually lacks.
    When Google walls us, falls back to OpenAlex's patent record (indexed
    coverage is spotty for pre-1976 patents); failures are labeled, not faked."""
    from .uspto import PatentError

    out: dict[str, Any] = {"publication_number": publication_number}
    walled = False
    try:
        out["cited_by_patents"] = _patents().citing(publication_number, page=page)
    except PatentError as exc:
        out["cited_by_patents"] = None
        out["google_note"] = str(exc)[:120]
        walled = True
    try:
        out["cited_by_papers"] = _patents().scholar(publication_number, "forward")["works"]
    except PatentError as exc:
        out["cited_by_papers"] = None
        out["scholar_note"] = str(exc)[:120]
        walled = True
    if walled:
        fallback = _openalex_patent_citations(publication_number)
        if fallback is not None:
            out["fallback"] = fallback
        else:
            out["fallback"] = {"reason": "not indexed on OpenAlex either; retry Google later"}
    return _cap(out)


def _openalex_patent_citations(publication_number: str) -> dict[str, Any] | None:
    bare = publication_number[:-1] if publication_number[-1:] in {"A", "B"} else publication_number
    for num in dict.fromkeys((bare, publication_number)):
        doi = f"https://patents.google.com/patent/{num}"
        work = next(iter(_openalex().paginate(
            "/works", {"filter": f"type:patent,doi:{doi}"},
            select=["id", "doi", "title", "publication_year"], per_page=5,
        )), None)
        if work is None:
            continue
        citing = [
            {"openalex_id": w["id"], "title": w.get("title"), "year": w.get("publication_year")}
            for w in _openalex().iter_citing_works(work["id"], max_pages=1)
        ][:20]
        return {
            "source": "openalex",
            "work": {k: work.get(k) for k in ("id", "doi", "title", "publication_year")},
            "cited_by_papers": citing,
        }
    return None


@tool
def patent_prior_art(publication_number: str) -> str:
    """Academic works the patent itself cites (examiner (backward scholar))."""
    from .uspto import PatentError

    try:
        return _cap(_patents().scholar(publication_number, "backward"))
    except PatentError as exc:
        return _cap({"works": None, "note": str(exc)[:160]})


@tool
def lineage_dossier(work: str, depth: int = 2, limit: int = 8) -> str:
    """A paper's biography + horizon in one call: ancestors (bridge-ranked),

    downstream works by citation acceleration, the research schools it fuses,
    and a 'story' markdown rendering. work = DOI, OpenAlex ID, or title."""
    from .dossier import lineage_dossier as build

    cache = Path(os.environ.get("HG_GRAPH_CACHE", "data/cache"))
    return _cap(build(_openalex(), work, depth=depth, limit=limit, cache_dir=cache))


@tool
def citation_context(citing_work: str, cited_work: str) -> str:
    """How one work cites another (S2): snippet + intent (builds-upon/method/

    comparison/background) — turns a raw citation edge into an argument.
    Snippets exist for only part of the corpus; absence is stated, not guessed."""
    from .s2 import SemanticScholarError

    try:
        found = _s2().citation_context(citing_work, cited_work)
    except SemanticScholarError as exc:
        return _cap({"error": str(exc)[:160]})
    return _cap(found or {"error": "citing work unknown to Semantic Scholar"})


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
