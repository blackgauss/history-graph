"""Google Patents XHR client (keyless) for patent search and citations.

The ``xhr/query`` endpoints return plain JSON and need no key; the
``xhr/scholar`` endpoint (patent <-> academic-paper citations) is Google-bot
walled from datacenter IPs much of the time, so it is best-effort: a captcha
page raises :class:`PatentCaptchaError` (retry later), never a fabrication.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .observability import instrument

BASE_URL = "https://patents.google.com"


class PatentError(RuntimeError):
    """Unexpected response shape from the patents endpoints."""


class PatentCaptchaError(PatentError):
    """Google's bot wall was served; retry later (like sci-hub captcha)."""


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for cluster in (payload.get("results") or {}).get("cluster") or []:
        for row in cluster.get("result") or []:
            p = row.get("patent") or {}
            parts = str(row.get("id", "")).split("/")
            pub = parts[1] if len(parts) > 1 and parts[0] == "patent" else ""
            out.append(
                {
                    "publication_number": pub,
                    "title": (p.get("title") or "").strip(),
                    "inventor": p.get("inventor"),
                    "assignee": p.get("assignee"),
                    "priority_date": p.get("priority_date"),
                    "grant_date": p.get("grant_date"),
                    "snippet": (p.get("snippet") or "")[:240],
                }
            )
    return out


class PatentSearchClient:
    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = 1.5,
    ) -> None:
        self._http = http or httpx.Client(
            base_url=base_url,
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; history-graph)"},
        )
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

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=0.5, max=5),
        stop=stop_after_attempt(2),
        reraise=True,
    )
    def _get(self, path: str, *, url: str, referer: str | None = None) -> Any:
        with instrument("http.googlepatents"):
            self._throttle()
            response = self._http.get(
                path, params={"url": url}, headers=({"Referer": referer} if referer else None)
            )
            if "Sorry" in response.text[:400]:
                raise PatentCaptchaError("google bot wall; retry later")
            if response.status_code >= 400:
                raise PatentError(f"status {response.status_code} for {url}")
            try:
                return json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise PatentError(f"non-JSON body from {path}") from exc

    @staticmethod
    def _inner(q: str, *, after: int | None = None, before: int | None = None) -> str:
        inner = f"q={q}"
        if after:
            inner += f"&after=priority:{after}0101"
        if before:
            inner += f"&before=priority:{before}1231"
        return inner

    def search(
        self,
        q: str,
        *,
        after: int | None = None,
        before: int | None = None,
        assignee: str | None = None,
        inventor: str | None = None,
        limit: int = 20,
        page: int = 0,
    ) -> dict[str, Any]:
        inner = self._inner(q, after=after, before=before)
        if assignee:
            inner += f"&assignee={assignee}"
        if inventor:
            inner += f"&inventor={inventor}"
        if page:
            inner += f"&page={page}"
        payload = self._get("/xhr/query", url=inner)
        results = payload.get("results") or {}
        return {"total": results.get("total_num_results"), "patents": _rows(payload)[:limit]}

    def citing(self, publication_number: str, *, limit: int = 25, page: int = 0) -> dict[str, Any]:
        inner = f"citing={publication_number}" + (f"&page={page}" if page else "")
        payload = self._get("/xhr/query", url=inner)
        results = payload.get("results") or {}
        return {"total": results.get("total_num_results"), "patents": _rows(payload)[:limit]}

    def scholar(self, publication_number: str, direction: str = "forward") -> dict[str, Any]:
        """Ac <-> patent citations ('forward' = papers citing the patent).

        Captcha-prone; raises PatentCaptchaError when walled."""
        if direction not in {"forward", "backward"}:
            raise ValueError(direction)
        payload = self._get(
            "/xhr/scholar",
            url=f"id={publication_number}&kind={direction}",
            referer=f"{BASE_URL}/patent/{publication_number}/en",
        )
        rows = []
        for entry in payload.get("results") or []:
            for row in (entry.get("cluster") or [{}])[0].get("result") or []:
                p = row.get("patent") or row
                rows.append(
                    {
                        "title": (p.get("title") or "").strip(),
                        "year": p.get("pub_year") or p.get("publication_date"),
                        "authors": (p.get("maintainer") or p.get("inventor") or "").strip() or None,
                        "venue": (p.get("assignee") or p.get("maintainer") or "").strip() or None,
                    }
                )
        return {"publication_number": publication_number, "direction": direction, "works": rows}
