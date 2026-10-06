from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any

from services.storage.music_cache import social_media_key, youtube_video_id


_LYRIC_BRACKET_RE = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(?:lyrics?|lirik)\b[^\)\]\}]*[\)\]\}]",
    flags=re.IGNORECASE,
)
_LYRIC_WORD_RE = re.compile(
    r"\b(?:official\s+)?(?:lyrics?|lirik)(?:\s+video)?\b",
    flags=re.IGNORECASE,
)
_ORIGINAL_QUALITY_RE = re.compile(
    r"\boriginal\s+quality\b",
    flags=re.IGNORECASE,
)


def clean_music_title(value: object) -> str:
    title = html.unescape(str(value or "Audio")).strip()
    title = _LYRIC_BRACKET_RE.sub(" ", title)
    title = _LYRIC_WORD_RE.sub(" ", title)
    title = _ORIGINAL_QUALITY_RE.sub(" ", title)
    title = re.sub(r"\s{2,}", " ", title)
    title = re.sub(r"\s*[-–—|•:·]+\s*$", "", title).strip()
    return title or "Audio"


def _normalise_text(value: object, *, clean_title: bool = False) -> str:
    text = clean_music_title(value) if clean_title else html.unescape(str(value or ""))
    text = text.casefold().strip()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def canonical_music_source(service: object, source_url: object) -> str:
    source = str(service or "").strip().lower()
    url = str(source_url or "").strip()
    if not url:
        return ""

    if source == "youtube":
        video_id = youtube_video_id(url)
        if video_id:
            return f"youtube:{video_id}"

    key = social_media_key(source, url)
    if key:
        return f"{source}:{key}"

    return f"{source}:{url.rstrip('/').casefold()}"


def track_identity_keys(
    *,
    service: object = None,
    source_url: object = None,
    title: object = None,
    performer: object = None,
    telegram_file_id: object = None,
) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()

    source_key = canonical_music_source(service, source_url)
    if source_key:
        keys.add(("source", source_key))

    file_id = str(telegram_file_id or "").strip()
    if file_id:
        keys.add(("file", file_id))

    normal_title = _normalise_text(title, clean_title=True)
    normal_performer = _normalise_text(performer)
    if normal_title and normal_performer:
        keys.add(("meta", normal_title, normal_performer))

    return keys


def identity_keys_for_track(track: Any) -> set[tuple[str, ...]]:
    return track_identity_keys(
        service=getattr(track, "service", None),
        source_url=getattr(track, "source_url", None),
        title=getattr(track, "title", None),
        performer=getattr(track, "performer", None),
        telegram_file_id=getattr(track, "telegram_file_id", None),
    )


def find_duplicate_track(
    tracks: Iterable[Any],
    *,
    service: object,
    source_url: object,
    title: object = None,
    performer: object = None,
    telegram_file_id: object = None,
    exclude_audio_message_id: int | None = None,
) -> Any | None:
    candidate_keys = track_identity_keys(
        service=service,
        source_url=source_url,
        title=title,
        performer=performer,
        telegram_file_id=telegram_file_id,
    )
    if not candidate_keys:
        return None

    for track in tracks:
        audio_message_id = getattr(track, "audio_message_id", None)
        if (
            exclude_audio_message_id is not None
            and audio_message_id is not None
            and int(audio_message_id) == int(exclude_audio_message_id)
        ):
            continue
        if candidate_keys.intersection(identity_keys_for_track(track)):
            return track
    return None


def dedupe_music_group_tracks(
    tracks: Iterable[Any],
) -> tuple[list[Any], list[Any]]:
    kept: list[Any] = []
    duplicates: list[Any] = []
    seen: set[tuple[str, ...]] = set()

    for track in tracks:
        keys = identity_keys_for_track(track)
        if keys and keys.intersection(seen):
            duplicates.append(track)
            continue
        kept.append(track)
        seen.update(keys)

    return kept, duplicates


def duplicate_keeper_message_id(
    tracks: Iterable[Any],
    *,
    candidate_audio_message_id: int,
    service: object,
    source_url: object,
    title: object = None,
    performer: object = None,
    telegram_file_id: object = None,
) -> int:
    candidate_id = int(candidate_audio_message_id)
    candidate_keys = track_identity_keys(
        service=service,
        source_url=source_url,
        title=title,
        performer=performer,
        telegram_file_id=telegram_file_id,
    )
    if not candidate_keys:
        return candidate_id

    matching_ids = [candidate_id]
    for track in tracks:
        audio_message_id = getattr(track, "audio_message_id", None)
        if audio_message_id is None:
            continue
        if candidate_keys.intersection(identity_keys_for_track(track)):
            matching_ids.append(int(audio_message_id))

    return min(matching_ids)
