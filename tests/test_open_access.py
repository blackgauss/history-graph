"""Tests for public PDF discovery and retrieval."""

from __future__ import annotations

import httpx
import pytest

from history_graph.open_access import OpenAccessError, OpenAccessPdfClient, pdf_locations


def test_pdf_locations_prefers_oa_and_skips_closed_or_duplicate_locations() -> None:
    work = {
        "best_oa_location": {
            "pdf_url": "https://repo.example/paper.pdf",
            "landing_page_url": "https://repo.example/paper",
            "license": "cc-by",
        },
        "locations": [
            {"pdf_url": "https://repo.example/paper.pdf", "is_oa": True},
            {"pdf_url": "https://publisher.example/closed.pdf", "is_oa": False},
            {"pdf_url": "https://archive.example/paper.pdf", "is_oa": True},
            {"pdf_url": "http://legacy.example/paper.pdf", "is_oa": True},
        ],
    }
    assert [item["pdf_url"] for item in pdf_locations(work)] == [
        "https://repo.example/paper.pdf",
        "https://archive.example/paper.pdf",
    ]


def test_fetch_accepts_pdf_and_returns_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/pdf"
        return httpx.Response(200, content=b"%PDF-1.7 example", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client = OpenAccessPdfClient(http=http, clock=lambda: 10.0, sleep=lambda _: None)
    content, url = client.fetch("https://repo.example/paper.pdf")
    assert content.startswith(b"%PDF-")
    assert url == "https://repo.example/paper.pdf"


def test_fetch_rejects_non_pdf_html() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>paywall</html>", request=request)
        )
    )
    client = OpenAccessPdfClient(http=http, clock=lambda: 10.0, sleep=lambda _: None)
    with pytest.raises(OpenAccessError) as exc_info:
        client.fetch("https://repo.example/paper.pdf")
    assert exc_info.value.reason == "bad_format"


def test_fetch_rejects_oversized_pdf() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"%PDF-" + b"x" * 20, request=request)
        )
    )
    client = OpenAccessPdfClient(
        http=http, clock=lambda: 10.0, sleep=lambda _: None, max_bytes=10
    )
    with pytest.raises(OpenAccessError) as exc_info:
        client.fetch("https://repo.example/paper.pdf")
    assert exc_info.value.reason == "response_too_large"


def test_fetch_rejects_non_https_url_before_request() -> None:
    client = OpenAccessPdfClient(clock=lambda: 10.0, sleep=lambda _: None)
    with pytest.raises(OpenAccessError, match="HTTPS"):
        client.fetch("http://repo.example/paper.pdf")
