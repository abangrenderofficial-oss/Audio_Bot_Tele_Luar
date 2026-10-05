from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


POT_PROVIDER_URL = "http://127.0.0.1:4416/get_pot"


def _pot_bridge_authorized(path: str) -> bool:
    expected = (os.getenv("POT_BRIDGE_TOKEN") or "").strip()
    if not expected:
        return False
    parsed = urlsplit(path)
    supplied = parse_qs(parsed.query).get("key", [""])[0]
    return hmac.compare_digest(supplied, expected)


def _fetch_local_pot() -> bytes:
    request = Request(
        POT_PROVIDER_URL,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        payload = response.read()
    parsed = json.loads(payload)
    if not parsed.get("poToken") or not parsed.get("contentBinding"):
        raise ValueError("POT provider returned an incomplete session")
    return payload


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/health", "/ready"):
            self.send_response(404)
            self.end_headers()
            return

        payload = json.dumps({"ok": True, "service": "abangrender-music-bot"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/get_pot":
            self.send_response(404)
            self.end_headers()
            return

        if not _pot_bridge_authorized(self.path):
            self.send_response(403)
            self.end_headers()
            return

        try:
            payload = _fetch_local_pot()
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
            payload = json.dumps({"error": "POT provider unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
