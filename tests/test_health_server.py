import json

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
