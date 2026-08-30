"""Run the thread->report pipeline fully offline from cassettes.

Shared by profile_pipeline.py and debug_pipeline.py. Deterministic:
replayed HTTP, injected clock (no sleeps), hash seed pinned by callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from history_graph.report import run_report
from history_graph.testing import openalex_client
from history_graph.thread import run_thread

SEEDS = Path("data/seed/computing_thread.yaml")


def run_offline_pipeline(workdir: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    raw_dir = workdir / "thread"
    client = openalex_client("openalex-thread")
    try:
        thread_manifest = run_thread(client, SEEDS, raw_dir)
    finally:
        client.close()
    report_path = run_report(raw_dir)
    return {"thread": thread_manifest, "report": str(report_path)}
