"""Safe, quiet, shared cleanup for private chats and connected music groups.

Telegram bots cannot list arbitrary chat history. We delete only observed IDs,
registered source-link IDs, and the /clearall command itself. Never infer an
unobserved message ID or remove a known audio simply because it is old.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from app_context import bot, db
from services.logger import logger as logging
from services.music_group_dedupe import dedupe_music_group_tracks

logging = logging.bind(service="music_chat_cleanup")


def cleanup_plan(tracks: list, observed: list) -> tuple[set[int], set[int], list]:
    """Return (all_audio_ids, duplicate_audio_ids, keeper tracks). Pure logic."""
    keeper_tracks, duplicate_tracks = dedupe_music_group_tracks(tracks)
    all_audio_ids = {
        int(t.audio_message_id) for t in tracks
        if getattr(t, "audio_message_id", None) is not None
    }
    duplicates = {
        int(t.audio_message_id) for t in duplicate_tracks
        if getattr(t, "audio_message_id", None) is not None
    }

    # User-sent audio/voice/audio documents: exact same Telegram file.
    # Do not guess from filenames or titles alone.
    observed_audio = sorted(
        (m for m in observed if getattr(m, "is_audio", False)),
        key=lambda m: int(m.message_id),
    )
    keeper_ids = {
        int(t.audio_message_id) for t in keeper_tracks
        if getattr(t, "audio_message_id", None) is not None
    }
    by_audio_key: dict[str, list[int]] = defaultdict(list)
    for item in observed_audio:
        mid = int(item.message_id)
        all_audio_ids.add(mid)
        key = str(getattr(item, "audio_key", "") or "").strip()
        if key:
            by_audio_key[key].append(mid)
    for ids in by_audio_key.values():
        if len(ids) <= 1:
            continue
        # Prefer an already registered song keeper; otherwise retain earliest.
        existing_keeper = sorted(keeper_ids.intersection(ids))
        selected = existing_keeper[0] if existing_keeper else min(ids)
        duplicates.update(value for value in ids if value != selected)
    return all_audio_ids, duplicates, keeper_tracks


async def cleanup_music_chat(
    chat_id: int,
    *,
    chat_type: str,
    command_message_id: int | None = None,
) -> dict[str, int]:
    """Best-effort cleanup; never send status messages into the target chat."""
    cid = int(chat_id)
    private = chat_type == "private"

    if not private:
        connected = await db.is_music_group_connected(cid)
        if not connected:
            return {"deleted": 0, "duplicates": 0, "failed": 0}

    # Critical safety check: if the registry lookup fails, do NOT delete.
    if private:
        from handlers.private_music import _clean_private_audio_message
        from services.private_music_playlist import (
            delete_private_messages, get_private_source_message_ids,
            list_private_tracks_raw,
        )
        tracks = list(await list_private_tracks_raw(cid))
        source_ids = set(await get_private_source_message_ids(cid))
        delete_messages = delete_private_messages
        clean_audio = _clean_private_audio_message
        extra_ids: set[int] = set()
    else:
        from handlers.group_music import _clean_group_audio_message, _delete_group_messages
        raw = getattr(db, "list_music_group_tracks_raw", None)
        tracks = list(await raw(cid)) if callable(raw) else list(
            await db.list_music_group_tracks(cid)
        )
        source_ids = set(await db.get_music_group_source_message_ids(cid))
        get_extra = getattr(db, "get_music_group_cleanup_message_ids", None)
        extra_ids = set(await get_extra(cid)) if callable(get_extra) else set()
        delete_messages = _delete_group_messages
        clean_audio = _clean_group_audio_message

    reader = getattr(db, "get_music_chat_cleanup_messages", None)
    observed = list(await reader(cid)) if callable(reader) else []
    all_audio, duplicates, keepers = cleanup_plan(tracks, observed)

    observed_text = {
        int(m.message_id) for m in observed
        if not getattr(m, "is_audio", False)
    }
    candidates = source_ids | observed_text | extra_ids | duplicates
    if command_message_id is not None:
        candidates.add(int(command_message_id))

    # Protection wins over text/link records, except a confirmed duplicate.
    targets = (candidates - all_audio) | duplicates
    targets.discard(0)
    if not targets:
        # Still correct existing audio captions, without notifications.
        for track in keepers:
            try:
                await clean_audio(cid, track)
            except Exception as exc:
                logging.warning("Audio caption update failed: chat=%s error=%s", cid, exc)
        return {"deleted": 0, "duplicates": 0, "failed": 0}

    deleted, failed = await delete_messages(cid, targets)

    # Run caption/title cleanup only for surviving keeper messages.
    for track in keepers:
        mid = getattr(track, "audio_message_id", None)
        if mid is None or int(mid) not in deleted:
            try:
                await clean_audio(cid, track)
            except Exception as exc:
                logging.warning("Audio caption update failed: chat=%s error=%s", cid, exc)

    if deleted:
        remover = getattr(db, "remove_music_chat_cleanup_messages", None)
        if callable(remover):
            await remover(cid, sorted(deleted))
        removed_sources = source_ids & deleted
        if not private and removed_sources:
            await db.mark_music_group_links_cleared(cid, sorted(removed_sources))
        if not private:
            legacy_remover = getattr(db, "remove_music_group_cleanup_messages", None)
            if callable(legacy_remover):
                await legacy_remover(cid, sorted(extra_ids & deleted))

    logging.info(
        "Music chat clean: chat=%s type=%s deleted=%s duplicates=%s failed=%s audio_kept=%s",
        cid, chat_type, len(deleted), len(duplicates & deleted), len(failed),
        len(all_audio - deleted),
    )
    return {
        "deleted": len(deleted),
        "duplicates": len(duplicates & deleted),
        "failed": len(failed),
    }


async def autoclean_music_chats() -> None:
    """Daily quiet job. Only private chats observed after migration + joined music groups."""
    list_targets = getattr(db, "list_music_chat_cleanup_targets", None)
    if not callable(list_targets):
        logging.warning("Auto-clean skipped: no persistent music cleanup registry")
        return
    targets = list(await list_targets())
    total = defaultdict(int)
    for chat_id, kind in targets:
        try:
            stats = await cleanup_music_chat(chat_id, chat_type=kind)
            for key, value in stats.items():
                total[key] += value
        except Exception as exc:
            total["errors"] += 1
            logging.warning(
                "Auto-clean failed for chat=%s type=%s: %s",
                chat_id, kind, exc,
            )
        await asyncio.sleep(0.05)
    logging.info("Music auto-clean finished: chats=%s stats=%s", len(targets), dict(total))
