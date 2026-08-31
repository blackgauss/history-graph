"""Semantic Scholar citation-context client (no key required).

Answers "does B cite A as background or as a method it builds on?" — metadata
OpenAlex lacks. Context snippets exist only for part of the corpus; absence is
reported as such, never guessed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from .http import (
    CanNotTell,
    bad_format_error,
    http_status_outcome,
    retry_transient,
)
from .observability import instrument

BASE_URL = "https://api.semanticscholar.org/graph/v1"

_BUILD_MARKERS = (
    "build on", "built on", "extend", "following", "introduced in", "proposed in",
    "as described in", "based on", "using the method", "adapt",
)
_BACKGROUND_MARKERS = ("see also", "for example", "general background", "early work", "e.g.")

_INTENTS = {"reference", "method", "used", "background", "extension", "comparison", "motivation"}


def classify_context(context: str | None, intents: list[str] | None) -> str:
    """Keyword heuristic; S2 'intents' is often empty, so text is the signal."""
    for intent in intents or []:
        if intent in _INTENTS and intent != "reference":
            return intent
    text = (context or "").lower()
    if any(m in text for m in _BUILD_MARKERS):
        return "builds-upon"
    if any(m in text for m in _BACKGROUND_MARKERS):
        return "background"
    return "reference" if not text else "mention"


class SemanticScholarError(CanNotTell):
    """Raised when the S2 Graph API responds in an unexpected shape."""

    reason = "server_error"


class SemanticScholarClient:
    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = 0.6,  # unauthenticated tier is strict
    ) -> None:
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)
        self._clock = clock
        self._sleep = sleep
        self._min_interval = min_interval_s
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        wait = self._min_interval - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    @retry_transient
    def _fetch(self, path: str, params: dict[str, Any]) -> httpx.Response:
        with instrument("http.semanticscholar"):
            self._throttle()
            response = self._http.get(path, params=params)
        if response.status_code == 429:
            raise httpx.HTTPStatusError("429", request=response.request, response=response)
        if response.status_code != 404:
            response.raise_for_status()
        return response

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._fetch(path, params)
        except httpx.HTTPError as exc:
            raise http_status_outcome(SemanticScholarError, exc) from exc
        if response.status_code == 404:
            return {"__status__": 404}
        try:
            payload = response.json()
        except ValueError as exc:
            raise bad_format_error(SemanticScholarError, path) from exc
        if not isinstance(payload, dict):
            raise SemanticScholarError(f"expected object from {path}")
        return payload

    _PREFIXED = ("doi:", "arxiv:", "corpusid:", "pmid:")

    @staticmethod
    def to_s2_id(ref: str) -> str:
        ref = ref.strip()
        if ref.lower().startswith(("https://doi.org/", "doi:")):
            ref = ref.split("doi.org/", 1)[-1]
            return ref if ref.lower().startswith(SemanticScholarClient._PREFIXED) else f"doi:{ref}"
        if ref.startswith("10."):
            return f"doi:{ref}"
        return ref  # assume it is already an S2-prefixed id

    def citation_context(
        self, citing_ref: str, cited_ref: str, *, max_pages: int = 10
    ) -> dict[str, Any] | None:
        """Snippet where `citing` cites `cited`, plus heuristic intent."""
        citing, cited = self.to_s2_id(citing_ref), self.to_s2_id(cited_ref)
        cited_norm = cited_ref.lower().removeprefix("https://doi.org/")
        offset = 0
        for _ in range(max_pages):
            page = self._get(
                f"paper/{citing}/citations",
                {
                    "fields": "citedPaper.paperId,citedPaper.title,citedPaper.externalIds,"
                    "context,intents,isInText",
                    "limit": 100,
                    "offset": offset,
                },
            )
            if page.get("__status__") == 404:
                return None
            rows = page.get("data")
            if not isinstance(rows, list):
                raise SemanticScholarError(f"unexpected citations payload from {citing}")
            for row in rows:
                external = ((row.get("citedPaper") or {}).get("externalIds") or {})
                doi = str(external.get("DOI") or "").lower()
                hit = (
                    doi == cited_norm
                    or (row.get("citedPaper") or {}).get("paperId") == cited.split(":")[-1]
                )
                if hit:
                    return {
                        "context": row.get("context"),
                        "intents": row.get("intents") or [],
                        "in_text": row.get("isInText"),
                        "intent": classify_context(row.get("context"), row.get("intents")),
                    }
            if not rows or page.get("next") is None:
                break
            offset += 100
        return {"intent": "reference", "context": None, "note": "no snippet (S2 context is sparse)"}


def default_client() -> SemanticScholarClient:
    return SemanticScholarClient()
