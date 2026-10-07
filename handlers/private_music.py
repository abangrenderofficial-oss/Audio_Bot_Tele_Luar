from __future__ import annotations

import asyncio
import html
import json
import shutil
from collections.abc import Iterable

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.media_group import MediaGroupBuilder

from app_context import bot
from services.logger import logger as logging
from services.music_group_dedupe import (
    clean_music_title,
    dedupe_music_group_tracks,
)
from services.private_music_playlist import (
    delete_private_messages,
    get_private_source_message_ids,
    list_private_tracks,
    list_private_tracks_raw,
    search_private_tracks,
)

logging = logging.bind(service="private_music")
router = Router(name=__name__)

_CLEARALL_SWEEP_LIMIT = 1500
_CLEARALL_SWEEP_MARGIN = 250


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
    tracks, _duplicate_tracks = dedupe_music_group_tracks(raw_tracks)

    # Private /clearall must never delete music audio. Preserve every recorded
    # audio message, including any legacy duplicates.
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
        target_ids.difference_update(audio_message_ids)
        safe_mode = "tracked-sweep"
    else:
        # If there is no audio registry, Bot API gives us no way to inspect an
        # old message by id before deleting it. Delete only known link/source
        # messages and this command instead of risking another audio deletion.
        sweep_floor = current_message_id
        target_ids = set(source_ids) | {current_message_id}
        safe_mode = "known-only"

    deleted, failed = await delete_private_messages(
        message.chat.id,
        target_ids,
    )

    for track in tracks:
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


async def _send_audio_batch(
    message: types.Message,
    tracks: Iterable[object],
) -> int:
    rows = list(tracks)
    if not rows:
        return 0

    builder = MediaGroupBuilder()
    for track in rows:
        kwargs: dict[str, object] = {
            "media": str(track.telegram_file_id),
            "title": clean_music_title(getattr(track, "title", None))[:64],
            "performer": str(
                getattr(track, "performer", None) or "Music"
            )[:64],
        }
        duration = getattr(track, "duration_seconds", None)
        if duration:
            try:
                kwargs["duration"] = max(1, round(float(duration)))
            except (TypeError, ValueError):
                pass
        builder.add_audio(**kwargs)

    try:
        sent = await message.answer_media_group(media=builder.build())
        return len(sent)
    except Exception as album_error:
        logging.warning(
            "Private playall media-group failed; retrying individually: %s",
            album_error,
        )

    sent_count = 0
    for track in rows:
        try:
            await message.answer_audio(
                audio=str(track.telegram_file_id),
                title=clean_music_title(
                    getattr(track, "title", None)
                )[:64],
                performer=str(
                    getattr(track, "performer", None) or "Music"
                )[:64],
            )
            sent_count += 1
        except Exception as exc:
            logging.warning(
                "Private playall track failed: user=%s error=%s",
                getattr(getattr(message, "from_user", None), "id", None),
                exc,
            )
        await asyncio.sleep(0.05)
    return sent_count


@router.message(Command("playall"), F.chat.type == "private")
async def play_all_private_music(message: types.Message) -> None:
    user = getattr(message, "from_user", None)
    if user is None:
        return

    tracks = list(await list_private_tracks(user.id))
    if not tracks:
        await message.answer(
            "Playlist private masih kosong. Hantar link lagu dulu."
        )
        return

    status = await message.answer(
        f"▶️ Susun playlist dari awal • {len(tracks)} track..."
    )
    sent_count = 0
    for offset in range(0, len(tracks), 10):
        sent_count += await _send_audio_batch(
            message,
            tracks[offset : offset + 10],
        )
        await asyncio.sleep(0.25)

    try:
        await status.edit_text(
            f"✅ Playlist dihantar dari awal • {sent_count}/{len(tracks)} track."
        )
    except Exception:
        pass


async def _youtube_search(
    query: str,
    limit: int = 5,
) -> list[dict[str, object]]:
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
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=25,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return []

    if process.returncode != 0:
        return []

    try:
        payload = json.loads(
            stdout.decode("utf-8", errors="replace")
        )
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
                "uploader": str(
                    entry.get("uploader")
                    or entry.get("channel")
                    or ""
                ).strip(),
                "url": (
                    "https://www.youtube.com/watch?v="
                    f"{video_id}"
                ),
            }
        )

    return results[:limit]


@router.message(Command("search"), F.chat.type == "private")
async def search_private_music(
    message: types.Message,
    command: CommandObject,
) -> None:
    user = getattr(message, "from_user", None)
    if user is None:
        return

    query = str(command.args or "").strip()
    if not query:
        await message.answer(
            "🔎 Guna <code>/search tajuk lagu</code>\n"
            "Contoh: <code>/search Sinaran Sheila Majid</code>",
            parse_mode="HTML",
        )
        return

    local_tracks = list(
        await search_private_tracks(user.id, query, limit=5)
    )
    if local_tracks:
        lines = ["🔎 <b>Jumpa dalam playlist private:</b>"]
        for index, track in enumerate(local_tracks, start=1):
            title = html.escape(
                clean_music_title(getattr(track, "title", None))
            )
            performer = html.escape(
                str(getattr(track, "performer", None) or "")
            )
            lines.append(
                f"{index}. <b>{title}</b>"
                + (f" — {performer}" if performer else "")
            )
        lines.append(
            "\nGuna /playall untuk hantar playlist dari awal."
        )
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
        )
        return

    status = await message.answer("🔎 Mencari lagu...")
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
        "\nHantar link pilihan dalam private chat ini — bot akan convert ke audio."
    )
    try:
        await status.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TypeError:
        await status.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
        )
