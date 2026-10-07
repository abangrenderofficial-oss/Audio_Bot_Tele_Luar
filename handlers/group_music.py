from __future__ import annotations

import asyncio
import html
import json
import shutil
from typing import Iterable

from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.media_group import MediaGroupBuilder

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
        f"Playlist tersimpan sekarang: <b>{count}</b> track.",
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

    await set_admin_music_monitor_group(
        message.chat.id,
        group_title=getattr(message.chat, "title", None),
    )
    await _reply_tracked(
        message,
        "🔐 <b>Admin Music Monitor connected.</b>\n\n"
        "Audio yang berjaya dihantar kepada user luar dalam private chat "
        "akan disalin automatik ke group ini bersama username, ID, nama, "
        "masa dan platform.",
        parse_mode="HTML",
    )


@router.message(Command("playlist"))
async def show_group_playlist(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    if not await _connected(message):
        await _reply_tracked(message, "Music Group belum connected. Guna /connectmusic dulu.")
        return

    total = await db.get_music_group_track_count(message.chat.id)
    tracks = list(
        await db.list_music_group_tracks(
            message.chat.id,
            limit=30,
        )
    )
    if not tracks:
        await _reply_tracked(message,
            "🎵 Playlist group masih kosong. Hantar link lagu dulu dan bot akan kumpulkan."
        )
        return

    lines = [
        "🎵 <b>Group Playlist</b>",
        f"<b>{total}</b> track • paling awal → paling baru",
        "",
    ]
    for index, track in enumerate(tracks, start=1):
        title = html.escape(str(getattr(track, "title", None) or "Audio"))
        performer = html.escape(
            str(getattr(track, "performer", None) or "")
        )
        label = f"{index}. <b>{title}</b>"
        if performer:
            label += f" — {performer}"
        lines.append(label)

    if total > len(tracks):
        lines.append(f"\n… dan <b>{total - len(tracks)}</b> track lagi.")

    lines.append(
        "\n▶️ /playall — hantar queue ke native Telegram music player."
    )
    await _reply_tracked(message, "\n".join(lines), parse_mode="HTML")


@router.message(Command("clearlink"))
async def clear_music_links(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    if not await _require_group_admin(message):
        return
    if not await _connected(message):
        await _reply_tracked(message, "Music Group belum connected. Guna /connectmusic dulu.")
        return

    message_ids = await db.get_music_group_source_message_ids(message.chat.id)
    if not message_ids:
        await _reply_tracked(message, "✅ Tak ada sisa link yang perlu dibuang.")
        return

    deleted: list[int] = []
    failed = 0
    for message_id in message_ids:
        try:
            await bot.delete_message(message.chat.id, message_id)
            deleted.append(message_id)
        except Exception:
            failed += 1
        await asyncio.sleep(0.03)

    if deleted:
        await db.mark_music_group_links_cleared(message.chat.id, deleted)

    text = f"🧹 Link dibersihkan: <b>{len(deleted)}</b>."
    if failed:
        text += (
            f"\nTak dapat delete: <b>{failed}</b>. "
            "Pastikan bot jadi admin dan ada permission Delete Messages."
        )
    await _reply_tracked(message, text, parse_mode="HTML")


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


async def _send_audio_batch(
    message: types.Message,
    tracks: Iterable[object],
) -> int:
    tracks = list(tracks)
    if not tracks:
        return 0

    builder = MediaGroupBuilder()
    for track in tracks:
        kwargs: dict[str, object] = {
            "media": str(track.telegram_file_id),
            "title": str(track.title or "Audio")[:64],
            "performer": str(track.performer or "Music Group")[:64],
        }
        if getattr(track, "duration_seconds", None):
            try:
                kwargs["duration"] = max(1, round(float(track.duration_seconds)))
            except (TypeError, ValueError):
                pass
        builder.add_audio(**kwargs)

    try:
        sent = await message.answer_media_group(media=builder.build())
        return len(sent)
    except Exception as album_error:
        logging.warning("Playlist audio album failed; retrying individually: %s", album_error)

    sent_count = 0
    for track in tracks:
        try:
            await message.answer_audio(
                audio=str(track.telegram_file_id),
                title=str(track.title or "Audio")[:64],
                performer=str(track.performer or "Music Group")[:64],
            )
            sent_count += 1
        except Exception as exc:
            logging.warning(
                "Playlist track send failed: group=%s track=%s error=%s",
                message.chat.id,
                getattr(track, "id", None),
                exc,
            )
        await asyncio.sleep(0.05)
    return sent_count


@router.message(Command("playall"))
async def play_all_group_music(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    if not await _connected(message):
        await _reply_tracked(message, "Music Group belum connected. Guna /connectmusic dulu.")
        return

    tracks = list(await db.list_music_group_tracks(message.chat.id))
    if not tracks:
        await _reply_tracked(message,
            "Playlist group masih kosong. Hantar link lagu dulu dan bot akan kumpulkan."
        )
        return

    status = await _reply_tracked(message,
        f"▶️ Susun playlist dari awal • {len(tracks)} track..."
    )
    sent_count = 0
    for offset in range(0, len(tracks), 10):
        sent_count += await _send_audio_batch(message, tracks[offset : offset + 10])
        await asyncio.sleep(0.25)

    try:
        await status.edit_text(
            f"✅ Playlist dihantar dari awal • {sent_count}/{len(tracks)} track."
        )
    except Exception:
        pass


async def _youtube_search(query: str, limit: int = 5) -> list[dict[str, object]]:
    binary = shutil.which("yt-dlp")
    command = [binary] if binary else ["python", "-m", "yt_dlp"]
    command.extend(
        [
            "--flat-playlist",
            "--skip-download",
            "--no-warnings",
            "--quiet",
            "--dump-single-json",
            f"ytsearch{max(1, min(limit, 8))}:{query}",
        ]
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=25)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return []
    if process.returncode != 0:
        return []

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []

    results: list[dict[str, object]] = []
    for entry in payload.get("entries") or []:
        video_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not video_id or not title:
            continue
        results.append(
            {
                "title": title,
                "uploader": str(entry.get("uploader") or entry.get("channel") or "").strip(),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return results[:limit]


@router.message(Command("search"))
async def search_music(message: types.Message, command: CommandObject) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    if not await _connected(message):
        await _reply_tracked(message, "Music Group belum connected. Guna /connectmusic dulu.")
        return

    query = str(command.args or "").strip()
    if not query:
        await _reply_tracked(message,
            "🔎 Guna <code>/search tajuk lagu</code>\n"
            "Contoh: <code>/search Sinaran Sheila Majid</code>",
            parse_mode="HTML",
        )
        return

    local_tracks = list(
        await db.search_music_group_tracks(message.chat.id, query, limit=5)
    )
    if local_tracks:
        lines = ["🔎 <b>Jumpa dalam playlist group:</b>"]
        for index, track in enumerate(local_tracks, start=1):
            title = html.escape(str(track.title or "Audio"))
            performer = html.escape(str(track.performer or ""))
            lines.append(
                f"{index}. <b>{title}</b>"
                + (f" — {performer}" if performer else "")
            )
        lines.append("\nGuna /playall untuk main playlist dari awal.")
        await _reply_tracked(message, "\n".join(lines), parse_mode="HTML")
        return

    status = await _reply_tracked(message, "🔎 Mencari lagu...")
    results = await _youtube_search(query)
    if not results:
        try:
            await status.edit_text(
                "Tak jumpa hasil sekarang. Cuba tajuk/artist yang lebih tepat."
            )
        except Exception:
            pass
        return

    lines = ["🔎 <b>Hasil carian:</b>"]
    for index, item in enumerate(results, start=1):
        title = html.escape(str(item["title"]))
        uploader = html.escape(str(item.get("uploader") or ""))
        url = html.escape(str(item["url"]), quote=True)
        lines.append(
            f'{index}. <a href="{url}">{title}</a>'
            + (f" — {uploader}" if uploader else "")
        )
    lines.append(
        "\nHantar link pilihan ke group ini — bot akan convert dan tambah ke playlist."
    )
    try:
        await status.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TypeError:
        await status.edit_text("\n".join(lines), parse_mode="HTML")


@router.message(Command("playsync"))
async def play_sync_info(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    await _reply_tracked(
        message,
        "🎧 <b>PlaySync</b> perlukan player bersama (Music Room / Mini App). "
        "Telegram tak benarkan bot kawal Play/Pause/Skip pada player Telegram "
        "di telefon ahli lain secara remote.\n\n"
        "Route /playsync dah disediakan, tapi aku tak akan fake sync pada native player. "
        "Bila Music Room dibuat nanti, user yang join sync akan ikut Play/Pause/Skip/Repeat "
        "leader dan masih boleh keluar dengan /stopsync.",
        parse_mode="HTML",
    )


@router.message(Command("stopsync"))
async def stop_sync_info(message: types.Message) -> None:
    if not await _require_group(message):
        return
    await _remember_command(message)
    await _reply_tracked(
        message,
        "⏹ PlaySync native belum aktif. Bila Music Room siap, /stopsync akan "
        "keluarkan user itu sahaja daripada sesi sync tanpa ganggu playlist group."
    )
