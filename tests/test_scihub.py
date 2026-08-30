"""Unit tests for the Sci-Hub client: PDF extraction and mirror fallback."""

from __future__ import annotations

import httpx
import pytest

from history_graph.scihub import SciHubClient, SciHubError, find_pdf_url

PDF = b"%PDF-1.5 fake pdf bytes"
DOI = "10.1234/abcd"

MIRRORS = ("https://hub-a.test", "https://hub-b.test")


def make_client(handler, mirrors=MIRRORS) -> SciHubClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, follow_redirects=True)
    return SciHubClient(mirrors=mirrors, http=http_client, min_interval_s=0)


def landing_page(pdf_path: str) -> httpx.Response:
    html = f'<html><body><iframe src="{pdf_path}"></iframe></body></html>'
    return httpx.Response(200, html=html)


def test_get_pdf_downloads_iframe_target() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == f"/{DOI}":
            return landing_page("/downloads/abc/prefix.10.1234.abcd.pdf")
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    client = make_client(handler)
    assert client.get_pdf(DOI) == PDF
    assert seen[0] == f"https://hub-a.test/{DOI}"
    assert seen[1].endswith("prefix.10.1234.abcd.pdf")


def test_get_pdf_resolves_relative_pdf_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{DOI}":
            return landing_page("abc/paper.pdf")
        assert str(request.url) == "https://hub-a.test/10.1234/abc/paper.pdf"
        return httpx.Response(200, content=PDF)

    client = make_client(handler)
    assert client.get_pdf(DOI) == PDF


def test_get_pdf_sends_referer_from_landing_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{DOI}":
            return landing_page("/paper.pdf")
        assert request.headers["referer"] == f"https://hub-a.test/{DOI}"
        return httpx.Response(200, content=PDF)

    client = make_client(handler)
    assert client.get_pdf(DOI) == PDF


def test_get_pdf_falls_back_to_next_mirror_without_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hub-a.test" and request.url.path == f"/{DOI}":
            return httpx.Response(200, html="<html>captcha</html>")
        if request.url.path == f"/{DOI}":
            return landing_page("/paper.pdf")
        return httpx.Response(200, content=PDF)

    client = make_client(handler)
    assert client.get_pdf(DOI) == PDF


def test_get_pdf_falls_back_when_link_is_not_a_pdf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{DOI}":
            return landing_page("/paper.pdf")
        if request.url.host == "hub-a.test":
            return httpx.Response(200, text="not a pdf")
        return httpx.Response(200, content=PDF)

    client = make_client(handler)
    assert client.get_pdf(DOI) == PDF


def test_get_pdf_raises_when_all_mirrors_404() -> None:
    client = make_client(lambda request: httpx.Response(404))
    with pytest.raises(SciHubError):
        client.get_pdf(DOI)


def test_connect_errors_are_not_retried_per_mirror() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("name or service not known", request=request)

    client = make_client(handler)
    with pytest.raises(SciHubError):
        client.get_pdf(DOI)

    assert calls == len(MIRRORS)  # one attempt per mirror, no retry storm


def test_get_pdf_honours_retry_after_on_429() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        if request.url.path == f"/{DOI}":
            return landing_page("/paper.pdf")
        return httpx.Response(200, content=PDF)

    client = make_client(handler)
    assert client.get_pdf(DOI) == PDF


def test_default_mirrors_start_with_sci_hub_se() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == f"/{DOI}":
            return landing_page("/paper.pdf")
        return httpx.Response(200, content=PDF)

    client = SciHubClient(
        http=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        min_interval_s=0,
    )
    assert client.get_pdf(DOI) == PDF
    assert seen[0].startswith("https://sci-hub.se/")


def test_find_pdf_url_ignores_anchor_links() -> None:
    html = """
    <html><body>
      <a href="#pdf">PDF</a>
      <iframe src="https://hub-a.test/downloads/x.pdf?q=1"></iframe>
    </body></html>
    """
    url = find_pdf_url(html, httpx.URL("https://hub-a.test/10.1/2"))
    assert url == "https://hub-a.test/downloads/x.pdf?q=1"


def test_find_pdf_url_prefers_pdf_links_over_captcha_iframes() -> None:
    html = (
        '<iframe src="/captcha/challenge"></iframe>'
        '<a href = "/storage/2024/ab/paper.pdf">PDF</a>'
    )
    url = find_pdf_url(html, httpx.URL("https://sci-hub.ru/10.1/2"))
    assert url == "https://sci-hub.ru/storage/2024/ab/paper.pdf"


def test_find_pdf_url_handles_spaced_attributes() -> None:
    html = '<a class = "more" href = "/storage/2024/ab/krizhevsky2017.pdf">PDF</a>'
    url = find_pdf_url(html, httpx.URL("https://sci-hub.ru/10.1145/3065386"))
    assert url == "https://sci-hub.ru/storage/2024/ab/krizhevsky2017.pdf"


def test_find_pdf_url_returns_none_without_pdf_links() -> None:
    assert find_pdf_url("<html><body>nope</body></html>", httpx.URL("https://x.test")) is None
