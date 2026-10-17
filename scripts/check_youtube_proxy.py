"""Private MusiX proxy preflight. Never print proxy credentials or YouTube cookies.

Usage (after YTDLP_YOUTUBE_PROXY is set as a secret in Bot Hosting):
    python3 -u scripts/check_youtube_proxy.py

Only requests metadata of a small public test video; does not download or
send audio and does not interact with the Telegram bot.
"""
from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL

from services.platforms.youtube_media import build_ytdlp_youtube_options

_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


class QuietLogger:
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


def validate_proxy_config(proxy_url: str) -> bool:
    if not proxy_url or proxy_url.strip() != proxy_url:
        return False
    try:
        parts = urlsplit(proxy_url)
        return (
            parts.scheme.lower() in {"http", "https", "socks5", "socks5h"}
            and bool(parts.hostname)
            and parts.port is not None
            and 0 < parts.port <= 65535
        )
    except (TypeError, ValueError):
        return False


def check() -> int:
    proxy_url = (os.getenv("YTDLP_YOUTUBE_PROXY") or "").strip()
    if not validate_proxy_config(proxy_url):
        print("proxy_preflight=not_configured_or_invalid", flush=True)
        return 2
    options = build_ytdlp_youtube_options(
        skip_download=True,
        noplaylist=True,
        quiet=True,
        no_warnings=True,
        socket_timeout=9,
        retries=0,
        extractor_retries=0,
        ignore_no_formats_error=True,
    )
    # Reject silent direct access if routing ever changes in the future.
    if options.get("proxy") != proxy_url:
        print("proxy_preflight=routing_mismatch", flush=True)
        return 3

    options["logger"] = QuietLogger()
    started = time.monotonic()
    try:
        with YoutubeDL(options) as downloader:
            data = downloader.extract_info(_TEST_URL, download=False)
        audio_formats = [
            item for item in (data or {}).get("formats", [])
            if item.get("acodec") not in (None, "none")
        ]
        print("proxy_preflight=success" if audio_formats else "proxy_preflight=no_audio_formats")
        print("audio_formats_present=" + str(bool(audio_formats)).lower())
        print("elapsed_seconds=" + str(round(time.monotonic() - started, 2)))
        return 0 if audio_formats else 4
    except Exception as exc:
        message = str(exc).lower()
        if "407" in message or "proxy authentication" in message:
            category = "proxy_authentication"
        elif "proxy" in message or "connection" in message or "timed out" in message:
            category = "network_or_proxy"
        elif "sign in" in message or "not a bot" in message or "login" in message:
            category = "youtube_auth_or_ip"
        elif "token" in message or "format" in message:
            category = "youtube_token_or_format"
        else:
            category = "youtube_upstream"
        # Exception text may include secret-bearing connection URLs, so NEVER print it.
        print("proxy_preflight=failed")
        print("failure_category=" + category)
        print("elapsed_seconds=" + str(round(time.monotonic() - started, 2)))
        return 5


if __name__ == "__main__":
    sys.exit(check())
