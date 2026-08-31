"""Harness runs inside the test suite: offline fixtures always, cassettes when recorded."""

import pytest

from history_graph import mine
from history_graph.testing import CASSETTE_DIR, openalex_client


def or_skip(name: str, *, record: bool = False):
    if not (CASSETTE_DIR / "openalex.json").exists():
        pytest.skip("no openalex cassette recorded yet")
    return openalex_client(name, record=False)


def _fixture_queries():
    import json
    from pathlib import Path

    queries = json.loads(Path("tests/insight_queries.json").read_text())
    return [q for q in queries if q.get("fixture")]


def _cassette_queries():
    import json
    from pathlib import Path

    queries = json.loads(Path("tests/insight_queries.json").read_text())
    return [q for q in queries if q.get("cassette")]


@pytest.mark.parametrize("query", _fixture_queries(), ids=lambda q: q["id"])
def test_fixture_queries(query, tmp_path):
    graded = mine.run_query(or_skip, query, tmp_path)
    assert graded["ok"], graded["fails"]


def test_cassette_query(tmp_path):
    for query in _cassette_queries():
        graded = mine.run_query(or_skip, query, tmp_path)
        assert graded["ok"], graded["fails"]
