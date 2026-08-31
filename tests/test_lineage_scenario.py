"""End-to-end lineage-curation scenario over a truly mounted MCP server.

The server runs as a stdio subprocess (exactly how an agent mounts it) with
HG_* pointing at a temp workspace; this test client plays the agent walking
"How did Boolean algebra reach the modern CPU?":

detect gap -> resolve candidates -> (fetch/quote evidence) -> propose -> apply

HTTP inside the server replays tests/cassettes deterministically
(HG_CASSETTES=1); record the scenario's novel calls once with
HG_CASSETTE_MODE=1 pytest tests/test_lineage_scenario.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from history_graph.report import run_report
from history_graph.testing import openalex_client
from history_graph.thread import run_thread

REPO = Path(__file__).parent.parent
SEEDS = Path("data/seed/computing_thread.yaml")

SCENARIO_YAML = """\
# the computing thread (scenario sandbox)
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


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    raw_dir = tmp_path / "thread"
    client = openalex_client("openalex-thread")
    try:
        run_thread(client, SEEDS, raw_dir)
    finally:
        client.close()
    run_report(raw_dir)
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "thread.yaml").write_text(SCENARIO_YAML, encoding="utf-8")
    (seed_dir / "dois.txt").write_text("# seeds\n", encoding="utf-8")
    return {
        "HG_ALLOW_CURATED_WRITES": "1",  # the scenario plays the human approving
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "HG_THREAD_DIR": str(raw_dir),
        "HG_PDFS_DIR": str(tmp_path / "pdfs"),
        "HG_PROPOSED_DIR": str(tmp_path / "proposed"),
        "HG_THREAD_YAML": str(seed_dir / "thread.yaml"),
        "HG_SEED_TXT": str(seed_dir / "dois.txt"),
        "HG_CASSETTES": "1",
        **({"HG_CASSETTE_MODE": "record"} if os.environ.get("HG_CASSETTE_MODE") else {}),
    }


def _parse(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    text = structured["result"] if structured else result.content[0].text
    return json.loads(text)


def test_boole_to_cpu_lineage_walk(env: dict[str, str]) -> None:
    async def drive() -> None:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "history_graph.mcp_server"],
            env=env, cwd=str(REPO),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name: str, **arguments: Any) -> Any:
                    return _parse(await session.call_tool(name, arguments))

                # 1. the gap: 1840s logic-of-events era has no Boole, and no
                #    bridge from logic to circuits before ENIAC-era hardware
                early = await call("list_events", year_from=1800, year_to=1944)
                blob = json.dumps(early).lower()
                assert "boole" not in blob
                assert "switching circuits" not in blob

                # 2. resolve candidates (title search: the 1854 original has no
                #    usable DOI; the thesis resolves to its 1938 record)
                boole = await call(
                    "find_by_title", title="An Investigation of the Laws of Thought"
                )
                assert "Laws of Thought" in boole["title"]
                thesis = await call(
                    "find_by_title",
                    title="A symbolic analysis of relay and switching circuits",
                )
                assert thesis["year"] == 1938, thesis

                # 3. hit the metadata wall, then go get evidence: OpenAlex
                #    has no reference trail for this era of works, so the
                #    agent fetches the thesis full text and quotes it
                shannon = await call("get_paper", entry_or_doi="shannon-math-theory-comm-1948")
                assert shannon["openalex_id"].endswith("W1995875735")
                refs = await call("references_of", openalex_id=shannon["openalex_id"])
                assert refs["reference_count"] == 0  # the citation trail is dry here

                thesis_doi = (thesis.get("doi") or "").removeprefix("https://doi.org/")
                fetched = await call("fetch_pdf", doi=thesis_doi)
                assert fetched["status"] in {"downloaded", "failed"}
                evidence = await call("search_fulltext", query="switching")
                if fetched["status"] == "downloaded":
                    assert evidence["hits"], "downloaded pdf should yield searchable text"

                # 4. propose through the quarantine, then approve
                s1 = await call("propose_seed", doi=thesis_doi, reason="Boolean->circuits bridge")
                assert s1["status"] == "proposed"
                e_boole = await call(
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
                    evidence="OpenAlex W-record for the 1854 original; cites none (pre-DOI)",
                )
                assert e_boole["status"] == "proposed"
                e_thesis = await call(
                    "propose_event",
                    entry={
                        "id": "shannon-switching-circuits-1938",
                        "date": "1938",
                        "kind": "paper",
                        "title": "A symbolic analysis of relay and switching circuits",
                        "who": "Claude Shannon",
                        "refs": {"doi": thesis_doi},
                        "related": ["boole-laws-of-thought-1854", "eniac-completed-1945"],
                    },
                    evidence=(
                        "OpenAlex 1938 record + Shannon 1948 references"
                        if not evidence["hits"]
                        else evidence["hits"][0]["excerpt"]
                    ),
                )
                assert e_thesis["status"] == "proposed"

                pending = await call("list_proposals")
                assert len(pending["events"]) == 2 and len(pending["seeds"]) == 1

                applied = await call("apply_proposals", repro=False)
                assert sorted(applied["events_added"]) == [
                    "boole-laws-of-thought-1854",
                    "shannon-switching-circuits-1938",
                ]
                assert applied["seeds_added"] == [thesis_doi.lower()]

                assert (await call("list_proposals")) == {"seeds": [], "events": []}

                seeds_text = Path(env["HG_SEED_TXT"]).read_text(encoding="utf-8").lower()
                assert thesis_doi.lower() in seeds_text
                assert "boolean->circuits bridge" in seeds_text

    asyncio.run(drive())

    yaml_text = Path(env["HG_THREAD_YAML"]).read_text(encoding="utf-8")
    assert "scenario sandbox" in yaml_text  # comments survive approval
    assert "boole-laws-of-thought-1854" in yaml_text
    assert "shannon-switching-circuits-1938" in yaml_text
