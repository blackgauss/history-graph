"""Sci-Hub client: DOI -> PDF bytes via public mirrors.

For each mirror we fetch ``{mirror}/{doi}``, extract the embedded PDF link
from the landing page, then download the PDF with the landing page as
referer. Mirrors that 404 or fail to expose a usable PDF are skipped in
favour of the next one. Throttled and retried like the OpenAlex client.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence

import httpx

from .http import CanNotTell, retry_mirrors
from .observability import instrument

logger = logging.getLogger(__name__)

MIRRORS = ("https://sci-hub.se", "https://sci-hub.st", "https://sci-hub.ru")

MIN_REQUEST_INTERVAL_S = 15.0  # sci-hub tolerates ~3 req/min before captcha walls
PDF_MAGIC = b"%PDF"

# sci-hub mirrors are hostile to library default agents; pose as a browser
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

_PDF_PATTERNS = (
    re.compile(r"""<(?:iframe|embed)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']*\.pdf[^"']*)["']""", re.IGNORECASE),
    re.compile(r"""location\.href\s*=\s*["']([^"']+)["']"""),
)

class SciHubError(CanNotTell):
    """Raised when no mirror yields a PDF for a DOI (coverage shifts; retry later)."""

    reason = "unavailable"




def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content.startswith(PDF_MAGIC) or "pdf" in content_type.lower()


def find_pdf_url(html: str, base_url: httpx.URL) -> str | None:
    """Extract the embedded PDF URL from a sci-hub landing page.

    Collects every candidate link and prefers ones that look like PDFs, so
    captcha iframes and JS redirects do not shadow the real download link.
    """
    candidates: list[str] = []
    for pattern in _PDF_PATTERNS:
        groups = (m.group(1) for m in pattern.finditer(html))
        candidates.extend(c for c in groups if not c.startswith("#"))
    scored = [c for c in candidates if ".pdf" in c.lower() or "/pdf" in c.lower()]
    chosen = scored[0] if scored else (candidates[0] if candidates else None)
    if chosen is None:
        return None
    return str(base_url.join(chosen))


class SciHubClient:
    """Minimal Sci-Hub client suitable for batch PDF downloads."""

    def __init__(
        self,
        mirrors: Sequence[str] | None = None,
        *,
        http: httpx.Client | None = None,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._mirrors = tuple(mirrors) if mirrors else MIRRORS
        self._http = http or httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_request_monotonic = 0.0

    def close(self) -> None:
        self._http.close()

    # -- low level -----------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = self._clock() - self._last_request_monotonic
        wait = self._min_interval_s - elapsed
        if wait > 0:
            self._sleep(wait)
        self._last_request_monotonic = self._clock()

    @retry_mirrors
    def _get(self, url: str, *, referer: str | None = None) -> httpx.Response:
        headers = {"Referer": referer} if referer else {}
        with instrument("http.scihub"):
            self._throttle()
            response = self._http.get(str(url), headers=headers)
            if response.status_code == 429 and (
                retry_after := response.headers.get("retry-after")
            ):
                try:
                    self._sleep(min(float(retry_after), 60.0))
                except ValueError:
                    pass
            response.raise_for_status()
        return response

    # -- public api ----------------------------------------------------------

    def get_pdf(self, doi: str) -> bytes:
        """Download the PDF for a bare DOI; tries every mirror in order."""
        doi = doi.strip()
        problems: list[str] = []
        for mirror in self._mirrors:
            try:
                pdf = self._fetch_from_mirror(mirror, doi)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    problems.append(f"{mirror}: 404")
                    continue
                raise
            except httpx.RequestError as exc:
                problems.append(f"{mirror}: {exc.__class__.__name__}: {exc}")
                continue
            except SciHubError as exc:
                problems.append(f"{mirror}: {exc}")
                continue
            if pdf is not None:
                logger.info("Fetched PDF for %s from %s", doi, mirror)
                return pdf
            problems.append(f"{mirror}: no PDF link on landing page")
        raise SciHubError(f"No PDF for {doi} ({'; '.join(problems)})")

    def _fetch_from_mirror(self, mirror: str, doi: str) -> bytes | None:
        page = self._get(f"{mirror}/{doi}")
        pdf_url = find_pdf_url(page.text, page.url)
        if pdf_url is None:
            return None
        response = self._get(pdf_url, referer=str(page.url))
        content = response.content
        if not _looks_like_pdf(content, response.headers.get("content-type", "")):
            raise SciHubError(f"link at {pdf_url} is not a PDF")
        return content
