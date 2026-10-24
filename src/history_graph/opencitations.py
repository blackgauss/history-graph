"""Client for OpenCitations' DOI citation index (Index API v2)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from .http import (
    CanNotTell,
    Throttle,
    bad_format_error,
    http_status_outcome,
    retry_transient,
)
from .observability import instrument

BASE_URL = "https://api.opencitations.net/index/v2"
MAX_RESPONSE_BYTES = 8_000_000
_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")


class OpenCitationsError(CanNotTell):
    """OpenCitations could not provide a trustworthy answer."""

    reason = "server_error"


class OpenCitationsClient:
    """Read DOI references/citations, optionally authenticating with an API token."""

    def __init__(
        self,
        token: str | None = None,
        *,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        headers = {"Authorization": token} if token else {}
        self._http = http or httpx.Client(
            base_url=base_url,
            timeout=60.0,
            headers=headers,
        )
        if http is not None and token:
            self._http.headers["Authorization"] = token
        self._throttle = Throttle(1 / 3, clock=clock, sleep=sleep)
        self._max_response_bytes = max_response_bytes

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def normalize_doi(doi: str) -> str:
        value = doi.strip().lower()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break
        if not value.startswith("10.") or any(char.isspace() for char in value):
            raise ValueError("doi must be a DOI (for example, 10.1038/example)")
        return value

    @staticmethod
    def _filters(creation_after: str | None, creation_before: str | None) -> list[tuple[str, str]]:
        filters: list[tuple[str, str]] = []
        for bound, operator in ((creation_after, ">"), (creation_before, "<")):
            if bound is None:
                continue
            if not _DATE_RE.fullmatch(bound):
                raise ValueError("creation date filters must be YYYY, YYYY-MM, or YYYY-MM-DD")
            filters.append(("filter", f"creation:{operator}{bound}"))
        if creation_after and creation_before and creation_after >= creation_before:
            raise ValueError("creation_after must be earlier than creation_before")
        return filters

    @retry_transient
    def _fetch(self, path: str, params: list[tuple[str, str]]) -> bytes:
        self._throttle.wait()
        with instrument("http.opencitations"):
            with self._http.stream("GET", path, params=params) as response:
                if response.status_code >= 400:
                    response.read()
                    response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        error = OpenCitationsError(
                            "response exceeded the configured size limit; narrow the date range"
                        )
                        error.reason = "response_too_large"
                        raise error
                return bytes(body)

    def _request(self, path: str, params: list[tuple[str, str]]) -> Any:
        try:
            body = self._fetch(path, params)
        except httpx.HTTPError as exc:
            raise http_status_outcome(OpenCitationsError, exc) from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise bad_format_error(OpenCitationsError, path) from exc
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise OpenCitationsError(f"expected a list of citation records from {path}")
        return payload

    def _doi_records(
        self,
        operation: str,
        doi: str,
        *,
        creation_after: str | None = None,
        creation_before: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized = self.normalize_doi(doi)
        identifier = quote(f"doi:{normalized}", safe=":/._-()")
        params = self._filters(creation_after, creation_before)
        return self._request(f"/{operation}/{identifier}", params)

    def references(
        self,
        doi: str,
        *,
        creation_after: str | None = None,
        creation_before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return outgoing citation edges for a DOI."""
        return self._doi_records(
            "references",
            doi,
            creation_after=creation_after,
            creation_before=creation_before,
        )

    def citations(
        self,
        doi: str,
        *,
        creation_after: str | None = None,
        creation_before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return incoming citation edges for a DOI."""
        return self._doi_records(
            "citations",
            doi,
            creation_after=creation_after,
            creation_before=creation_before,
        )

    def citation_count(self, doi: str) -> int:
        """Return OpenCitations' indexed incoming-citation count for a DOI."""
        normalized = self.normalize_doi(doi)
        identifier = quote(f"doi:{normalized}", safe=":/._-()")
        payload = self._request(f"/citation-count/{identifier}", [])
        if len(payload) != 1:
            raise OpenCitationsError("expected one citation-count record")
        try:
            return int(payload[0]["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenCitationsError("invalid citation-count response") from exc


def identifiers(value: str | None) -> dict[str, str]:
    """Extract common identifiers while retaining the provider's raw value."""
    out: dict[str, str] = {"raw": value or ""}
    for token in (value or "").split():
        prefix, separator, identifier = token.partition(":")
        if not separator:
            continue
        if prefix == "doi":
            out["doi"] = identifier.lower()
        elif prefix == "openalex":
            out["openalex_id"] = f"https://openalex.org/{identifier}"
        elif prefix in {"pmid", "pmcid", "omid"}:
            out[prefix] = identifier
    return out


def compact_edge(edge: dict[str, Any]) -> dict[str, Any]:
    """Keep the citation relation and its source identifiers explicit."""
    return {
        "oci": edge.get("oci"),
        "citing": identifiers(edge.get("citing")),
        "cited": identifiers(edge.get("cited")),
        "creation": edge.get("creation"),
        "timespan": edge.get("timespan"),
        "journal_self_citation": edge.get("journal_sc"),
        "author_self_citation": edge.get("author_sc"),
    }
