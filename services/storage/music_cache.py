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
    if source not in {"tiktok", "instagram", "threads", "twitter", "spotify", "soundcloud"} or not raw:
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

    elif source == "spotify":
        if len(parts) >= 2 and parts[-2] == "track":
            track_id = parts[-1]
            if re.fullmatch(r"[A-Za-z0-9]{8,64}", track_id):
                candidate = f"track:{track_id}"

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


async def get_remote_music_group_connected(group_id: int) -> bool | None:
    data = await _call(
        {"action": "group_connected_get", "group_id": int(group_id)}
    )
    if not data or "connected" not in data:
        return None
    return bool(data.get("connected"))


async def set_remote_music_group_connected(
    group_id: int,
    *,
    connected: bool,
    connected_by_user_id: int | None = None,
) -> bool:
    data = await _call(
        {
            "action": "group_connected_set",
            "group_id": int(group_id),
            "connected": bool(connected),
            "connected_by_user_id": (
                int(connected_by_user_id)
                if connected_by_user_id is not None
                else None
            ),
        },
        timeout_seconds=5.0,
    )
    return bool(data and data.get("ok"))


async def add_remote_music_group_track(
    *,
    group_id: int,
    added_by_user_id: int | None,
    service: str,
    source_url: str,
    title: str | None,
    performer: str | None,
    telegram_file_id: str,
    duration_seconds: float | None,
    source_message_id: int | None,
    audio_message_id: int,
) -> dict[str, Any] | None:
    data = await _call(
        {
            "action": "group_track_add",
            "group_id": int(group_id),
            "added_by_user_id": (
                int(added_by_user_id) if added_by_user_id is not None else None
            ),
            "service": str(service or "unknown"),
            "source_url": str(source_url or ""),
            "title": str(title) if title else None,
            "performer": str(performer) if performer else None,
            "telegram_file_id": str(telegram_file_id),
            "duration_seconds": (
                float(duration_seconds) if duration_seconds is not None else None
            ),
            "source_message_id": (
                int(source_message_id) if source_message_id is not None else None
            ),
            "audio_message_id": int(audio_message_id),
        },
        timeout_seconds=5.0,
    )
    item = data.get("item") if data and data.get("ok") else None
    return dict(item) if isinstance(item, dict) else None


async def list_remote_music_group_tracks(
    group_id: int,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]] | None:
    payload: dict[str, Any] = {
        "action": "group_tracks_list",
        "group_id": int(group_id),
    }
    if limit is not None:
        payload["limit"] = max(1, min(int(limit), 500))
    data = await _call(payload, timeout_seconds=4.0)
    if not data or not isinstance(data.get("items"), list):
        return None
    return [dict(item) for item in data["items"] if isinstance(item, dict)]


async def search_remote_music_group_tracks(
    group_id: int,
    query: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]] | None:
    data = await _call(
        {
            "action": "group_tracks_search",
            "group_id": int(group_id),
            "query": str(query or ""),
            "result_limit": max(1, min(int(limit), 25)),
            "limit": 500,
        },
        timeout_seconds=4.0,
    )
    if not data or not isinstance(data.get("items"), list):
        return None
    return [dict(item) for item in data["items"] if isinstance(item, dict)]


async def find_remote_music_group_track_by_source(
    group_id: int,
    source_url: str,
) -> dict[str, Any] | None:
    data = await _call(
        {
            "action": "group_track_find_source",
            "group_id": int(group_id),
            "source_url": str(source_url or ""),
        },
        timeout_seconds=3.0,
    )
    item = data.get("item") if data and data.get("hit") else None
    return dict(item) if isinstance(item, dict) else None


async def get_remote_music_group_source_message_ids(
    group_id: int,
) -> list[int] | None:
    data = await _call(
        {"action": "group_source_messages", "group_id": int(group_id)},
        timeout_seconds=4.0,
    )
    values = data.get("message_ids") if data else None
    if not isinstance(values, list):
        return None
    result: list[int] = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


async def clear_remote_music_group_links(
    group_id: int,
    message_ids: list[int],
) -> bool:
    data = await _call(
        {
            "action": "group_links_clear",
            "group_id": int(group_id),
            "message_ids": [int(value) for value in message_ids],
        },
        timeout_seconds=5.0,
    )
    return bool(data and data.get("ok"))


async def get_remote_music_group_track_count(group_id: int) -> int | None:
    data = await _call(
        {"action": "group_track_count", "group_id": int(group_id)},
        timeout_seconds=3.0,
    )
    if not data or "count" not in data:
        return None
    try:
        return max(0, int(data.get("count") or 0))
    except (TypeError, ValueError):
        return None
