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


# ------------------------------------------------------------- patent_links fallback


def test_patent_links_falls_back_to_openalex_when_walled(monkeypatch):
    from history_graph import mcp_server
    from history_graph.uspto import PatentCaptchaError

    class Walled:
        def citing(self, pn, page=0):
            raise PatentCaptchaError("google bot wall")

        def scholar(self, pn, kind):
            raise PatentCaptchaError("google bot wall")

    class OA:
        def paginate(self, path, params, *, select=None, per_page=200, max_pages=None):
            return iter([] if "US9999" in params["filter"] else [{
                "id": "W1983129989", "doi": "https://patents.google.com/patent/US2466157",
                "title": "Delay means for electric digital computers", "publication_year": 1948,
            }])

        def iter_citing_works(self, wid, *, select=None, max_pages=None):
            assert wid == "https://openalex.org/W1983129989" or wid == "W1983129989"
            return iter([{"id": "W2073840571", "title": "A mathematical theory of communication",
                          "publication_year": 1948}])

    monkeypatch.setattr(mcp_server, "_clients", {"patents": Walled(), "openalex": OA()})
    import json

    out = json.loads(mcp_server.patent_links("US2466157A"))
    assert out["cited_by_patents"] is None and "google_note" in out
    assert out["fallback"]["source"] == "openalex"
    assert out["fallback"]["cited_by_papers"][0]["openalex_id"] == "W2073840571"

    missing = json.loads(mcp_server.patent_links("US9999999A"))
    assert "not indexed" in missing["fallback"]["reason"]
