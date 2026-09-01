FROM python:3.12-slim AS base
ENV UV_PROJECT_ENVIRONMENT=/app/.venv PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY data ./data
ENV HG_MCP_TRANSPORT=http HG_MCP_HOST=0.0.0.0 HG_MCP_PORT=8765
EXPOSE 8765
CMD ["python", "-m", "history_graph.mcp_server"]
