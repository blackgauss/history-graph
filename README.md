# history-graph

A knowledge graph for the *intellectual lineage of computing*: papers, patents, and
events linked by real citation evidence — plus a toolset that lets an AI agent
research, grade, and (only a human) bless that lineage.

Three things, deliberately separated:

1. **Read** — ask any paper for its biography: `lineage_dossier` shows what led to
   it (bridge-ranked ancestors), where it is heading (citing works ranked by
   citation *acceleration*), and which research schools it fuses. Deterministic,
   replayable, no hallucinated steps.
2. **Research** — ~26 MCP tools for agents: citation paths, concept-gap scoring,
   patent↔paper links, S2 citation-intent, and a hypothesis workflow (threads).
3. **Curate** — a human-gated pipeline: agents *propose*, machines *grade*, humans
   *promote*. The curated seed file is written by exactly one party: you.

## Setup

```sh
uv sync
uv run pytest -q                # offline; cassettes replay all HTTP
```

To let agents use it from any project, register the MCP server (see
`~/.config/opencode/opencode.json`):

```json
"mcp": {
  "history-graph": {
    "type": "local",
    "command": ["uv", "run", "--project", "<path-to-this-repo>",
                "python", "-m", "history_graph.mcp_server"],
    "environment": { "HG_*": "…absolute data paths…" }
  }
}
```

Full tool table: [docs/mcp.md](docs/mcp.md). Worked example:
[docs/demo/boole-to-cpu-walk.md](docs/demo/boole-to-cpu-walk.md).

## The thread workflow (the interesting part)

A **thread** is a lineage *claim* as dated entries with edges:
"backpropagation → … → click-through-rate prediction". Agents build threads in
quarantine, machines score them against real citations, humans promote them.
Lifecycle, scorecard verdicts, and promotion: [docs/threads.md](docs/threads.md).

```
agent: propose_thread / add_thread_entries / grow_thread
machine: test_thread  → per-edge evidence: cites-earlier / co-cited / needs-text /
                        anachronism; verdict sound|retest|needs-text|gaps — an API
                        that couldn't answer says so, it never poses as "absent"
human: promote_thread (needs HG_ALLOW_CURATED_WRITES=1)
```

## Data pipeline (DVC)

`uv run dvc repro` runs: `ingest` (seeds in `data/seed/dois.txt` → OpenAlex
metadata in `data/raw`) → `thread` + `report` (resolved papers/events, CSVs)
→ `pdfs` (Sci-Hub full text). Large outputs are DVC-tracked, not git-tracked.

## Determinism

- **Cassettes + content-addressed store** — all HTTP replays offline from
  `tests/cassettes/`; per-work records (`works/W*.json`) mean refactors that
  change batch shape or selected fields don't invalidate replays. Dead OpenAlex
  IDs are tombstoned, never rendered as "not found".
- **Goldens** — `tests/golden/*` diff the pipeline *output*, not inputs; bless
  changes with `HG_UPDATE_GOLDENS=1` after reading the diff.
- **Graded harness** — `uv run python scripts/mine_threads.py` runs curated
  lineage questions (fixture + replayed tiers) and diff-golds them.
- Record live traffic with `HG_CASSETTE_MODE=record`; transient failures and
  bot-walls are never persisted. OpenAlex now meters per-IP credit budget:
  when it's exhausted, recordings pause with a clear error and resume at
  midnight UTC (`scripts/record_cassettes.py`, `scripts/normalize_cassettes.py`).

## Environment

`OPENALEX_MAILTO` (politeness), `HG_THREAD_DIR/_YAML`, `HG_SEED_TXT`,
`HG_PDFS_DIR`, `HG_PROPOSED_DIR`, `HG_CANDIDATES_DIR`, `HG_GRAPH_CACHE`,
`HG_CASSETTES`, `HG_CASSETTE_MODE`, `HG_UPDATE_GOLDENS`,
`HG_ALLOW_CURATED_WRITES` (human gate for curated writes). Details:
`.env.example` (nothing autoloads it; set in shell or MCP config).

## Layout

```
src/history_graph/   client/http (outcome contract), paths, dossier, insight,
                     candidates (threads), proposals (quarantine), uspto, s2,
                     scihub, mine + testing (determinism infra)
scripts/             mine_threads, record_cassettes, normalize_cassettes, demos
data/seed/           curated inputs (human-owned); data/candidates/ + data/proposed/
                     (agent-owned quarantine, gitignored)
tests/               cassettes + store, goldens, fixtures, offline suite
docs/                mcp.md, threads.md, demo/
```
