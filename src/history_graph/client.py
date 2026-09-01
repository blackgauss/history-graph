"""Thin keyless HTTP client for the OpenAlex API.

Joins the polite pool via a ``mailto`` parameter, throttles to stay under
10 req/s, retries transient failures with exponential backoff, and exposes
cursor-based pagination.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from .http import CanNotTell, bad_format_error, http_status_outcome, retry_transient
from .observability import instrument

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
    "related_works",
    "authorships",
    "funders",
    "ids",
    "counts_by_year",
    "primary_location",
    "best_oa_location",
    "open_access",
    "abstract_inverted_index",
)

class OpenAlexError(CanNotTell):
    """Raised when the API responds in an unexpected shape."""

    reason = "server_error"




class OpenAlexClient:
    """Minimal OpenAlex client suitable for batch ingestion."""

    def __init__(
        self,
        mailto: str | None = None,
        api_key: str | None = None,
        *,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._mailto = mailto
        self._api_key = api_key
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)
        self._clock = clock
        self._sleep = sleep
        self._last_request_monotonic = 0.0

    def close(self) -> None:
        self._http.close()

    # -- low level -----------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = self._clock() - self._last_request_monotonic
        wait = MIN_REQUEST_INTERVAL_S - elapsed
        if wait > 0:
            self._sleep(wait)
        self._last_request_monotonic = self._clock()

    @retry_transient
    def _fetch(self, path: str, params: Mapping[str, Any]) -> httpx.Response:
        merged: dict[str, Any] = {"mailto": self._mailto} if self._mailto else {}
        if self._api_key:
            merged["api_key"] = self._api_key
        merged.update(params)
        with instrument("http.openalex"):
            self._throttle()
            response = self._http.get(path, params=merged)
        response.raise_for_status()
        return response

    def _request(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._fetch(path, params)
        except httpx.HTTPError as exc:  # status + transport: never raw, always typed
            raise http_status_outcome(OpenAlexError, exc) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise bad_format_error(OpenAlexError, path) from exc
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

    def get_work(
        self,
        ref: str,
        *,
        select: Sequence[str] | None = None,
        corpus: str = "all",
    ) -> dict[str, Any] | None:
        """Singleton by-id / by-doi read: free with an api_key, corpus=all so
        expansion-corpus works are never mistaken for absent, 404 = proven death."""
        ref = ref.strip()
        try:
            response = self._fetch(f"{WORKS_PATH}/{quote(ref, safe=':/')}", {"corpus": corpus})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise http_status_outcome(OpenAlexError, exc) from exc
        except httpx.HTTPError as exc:
            raise http_status_outcome(OpenAlexError, exc) from exc
        try:
            record = response.json()
        except ValueError as exc:
            raise bad_format_error(OpenAlexError, ref) from exc
        if not isinstance(record, dict) or ("results" in record and "meta" in record):
            raise OpenAlexError(f"Expected work object for {ref}, got page/other shape")
        if select:
            fields = set(select)
            record = {k: v for k, v in record.items() if k in fields}
        return record

    def get_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Resolve a bare DOI (e.g. ``10.1038/s41586-021-03819-2``) to a work."""
        return self.get_work(f"doi:{doi.strip().lower()}", select=WORK_FIELDS)

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
        """Fetch works as free singletons; dead ids vanish, like the old batch."""
        for work_id in list(work_ids):
            found = self.get_work(work_id, select=select)
            if found is not None:
                yield found

    def _get_works_batch_legacy(
        self,
        work_ids: list[str],
        *,
        select: Sequence[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
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
        per_page: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield works that cite ``work_id`` (e.g. ``W2741809807``)."""
        return self.paginate(
            WORKS_PATH,
            {"filter": f"cites:{work_id}"},
            select=select or WORK_FIELDS,
            max_pages=max_pages,
            per_page=per_page or PER_PAGE_MAX,
        )
