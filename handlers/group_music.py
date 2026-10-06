from __future__ import annotations

import asyncio
import html
import json
import shutil
from typing import Iterable

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.media_group import MediaGroupBuilder

from app_context import bot, db
from handlers.commands import update_info
from services.logger import logger as logging

logging = logging.bind(service="group_music")
router = Router(name=__name__)

_GROUP_TYPES = {"group", "supergroup"}


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
    await message.reply("Command ni hanya admin group boleh guna.")
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


@router.message(Command("connectmusic"))
async def connect_music_group(message: types.Message) -> None:
    if not await _require_group(message):
        return
    if not await _require_group_admin(message):
        return

    await _ensure_group_record(message)
    await db.set_music_group_connected(
        message.chat.id,
        connected=True,
        connected_by_user_id=(message.from_user.id if message.from_user else None),
    )
    count = await db.get_music_group_track_count(message.chat.id)
    await message.reply(
        "🎵 <b>Music Group connected.</b>\n\n"
        "Mulai sekarang ahli group boleh hantar link YouTube / TikTok / Instagram / "
        "Threads / X dan bot akan hantar audio terus dalam group.\n\n"
        f"Playlist tersimpan sekarang: <b>{count}</b> track.",
        parse_mode="HTML",
    )


@router.message(Command("clearlink"))
async def clear_music_links(message: types.Message) -> None:
    if not await _require_group(message):
        return
    if not await _require_group_admin(message):
        return
    if not await _connected(message):
        await message.reply("Music Group belum connected. Guna /connectmusic dulu.")
        return

    message_ids = await db.get_music_group_source_message_ids(message.chat.id)
    if not message_ids:
        await message.reply("✅ Tak ada sisa link yang perlu dibuang.")
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
    await message.reply(text, parse_mode="HTML")


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
    if not await _connected(message):
        await message.reply("Music Group belum connected. Guna /connectmusic dulu.")
        return

    tracks = list(await db.list_music_group_tracks(message.chat.id))
    if not tracks:
        await message.reply(
            "Playlist group masih kosong. Hantar link lagu dulu dan bot akan kumpulkan."
        )
        return

    status = await message.reply(
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
    if not await _connected(message):
        await message.reply("Music Group belum connected. Guna /connectmusic dulu.")
        return

    query = str(command.args or "").strip()
    if not query:
        await message.reply(
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
        await message.reply("\n".join(lines), parse_mode="HTML")
        return

    status = await message.reply("🔎 Mencari lagu...")
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
    await message.reply(
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
    await message.reply(
        "⏹ PlaySync native belum aktif. Bila Music Room siap, /stopsync akan "
        "keluarkan user itu sahaja daripada sesi sync tanpa ganggu playlist group."
    )
