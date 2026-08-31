"""Quarantine for agent-proposed graph additions awaiting human approval.

Agents write here via the MCP tools; nothing under data/seed or the thread
YAML changes until ``apply_proposals`` moves items across (comment-preserving
append for YAML, append for the plain DOI list).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .download import normalize_doi
from .models import ThreadEntry

DEFAULT_PROPOSED_DIR = Path("data/proposed")
SEEDS_FILE = "dois.txt"
EVENTS_FILE = "events.jsonl"

DEFAULT_THREAD_YAML = Path("data/seed/computing_thread.yaml")
DEFAULT_SEED_TXT = Path("data/seed/dois.txt")


def _seeds_path(proposed_dir: Path) -> Path:
    return proposed_dir / SEEDS_FILE


def _events_path(proposed_dir: Path) -> Path:
    return proposed_dir / EVENTS_FILE


def _existing_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def propose_seed(
    doi: str, reason: str, *, proposed_dir: Path = DEFAULT_PROPOSED_DIR
) -> dict[str, Any]:
    """Record a DOI the agent wants added as an ingest seed (deduplicated)."""
    proposed_dir.mkdir(parents=True, exist_ok=True)
    key = normalize_doi(doi)
    existing = {
        line.split("#", 1)[0].strip().lower() for line in _existing_lines(_seeds_path(proposed_dir))
    }
    if key in existing:
        return {"status": "duplicate", "doi": key}
    with _seeds_path(proposed_dir).open("a", encoding="utf-8") as handle:
        handle.write(f"{key}  # {reason.strip()}\n")
    return {"status": "proposed", "doi": key}


def propose_event(
    entry: dict[str, Any],
    evidence: str,
    *,
    proposed_dir: Path = DEFAULT_PROPOSED_DIR,
) -> dict[str, Any]:
    """Validate a timeline entry against the thread schema and quarantine it."""
    validated = ThreadEntry.model_validate(entry)  # raises pydantic.ValidationError
    proposed_dir.mkdir(parents=True, exist_ok=True)
    existing_ids = {row["entry"]["id"] for row in list_event_proposals(proposed_dir)}
    if validated.id in existing_ids:
        return {"status": "duplicate", "id": validated.id}
    payload = {
        "entry": validated.model_dump(exclude_none=True, exclude_defaults=True),
        "evidence": evidence.strip(),
    }
    with _events_path(proposed_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {"status": "proposed", "id": validated.id}


def list_proposals(*, proposed_dir: Path = DEFAULT_PROPOSED_DIR) -> dict[str, Any]:
    seeds = []
    for line in _existing_lines(_seeds_path(proposed_dir)):
        doi, _, reason = line.partition("#")
        seeds.append({"doi": doi.strip(), "reason": reason.strip()})
    return {"seeds": seeds, "events": list_event_proposals(proposed_dir)}


def list_event_proposals(proposed_dir: Path = DEFAULT_PROPOSED_DIR) -> list[dict[str, Any]]:
    path = _events_path(proposed_dir)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def thread_ids(path: Path) -> set[str]:
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        part = line.strip().removeprefix("- ")
        if part.startswith("id:"):
            ids.add(part.split(":", 1)[1].strip().strip('"'))
    return ids


def apply_proposals(
    *,
    proposed_dir: Path = DEFAULT_PROPOSED_DIR,
    thread_yaml: Path = DEFAULT_THREAD_YAML,
    seed_txt: Path = DEFAULT_SEED_TXT,
) -> dict[str, Any]:
    """Move quarantined proposals into the curated inputs. Returns a summary.

    The thread YAML is appended to (not re-dumped) so hand-written comments
    survive; seeds are appended with their reason as a trailing comment.
    """
    if not proposed_dir.exists():
        return {"events_added": [], "seeds_added": [], "skipped": []}

    skipped: list[str] = []
    events_added: list[str] = []
    existing_ids = thread_ids(thread_yaml)
    blocks: list[str] = []
    for row in list_event_proposals(proposed_dir):
        ThreadEntry.model_validate(row["entry"])
        if row["entry"]["id"] in existing_ids:
            skipped.append(f"event {row['entry']['id']}: id already in thread")
            continue
        blocks.append(yaml_block(row["entry"], row.get("evidence", "")))
        events_added.append(row["entry"]["id"])
    if blocks:
        with thread_yaml.open("a", encoding="utf-8") as handle:
            handle.write("".join("\n" + block for block in blocks))
        _events_path(proposed_dir).unlink()

    seeds_added: list[str] = []
    proposed_seeds = _existing_lines(_seeds_path(proposed_dir))
    if proposed_seeds:
        curated = {line.split("#", 1)[0].strip().lower() for line in _existing_lines(seed_txt)}
        seen = set(curated)
        with seed_txt.open("a", encoding="utf-8") as handle:
            for raw in proposed_seeds:
                doi, _, reason = raw.partition("#")
                doi = doi.strip().lower()
                if doi in seen:
                    skipped.append(f"seed {doi}: already seeded")
                    continue
                seen.add(doi)
                handle.write(f"{doi}  # {reason.strip() or 'proposed'}\n")
                seeds_added.append(doi)
        if len(seeds_added) == len(proposed_seeds):
            _seeds_path(proposed_dir).unlink(missing_ok=True)

    return {"events_added": events_added, "seeds_added": seeds_added, "skipped": skipped}


def yaml_block(entry: dict[str, Any], evidence: str) -> str:
    lines = [
        f"  - id: {entry['id']}",
        f"    date: \"{entry['date']}\"",
        f"    kind: {entry['kind']}",
        f"    title: {json.dumps(entry['title'], ensure_ascii=False)}",
    ]
    if entry.get("who"):
        lines.append(f"    who: {json.dumps(entry['who'], ensure_ascii=False)}")
    refs = entry.get("refs") or {}
    if refs:
        lines.append("    refs:")
        for key, value in refs.items():
            lines.append(f"      {key}: {json.dumps(value, ensure_ascii=False)}")
    if entry.get("related"):
        lines.append("    related: [" + ", ".join(entry["related"]) + "]")
    note = entry.get("notes") or ""
    if evidence:
        note = f"{note} [proposal evidence: {evidence}]".strip()
    if note:
        lines.append(f"    notes: {json.dumps(note, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"
