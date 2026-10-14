from __future__ import annotations

import asyncio
import html
from typing import Iterable

from aiogram import Router, types
from aiogram.filters import Command

from app_context import bot, db
from handlers.commands import update_info
from services.admin_music_monitor import (
    is_admin_music_owner,
    set_admin_music_monitor_group,
)
from services.logger import logger as logging
from services.music_group_dedupe import (
    clean_music_title,
    dedupe_music_group_tracks,
)

logging = logging.bind(service="group_music")
router = Router(name=__name__)

_GROUP_TYPES = {"group", "supergroup"}
_CLEARALL_SWEEP_LIMIT = 1500
_CLEARALL_SWEEP_MARGIN = 250
_DELETE_BATCH_SIZE = 100


def _is_group(message: types.Message) -> bool:
    return str(getattr(message.chat, "type", "")).lower().split(".")[-1] in _GROUP_TYPES


async def _require_group(message: types.Message) -> bool:
    if _is_group(message):
        return True
    await message.reply(
        "Command ni digunakan dalam group. Add bot ke group, kemudian taip /connectmusic."
    )
    return False


async def _is_group_admin(message: types.Message) -> bool:
    if not message.from_user:
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except Exception:
        return False
    status = getattr(member, "status", "")
    value = getattr(status, "value", status)
    return str(value).lower() in {"administrator", "creator", "owner"}


async def _require_group_admin(message: types.Message) -> bool:
    if await _is_group_admin(message):
        return True
    await _reply_tracked(message, "Command ni hanya admin group boleh guna.")
    return False


async def _ensure_group_record(message: types.Message) -> None:
    await update_info(message)
    upsert_group = getattr(db, "upsert_group", None)
    if not callable(upsert_group):
        return
    try:
        member_count = await bot.get_chat_member_count(message.chat.id)
    except Exception:
        member_count = 0
    await upsert_group(
        chat_id=message.chat.id,
        title=getattr(message.chat, "title", None),
        username=getattr(message.chat, "username", None),
        chat_type=str(getattr(message.chat, "type", "group")).lower().split(".")[-1],
        status="active",
        member_count=member_count,
        last_thread_id=getattr(message, "message_thread_id", None),
    )


async def _connected(message: types.Message) -> bool:
    checker = getattr(db, "is_music_group_connected", None)
    if not callable(checker):
        return False
    return bool(await checker(message.chat.id))


async def _remember_cleanup_message(
    group_id: int,
    message_id: object,
    *,
    kind: str,
) -> None:
    recorder = getattr(db, "add_music_group_cleanup_message", None)
    if not callable(recorder) or message_id is None:
        return
    try:
        await recorder(
            group_id=int(group_id),
            message_id=int(message_id),
            kind=kind,
        )
    except Exception as exc:
        logging.warning(
            "Cleanup message tracking failed: group=%s message=%s kind=%s error=%s",
            group_id,
            message_id,
            kind,
            exc,
        )


async def _remember_command(message: types.Message) -> None:
    await _remember_cleanup_message(
        message.chat.id,
        getattr(message, "message_id", None),
        kind="command",
    )


async def _reply_tracked(
    message: types.Message,
    *args,
    kind: str = "bot_text",
    **kwargs,
):
    sent = await message.reply(*args, **kwargs)
    await _remember_cleanup_message(
        message.chat.id,
        getattr(sent, "message_id", None),
        kind=kind,
    )
    return sent


@router.message(Command("connectmusic"))
async def connect_music_group(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _ensure_group_record(message)
    await _remember_command(message)

    await db.set_music_group_connected(
        message.chat.id,
        connected=True,
        connected_by_user_id=(message.from_user.id if message.from_user else None),
    )
    count = await db.get_music_group_track_count(message.chat.id)
    await _reply_tracked(message,
        "🎵 <b>Music Group connected.</b>\n\n"
        "Mulai sekarang ahli group boleh hantar link YouTube / TikTok / Instagram / "
        "Threads / X dan bot akan hantar audio terus dalam group.\n\n"
        f"Audio tersimpan di group: <b>{count}</b> track.",
        parse_mode="HTML",
    )


@router.message(Command("connectadminmusic"))
async def connect_admin_music_monitor(message: types.Message) -> None:
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    if not is_admin_music_owner(user_id):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        return

    if not await _require_group(message):
        return

    try:
        await set_admin_music_monitor_group(
            message.chat.id,
            group_title=getattr(message.chat, "title", None),
        )
    except Exception as exc:
        logging.error(
            "Admin monitor connection failed: group=%s error=%s",
            message.chat.id,
            exc,
        )
        await _reply_tracked(
            message,
            "❌ <b>Admin Music Monitor belum berjaya disambungkan.</b>\n"
            "Konfigurasi group gagal disimpan. Semak storage bot dan cuba lagi.",
            parse_mode="HTML",
        )
        return

    await _reply_tracked(
        message,
        "🔐 <b>Admin Music Monitor connected.</b>\n\n"
        "Audio yang berjaya dihantar kepada user luar dalam private chat "
        "akan disalin automatik ke group ini bersama username, ID, nama, "
        "masa dan platform.",
        parse_mode="HTML",
    )


async def _delete_group_messages(
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
        for start in range(0, len(ids), _DELETE_BATCH_SIZE):
            batch = ids[start : start + _DELETE_BATCH_SIZE]
            try:
                await bulk_delete(chat_id=chat_id, message_ids=batch)
                deleted.update(batch)
            except Exception as exc:
                logging.debug(
                    "Clearall bulk delete failed: group=%s first=%s last=%s error=%s",
                    chat_id,
                    batch[0],
                    batch[-1],
                    exc,
                )
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


async def _clean_group_audio_message(
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
    except Exception as exc:
        logging.debug(
            "Clearall audio cleanup edit failed: group=%s message=%s error=%s",
            chat_id,
            message_id,
            exc,
        )
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=int(message_id),
                caption=f"🎵 {html.escape(clean_title)}",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(Command("clearall"))
async def clear_all_group_text(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    if not await _require_group_admin(message):
        return
    if not await _connected(message):
        await _reply_tracked(
            message,
            "Music Group belum connected. Guna /connectmusic dulu.",
        )
        return

    raw_lister = getattr(db, "list_music_group_tracks_raw", None)
    if callable(raw_lister):
        raw_tracks = list(await raw_lister(message.chat.id))
    else:
        raw_tracks = list(await db.list_music_group_tracks(message.chat.id))

    tracks, _duplicate_tracks = dedupe_music_group_tracks(raw_tracks)
    # /clearall is text/link cleanup only. Preserve EVERY audio message we
    # know about, including older duplicate songs. Duplicate prevention is
    # handled when a new song is sent, not by /clearall.
    audio_message_ids = {
        int(track.audio_message_id)
        for track in raw_tracks
        if getattr(track, "audio_message_id", None) is not None
    }

    cleanup_getter = getattr(db, "get_music_group_cleanup_message_ids", None)
    tracked_ids = set(
        await cleanup_getter(message.chat.id)
        if callable(cleanup_getter)
        else []
    )
    source_ids = set(await db.get_music_group_source_message_ids(message.chat.id))
    current_message_id = int(message.message_id)

    # Render free restarts wipe the in-memory cleanup ledger, so /clearall
    # cannot rely on tracked ids alone. Sweep the recent message-id range and
    # explicitly preserve every song-audio message stored in the persistent
    # Music Group playlist. This catches bot text, commands, link previews,
    # failed-link replies and ordinary group text even after a redeploy.
    known_ids = (
        set(audio_message_ids)
        | set(source_ids)
        | set(tracked_ids)
        | {current_message_id}
    )
    earliest_known = min(known_ids) if known_ids else current_message_id
    sweep_floor = max(
        1,
        max(
            current_message_id - _CLEARALL_SWEEP_LIMIT,
            earliest_known - _CLEARALL_SWEEP_MARGIN,
        ),
    )
    sweep_ids = set(range(sweep_floor, current_message_id + 1))

    # Explicit ids are still included when they fall just outside the sweep.
    # Known audio ids are never sent to Telegram's delete API.
    target_ids = sweep_ids | tracked_ids | source_ids | {current_message_id}
    target_ids.difference_update(audio_message_ids)

    deleted, failed = await _delete_group_messages(
        message.chat.id,
        target_ids,
    )

    for track in raw_tracks:
        await _clean_group_audio_message(message.chat.id, track)

    deleted_source_ids = sorted(source_ids.intersection(deleted))
    if deleted_source_ids:
        await db.mark_music_group_links_cleared(
            message.chat.id,
            deleted_source_ids,
        )

    cleanup_remover = getattr(db, "remove_music_group_cleanup_messages", None)
    if callable(cleanup_remover) and deleted:
        await cleanup_remover(message.chat.id, sorted(deleted))

    logging.info(
        "Group clearall complete: group=%s sweep=%s-%s deleted=%s failed=%s "
        "audio_preserved=%s",
        message.chat.id,
        sweep_floor,
        current_message_id,
        len(deleted),
        len(failed),
        len(audio_message_ids),
    )
