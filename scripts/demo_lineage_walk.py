"""Generate a human-readable transcript of the Boole->CPU lineage walk.

Uses the recorded cassettes (HG_CASSETTES=1) so the transcript is fully
deterministic; writes docs/demo/boole-to-cpu-walk.md by default.

    uv run python scripts/demo_lineage_walk.py [outfile]
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import os

os.environ["HG_CASSETTES"] = "1"

from history_graph import mcp_server
from history_graph.report import run_report
from history_graph.testing import openalex_client, scihub_client
from history_graph.thread import run_thread

REPO = Path(__file__).parent.parent
SEEDS = REPO / "data/seed/computing_thread.yaml"

SANDBOX_YAML = """\
# the computing thread (demo sandbox)
entries:
  - id: turing-computable-numbers-1936
    date: "1936"
    kind: paper
    title: "On Computable Numbers"
    who: Alan Turing
  - id: shannon-math-theory-comm-1948
    date: "1948"
    kind: paper
    title: "A Mathematical Theory of Communication"
    who: Claude Shannon
  - id: eniac-completed-1945
    date: "1945"
    kind: tech
    title: "ENIAC completed"
"""


def call(name: str, **args: Any) -> Any:
    fn = next(f for f in mcp_server.TOOLS if f.__name__ == name)
    print(f"  tool: {name} {args if args else ''}", file=sys.stderr)
    return json.loads(fn(**args))


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs/demo/boole-to-cpu-walk.md"
    tmp = Path(tempfile.mkdtemp(prefix="hg-demo-"))
    raw = tmp / "thread"
    client = openalex_client("openalex-thread")
    run_thread(client, SEEDS, raw)
    client.close()
    run_report(raw)
    seed_dir = tmp / "seed"
    seed_dir.mkdir()
    (seed_dir / "thread.yaml").write_text(SANDBOX_YAML, encoding="utf-8")
    (seed_dir / "dois.txt").write_text("# seeds\n", encoding="utf-8")
    os.environ.update(
        HG_THREAD_DIR=str(raw),
        HG_PDFS_DIR=str(tmp / "pdfs"),
        HG_PROPOSED_DIR=str(tmp / "proposed"),
        HG_THREAD_YAML=str(seed_dir / "thread.yaml"),
        HG_SEED_TXT=str(seed_dir / "dois.txt"),
    )
    mcp_server._clients["openalex"] = openalex_client("openalex")
    mcp_server._clients["scihub"] = scihub_client("scihub")

    md: list[str] = [
        "# How did Boolean algebra reach the modern CPU?",
        "",
        "*A deterministic replay (HTTP cassettes) of an agent walking this question",
        "purely through the `history-graph` MCP tools. Each step shows the tool call",
        "and its result. Reproduce with `uv run python scripts/demo_lineage_walk.py`.*",
    ]

    def step(title: str, body: str) -> None:
        md.extend(["", f"## {title}", "", body])

    early = [
        e
        for e in call("list_events", year_from=1800, year_to=1944, limit=20)["events"]
    ]
    blob = json.dumps(early).lower()
    assert "boole" not in blob
    step(
        "1. Inspect the curated thread -> spot the gap",
        "Nothing in 1800-1944 mentions **Boole** or **switching circuits**; the jump\n"
        "from Turing's symbols to ENIAC's hardware has no bridge.\n\n"
        + "\n".join(f"- `{e['date']}` {e['title']}" for e in early),
    )

    boole = call("find_by_title", title="An Investigation of the Laws of Thought")
    thesis = call(
        "find_by_title", title="A symbolic analysis of relay and switching circuits"
    )
    step(
        "2. Resolve candidates via OpenAlex",
        f"```\n{json.dumps(boole, indent=2)}\n```\n\n"
        f"```\n{json.dumps(thesis, indent=2)}\n```\n\n"
        "*The 1854 original resolves only by title (pre-DOI era); the 1938 MIT thesis\n"
        "has its own record.*",
    )

    shannon = call("get_paper", entry_or_doi="shannon-math-theory-comm-1948")
    refs = call("references_of", openalex_id=shannon["openalex_id"])
    step(
        "3. Hit the metadata wall",
        f"`references_of({shannon['openalex_id'].rsplit('/', 1)[-1]})` -> "
        f"**{refs['reference_count']} references**. OpenAlex has no reference trail for\n"
        "this era of works, so metadata alone cannot walk the link. The agent has to go\n"
        "read the paper.",
    )

    doi = (thesis.get("doi") or "").removeprefix("https://doi.org/")
    fetched = call("fetch_pdf", doi=doi)
    quote = call("search_fulltext", query="Boolean", context_chars=320)
    assert fetched["status"] == "downloaded" and quote["hits"], "cassette demo prerequisite"
    excerpt = quote["hits"][0]["excerpt"]
    step(
        "4. Fetch the full text and pull an actual quote",
        f"`fetch_pdf({doi!r})` -> {fetched['status']} via sci-hub (11-page thesis).\n\n"
        f"`search_fulltext(\"Boolean\")` ->\n\n> …{excerpt}…\n\n"
        "*That sentence is the Boolean->circuits link, in Shannon's own words.*",
    )

    p1 = call(
        "propose_seed", doi=doi, reason="Boolean->circuits bridge paper"
    )
    e1 = call(
        "propose_event",
        entry={
            "id": "boole-laws-of-thought-1854",
            "date": "1854",
            "kind": "paper",
            "title": "An Investigation of the Laws of Thought",
            "who": "George Boole",
            "refs": {"title_search": "An Investigation of the Laws of Thought"},
            "related": ["turing-computable-numbers-1936"],
        },
        evidence="OpenAlex W-record for the 1854 original (no references: pre-DOI era)",
    )
    e2 = call(
        "propose_event",
        entry={
            "id": "shannon-switching-circuits-1938",
            "date": "1938",
            "kind": "paper",
            "title": "A symbolic analysis of relay and switching circuits",
            "who": "Claude Shannon",
            "refs": {"doi": doi},
            "related": ["boole-laws-of-thought-1854", "eniac-completed-1945"],
        },
        evidence=f"thesis quote: {excerpt[:160]}...",
    )
    step(
        "5. Propose into quarantine (schema-validated, evidence-required)",
        f"- `propose_seed` -> {p1['status']}: `{doi}`\n"
        f"- `propose_event` -> {e1['status']}: `{e1['id']}`\n"
        f"- `propose_event` -> {e2['status']}: `{e2['id']}`",
    )

    applied = call("apply_proposals", repro=False)
    yaml_text = Path(os.environ["HG_THREAD_YAML"]).read_text(encoding="utf-8")
    added = (
        yaml_text.split("# ---", 1)[-1] if "# ---" in yaml_text else yaml_text[len(SANDBOX_YAML):]
    )
    step(
        "6. Human approves; curated inputs grow",
        f"`apply_proposals` -> events {applied['events_added']}, seeds {applied['seeds_added']}\n\n"
        "Appended to `computing_thread.yaml` (comments preserved):\n\n"
        f"```yaml{added}\n```\n\n"
        "*Next `dvc repro` re-resolves papers, rebuilds report.md, and the "
        "citation-graph diff becomes reviewable like any other code change.*",
    )

    md += [
        "",
        "---",
        "*Nothing above touched the network at generation time - the same walk runs",
        "in `tests/test_lineage_scenario.py` as a regression guard.*",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
