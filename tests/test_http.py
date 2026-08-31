"""The shared outcome contract: absence is None, ambiguity is CanNotTell."""

import httpx
import pytest

from history_graph.client import OpenAlexClient, OpenAlexError
from history_graph.http import is_probe_error
from history_graph.uspto import PatentCaptchaError, PatentSearchClient


class _Transport(httpx.BaseTransport):
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        if callable(self.response):
            return self.response(request)
        return self.response


def _client(*, status=200, body=b"{}", headers=None, base="https://oa.test"):
    def respond(request):
        return httpx.Response(status, content=body, headers=headers or {}, request=request)

    transport = _Transport(respond)
    client = OpenAlexClient(
        http=httpx.Client(base_url=base, transport=transport),
        clock=lambda: 1e9, sleep=lambda _s: None,
    )
    return client, transport


def test_persistent_rate_limit_is_typed_and_unknown_not_absent():
    budget = b'{"message": "Insufficient budget. Resets at midnight UTC."}'
    client, transport = _client(status=429, body=budget)
    with pytest.raises(OpenAlexError) as exc_info:
        list(client.paginate("/works"))
    assert transport.calls == 5  # retried, then gave up honestly
    assert exc_info.value.reason == "rate_limited"
    assert is_probe_error(exc_info.value)


def test_connect_gives_up_as_probe_error():
    transport = _Transport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("dns", request=r)))
    client = OpenAlexClient(
        http=httpx.Client(base_url="https://oa.test", transport=transport),
        clock=lambda: 1e9, sleep=lambda _s: None,
    )
    with pytest.raises(OpenAlexError) as exc_info:
        list(client.paginate("/works"))
    assert not hasattr(exc_info.value, "status_code")
    assert exc_info.value.reason == "transport_error"


def test_patent_captcha_is_not_retried_or_fabricated():
    wall = b"<html><title>Sorry!</title>we're not a robot</html>"
    transport = _Transport(lambda r: httpx.Response(200, content=wall, request=r))
    client = PatentSearchClient(http=httpx.Client(transport=transport, base_url="https://patents.google.com"))
    with pytest.raises(PatentCaptchaError) as exc_info:
        client.search("adaptive memory")
    assert transport.calls == 1  # bot wall: fail fast, never parse the apology
    assert exc_info.value.reason == "bot_walled"


def test_non_json_success_body_is_bad_format_not_data():
    client, _ = _client(status=200, body=b"<html>nope</html>")
    with pytest.raises(OpenAlexError) as exc_info:
        client._request("/works", {})
    assert exc_info.value.reason == "bad_format"


def test_env_file_seeds_without_clobbering(tmp_path):
    from history_graph.http import dotenv_defaults, parse_env_file

    env_file = tmp_path / ".env"
    env_file.write_text('A=1\nB = "two"  # trailing\n# comment\nC=\'3\'\nD=4 # real comment\n')
    env = dict(parse_env_file(env_file.read_text()))
    assert env == {"A": "1", "B": "two", "C": "3", "D": "4"}

    environ = {"A": "already-set"}
    dotenv_defaults(env_file, environ)
    assert environ == {"A": "already-set", "B": "two", "C": "3", "D": "4"}
