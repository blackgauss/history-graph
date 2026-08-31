"""Offline tests for the Google Patents client."""

import httpx
import pytest

from history_graph import uspto


def client_for(handler):
    http = httpx.Client(base_url=uspto.BASE_URL, transport=httpx.MockTransport(handler))
    return uspto.PatentSearchClient(http=http, sleep=lambda s: None, clock=lambda: 0.0)


def payload(rows, total=2):
    return httpx.Response(
        200,
        json={
            "results": {
                "total_num_results": total,
                "cluster": [{"result": rows}],
            }
        },
    )


ROW = {
    "id": "patent/US2688403A/en",
    "patent": {
        "title": " Electronic calculating device using sound delay",
        "inventor": "Jr John Presper Eckert",
        "assignee": "Eckert",
        "priority_date": "1946-01-15",
        "grant_date": "1954-06-29",
    },
}


def test_search_parses_rows_and_builds_query():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return payload([ROW])

    out = client_for(handler).search("stored program", after=1945, before=1955, limit=5)
    assert out["total"] == 2
    assert out["patents"][0]["publication_number"] == "US2688403A"
    assert "after=priority:19450101" in seen["url"]
    assert "before=priority:19551231" in seen["url"]


def test_citing_query_uses_publication_number():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return payload([{"id": "patent/US3120606A/en", "patent": {"title": "later"}}])

    out = client_for(handler).citing("US2688403A")
    assert seen["url"].startswith("citing=US2688403A")
    assert out["patents"][0]["publication_number"] == "US3120606A"


def test_captcha_is_its_own_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>Sorry...</title>")

    with pytest.raises(uspto.PatentCaptchaError):
        client_for(handler).search("anything")


def test_scholar_parses_best_effort_and_sends_kind():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"cluster": [{"result": [
                        {"patent": {"title": "Theory of coding", "pub_year": 1949,
                                    "maintainer": "C. E. Shannon", "assignee": "Bell Labs"}},
                    ]}]}
                ]
            },
        )

    out = client_for(handler).scholar("US2688403A", "forward")
    assert "kind=forward" in seen["url"]
    assert out["works"] == [
        {"title": "Theory of coding", "year": 1949,
         "authors": "C. E. Shannon", "venue": "Bell Labs"}
    ]
