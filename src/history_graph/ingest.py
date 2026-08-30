"""Ingestion flow: seed DOIs -> raw OpenAlex records as JSONL on disk.

Stages:
1. resolve_seeds      DOI list          -> data/raw/works.jsonl
2. expand_citations   seed work ids     -> data/raw/cited_by.jsonl
3. hydrate_references referenced_works  -> data/raw/references.jsonl

A manifest.json with row counts lands next to the outputs.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import WORK_FIELDS, OpenAlexClient
from .observability import instrumented

logger = logging.getLogger(__name__)

WORKS_FILE = "works.jsonl"
CITED_BY_FILE = "cited_by.jsonl"
REFERENCES_FILE = "references.jsonl"
MANIFEST_FILE = "manifest.json"


def load_dois(path: Path) -> list[str]:
    """Read one DOI per line; blank lines and #-comments are ignored."""
    dois: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        without_comment = line.split("#", 1)[0].strip()
        if without_comment:
            dois.append(without_comment)
    return dois


@dataclass
class JsonlWriter:
    """Append-only JSONL sink that tracks how many records were written."""

    path: Path
    count: int = 0
    _handle: Any = field(default=None, repr=False)

    def __enter__(self) -> JsonlWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def write(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1


def collect_referenced_work_ids(records: Iterable[dict[str, Any]]) -> set[str]:
    """Extract distinct referenced_works ids from already-fetched work records."""
    refs: set[str] = set()
    for record in records:
        refs.update(record.get("referenced_works") or [])
    return refs


def resolve_seeds(
    client: OpenAlexClient,
    dois: Sequence[str],
    writer: JsonlWriter,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch each seed DOI; returns (resolved records, requested count)."""
    resolved: list[dict[str, Any]] = []
    for doi in dois:
        work = client.get_work_by_doi(doi)
        if work is None:
            logger.warning("No OpenAlex record for DOI %s - skipping", doi)
            continue
        writer.write(work)
        resolved.append(work)
        logger.info("Resolved %s -> %s (%s)", doi, work.get("id"), work.get("title"))
    return resolved, len(dois)


def expand_citations(
    client: OpenAlexClient,
    seed_work_ids: Sequence[str],
    writer: JsonlWriter,
    *,
    max_pages_per_seed: int | None = None,
) -> int:
    """Pull inbound citations for each seed work; returns records written."""
    for work_id in seed_work_ids:
        before = writer.count
        for citing in client.iter_citing_works(
            work_id, select=WORK_FIELDS, max_pages=max_pages_per_seed
        ):
            writer.write(citing)
        logger.info("cites:%s -> %d records", work_id, writer.count - before)
    return writer.count


def hydrate_references(
    client: OpenAlexClient,
    ref_ids: Sequence[str],
    known_ids: set[str],
    writer: JsonlWriter,
    *,
    limit: int | None = None,
) -> int:
    """Batch-fetch referenced works we have not seen yet; returns hydrated count."""
    missing = [wid for wid in sorted(ref_ids) if wid not in known_ids]
    if limit is not None:
        missing = missing[:limit]
    logger.info("Hydrating %d of %d distinct referenced works", len(missing), len(ref_ids))
    for work in client.get_works(missing, select=WORK_FIELDS):
        writer.write(work)
    return writer.count


@instrumented("stage.ingest")
def run_ingest(
    client: OpenAlexClient,
    seeds_path: Path,
    raw_dir: Path,
    *,
    max_pages_per_seed: int | None = 3,
    hydrate_limit: int | None = 100,
) -> dict[str, Any]:
    """Run the full ingestion POC; returns the manifest dict."""
    started = time.monotonic()
    dois = load_dois(seeds_path)
    raw_dir.mkdir(parents=True, exist_ok=True)

    with JsonlWriter(raw_dir / WORKS_FILE) as works_out:
        seeds, requested = resolve_seeds(client, dois, works_out)

    seed_ids = [work["id"] for work in seeds]
    fetched_records = list(seeds)

    cited_by_path = raw_dir / CITED_BY_FILE
    references_path = raw_dir / REFERENCES_FILE

    with JsonlWriter(cited_by_path) as cited_by_out:
        expand_citations(client, seed_ids, cited_by_out, max_pages_per_seed=max_pages_per_seed)
    cited_by_records = [
        json.loads(line) for line in cited_by_path.read_text(encoding="utf-8").splitlines() if line
    ]
    fetched_records.extend(cited_by_records)

    ref_ids = collect_referenced_work_ids(fetched_records)
    known_ids = {record["id"] for record in fetched_records}
    with JsonlWriter(references_path) as references_out:
        hydrate_references(client, ref_ids, known_ids, references_out, limit=hydrate_limit)

    manifest = {
        "seeds_requested": requested,
        "seeds_resolved": len(seeds),
        "citing_works_fetched": len(cited_by_records),
        "distinct_referenced_work_ids": len(ref_ids),
        "references_hydrated": references_out.count,
        "duration_seconds": round(time.monotonic() - started, 2),
        "outputs": {
            WORKS_FILE: len(seeds),
            CITED_BY_FILE: len(cited_by_records),
            REFERENCES_FILE: references_out.count,
        },
    }
    (raw_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
