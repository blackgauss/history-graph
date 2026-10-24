"""MCP server exposing the lineage graph for autonomous curation agents.

Run (stdio transport, for mounting into an agent):

    uv run python -m history_graph.mcp_server

Tools are grouped: inspect the curated graph (thread_status, list_events,
get_paper, *_edges), explore/cross-check OpenAlex and OpenCitations, inspect
papers and gather page-located evidence, and propose additions (propose_seed,
propose_event -> human-gated apply_proposals). Everything read-only is capped
at ~12k chars per call; nothing outside data/proposed/ is ever written by a
proposal tool.

Data locations honor HG_THREAD_DIR / HG_PDFS_DIR / HG_PROPOSED_DIR /
HG_THREAD_YAML / HG_SEED_TXT, defaulting to the repo layout.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import candidates
from .client import WORK_FIELDS, OpenAlexClient, OpenAlexError
from .download import TEXT_DIR, doi_to_filename, extract_pdf_texts, normalize_doi, run_download
from .open_access import OpenAccessError, OpenAccessPdfClient, pdf_locations
from .opencitations import OpenCitationsClient, OpenCitationsError, compact_edge, identifiers
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


def _open_access_pdfs_dir() -> Path:
    return Path(os.environ.get("HG_OPEN_ACCESS_PDFS_DIR", str(_pdfs_dir() / "open_access")))


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
        return json.dumps(
            {
                "truncated": True,
                "reason": "response exceeded the tool output limit; narrow the query",
                "original_chars": len(text),
                "preview": text[: MAX_CHARS // 3],
            },
            ensure_ascii=False,
        )
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
                mailto=os.environ.get("OPENALEX_MAILTO", "history-graph@localhost")
            )
    return _clients["openalex"]


def _opencitations() -> OpenCitationsClient:
    if "opencitations" not in _clients:
        _clients["opencitations"] = OpenCitationsClient(
            token=os.environ.get("OPENCITATIONS_TOKEN") or None
        )
    return _clients["opencitations"]


def _open_access_pdf_client() -> OpenAccessPdfClient:
    if "open_access_pdf" not in _clients:
        _clients["open_access_pdf"] = OpenAccessPdfClient()
    return _clients["open_access_pdf"]


def _ensure_open_access_pdf(doi: str, work: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_doi(doi)
    pdf_dir = _open_access_pdfs_dir()
    pdf_path = pdf_dir / doi_to_filename(normalized)
    text_path = pdf_dir / TEXT_DIR / f"{pdf_path.stem}.txt"
    metadata_path = pdf_path.with_suffix(".json")

    if pdf_path.exists():
        metadata: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {}
        extraction = extract_pdf_texts(pdf_dir)
        return {
            "status": "cached",
            "doi": normalized,
            "pdf_path": str(pdf_path),
            "text_path": str(text_path) if text_path.exists() else None,
            "text_extracted": text_path.exists(),
            "extraction": extraction,
            "source": metadata or {"kind": "local_cache", "origin": "unknown"},
        }

    candidates = pdf_locations(work)
    if not candidates:
        return {
            "status": "unavailable",
            "doi": normalized,
            "reason": "OpenAlex has no listed open-access PDF URL for this work",
            "landing_pages": [
                location.get("landing_page_url")
                for location in work.get("locations", [])
                if isinstance(location, dict) and location.get("landing_page_url")
            ][:10],
        }

    failures: list[dict[str, str]] = []
    for source in candidates:
        try:
            content, _final_url = _open_access_pdf_client().fetch(source["pdf_url"])
        except OpenAccessError as exc:
            failures.append({"url": source["pdf_url"], "reason": str(exc)[:160]})
            continue

        pdf_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = pdf_path.with_suffix(".pdf.partial")
        temporary_path.write_bytes(content)
        temporary_path.replace(pdf_path)
        metadata = {
            "doi": normalized,
            "title": work.get("title"),
            "year": work.get("publication_year"),
            "access": "open_access",
            "source": "OpenAlex-listed OA location",
            "pdf_url": source["pdf_url"],
            "landing_page_url": source.get("landing_page_url"),
            "license": source.get("license"),
            "repository_or_publisher": source.get("source"),
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        extraction = extract_pdf_texts(pdf_dir)
        return {
            "status": "downloaded",
            "doi": normalized,
            "pdf_path": str(pdf_path),
            "text_path": str(text_path) if text_path.exists() else None,
            "text_extracted": text_path.exists(),
            "extraction": extraction,
            "source": metadata,
        }

    return {
        "status": "unavailable",
        "doi": normalized,
        "reason": "none of the listed open-access PDF URLs returned a valid PDF",
        "failures": failures,
        "landing_pages": [source.get("landing_page_url") for source in candidates],
    }


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


def _compact_work(
    work: dict[str, Any], *, include_reference_ids: bool = True
) -> dict[str, Any]:
    authors = [
        (a.get("author") or {}).get("display_name") for a in work.get("authorships") or []
    ]
    source = ((work.get("primary_location") or {}).get("source") or {})
    compact = {
        "openalex_id": work.get("id"),
        "doi": work.get("doi"),
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "type": work.get("type"),
        "cited_by": work.get("cited_by_count"),
        "authors": authors[:12],
        "author_count": len(authors),
        "venue": source.get("display_name"),
    }
    if include_reference_ids:
        compact["referenced_works"] = work.get("referenced_works")
    return compact


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
    """References of a paper; reports unresolved IDs and any output truncation."""
    if not 1 <= limit <= 200:
        return _cap({"status": "invalid_request", "error": "limit must be between 1 and 200"})
    work = next(iter(_openalex().get_works([openalex_id])), None)
    if work is None:
        return _cap({"status": "not_found", "openalex_id": openalex_id})
    all_refs = work.get("referenced_works")
    if not isinstance(all_refs, list):
        return _cap(
            {
                "status": "unavailable",
                "openalex_id": openalex_id,
                "error": "OpenAlex did not provide referenced_works for this record",
            }
        )
    refs = all_refs[:limit]
    resolved_by_id = {r.get("id"): r for r in _openalex().get_works(refs)}
    resolved = [
        _compact_work(resolved_by_id[ref], include_reference_ids=False)
        for ref in refs
        if ref in resolved_by_id
    ]
    unresolved = [ref for ref in refs if ref not in resolved_by_id]
    complete = len(refs) == len(all_refs) and not unresolved
    return _cap(
        {
            "status": "complete" if complete else "partial",
            "source": "OpenAlex",
            "direction": "references",
            "citing": _compact_work(work, include_reference_ids=False),
            "reference_count": len(all_refs),
            "returned_count": len(resolved),
            "unresolved_reference_ids": unresolved,
            "truncated": len(refs) < len(all_refs),
            "complete": complete,
            "references": resolved,
        }
    )


@tool
def citing_works(openalex_id: str, cursor: str = "*", per_page: int = 50) -> str:
    """One page of works citing this paper; resume with the returned next_cursor."""
    if not 1 <= per_page <= 200:
        return _cap(
            {"status": "invalid_request", "error": "per_page must be between 1 and 200"}
        )
    page = next(
        _openalex().iter_pages(
            "/works",
            {"filter": f"cites:{openalex_id}"},
            per_page=per_page,
            select=WORK_FIELDS,
            max_pages=1,
            cursor=cursor,
        ),
        None,
    )
    if page is None:
        return _cap({"status": "unavailable", "error": "OpenAlex returned no page"})
    meta = page["meta"]
    next_cursor = page["next_cursor"]
    works = page["results"]
    return _cap(
        {
            "status": "complete" if next_cursor is None else "partial",
            "source": "OpenAlex",
            "direction": "cited_by",
            "openalex_id": openalex_id,
            "total_available": meta.get("count"),
            "returned_count": len(works),
            "complete": next_cursor is None,
            "next_cursor": next_cursor,
            "works": [
                _compact_work(work, include_reference_ids=False) for work in works
            ],
        }
    )


def _opencitations_result(
    records: list[dict[str, Any]], *, doi: str, direction: str, limit: int,
    creation_after: str | None, creation_before: str | None,
) -> str:
    shown = records[:limit]
    complete = len(shown) == len(records)
    return _cap(
        {
            "status": "complete" if complete else "partial",
            "source": "OpenCitations Index v2",
            "direction": direction,
            "doi": doi,
            "coverage_scope": (
                "OpenCitations indexed DOI citations; not a universal citation census"
            ),
            "total_available": len(records),
            "returned_count": len(shown),
            "complete": complete,
            "creation_after": creation_after,
            "creation_before": creation_before,
            "edges": [compact_edge(edge) for edge in shown],
        }
    )


@tool
def opencitations_references(
    doi: str,
    limit: int = 100,
    creation_after: str | None = None,
    creation_before: str | None = None,
) -> str:
    """Get outgoing DOI citation edges from OpenCitations, separately from OpenAlex."""
    if not 1 <= limit <= 200:
        return _cap({"status": "invalid_request", "error": "limit must be between 1 and 200"})
    try:
        normalized = OpenCitationsClient.normalize_doi(doi)
        records = _opencitations().references(
            normalized, creation_after=creation_after, creation_before=creation_before
        )
    except ValueError as exc:
        return _cap({"status": "invalid_request", "error": str(exc)})
    except OpenCitationsError as exc:
        return _cap(
            {"status": "unavailable", "source": "OpenCitations Index v2", "reason": exc.reason,
             "error": str(exc)[:180]}
        )
    return _opencitations_result(
        records,
        doi=normalized,
        direction="references",
        limit=limit,
        creation_after=creation_after,
        creation_before=creation_before,
    )


@tool
def opencitations_citations(
    doi: str,
    limit: int = 100,
    creation_after: str | None = None,
    creation_before: str | None = None,
) -> str:
    """Get incoming DOI citation edges from OpenCitations; use date filters for large works."""
    if not 1 <= limit <= 200:
        return _cap({"status": "invalid_request", "error": "limit must be between 1 and 200"})
    try:
        normalized = OpenCitationsClient.normalize_doi(doi)
        records = _opencitations().citations(
            normalized, creation_after=creation_after, creation_before=creation_before
        )
    except ValueError as exc:
        return _cap({"status": "invalid_request", "error": str(exc)})
    except OpenCitationsError as exc:
        return _cap(
            {"status": "unavailable", "source": "OpenCitations Index v2", "reason": exc.reason,
             "error": str(exc)[:180],
             "hint": "Narrow the creation-date range if the response is too large."}
        )
    return _opencitations_result(
        records,
        doi=normalized,
        direction="cited_by",
        limit=limit,
        creation_after=creation_after,
        creation_before=creation_before,
    )


@tool
def citation_source_cross_check(doi: str, limit: int = 100) -> str:
    """Compare OpenAlex and OpenCitations DOI references and inbound citation counts."""
    if not 1 <= limit <= 200:
        return _cap({"status": "invalid_request", "error": "limit must be between 1 and 200"})
    try:
        normalized = OpenCitationsClient.normalize_doi(doi)
    except ValueError as exc:
        return _cap({"status": "invalid_request", "error": str(exc)})

    try:
        work = _openalex().get_work_by_doi(normalized)
    except OpenAlexError as exc:
        return _cap(
            {"status": "unavailable", "source": "OpenAlex", "reason": exc.reason,
             "error": str(exc)[:180]}
        )
    if work is None:
        return _cap({"status": "not_found", "source": "OpenAlex", "doi": normalized})

    oa_ref_ids = work.get("referenced_works")
    if not isinstance(oa_ref_ids, list):
        oa_ref_ids = []
        oa_refs_error = "OpenAlex omitted referenced_works"
    else:
        oa_refs_error = None
    oa_ref_records: list[dict[str, Any]] = []
    if oa_ref_ids:
        try:
            oa_ref_records = list(_openalex().get_works(oa_ref_ids[:limit]))
        except OpenAlexError as exc:
            oa_refs_error = str(exc)[:180]
    oa_dois = {
        OpenCitationsClient.normalize_doi(ref["doi"])
        for ref in oa_ref_records
        if ref.get("doi")
    }
    oa_resolved_ids = {ref.get("id") for ref in oa_ref_records}
    oa_unresolved = [ref_id for ref_id in oa_ref_ids[:limit] if ref_id not in oa_resolved_ids]

    oc_ref_records: list[dict[str, Any]] = []
    oc_refs_error = None
    try:
        oc_ref_records = _opencitations().references(normalized)
    except OpenCitationsError as exc:
        oc_refs_error = str(exc)[:180]
    oc_dois = {
        ids["doi"]
        for record in oc_ref_records
        if (ids := identifiers(record.get("cited"))).get("doi")
    }

    try:
        oc_citation_count = _opencitations().citation_count(normalized)
        oc_count_error = None
    except OpenCitationsError as exc:
        oc_citation_count = None
        oc_count_error = str(exc)[:180]

    oa_truncated = len(oa_ref_ids) > limit
    oc_truncated = len(oc_ref_records) > limit
    errors = {
        source: error
        for source, error in (
            ("openalex_references", oa_refs_error),
            ("opencitations_references", oc_refs_error),
            ("opencitations_citation_count", oc_count_error),
        )
        if error
    }
    complete = not errors and not oa_truncated and not oc_truncated and not oa_unresolved
    shared = oa_dois & oc_dois
    return _cap(
        {
            "status": "complete" if complete else "partial",
            "doi": normalized,
            "limit": limit,
            "openalex": {
                "work_id": work.get("id"),
                "source_reference_count": len(oa_ref_ids),
                "resolved_reference_count": len(oa_ref_records),
                "unresolved_reference_ids": oa_unresolved,
                "cited_by_count": work.get("cited_by_count"),
                "truncated": oa_truncated,
            },
            "opencitations": {
                "source": "OpenCitations Index v2",
                "reference_count": len(oc_ref_records),
                "cited_by_count": oc_citation_count,
                "truncated": oc_truncated,
            },
            "reference_doi_comparison": {
                "shared_count": len(shared),
                "openalex_only_count": len(oa_dois - oc_dois),
                "opencitations_only_count": len(oc_dois - oa_dois),
                "shared_dois": sorted(shared)[:limit],
                "openalex_only_dois": sorted(oa_dois - oc_dois)[:limit],
                "opencitations_only_dois": sorted(oc_dois - oa_dois)[:limit],
                "comparison_note": (
                    "Only resolved DOI references are compared; source coverage can differ."
                ),
            },
            "errors": errors,
        }
    )


# ---------------------------------------------------------------------- evidence


@tool
def search_fulltext(query: str, context_chars: int = 200, limit: int = 5) -> str:
    """Search extracted local/OA PDFs and return page-numbered source excerpts."""
    if not query.strip():
        return _cap({"status": "invalid_request", "error": "query must not be empty"})
    if not 1 <= limit <= 50 or not 0 <= context_chars <= 2_000:
        return _cap({"status": "invalid_request", "error": "invalid limit or context_chars"})
    papers = _read_jsonl(_thread_dir() / "papers.jsonl")
    titles = {
        doi_to_filename(p.get("doi") or "").removesuffix(".pdf"): p.get("title")
        for p in papers
    }
    dois = {
        doi_to_filename(p.get("doi") or "").removesuffix(".pdf"): p.get("doi")
        for p in papers
    }
    corpora = (
        (_pdfs_dir() / TEXT_DIR, "local_cache"),
        (_open_access_pdfs_dir() / TEXT_DIR, "open_access"),
    )
    corpus = [
        (text, source)
        for directory, source in corpora
        if directory.exists()
        for text in sorted(directory.glob("*.txt"))
    ]
    hits: list[dict[str, Any]] = []
    match_pages = 0
    lowered = query.lower()
    for txt, source in corpus:
        body = txt.read_text(encoding="utf-8", errors="replace")
        metadata: dict[str, Any] = {}
        if source == "open_access":
            metadata_path = _open_access_pdfs_dir() / f"{txt.stem}.json"
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    metadata = {}
        pages = body.split("\f")
        while len(pages) > 1 and not pages[-1].strip():
            pages.pop()
        for page_number, page_text in enumerate(pages, start=1):
            page_lower = page_text.lower()
            position = page_lower.find(lowered)
            if position < 0:
                continue
            match_pages += 1
            if len(hits) >= limit:
                continue
            start = max(0, position - context_chars // 2)
            stop = position + len(query) + context_chars // 2
            hits.append(
                {
                    "file": txt.name,
                    "doi": metadata.get("doi") or dois.get(txt.stem),
                    "title": metadata.get("title") or titles.get(txt.stem),
                    "source": source,
                    "source_url": metadata.get("landing_page_url") or metadata.get("pdf_url"),
                    "page": page_number,
                    "excerpt": " ".join(page_text[start:stop].split()),
                }
            )
    return _cap(
        {
            "status": "complete" if match_pages <= limit else "partial",
            "query": query,
            "corpus_files": len(corpus),
            "matches_found": match_pages,
            "returned_count": len(hits),
            "truncated": match_pages > limit,
            "hits": hits,
        }
    )


def _paper_work(doi: str) -> tuple[str, dict[str, Any]] | dict[str, Any]:
    normalized = normalize_doi(doi)
    if not normalized.startswith("10.") or any(char.isspace() for char in normalized):
        return {"status": "invalid_request", "error": "doi must be a valid DOI"}
    try:
        work = _openalex().get_work_by_doi(normalized)
    except OpenAlexError as exc:
        return {"status": "unavailable", "source": "OpenAlex", "reason": exc.reason,
                "error": str(exc)[:180]}
    if work is None:
        return {"status": "not_found", "doi": normalized}
    return normalized, work


@tool
def fetch_open_access_pdf(doi: str) -> str:
    """Fetch and cache a publicly listed OpenAlex OA PDF; does not use Sci-Hub."""
    result = _paper_work(doi)
    if isinstance(result, dict):
        return _cap(result)
    normalized, work = result
    try:
        return _cap(_ensure_open_access_pdf(normalized, work))
    except OSError as exc:
        return _cap({"status": "unavailable", "doi": normalized, "reason": str(exc)[:160]})


@tool
def inspect_document(
    doi: str,
    query: str | None = None,
    page: int | None = None,
    limit: int = 5,
    context_chars: int = 300,
    max_chars: int = 5_000,
) -> str:
    """Read an OA paper page, search its full text, or preview page one.

    Downloads only OpenAlex-listed OA PDFs. Query hits include page numbers and
    short excerpts; if no OA copy exists, returns the abstract and source links.
    """
    if page is not None and page < 1:
        return _cap({"status": "invalid_request", "error": "page numbers are 1-based"})
    if page is not None and query:
        return _cap({"status": "invalid_request", "error": "choose either page or query"})
    if not 1 <= limit <= 10 or not 500 <= max_chars <= 8_000:
        return _cap({"status": "invalid_request", "error": "invalid limit or max_chars"})
    if not 0 <= context_chars <= 1_000:
        return _cap({"status": "invalid_request", "error": "context_chars must be 0..1000"})

    result = _paper_work(doi)
    if isinstance(result, dict):
        return _cap(result)
    normalized, work = result
    try:
        access = _ensure_open_access_pdf(normalized, work)
    except OSError as exc:
        return _cap(
            {"status": "unavailable", "doi": normalized, "reason": str(exc)[:160]}
        )
    text_path = access.get("text_path")
    source = access.get("source") or {}

    # Existing pre-OA cache may still be inspected, but its origin is not inferred.
    if not text_path:
        legacy_path = (
            _pdfs_dir() / TEXT_DIR / f"{doi_to_filename(normalized).removesuffix('.pdf')}.txt"
        )
        if legacy_path.exists():
            text_path = str(legacy_path)
            source = {"kind": "existing_local_cache", "origin": "unknown"}

    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    common = {
        "doi": normalized,
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "source": source,
        "abstract": abstract,
    }
    if not text_path:
        return _cap(
            {
                "status": "full_text_unavailable",
                **common,
                "access_status": access.get("status"),
                "reason": access.get("reason"),
                "landing_pages": access.get("landing_pages", []),
            }
        )

    try:
        body = Path(text_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _cap({"status": "unavailable", **common, "error": str(exc)[:160]})
    pages = body.split("\f")
    while len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    common["page_count"] = len(pages)

    if page is not None:
        if page > len(pages):
            return _cap({"status": "page_not_found", **common, "requested_page": page})
        page_text = pages[page - 1]
        return _cap(
            {
                "status": "complete" if len(page_text) <= max_chars else "partial",
                **common,
                "page": page,
                "text": page_text[:max_chars],
                "truncated": len(page_text) > max_chars,
            }
        )

    if query:
        lowered = query.lower()
        hits: list[dict[str, Any]] = []
        matches = 0
        for page_number, page_text in enumerate(pages, start=1):
            position = page_text.lower().find(lowered)
            if position < 0:
                continue
            matches += 1
            if len(hits) >= limit:
                continue
            start = max(0, position - context_chars)
            stop = min(len(page_text), position + len(query) + context_chars)
            hits.append(
                {
                    "page": page_number,
                    "excerpt": " ".join(page_text[start:stop].split()),
                }
            )
        return _cap(
            {
                "status": "complete" if matches <= limit else "partial",
                **common,
                "query": query,
                "matches_found": matches,
                "returned_count": len(hits),
                "truncated": matches > limit,
                "hits": hits,
            }
        )

    first_page = pages[0] if pages else ""
    return _cap(
        {
            "status": "partial",
            **common,
            "mode": "first_page_preview",
            "page": 1,
            "text": first_page[:max_chars],
            "truncated": len(first_page) > max_chars,
            "hint": "Pass page=N to read a page or query=... to find matching passages.",
        }
    )


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

    if not 1 <= limit <= 100:
        return _cap({"status": "invalid_request", "error": "limit must be between 1 and 100"})
    if after_year is not None and before_year is not None and after_year > before_year:
        return _cap(
            {"status": "invalid_request", "error": "after_year must not exceed before_year"}
        )
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
        return _cap({"status": "unavailable", "source": "Google Patents", "error": str(exc)[:160]})
    return _cap(found)


@tool
def patent_links(publication_number: str, page: int = 0) -> str:
    """Citations around a patent: citing patents, plus (best-effort) academic

    works citing it — the patent<->paper bridge OpenAlex usually lacks.
    When Google walls us, falls back to OpenAlex's patent record (indexed
    coverage is spotty for pre-1976 patents); failures are labeled, not faked."""
    from .uspto import PatentError

    if page < 0:
        return _cap({"status": "invalid_request", "error": "page must be non-negative"})
    out: dict[str, Any] = {
        "status": "best_effort",
        "complete": False,
        "publication_number": publication_number,
        "sources": {
            "patent_citations": "Google Patents",
            "paper_citations": "Google Patents Scholar",
        },
        "coverage_note": (
            "Patent citation coverage varies by source and publication; verify important edges."
        ),
    }
    source_failed = False
    try:
        out["cited_by_patents"] = _patents().citing(publication_number, page=page)
    except PatentError as exc:
        out["cited_by_patents"] = None
        out["patent_source_error"] = str(exc)[:120]
        source_failed = True
    try:
        scholar = _patents().scholar(publication_number, "forward")
        out["cited_by_papers"] = scholar["works"]
        out["paper_citations_completeness"] = "unknown"
    except PatentError as exc:
        out["cited_by_papers"] = None
        out["scholar_source_error"] = str(exc)[:120]
        source_failed = True
    if source_failed:
        out["fallback"] = _openalex_patent_citations(publication_number)
    return _cap(out)


def _openalex_patent_citations(publication_number: str) -> dict[str, Any]:
    from .client import OpenAlexError

    bare = publication_number[:-1] if publication_number[-1:] in {"A", "B"} else publication_number
    try:
        for num in dict.fromkeys((bare, publication_number)):
            doi = f"https://patents.google.com/patent/{num}"
            work = next(iter(_openalex().paginate(
                "/works", {"filter": f"type:patent,doi:{doi}"},
                select=["id", "doi", "title", "publication_year"], per_page=5,
            )), None)
            if work is None:
                continue
            page = next(
                _openalex().iter_pages(
                    "/works",
                    {"filter": f"cites:{work['id']}"},
                    select=WORK_FIELDS,
                    per_page=20,
                    max_pages=1,
                ),
                None,
            )
            if page is None:
                return {"status": "unavailable", "source": "OpenAlex", "reason": "no page returned"}
            next_cursor = page["next_cursor"]
            citing = page["results"]
            return {
                "status": "complete" if next_cursor is None else "partial",
                "source": "OpenAlex",
                "direction": "cited_by_papers",
                "total_available": page["meta"].get("count"),
                "returned_count": len(citing),
                "complete": next_cursor is None,
                "next_cursor": next_cursor,
                "work": {k: work.get(k) for k in ("id", "doi", "title", "publication_year")},
                "cited_by_papers": [
                    _compact_work(paper, include_reference_ids=False) for paper in citing
                ],
            }
    except OpenAlexError as exc:
        return {"status": "unavailable", "source": "OpenAlex", "reason": str(exc)[:160]}
    return {
        "status": "unavailable",
        "source": "OpenAlex",
        "reason": "patent not indexed; no citation fallback available",
    }


@tool
def patent_prior_art(publication_number: str) -> str:
    """Best-effort scholarly references associated with a patent (not patent citations)."""
    from .uspto import PatentError

    try:
        result = _patents().scholar(publication_number, "backward")
        result["status"] = "best_effort"
        result["complete"] = False
        result["coverage_note"] = "Google Patents Scholar coverage is not exhaustive."
        return _cap(result)
    except PatentError as exc:
        return _cap(
            {
                "status": "unavailable",
                "complete": False,
                "source": "Google Patents Scholar",
                "publication_number": publication_number,
                "works": None,
                "note": str(exc)[:160],
            }
        )


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
