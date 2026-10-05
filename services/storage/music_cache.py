from __future__ import annotations

import hashlib
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


def social_media_key(source: str, url: str) -> str | None:
    source = str(source or "").strip().lower()
    raw = str(url or "").strip()
    if source not in {"tiktok", "instagram", "threads", "twitter"} or not raw:
        return None

    try:
        parsed = urlparse(raw)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    candidate = ""

    if source == "tiktok":
        for idx, part in enumerate(parts[:-1]):
            if part == "video" and idx + 1 < len(parts):
                value = parts[idx + 1]
                if re.fullmatch(r"\d{8,32}", value):
                    candidate = f"video:{value}"
                    break
        if not candidate and host in {"vm.tiktok.com", "vt.tiktok.com"} and parts:
            candidate = f"short:{parts[0][:80]}"

    elif source == "instagram":
        if len(parts) >= 2 and parts[0] in {"reel", "reels", "p", "tv"}:
            code = parts[1]
            if re.fullmatch(r"[A-Za-z0-9_-]{5,40}", code):
                candidate = f"{parts[0]}:{code}"

    elif source == "threads":
        for idx, part in enumerate(parts[:-1]):
            if part == "post" and idx + 1 < len(parts):
                code = parts[idx + 1]
                if re.fullmatch(r"[A-Za-z0-9_-]{5,60}", code):
                    candidate = f"post:{code}"
                    break

    elif source == "twitter":
        for idx, part in enumerate(parts[:-1]):
            if part == "status" and idx + 1 < len(parts):
                value = parts[idx + 1]
                if re.fullmatch(r"\d{8,32}", value):
                    candidate = f"status:{value}"
                    break

    if candidate:
        return candidate

    normalized = f"{host}{parsed.path.rstrip('/')}"
    if parsed.query and source == "tiktok":
        normalized += f"?{parsed.query}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]
    return f"url:{digest}"


def configured() -> bool:
    return bool(_API_URL and _API_KEY)


async def _call(
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 1.8,
) -> dict[str, Any] | None:
    if not configured():
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                max(0.5, float(timeout_seconds)),
                connect=min(2.0, max(0.5, float(timeout_seconds))),
            ),
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
        logging.warning(
            "Remote music cache unavailable: type=%s error=%s",
            type(exc).__name__,
            exc,
        )
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
        },
        timeout_seconds=5.0,
    )
    return bool(data and data.get("ok"))


async def warm_music_cache(*, timeout_seconds: float = 8.0) -> bool:
    data = await _call(
        {"action": "ping"},
        timeout_seconds=timeout_seconds,
    )
    return bool(data and data.get("ok"))


async def get_cached_social_audio(
    source: str,
    source_url: str,
    *,
    variant: str = "fast_original",
) -> dict[str, Any] | None:
    media_key = social_media_key(source, source_url)
    if not media_key:
        return None
    data = await _call(
        {
            "action": "social_get",
            "source": source,
            "media_key": media_key,
            "variant": variant,
        }
    )
    item = data.get("item") if data and data.get("hit") else None
    if not isinstance(item, dict) or not item.get("telegram_file_id"):
        return None
    item = dict(item)
    item["media_key"] = media_key
    return item


async def store_cached_social_audio(
    source: str,
    source_url: str,
    *,
    telegram_file_id: str,
    variant: str = "fast_original",
    title: str | None = None,
    performer: str | None = None,
    duration_seconds: float | None = None,
    file_size_bytes: int | None = None,
) -> bool:
    media_key = social_media_key(source, source_url)
    if not media_key or not telegram_file_id:
        return False

    data = await _call(
        {
            "action": "social_upsert",
            "source": source,
            "media_key": media_key,
            "variant": variant,
            "telegram_file_id": telegram_file_id,
            "title": title,
            "performer": performer,
            "duration_seconds": duration_seconds,
            "file_size_bytes": file_size_bytes,
            "source_url": source_url,
        },
        timeout_seconds=5.0,
    )
    return bool(data and data.get("ok"))
