"""Resolve a curated timeline thread against OpenAlex.

Reads a thread YAML file, resolves ``paper`` entries to OpenAlex works (by
DOI first, then title search), and writes:

- data/thread/events.jsonl     every entry, normalized
- data/thread/papers.jsonl     resolved papers with core work fields
- data/thread/manifest.json    resolution summary
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .client import WORK_FIELDS, OpenAlexClient
from .models import ThreadEntry, parse_thread

logger = logging.getLogger(__name__)

DEFAULT_SEEDS = Path("data/seed/computing_thread.yaml")
DEFAULT_RAW_DIR = Path("data/thread")

EVENTS_FILE = "events.jsonl"
PAPERS_FILE = "papers.jsonl"
MANIFEST_FILE = "manifest.json"


def load_thread(path: Path) -> list[ThreadEntry]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_thread(payload)


def resolve_papers(
    client: OpenAlexClient,
    entries: list[ThreadEntry],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Resolve paper entries; returns ({entry_id: work}, unresolved_ids)."""
    resolved: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for entry in entries:
        if entry.kind != "paper":
            continue
        work = None
        via = None
        if entry.refs.doi:
            work = client.get_work_by_doi(entry.refs.doi)
            via = "doi"
        if work is None and entry.refs.title_search:
            work = client.get_work_by_title(entry.refs.title_search)
            via = "title"
        if work is None:
            ref_hint = entry.refs.doi or entry.refs.title_search
            logger.warning("Unresolved paper %s (%s)", entry.id, ref_hint)
            unresolved.append(entry.id)
            continue
        logger.info(
            "Resolved %s via %s -> %s", entry.id, via, work.get("title")
        )
        record = {
            "id": entry.id,
            "openalex_id": work.get("id"),
            "resolved_via": via,
            **{k: work[k] for k in WORK_FIELDS if k in work and k != "id"},
        }
        resolved[entry.id] = record
    return resolved, unresolved


def _event_record(entry: ThreadEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "date": entry.date,
        "year": entry.year,
        "kind": entry.kind,
        "sub": entry.sub,
        "title": entry.title,
        "who": entry.who,
        "related": entry.related,
        "notes": entry.notes,
    }


def run_thread(client: OpenAlexClient, seeds_path: Path, raw_dir: Path) -> dict[str, Any]:
    entries = load_thread(seeds_path)
    raw_dir.mkdir(parents=True, exist_ok=True)

    events_path = raw_dir / EVENTS_FILE
    with events_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(_event_record(entry), ensure_ascii=False) + "\n")

    resolved, unresolved = resolve_papers(client, entries)

    papers_path = raw_dir / PAPERS_FILE
    with papers_path.open("w", encoding="utf-8") as handle:
        for record in resolved.values():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "entries": len(entries),
        "papers_resolved": len(resolved),
        "papers_unresolved": len(unresolved),
        "unresolved_ids": unresolved,
        "outputs": {EVENTS_FILE: len(entries), PAPERS_FILE: len(resolved)},
    }
    (raw_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="history-graph.thread", description=__doc__)
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--mailto",
        default=os.environ.get("OPENALEX_MAILTO", "history-graph-poc@example.org"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    client = OpenAlexClient(mailto=args.mailto)
    try:
        manifest = run_thread(client, args.seeds, args.raw_dir)
    finally:
        client.close()

    print("Thread ingestion complete:")
    for key, value in manifest.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
