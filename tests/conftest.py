"""Shared fixtures for history-graph tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from history_graph.client import BASE_URL, OpenAlexClient


def make_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture()
def client_factory() -> Callable[[Callable[[httpx.Request], httpx.Response]], OpenAlexClient]:
    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAlexClient:
        transport = make_transport(handler)
        http_client = httpx.Client(base_url=BASE_URL, transport=transport)
        return OpenAlexClient(mailto="test@example.org", http=http_client)

    return _factory


def works_page(results: list[dict[str, Any]], next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "meta": {"count": len(results), "next_cursor": next_cursor},
        "results": results,
    }
