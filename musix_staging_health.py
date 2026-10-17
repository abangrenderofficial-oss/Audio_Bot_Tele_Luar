"""Read-only standby monitor: never connects to Telegram or processes updates."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/ready"):
            self.send_error(404)
            return
        response = json.dumps({
            "ok": True, "service": "musix-staging",
            "telegramEnabled": False,
            "databaseConfigured": bool(os.getenv("DATABASE_URL")),
            "schema": os.getenv("MUSIX_DB_SCHEMA", "unset"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

print("MusiX STANDBY. DB configured:", bool(os.getenv("DATABASE_URL")), flush=True)
ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("SERVER_PORT", "8080"))), Handler).serve_forever()
