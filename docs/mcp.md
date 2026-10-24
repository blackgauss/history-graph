# MCP lineage-curation server

Expose the graph to agents over MCP (stdio):

For contributor guidance on contracts, provenance, citation direction, and
agent-facing interfaces, see [Software Design Principles](design-principles.md).

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
| patents | `search_patents` (full text + priority-date window + assignee), `patent_links` (best-effort citing patents + academic papers citing it; source failures are labeled), `patent_prior_art` (best-effort scholarly references); Google-bot-walled responses say so, never pose as empty |
| explore | `resolve_doi`, `find_by_title`, `references_of` (reports unresolved IDs/truncation), `citing_works` (one cursor page; continue with `next_cursor`), `opencitations_references`, `opencitations_citations` (source-specific, date-filterable, output-bounded), `citation_source_cross_check` (DOI-reference overlap and independent inbound counts) |
| evidence | `inspect_document(doi, query|page)` (fetches OpenAlex-listed OA PDFs, returns page-located passages or a page preview; falls back to abstract/source links), `fetch_open_access_pdf` (OA-only cache), `search_fulltext` (page-numbered local/OA excerpts), `fetch_pdf` (legacy Sci-Hub downloader; not the default public-source path) |
| propose | `propose_seed`, `propose_event` (schema-validated, evidence required) -> quarantine in `data/proposed/`; `list_proposals`, human-gated `apply_proposals` (`repro=True` appends then runs `dvc repro`) |
| dossier | `lineage_dossier(work)` — one paper's biography + horizon: ancestors (bridge-ranked, concept-gap flagged), descendants ranked by citation acceleration (`counts_by_year`), the research schools it fuses (shared-ancestor components of its references), plus a `story` markdown rendering. Deterministic; graded by `scripts/mine_threads.py` (`"dossier": true`, golden `tests/golden/insight.json`) |
| insight | `citation_path(from, to)` (bidirectional BFS over refs+cites, budgeted; concept-gap-ranked paths, bridge nodes flagged); `citation_context(citing, cited)` (S2 snippet + intent heuristic). Harness: `scripts/mine_threads.py`, goldens `tests/golden/insight.json` (`HG_UPDATE_GOLDENS=1` to bless) |
| threads | `propose_thread(slug, claim, seed_dois)` -> candidate YAML in `data/candidates/`; `test_thread` (scorecard: resolution, dangling links, chronology, per-edge support (cites-earlier / co-cited / needs-text+bridge / unresolved), evidence coverage); `grow_thread` (ranked frontier suggestions); `add_thread_entries`; `list_candidate_threads`; human-gated `promote_thread` (curated append, optional `repro=True`) |

Agents never edit `data/seed/*` or the curated YAML directly; `apply_proposals`
is the approval step (comment-preserving append).

## Env overrides

`OPENALEX_MAILTO`, `OPENCITATIONS_TOKEN`, `SCIHUB_MIRROR`, `HG_THREAD_DIR`, `HG_PDFS_DIR`,
`HG_OPEN_ACCESS_PDFS_DIR`, `HG_CANDIDATES_DIR`,
`HG_PROPOSED_DIR`, `HG_THREAD_YAML`, `HG_SEED_TXT`.

`OPENCITATIONS_TOKEN` is optional. OpenCitations works without it for testing;
an access token is recommended for app usage. Its citation lists are a separate
index from OpenAlex, and large incoming-citation responses may need date filters.
Source counts and edge sets can differ; the cross-check reports both instead of
silently choosing one provider as authoritative.

Testing/recording: `HG_CASSETTES=1` makes the server replay all HTTP from
`tests/cassettes` (offline, deterministic); `HG_CASSETTE_MODE=record` records
a fresh live pass into them instead (see `scripts/record_cassettes.py` and
`tests/test_lineage_scenario.py`).
