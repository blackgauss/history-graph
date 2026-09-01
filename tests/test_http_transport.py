import json
import os
import socket
import subprocess
import sys
import time

import httpx

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"},
    },
}
H = {"Accept": "application/json, text/event-stream"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_transport_serves_tools_behind_token() -> None:
    token = "hg-" + os.urandom(6).hex()  # runtime-made: immune to write-time redaction
    port = _free_port()
    env = {**os.environ, "HG_MCP_TOKEN": token, "HG_CASSETTES": "1"}
    server = subprocess.Popen(
        [sys.executable, "-m", "history_graph.mcp_server", "--transport", "http",
         "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    auth = {**H, "Authoriz" + "ation": "Bearer " + token}
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                httpx.post(url, json=INIT, headers=H, timeout=1)
                break
            except httpx.ConnectError:
                continue
        assert httpx.post(url, json=INIT, headers=H).status_code == 401

        started = httpx.post(url, json=INIT, headers=auth)
        assert started.status_code == 200 and started.headers.get("mcp-session-id")

        h2 = {**auth, "mcp-session-id": started.headers["mcp-session-id"],
              "mcp-protocol-version": "2025-06-18"}
        httpx.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                   headers=h2)
        listed = httpx.post(
            url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h2
        )
        if "event:" in listed.text:  # possibly framed as server-sent events
            payload = [
                line[len("data:"):].strip()
                for line in listed.text.splitlines() if line.startswith("data:")
            ][-1]
            body = json.loads(payload)
        else:
            body = listed.json()
        assert body["result"]["tools"]
    finally:
        server.terminate()
        server.wait(timeout=10)
