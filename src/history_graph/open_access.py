"""Retrieve full text from publicly accessible scholarly PDF locations."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .http import CanNotTell, retry_transient
from .observability import instrument

MAX_PDF_BYTES = 50_000_000


class OpenAccessError(CanNotTell):
    """An OpenAlex-listed public PDF could not be safely retrieved."""

    reason = "unavailable"


def pdf_locations(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deduplicated OpenAlex-listed OA PDF sources, best location first."""
    candidates = [work.get("best_oa_location")]
    candidates.extend(
        location
        for location in work.get("locations", [])
        if isinstance(location, dict) and location.get("is_oa") is True
    )
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for location in candidates:
        if not isinstance(location, dict):
            continue
        url = location.get("pdf_url")
        if not isinstance(url, str) or url in seen:
            continue
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            continue
        seen.add(url)
        source_obj = location.get("source")
        found.append(
            {
                "pdf_url": url,
                "landing_page_url": location.get("landing_page_url"),
                "license": location.get("license"),
                "source": (
                    source_obj.get("display_name") if isinstance(source_obj, dict) else None
                ),
            }
        )
    return found


class OpenAccessPdfClient:
    """Small bounded downloader for HTTPS PDFs listed by OpenAlex as open access."""

    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_bytes: int = MAX_PDF_BYTES,
    ) -> None:
        self._http = http or httpx.Client(
            timeout=45.0,
            follow_redirects=True,
            headers={"User-Agent": "history-graph/0.1 (open-access full text)"},
        )
        self._clock = clock
        self._sleep = sleep
        self._last_request = 0.0
        self._max_bytes = max_bytes

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        wait = 0.25 - (self._clock() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    @retry_transient
    def _get_content(self, url: str) -> tuple[bytes, str]:
        self._throttle()
        with instrument("http.open_access_pdf"):
            with self._http.stream("GET", url, headers={"Accept": "application/pdf"}) as response:
                if response.status_code >= 400:
                    response.read()
                    response.raise_for_status()
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > self._max_bytes:
                    error = OpenAccessError("PDF exceeded the configured download size limit")
                    error.reason = "response_too_large"
                    raise error
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_bytes:
                        error = OpenAccessError("PDF exceeded the configured download size limit")
                        error.reason = "response_too_large"
                        raise error
                return bytes(content), str(response.url)

    def fetch(self, url: str) -> tuple[bytes, str]:
        """Return validated PDF bytes and final URL, rejecting HTML/error pages."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise OpenAccessError("only absolute HTTPS PDF URLs are accepted")
        try:
            content, final_url = self._get_content(url)
        except OpenAccessError:
            raise
        except httpx.HTTPError as exc:
            error = OpenAccessError(f"PDF host request failed: {type(exc).__name__}")
            error.reason = "transport_error"
            raise error from exc
        if urlsplit(final_url).scheme != "https":
            raise OpenAccessError("redirected PDF URL must remain HTTPS")
        if not content.startswith(b"%PDF-"):
            error = OpenAccessError("OpenAlex PDF URL did not return a PDF document")
            error.reason = "bad_format"
            raise error
        return content, final_url
