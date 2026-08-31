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

Agents never edit `data/seed/*` or the curated YAML directly; `apply_proposals`
is the approval step (comment-preserving append).

## Env overrides

`OPENALEX_MAILTO`, `SCIHUB_MIRROR`, `HG_THREAD_DIR`, `HG_PDFS_DIR`,
`HG_PROPOSED_DIR`, `HG_THREAD_YAML`, `HG_SEED_TXT`.
