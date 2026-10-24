"""Unit tests for the OpenCitations Index API adapter."""

from __future__ import annotations

import json

import httpx
import pytest

from history_graph import opencitations


def client_for(handler, *, token: str | None = None, max_response_bytes: int = 100_000):
    http = httpx.Client(
        base_url=opencitations.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    return opencitations.OpenCitationsClient(
        token=token,
        http=http,
        sleep=lambda _: None,
        clock=lambda: 10.0,
        max_response_bytes=max_response_bytes,
    )


def test_references_normalizes_doi_and_parses_edges_without_auth() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=[{"oci": "1-2", "citing": "doi:10.1/source", "cited": "doi:10.1/ref"}],
        )

    client = client_for(handler)
    rows = client.references("https://doi.org/10.1126/science.1225829")

    assert seen["path"].endswith("/references/doi:10.1126/science.1225829")
    assert seen["authorization"] is None
    assert rows[0]["oci"] == "1-2"


def test_citations_passes_creation_filters_and_optional_token() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["filters"] = request.url.params.get_list("filter")
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    client = client_for(handler, token="test-token")
    rows = client.citations(
        "10.1016/j.molcel.2012.03.020",
        creation_after="2024",
        creation_before="2026-01",
    )

    assert rows == []
    assert captured["filters"] == ["creation:>2024", "creation:<2026-01"]
    assert captured["authorization"] == "test-token"


def test_citation_count_parses_count_as_integer() -> None:
    client = client_for(lambda request: httpx.Response(200, json=[{"count": "15654"}]))
    assert client.citation_count("10.1126/science.1225829") == 15654


def test_invalid_doi_is_rejected_before_request() -> None:
    client = client_for(lambda request: pytest.fail("must not make a request"))
    with pytest.raises(ValueError, match="must be a DOI"):
        client.references("not-a-doi")


def test_malformed_response_is_not_reported_as_empty() -> None:
    client = client_for(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(opencitations.OpenCitationsError) as exc_info:
        client.references("10.1126/science.1225829")
    assert exc_info.value.reason == "bad_format"


def test_oversized_response_is_explicitly_unknown() -> None:
    body = json.dumps([{"oci": "x", "citing": "x", "cited": "y"}])
    client = client_for(
        lambda request: httpx.Response(200, text=body),
        max_response_bytes=10,
    )
    with pytest.raises(opencitations.OpenCitationsError) as exc_info:
        client.citations("10.1126/science.1225829")
    assert exc_info.value.reason == "response_too_large"


def test_edge_compaction_preserves_direction_and_source_ids() -> None:
    edge = opencitations.compact_edge(
        {
            "oci": "1-2",
            "citing": "omid:br/1 openalex:W1 doi:10.1/source",
            "cited": "omid:br/2 doi:10.1/target",
            "creation": "2024-01-01",
            "timespan": "P2Y",
            "journal_sc": "no",
            "author_sc": "yes",
        }
    )
    assert edge["citing"]["openalex_id"] == "https://openalex.org/W1"
    assert edge["cited"]["doi"] == "10.1/target"
    assert edge["author_self_citation"] == "yes"
