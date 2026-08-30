"""Byte-stable regression: fully replayed pipeline vs committed golden files.

Regenerate on purpose with HG_UPDATE_GOLDENS=1 pytest -k golden, then review
the golden diff like any other API change.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from history_graph.download import run_download
from history_graph.report import run_report
from history_graph.testing import openalex_client, scihub_client
from history_graph.thread import run_thread

GOLDEN_DIR = Path(__file__).parent / "golden"
SEEDS = Path("data/seed/computing_thread.yaml")
UPDATE = bool(os.environ.get("HG_UPDATE_GOLDENS"))


def check_golden(name: str, content: str, *, redaction: str | None = None) -> None:
    if redaction:
        content = content.replace(redaction, "{tmp}")
    path = GOLDEN_DIR / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return
    assert path.exists(), f"golden {path} missing; run with HG_UPDATE_GOLDENS=1"
    assert content == path.read_text(encoding="utf-8"), f"golden mismatch for {name}"


def _json_golden(name: str, payload: Any, *, redaction: str | None = None) -> None:
    check_golden(name, json.dumps(payload, indent=2, sort_keys=True) + "\n", redaction=redaction)


@pytest.fixture()
def replayed_thread(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    raw_dir = tmp_path / "thread"
    client = openalex_client("openalex-thread")
    try:
        manifest = run_thread(client, SEEDS, raw_dir)
    finally:
        client.close()
    return raw_dir, manifest


def test_thread_stage_matches_golden(replayed_thread: tuple[Path, dict[str, Any]]) -> None:
    raw_dir, manifest = replayed_thread
    _json_golden("thread/manifest.json", manifest)
    check_golden("thread/events.jsonl", (raw_dir / "events.jsonl").read_text(encoding="utf-8"))
    check_golden("thread/papers.jsonl", (raw_dir / "papers.jsonl").read_text(encoding="utf-8"))


def test_report_stage_matches_golden(replayed_thread: tuple[Path, dict[str, Any]]) -> None:
    raw_dir, _ = replayed_thread
    report_path = run_report(raw_dir)
    check_golden("report.md", report_path.read_text(encoding="utf-8"))
    for csv in sorted(raw_dir.glob("*.csv")):
        check_golden(f"report/{csv.name}", csv.read_text(encoding="utf-8"))


def test_download_stage_matches_golden(tmp_path: Path) -> None:
    pdf_dois = ["10.1145/3065386", "10.1126/science.1225829"]
    seeds = tmp_path / "dois.txt"
    seeds.write_text("\n".join(pdf_dois) + "\n", encoding="utf-8")
    pdf_dir = tmp_path / "pdfs"
    client = scihub_client("scihub")
    try:
        manifest = run_download(client, [seeds], pdf_dir)
    finally:
        client.close()

    assert manifest["pdfs_downloaded"] == 2 and manifest["pdfs_failed"] == 0
    _json_golden("download/manifest.json", manifest, redaction=str(tmp_path))
    digests = {
        doi: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for doi, path in manifest["files"].items()
    }
    _json_golden("download/checksums.json", digests)
