from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from services.logger import logger as logging

logging = logging.bind(service="music_cache")

_API_URL = (os.getenv("MUSIC_CACHE_API_URL") or "").strip().rstrip("/")
_API_KEY = (os.getenv("MUSIC_CACHE_API_KEY") or "").strip()


def youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                candidate = parts[1]

    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def configured() -> bool:
    return bool(_API_URL and _API_KEY)


async def _call(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not configured():
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.5, connect=2.0),
            follow_redirects=True,
        ) as client:
            response = await client.post(
                _API_URL,
                headers={
                    "x-music-cache-key": _API_KEY,
                    "content-type": "application/json",
                    "user-agent": "AbangRender-MusicBot/1.0",
                },
                json=payload,
            )
        if response.status_code != 200:
            logging.warning(
                "Remote music cache returned HTTP %s", response.status_code
            )
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logging.warning("Remote music cache unavailable: %s", exc)
        return None


async def get_cached_audio(
    source_url: str,
    *,
    variant: str = "mp3_320",
) -> dict[str, Any] | None:
    video_id = youtube_video_id(source_url)
    if not video_id:
        return None
    data = await _call(
        {"action": "get", "video_id": video_id, "variant": variant}
    )
    item = data.get("item") if data and data.get("hit") else None
    if not isinstance(item, dict) or not item.get("telegram_file_id"):
        return None
    return item


async def store_cached_audio(
    source_url: str,
    *,
    telegram_file_id: str,
    variant: str = "mp3_320",
    title: str | None = None,
    performer: str | None = None,
    duration_seconds: float | None = None,
    file_size_bytes: int | None = None,
) -> bool:
    video_id = youtube_video_id(source_url)
    if not video_id or not telegram_file_id:
        return False

    data = await _call(
        {
            "action": "upsert",
            "video_id": video_id,
            "variant": variant,
            "telegram_file_id": telegram_file_id,
            "title": title,
            "performer": performer,
            "duration_seconds": duration_seconds,
            "file_size_bytes": file_size_bytes,
            "source_url": source_url,
        }
    )
    return bool(data and data.get("ok"))
