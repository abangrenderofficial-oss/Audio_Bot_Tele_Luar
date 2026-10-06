from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from aiogram import types

from app_context import bot
from services.logger import logger as logging
from services.music_group_dedupe import (
    clean_music_title,
    dedupe_music_group_tracks,
    duplicate_keeper_message_id,
)
from services.storage.music_cache import (
    add_remote_music_group_track,
    get_remote_music_group_source_message_ids,
    list_remote_music_group_tracks,
)

logging = logging.bind(service="private_music_playlist")

# Keep private playlists in the existing persistent music-cache namespace,
# but far away from real Telegram group IDs. This value stays well inside
# JavaScript/Python safe integer ranges.
_PRIVATE_PLAYLIST_BASE = -9_000_000_000_000
_memory_tracks: dict[int, list[SimpleNamespace]] = {}


def private_playlist_id(user_id: int) -> int:
    return _PRIVATE_PLAYLIST_BASE - int(user_id)


def _is_private_message(message: types.Message) -> bool:
    return (
        str(getattr(message.chat, "type", ""))
        .lower()
        .split(".")[-1]
        == "private"
    )


def _to_namespace(item: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**dict(item))


async def list_private_tracks_raw(user_id: int) -> list[Any]:
    playlist_id = private_playlist_id(user_id)
    try:
        remote = await list_remote_music_group_tracks(
            playlist_id,
            limit=500,
        )
    except Exception as exc:
        logging.warning(
            "Private playlist lookup failed: user=%s error=%s",
            user_id,
            exc,
        )
        remote = None

    if remote is not None:
        rows = [_to_namespace(item) for item in remote]
        _memory_tracks[int(user_id)] = list(rows)
        return rows

    return list(_memory_tracks.get(int(user_id), []))


async def list_private_tracks(user_id: int) -> list[Any]:
    rows = await list_private_tracks_raw(user_id)
    kept, _duplicates = dedupe_music_group_tracks(rows)
    return kept


async def search_private_tracks(
    user_id: int,
    query: str,
    *,
    limit: int = 5,
) -> list[Any]:
    needle = " ".join(str(query or "").casefold().split())
    if not needle:
        return []

    rows = await list_private_tracks(user_id)
    matched = [
        row
        for row in rows
        if needle
        in (
            f"{getattr(row, 'title', '')} "
            f"{getattr(row, 'performer', '')}"
        ).casefold()
    ]
    return matched[: max(1, min(int(limit), 25))]


async def get_private_source_message_ids(user_id: int) -> set[int]:
    playlist_id = private_playlist_id(user_id)
    result: set[int] = set()
    try:
        remote = await get_remote_music_group_source_message_ids(playlist_id)
    except Exception:
        remote = None

    if remote is not None:
        for value in remote:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                pass
        return result

    for track in await list_private_tracks_raw(user_id):
        value = getattr(track, "source_message_id", None)
        if value is not None:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                pass
    return result


async def record_private_audio(
    message: types.Message,
    *,
    service: str,
    source_url: str,
    file_id: object,
    audio_message_id: object,
    title: object = None,
    performer: object = None,
    duration: object = None,
) -> bool:
    if not _is_private_message(message):
        return False

    user = getattr(message, "from_user", None)
    if user is None or not file_id or audio_message_id is None:
        return False

    user_id = int(user.id)
    candidate_message_id = int(audio_message_id)
    clean_title = clean_music_title(title)

    try:
        raw_tracks = await list_private_tracks_raw(user_id)
        keeper_id = duplicate_keeper_message_id(
            raw_tracks,
            candidate_audio_message_id=candidate_message_id,
            service=service,
            source_url=source_url,
            title=clean_title,
            performer=performer,
            telegram_file_id=file_id,
        )
    except Exception as exc:
        logging.debug(
            "Private duplicate check failed: user=%s service=%s error=%s",
            user_id,
            service,
            exc,
        )
        raw_tracks = []
        keeper_id = candidate_message_id

    if keeper_id != candidate_message_id:
        for stale_message_id in (
            candidate_message_id,
            getattr(message, "message_id", None),
        ):
            if stale_message_id is None:
                continue
            try:
                await bot.delete_message(
                    message.chat.id,
                    int(stale_message_id),
                )
            except Exception:
                pass
        logging.info(
            "Private duplicate audio removed: user=%s service=%s "
            "removed=%s keeper=%s",
            user_id,
            service,
            candidate_message_id,
            keeper_id,
        )
        return False

    try:
        parsed_duration = (
            float(duration) if duration is not None else None
        )
    except (TypeError, ValueError):
        parsed_duration = None

    row = SimpleNamespace(
        id=None,
        group_id=private_playlist_id(user_id),
        added_by_user_id=user_id,
        service=str(service or "unknown"),
        source_url=str(source_url or ""),
        title=clean_title,
        performer=(str(performer) if performer else None),
        telegram_file_id=str(file_id),
        duration_seconds=parsed_duration,
        source_message_id=getattr(message, "message_id", None),
        audio_message_id=candidate_message_id,
    )

    try:
        stored = await add_remote_music_group_track(
            group_id=private_playlist_id(user_id),
            added_by_user_id=user_id,
            service=row.service,
            source_url=row.source_url,
            title=row.title,
            performer=row.performer,
            telegram_file_id=row.telegram_file_id,
            duration_seconds=row.duration_seconds,
            source_message_id=row.source_message_id,
            audio_message_id=row.audio_message_id,
        )
        if isinstance(stored, dict):
            row = _to_namespace(stored)
    except Exception as exc:
        logging.warning(
            "Private playlist persistence failed: user=%s service=%s error=%s",
            user_id,
            service,
            exc,
        )

    _memory_tracks.setdefault(user_id, []).append(row)
    return True


async def delete_private_messages(
    chat_id: int,
    message_ids: Iterable[int],
) -> tuple[set[int], set[int]]:
    ids = sorted({int(value) for value in message_ids if int(value) > 0})
    if not ids:
        return set(), set()

    deleted: set[int] = set()
    failed: set[int] = set()
    bulk_delete = getattr(bot, "delete_messages", None)

    if callable(bulk_delete):
        for start in range(0, len(ids), 100):
            batch = ids[start : start + 100]
            try:
                await bulk_delete(
                    chat_id=chat_id,
                    message_ids=batch,
                )
                deleted.update(batch)
            except Exception:
                for message_id in batch:
                    try:
                        await bot.delete_message(chat_id, message_id)
                        deleted.add(message_id)
                    except Exception:
                        failed.add(message_id)
                    await asyncio.sleep(0.02)
            await asyncio.sleep(0.04)
        return deleted, failed

    for message_id in ids:
        try:
            await bot.delete_message(chat_id, message_id)
            deleted.add(message_id)
        except Exception:
            failed.add(message_id)
        await asyncio.sleep(0.02)

    return deleted, failed
