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
    get_admin_music_monitor_group,
    count_unique_music_audio_users,
    set_admin_music_monitor_group,
)
from services.logger import logger as logging
from services.music_group_dedupe import (
    clean_music_title,
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


async def _is_monitor_group(message: types.Message) -> bool | None:
    """Never clean or register the admin monitoring destination as a music room."""
    try:
        monitor_id, _title = await get_admin_music_monitor_group()
        return monitor_id is not None and int(monitor_id) == int(message.chat.id)
    except Exception as exc:
        logging.warning("Cannot check admin monitor group: %s", exc)
        return None  # Unknown is never permission to clean OR reveal admin stats.


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
    if await _is_monitor_group(message) is not False:
        logging.warning("Music room connection rejected/uncertain for group=%s", message.chat.id)
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

    # If a prior /connectmusic was used here, turn it off. Monitoring always
    # overrides music-room/cleanup behaviour for the same Telegram group.
    try:
        if await db.is_music_group_connected(message.chat.id):
            await db.set_music_group_connected(message.chat.id, connected=False)
    except Exception as exc:
        logging.warning("Disabling music-room mode for monitor group failed: %s", exc)

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
    # Members may request cleanup; the bot still needs Delete Messages permission.
    if not await _require_group(message):
        return
    if await _is_monitor_group(message) is not False:
        return
    if not await _connected(message):
        await _reply_tracked(message, "Music Group belum connected. Guna /connectmusic dulu.")
        return
    from services.music_chat_cleanup import cleanup_music_chat

    await cleanup_music_chat(
        message.chat.id, chat_type="group",
        command_message_id=message.message_id,
    )


@router.message(Command("totalusermusic"))
async def total_users_music(message: types.Message) -> None:
    """Only owner, only inside /connectadminmusic destination, never MediaX."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    if not is_admin_music_owner(user_id):
        return
    if not _is_group(message) or await _is_monitor_group(message) is not True:
        return
    try:
        unique_users = await count_unique_music_audio_users()
    except Exception as exc:
        logging.error("MusiX unique audio user count failed: %s", exc)
        await message.reply("⚠️ Statistik MusiX belum tersedia. Semak database MusiX.")
        return
    await message.reply(
        f"🎵 <b>Total User MusiX</b>\n"
        f"👥 Pengguna unik yang berjaya menerima audio: <b>{unique_users:,}</b>\n"
        "🆔 Dikira berdasarkan Telegram user ID berlainan.\n"
        "Hanya audio dalam private chat MusiX, bukan MediaX.\n"
        "Bermula sejak fungsi rekod MusiX ini diaktifkan.",
        parse_mode="HTML",
    )
