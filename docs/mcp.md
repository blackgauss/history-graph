# MCP lineage-curation server

Expose the graph to agents over MCP (stdio):

```
uv run python -m history_graph.mcp_server
```

Register with an MCP client (example for an `mcpServers` config):

```json
{
  "mcpServers": {
    "history-graph": {
      "command": "uv",
      "args": ["run", "python", "-m", "history_graph.mcp_server"],
      "cwd": "<repo root>"
    }
  }
}
```

## Tools

| group | tools |
|---|---|
| inspect | `thread_status`, `list_events`, `get_paper`, `list_citation_edges`, `list_curated_edges` |
| explore | `resolve_doi`, `find_by_title`, `references_of`, `citing_works` |
| evidence | `search_fulltext` (extracted PDFs under `data/pdfs/text/`), `fetch_pdf` (sci-hub, 15s+ per request, captcha-retryable) |
| propose | `propose_seed`, `propose_event` (schema-validated, evidence required) -> quarantine in `data/proposed/`; `list_proposals`, human-gated `apply_proposals` (`repro=True` appends then runs `dvc repro`) |
| threads | `propose_thread(slug, claim, seed_dois)` -> candidate YAML in `data/candidates/`; `test_thread` (scorecard: resolution, dangling links, chronology, citation support per edge, evidence coverage); `grow_thread` (ranked frontier suggestions); `add_thread_entries`; `list_candidate_threads`; human-gated `promote_thread` (curated append, optional `repro=True`) |

Agents never edit `data/seed/*` or the curated YAML directly; `apply_proposals`
is the approval step (comment-preserving append).

## Env overrides

`OPENALEX_MAILTO`, `SCIHUB_MIRROR`, `HG_THREAD_DIR`, `HG_PDFS_DIR`, `HG_CANDIDATES_DIR`,
`HG_PROPOSED_DIR`, `HG_THREAD_YAML`, `HG_SEED_TXT`.

Testing/recording: `HG_CASSETTES=1` makes the server replay all HTTP from
`tests/cassettes` (offline, deterministic); `HG_CASSETTE_MODE=record` records
a fresh live pass into them instead (see `scripts/record_cassettes.py` and
`tests/test_lineage_scenario.py`).
