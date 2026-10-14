from __future__ import annotations

import asyncio

from aiogram import F, Router, types
from aiogram.filters import Command

from app_context import bot
from services.logger import logger as logging
from services.music_group_dedupe import (
    clean_music_title,
    dedupe_music_group_tracks,
)
from services.private_music_playlist import (
    delete_private_messages,
    get_private_source_message_ids,
    list_private_tracks_raw,
)

logging = logging.bind(service="private_music")
router = Router(name=__name__)

_CLEARALL_SWEEP_LIMIT = 1500
_CLEARALL_SWEEP_MARGIN = 250
_LEGACY_PROBE_LIMIT = 500


def _message_has_audio_payload(message: types.Message) -> bool:
    if getattr(message, "audio", None) is not None:
        return True
    if getattr(message, "voice", None) is not None:
        return True
    document = getattr(message, "document", None)
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    return bool(document is not None and mime_type.startswith("audio/"))


async def _probe_private_message_has_audio(
    chat_id: int,
    message_id: int,
) -> bool | None:
    try:
        probe = await bot.forward_message(
            chat_id=chat_id,
            from_chat_id=chat_id,
            message_id=int(message_id),
            disable_notification=True,
        )
    except Exception:
        return None

    try:
        return _message_has_audio_payload(probe)
    finally:
        try:
            await bot.delete_message(chat_id, int(probe.message_id))
        except Exception:
            pass


async def _clean_private_audio_message(
    chat_id: int,
    track: object,
) -> None:
    message_id = getattr(track, "audio_message_id", None)
    file_id = getattr(track, "telegram_file_id", None)
    if message_id is None or not file_id:
        return

    clean_title = clean_music_title(getattr(track, "title", None))
    media_kwargs: dict[str, object] = {
        "media": str(file_id),
        "title": clean_title[:64],
        "caption": f"🎵 {html.escape(clean_title)}",
        "parse_mode": "HTML",
    }

    performer = str(getattr(track, "performer", "") or "").strip()
    if performer:
        media_kwargs["performer"] = performer[:64]

    duration = getattr(track, "duration_seconds", None)
    try:
        parsed_duration = int(float(duration)) if duration is not None else 0
    except (TypeError, ValueError):
        parsed_duration = 0
    if parsed_duration > 0:
        media_kwargs["duration"] = parsed_duration

    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=int(message_id),
            media=types.InputMediaAudio(**media_kwargs),
        )
    except Exception:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=int(message_id),
                caption=f"🎵 {html.escape(clean_title)}",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(Command("clearall"), F.chat.type == "private")
async def clear_all_private_music(message: types.Message) -> None:
    user = getattr(message, "from_user", None)
    if user is None:
        return

    raw_tracks = list(await list_private_tracks_raw(user.id))
    _tracks, _duplicate_tracks = dedupe_music_group_tracks(raw_tracks)

    # Private /clearall is text/link cleanup only. It must never delete audio.
    # Duplicate prevention already happens when a new private song is sent.
    audio_message_ids = {
        int(track.audio_message_id)
        for track in raw_tracks
        if getattr(track, "audio_message_id", None) is not None
    }
    source_ids = set(await get_private_source_message_ids(user.id))
    current_message_id = int(message.message_id)

    if audio_message_ids:
        # Sweep only inside the reliably tracked private-music window. Older
        # history may contain audio sent before persistent tracking existed.
        # Sweeping from message 1 would risk deleting those legacy songs.
        anchor_ids = set(audio_message_ids) | set(source_ids)
        earliest_tracked = min(anchor_ids)
        sweep_floor = max(
            1,
            max(
                current_message_id - _CLEARALL_SWEEP_LIMIT,
                earliest_tracked,
            ),
        )
        target_ids = (
            set(range(sweep_floor, current_message_id + 1))
            | source_ids
            | {current_message_id}
        )
        # Every tracked audio is protected, including older duplicates.
        target_ids.difference_update(audio_message_ids)
        safe_mode = "tracked-sweep"
    else:
        # Legacy private history can predate persistent audio tracking. Probe
        # each recent message by briefly forwarding it back into the same
        # private chat. Telegram returns the forwarded Message object, so we
        # can inspect whether it is audio before deciding what to delete.
        sweep_floor = max(1, current_message_id - _LEGACY_PROBE_LIMIT)
        target_ids = set(source_ids) | {current_message_id}
        probed_audio_ids: set[int] = set()

        for candidate_id in range(sweep_floor, current_message_id):
            has_audio = await _probe_private_message_has_audio(
                message.chat.id,
                candidate_id,
            )
            if has_audio is True:
                probed_audio_ids.add(candidate_id)
            elif has_audio is False:
                target_ids.add(candidate_id)
            await asyncio.sleep(0.025)

        audio_message_ids.update(probed_audio_ids)
        target_ids.difference_update(audio_message_ids)
        safe_mode = "legacy-probe"

    deleted, failed = await delete_private_messages(
        message.chat.id,
        target_ids,
    )

    for track in raw_tracks:
        await _clean_private_audio_message(message.chat.id, track)

    logging.info(
        "Private clearall complete: user=%s mode=%s sweep=%s-%s "
        "deleted=%s failed=%s audio_preserved=%s",
        user.id,
        safe_mode,
        sweep_floor,
        current_message_id,
        len(deleted),
        len(failed),
        len(audio_message_ids),
    )
