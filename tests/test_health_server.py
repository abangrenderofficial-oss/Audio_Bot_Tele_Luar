import json
from http.client import HTTPConnection
from threading import Thread
from http.server import ThreadingHTTPServer

import pytest

import health_server


def test_pot_bridge_requires_matching_token(monkeypatch):
    monkeypatch.setenv("POT_BRIDGE_TOKEN", "bridge-secret")

    assert health_server._pot_bridge_authorized("/get_pot?key=bridge-secret") is True
    assert health_server._pot_bridge_authorized("/get_pot?key=wrong") is False
    assert health_server._pot_bridge_authorized("/get_pot") is False


def test_fetch_local_pot_posts_empty_json(monkeypatch):
    expected = {
        "poToken": "p" * 180,
        "contentBinding": "visitor-data",
        "expiresAt": 123456,
    }
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(expected).encode("utf-8")

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["data"] = request.data
        seen["content_type"] = request.get_header("Content-type")
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(health_server, "urlopen", fake_urlopen)

    payload = health_server._fetch_local_pot()

    assert json.loads(payload) == expected
    assert seen == {
        "url": "http://127.0.0.1:4416/get_pot",
        "data": b"{}",
        "content_type": "application/json",
        "timeout": 30,
    }


@pytest.mark.parametrize("path", ["/", "/health", "/ready", "/health?probe=uptimerobot"])
def test_health_endpoint_supports_head_and_get(path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), health_server.HealthHandler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        for method in ("HEAD", "GET"):
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read()
            assert response.status == 200
            assert response.getheader("Content-Type") == "application/json"
            assert int(response.getheader("Content-Length")) > 0
            if method == "HEAD":
                assert body == b""
            else:
                assert json.loads(body)["ok"] is True
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_unknown_head_path_still_returns_404():
    server = ThreadingHTTPServer(("127.0.0.1", 0), health_server.HealthHandler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("HEAD", "/missing")
        response = connection.getresponse()
        assert response.status == 404
        assert response.read() == b""
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
