# Deploying the MCP server

One host does the work; clients attach by URL and install nothing.

## Server (Docker)

    git clone https://github.com/blackgauss/history-graph && cd history-graph
    cp .env.example .env        # OPENALEX_MAILTO/OPENALEX_API_KEY (+HG_MCP_TOKEN)
    docker compose up --build -d

Clients mount it in their opencode/claude/cursor config:

    "mcp": {
      "history-graph": {
        "url": "http://<host>:8765/mcp",
        "headers": { "Authorization": "***" }
      }
    }

## Server (no Docker)

    uv sync
    HG_MCP_TRANSPORT=http HG_MCP_TOKEN=secret uv run python -m history_graph.mcp_server

## Notes
- Transport flags also accepted as CLI: `--transport http --host 0.0.0.0 --port 8765`
  (env fallbacks: `HG_MCP_TRANSPORT`, `HG_MCP_HOST`, `HG_MCP_PORT`, `HG_MCP_TOKEN`).
- `HG_MCP_TOKEN` unset = open port: keep it on the LAN/firewalled.
- Data lives in `./data` (writable volume); cassettes under `tests/cassettes`
  (mount writable only when recording with `HG_CASSETTE_MODE=record`).
- Human-gated curation runs where the data lives (`HG_ALLOW_CURATED_WRITES=1`
  in that server's env); thin clients never touch curated files.
