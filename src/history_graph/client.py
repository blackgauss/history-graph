"""Thin keyless HTTP client for the OpenAlex API.

Joins the polite pool via a ``mailto`` parameter, throttles to stay under
10 req/s, retries transient failures with exponential backoff, and exposes
cursor-based pagination.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

BASE_URL = "https://api.openalex.org"
WORKS_PATH = "/works"

BATCH_SIZE = 50  # max ids per pipe-joined openalex_id filter
PER_PAGE_MAX = 200
MIN_REQUEST_INTERVAL_S = 0.12  # ~8 req/s, safely under the polite pool limit

WORK_FIELDS = (
    "id",
    "doi",
    "title",
    "publication_year",
    "type",
    "cited_by_count",
    "referenced_works",
    "authorships",
)

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class OpenAlexError(RuntimeError):
    """Raised when the API responds in an unexpected shape."""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _TRANSIENT_STATUS


class OpenAlexClient:
    """Minimal OpenAlex client suitable for batch ingestion."""

    def __init__(
        self,
        mailto: str | None = None,
        *,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._mailto = mailto
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)
        self._last_request_monotonic = 0.0

    def close(self) -> None:
        self._http.close()

    # -- low level -----------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_monotonic
        wait = MIN_REQUEST_INTERVAL_S - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_monotonic = time.monotonic()

    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=0.5, max=8),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {"mailto": self._mailto} if self._mailto else {}
        merged.update(params)
        self._throttle()
        response = self._http.get(path, params=merged)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise OpenAlexError(f"Expected JSON object from {path}, got {type(payload)!r}")
        return payload

    # -- public api ----------------------------------------------------------

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        per_page: int = PER_PAGE_MAX,
        select: Sequence[str] | None = None,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield result records across all pages using cursor pagination."""
        cursor: str | None = "*"
        pages = 0
        base_params = dict(params or {})
        if select:
            base_params["select"] = ",".join(select)
        while cursor:
            page = self._request(path, {**base_params, "per-page": per_page, "cursor": cursor})
            meta = page.get("meta")
            if not isinstance(meta, dict):
                raise OpenAlexError(f"Missing 'meta' in paged response from {path}")
            yield from page.get("results", [])
            cursor = meta.get("next_cursor")
            pages += 1
            if max_pages is not None and pages >= max_pages:
                break

    def get_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Resolve a bare DOI (e.g. ``10.1038/s41586-021-03819-2``) to a work."""
        results = self.paginate(
            WORKS_PATH,
            {"filter": f"doi:{doi.strip().lower()}"},
            select=WORK_FIELDS,
            per_page=BATCH_SIZE,
        )
        return next(iter(results), None)

    def get_work_by_title(self, title: str) -> dict[str, Any] | None:
        """Best-effort resolution via ``title.search``; for works without DOIs."""
        results = self.paginate(
            WORKS_PATH,
            {"filter": "title.search:" + title},
            select=WORK_FIELDS,
            per_page=BATCH_SIZE,
        )
        return next(iter(results), None)

    def get_works(
        self,
        work_ids: Sequence[str],
        *,
        select: Sequence[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Fetch works in batches via the pipe-joined ``openalex_id`` filter."""
        for start in range(0, len(work_ids), BATCH_SIZE):
            chunk = work_ids[start : start + BATCH_SIZE]
            yield from self.paginate(
                WORKS_PATH,
                {"filter": "openalex_id:" + "|".join(chunk)},
                select=select or WORK_FIELDS,
            )

    def iter_citing_works(
        self,
        work_id: str,
        *,
        select: Sequence[str] | None = None,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield works that cite ``work_id`` (e.g. ``W2741809807``)."""
        return self.paginate(
            WORKS_PATH,
            {"filter": f"cites:{work_id}"},
            select=select or WORK_FIELDS,
            max_pages=max_pages,
        )
