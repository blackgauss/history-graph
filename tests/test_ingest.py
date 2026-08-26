"""Unit tests for the ingestion flow, run against a mocked OpenAlex API."""

from __future__ import annotations

import json
from pathlib import Path

import conftest
import httpx

from history_graph.client import BASE_URL, OpenAlexClient
from history_graph.ingest import load_dois, run_ingest


def _seed_work(work_id: str, doi: str, refs: list[str]) -> dict:
    return {
        "id": work_id,
        "doi": f"https://doi.org/{doi}",
        "title": f"Title of {work_id}",
        "publication_year": 2020,
        "type": "article",
        "cited_by_count": 5,
        "referenced_works": refs,
        "authorships": [
            {
                "author": {"id": f"A_{work_id}", "display_name": f"Author {work_id}"},
                "institutions": [{"id": f"I_{work_id}", "country_code": "US"}],
            }
        ],
    }


SEED_DOIS = ["10.9999/fake-a", "10.9999/fake-b"]
WORKS_BY_DOI = {
    SEED_DOIS[0]: _seed_work("WA", SEED_DOIS[0], refs=["W100"]),
    SEED_DOIS[1]: _seed_work("WB", SEED_DOIS[1], refs=[]),
}
CITING_WORKS = [
    _seed_work("WC1", "10.9999/citing-1", refs=["W100"]),
    _seed_work("WC2", "10.9999/citing-2", refs=[]),
]
HYDRATED = [_seed_work("W100", "10.9999/hydrated", refs=[])]


def test_load_dois_strips_comments_and_blanks(tmp_path: Path) -> None:
    seeds = tmp_path / "dois.txt"
    seeds.write_text(
        "\n# comment\n10.1/a\n\n  10.2/b  \n# another\n10.3/c\n",
        encoding="utf-8",
    )

    assert load_dois(seeds) == ["10.1/a", "10.2/b", "10.3/c"]


def test_run_ingest_end_to_end(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        filter_value = params.get("filter", "")
        if filter_value.startswith("doi:"):
            work = WORKS_BY_DOI.get(filter_value[4:])
            return httpx.Response(200, json=conftest.works_page([work] if work else []))
        if filter_value.startswith("cites:"):
            seed_id = filter_value[len("cites:") :]
            citing = {"WA": CITING_WORKS[:1], "WB": CITING_WORKS[1:]}.get(seed_id, [])
            return httpx.Response(200, json=conftest.works_page(citing))
        if filter_value.startswith("openalex_id:"):
            requested = set(filter_value[len("openalex_id:") :].split("|"))
            matches = [w for w in HYDRATED if w["id"] in requested]
            return httpx.Response(200, json=conftest.works_page(matches))
        raise AssertionError(f"Unexpected filter {filter_value!r}")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url=BASE_URL, transport=transport)
    client = OpenAlexClient(mailto="test@example.org", http=http_client)

    seeds_path = tmp_path / "dois.txt"
    seeds_path.write_text("\n".join(SEED_DOIS), encoding="utf-8")
    raw_dir = tmp_path / "raw"

    manifest = run_ingest(client, seeds_path, raw_dir)

    assert manifest["seeds_requested"] == 2
    assert manifest["seeds_resolved"] == 2
    assert manifest["citing_works_fetched"] == 2
    assert manifest["distinct_referenced_work_ids"] == 1
    assert manifest["references_hydrated"] == 1
    assert manifest["outputs"] == {
        "works.jsonl": 2,
        "cited_by.jsonl": 2,
        "references.jsonl": 1,
    }

    works_lines = (raw_dir / "works.jsonl").read_text(encoding="utf-8").splitlines()
    cited_by_lines = (raw_dir / "cited_by.jsonl").read_text(encoding="utf-8").splitlines()
    references_lines = (raw_dir / "references.jsonl").read_text(encoding="utf-8").splitlines()

    assert [json.loads(line)["id"] for line in works_lines] == ["WA", "WB"]
    assert {json.loads(line)["id"] for line in cited_by_lines} == {"WC1", "WC2"}
    assert [json.loads(line)["id"] for line in references_lines] == ["W100"]

    on_disk = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["references_hydrated"] == 1
