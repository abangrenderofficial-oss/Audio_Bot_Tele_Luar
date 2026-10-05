from __future__ import annotations

import asyncio
import json
import os
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIServer, make_server

import nodriver

from potoken_generator.extractor import PotokenExtractor


class ThreadingWSGIServer(WSGIServer, ThreadingMixIn):
    daemon_threads = True


class TrustedSessionHTTP:
    def __init__(self, extractor: PotokenExtractor) -> None:
        self.extractor = extractor

    @staticmethod
    def _response(start_response, status: str, payload: dict[str, Any]):
        body = json.dumps(payload).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        method = (environ.get("REQUEST_METHOD") or "GET").upper()

        if path in {"/", "/ping"} and method == "GET":
            token = self.extractor.get()
            return self._response(
                start_response,
                "200 OK",
                {"ok": True, "ready": token is not None},
            )

        if path in {"/get_pot", "/token"} and method in {"GET", "POST"}:
            token = self.extractor.get()
            if token is None:
                return self._response(
                    start_response,
                    "503 Service Unavailable",
                    {"error": "token_not_ready"},
                )
            return self._response(
                start_response,
                "200 OK",
                {
                    "updated": token.updated,
                    "potoken": token.potoken,
                    "visitor_data": token.visitor_data,
                },
            )

        if path == "/update" and method in {"GET", "POST"}:
            accepted = self.extractor.request_update()
            return self._response(
                start_response,
                "202 Accepted",
                {"ok": True, "accepted": accepted},
            )

        return self._response(start_response, "404 Not Found", {"error": "not_found"})


async def run() -> None:
    loop = asyncio.get_running_loop()
    interval = max(60, int(os.getenv("TOKEN_UPDATE_INTERVAL", "300")))
    port = int(os.getenv("PORT", "10000"))

    extractor = PotokenExtractor(loop, update_interval=interval)
    app = TrustedSessionHTTP(extractor)
    server = make_server("0.0.0.0", port, app, ThreadingWSGIServer)

    server_task = asyncio.create_task(asyncio.to_thread(server.serve_forever))
    extractor_task = asyncio.create_task(extractor.run())

    print(f"[TRUSTED-SESSION] HTTP server listening on 0.0.0.0:{port}", flush=True)

    try:
        await asyncio.gather(server_task, extractor_task)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    nodriver.loop().run_until_complete(run())
