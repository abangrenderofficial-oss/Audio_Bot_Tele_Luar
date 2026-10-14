from __future__ import annotations

import asyncio

from aiogram import F, Router, types
from aiogram.filters import Command

from app_context import bot
from services.logger import logger as logging
from services.music_group_dedupe import (
    clean_music_title,
)
from services.private_music_playlist import (
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
    from services.music_chat_cleanup import cleanup_music_chat

    await cleanup_music_chat(
        message.chat.id, chat_type="private",
        command_message_id=message.message_id,
    )
