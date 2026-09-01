"""Thread resolution tests against a mocked OpenAlex API."""

from __future__ import annotations

import json
from pathlib import Path

import conftest
import httpx

from history_graph.client import BASE_URL, OpenAlexClient
from history_graph.thread import run_thread

WORK_KNOWN = {
    "id": "https://openalex.org/W111",
    "doi": "https://doi.org/10.1/known",
    "title": "Known Work",
    "publication_year": 1948,
    "type": "article",
    "cited_by_count": 90000,
    "referenced_works": ["https://openalex.org/W222"],
    "authorships": [],
}
WORK_BY_TITLE = {
    "id": "https://openalex.org/W222",
    "doi": None,
    "title": "The Missing Paper Title",
    "publication_year": 2003,
    "type": "article",
    "cited_by_count": 3000,
    "referenced_works": ["https://openalex.org/W111"],
    "authorships": [],
}

SEEDS = """
entries:
  - id: known-paper
    date: "1948"
    kind: paper
    title: Known Work
    refs: {doi: "10.1/known"}
  - id: doiless-paper
    date: "2003"
    kind: paper
    title: Missing Paper
    refs: {title_search: "The Missing Paper Title"}
  - id: lost-paper
    date: "1900"
    kind: paper
    title: Lost
    refs: {doi: "10.1/gone", title_search: "Never Published Anywhere"}
  - id: some-patent
    date: "1876"
    kind: patent
    title: A Patent
"""


def test_run_thread_resolves_doi_title_fallback_and_unresolved(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        filter_value = request.url.params.get("filter", "")
        if request.url.path == "/works/doi:10.1/known":  # free singleton
            return httpx.Response(200, json=WORK_KNOWN)
        if request.url.path.startswith("/works/doi:"):
            return httpx.Response(404, json={})
        if filter_value == "title.search:The Missing Paper Title":
            return httpx.Response(200, json=conftest.works_page([WORK_BY_TITLE]))
        return httpx.Response(200, json=conftest.works_page([]))

    http_client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    client = OpenAlexClient(mailto="test@example.org", http=http_client)

    seeds = tmp_path / "thread.yaml"
    seeds.write_text(SEEDS, encoding="utf-8")
    raw_dir = tmp_path / "thread"

    manifest = run_thread(client, seeds, raw_dir)

    assert manifest["entries"] == 4
    assert manifest["papers_resolved"] == 2
    assert manifest["papers_unresolved"] == 1
    assert manifest["unresolved_ids"] == ["lost-paper"]

    papers = [json.loads(line) for line in (raw_dir / "papers.jsonl").read_text().splitlines()]
    by_entry = {record["id"]: record for record in papers}
    assert by_entry["known-paper"]["resolved_via"] == "doi"
    assert by_entry["doiless-paper"]["resolved_via"] == "title"
    assert by_entry["known-paper"]["openalex_id"].endswith("W111")

    events = [json.loads(line) for line in (raw_dir / "events.jsonl").read_text().splitlines()]
    assert len(events) == 4
    assert {event["year"] for event in events} == {1948, 2003, 1900, 1876}
    assert {event["id"] for event in events} == {
        "known-paper",
        "doiless-paper",
        "lost-paper",
        "some-patent",
    }

    on_disk = json.loads((raw_dir / "manifest.json").read_text())
    assert on_disk["outputs"]["events.jsonl"] == 4
