"""Unit tests for the OpenAlex client: pagination, batching, retries."""

from __future__ import annotations

from typing import Any

import conftest
import httpx
import pytest

from history_graph.client import OpenAlexClient


def test_paginate_walks_cursor_pages(client_factory) -> None:
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cursors.append(request.url.params.get("cursor"))
        if len(seen_cursors) == 1:
            return httpx.Response(
                200, json=conftest.works_page([{"id": "W1"}], next_cursor="cursor-2")
            )
        return httpx.Response(200, json=conftest.works_page([{"id": "W2"}]))

    client = client_factory(handler)
    records = list(client.paginate("/works", {"filter": "cites:W0"}))

    assert [record["id"] for record in records] == ["W1", "W2"]
    assert seen_cursors == ["*", "cursor-2"]


def test_paginate_respects_max_pages(client_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json=conftest.works_page([{"id": f"W{calls}"}], next_cursor=f"c{calls}")
        )

    client = client_factory(handler)
    records = list(client.paginate("/works", max_pages=2))

    assert len(records) == 2
    assert calls == 2


def test_get_work_by_doi_returns_first_result(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["filter"].startswith("doi:")
        return httpx.Response(
            200,
            json=conftest.works_page([{"id": "W42", "doi": params["filter"][4:]}]),
        )

    client = client_factory(handler)
    work = client.get_work_by_doi("10.9999/test")

    assert work is not None
    assert work["id"] == "W42"


def test_get_work_by_doi_returns_none_when_missing(client_factory) -> None:
    client = client_factory(lambda request: httpx.Response(200, json=conftest.works_page([])))
    assert client.get_work_by_doi("10.9999/nope") is None


def test_get_works_batches_ids_at_fifty(client_factory) -> None:
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        filters.append(request.url.params["filter"])
        return httpx.Response(200, json=conftest.works_page([]))

    client = client_factory(handler)
    ids = [f"W{i}" for i in range(75)]
    list(client.get_works(ids))  # exhaust generator

    assert len(filters) == 2
    assert filters[0].count("|") == 49  # first chunk holds 50 ids
    assert filters[1].startswith("openalex_id:W50|")
    assert filters[1].count("|") == 24  # second chunk holds 25 ids


def test_retry_recovers_from_transient_503(client_factory) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"meta": {}, "results": []})

    client: OpenAlexClient = client_factory(handler)
    records = list(client.paginate("/works"))

    assert records == []
    assert attempts == 3


def test_retry_gives_up_after_five_attempts(client_factory) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    client = client_factory(handler)
    with pytest.raises(httpx.HTTPStatusError):
        list(client.paginate("/works"))

    assert attempts == 5


def test_mailto_is_sent_for_polite_pool(client_factory) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["mailto"] = request.url.params.get("mailto")
        return httpx.Response(200, json=conftest.works_page([]))

    client = client_factory(handler)
    list(client.paginate("/works"))

    assert captured["mailto"] == "test@example.org"
