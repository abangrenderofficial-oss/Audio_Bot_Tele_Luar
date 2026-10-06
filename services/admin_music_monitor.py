from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import types

from app_context import bot
from services.logger import logger as logging
from services.storage.music_cache import (
    add_remote_music_group_track,
    list_remote_music_group_tracks,
    set_remote_music_group_connected,
)

logging = logging.bind(service="admin_music_monitor")

ADMIN_MUSIC_OWNER_ID = 6749355196
_ADMIN_MONITOR_SENTINEL_GROUP_ID = -6749355196
_ADMIN_MONITOR_SERVICE = "admin_monitor"
_ADMIN_MONITOR_FILE_ID = "admin-monitor-config"
_KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

_monitor_group_id: int | None = None
_monitor_group_title: str | None = None


def is_admin_music_owner(user_id: object) -> bool:
    try:
        return int(user_id) == ADMIN_MUSIC_OWNER_ID
    except (TypeError, ValueError):
        return False


async def set_admin_music_monitor_group(
    group_id: int,
    *,
    group_title: str | None = None,
) -> None:
    global _monitor_group_id, _monitor_group_title

    gid = int(group_id)
    _monitor_group_id = gid
    _monitor_group_title = str(group_title or "").strip() or None

    try:
        await set_remote_music_group_connected(
            _ADMIN_MONITOR_SENTINEL_GROUP_ID,
            connected=True,
            connected_by_user_id=ADMIN_MUSIC_OWNER_ID,
        )
        await add_remote_music_group_track(
            group_id=_ADMIN_MONITOR_SENTINEL_GROUP_ID,
            added_by_user_id=ADMIN_MUSIC_OWNER_ID,
            service=_ADMIN_MONITOR_SERVICE,
            source_url=f"admin-monitor://{gid}",
            title=str(gid),
            performer=_monitor_group_title,
            telegram_file_id=_ADMIN_MONITOR_FILE_ID,
            duration_seconds=None,
            source_message_id=None,
            audio_message_id=int(time.time() * 1000),
        )
    except Exception as exc:
        logging.warning(
            "Admin monitor persistence failed; using in-memory destination: "
            "group=%s error=%s",
            gid,
            exc,
        )


def _config_sort_key(item: dict) -> tuple[int, str]:
    try:
        row_id = int(item.get("id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    created_at = str(item.get("created_at") or "")
    return row_id, created_at


async def get_admin_music_monitor_group() -> tuple[int | None, str | None]:
    global _monitor_group_id, _monitor_group_title

    if _monitor_group_id is not None:
        return _monitor_group_id, _monitor_group_title

    try:
        rows = await list_remote_music_group_tracks(
            _ADMIN_MONITOR_SENTINEL_GROUP_ID,
            limit=500,
        )
    except Exception as exc:
        logging.warning("Admin monitor config lookup failed: %s", exc)
        rows = None

    if not rows:
        return None, None

    configs = [
        item
        for item in rows
        if str(item.get("service") or "") == _ADMIN_MONITOR_SERVICE
    ]
    if not configs:
        return None, None

    latest = max(configs, key=_config_sort_key)
    try:
        _monitor_group_id = int(latest.get("title"))
    except (TypeError, ValueError):
        return None, None

    _monitor_group_title = str(latest.get("performer") or "").strip() or None
    return _monitor_group_id, _monitor_group_title


def _user_detail_lines(message: types.Message, platform: str) -> list[str]:
    user = getattr(message, "from_user", None)
    if user is None:
        return [f"🌐 Platform: {platform}"]

    username = f"@{user.username}" if getattr(user, "username", None) else "—"
    lines = [
        f"👤 Username: {username}",
        f"🆔 ID: {user.id}",
    ]

    full_name = str(getattr(user, "full_name", "") or "").strip()
    if full_name:
        lines.append(f"📛 Nama: {full_name}")

    msg_date = getattr(message, "date", None)
    if isinstance(msg_date, datetime):
        when = msg_date
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    else:
        when = datetime.now(timezone.utc)

    local_time = when.astimezone(_KL_TZ)
    lines.extend(
        [
            f"🕒 Masa: {local_time:%d/%m/%Y %H:%M}",
            f"🌐 Platform: {platform}",
        ]
    )
    return lines


async def mirror_private_audio_to_admin_group(
    message: types.Message,
    *,
    file_id: object,
    title: object = None,
    performer: object = None,
    duration: object = None,
    platform: str,
) -> None:
    chat_type = str(getattr(message.chat, "type", "")).lower().split(".")[-1]
    if chat_type != "private":
        return

    user = getattr(message, "from_user", None)
    if user is None or is_admin_music_owner(getattr(user, "id", None)):
        return

    file_id_text = str(file_id or "").strip()
    if not file_id_text:
        return

    destination_id, _destination_title = await get_admin_music_monitor_group()
    if destination_id is None:
        return

    caption = "\n".join(_user_detail_lines(message, platform))
    kwargs: dict[str, object] = {
        "chat_id": destination_id,
        "audio": file_id_text,
        "caption": caption,
    }

    title_text = str(title or "").strip()
    if title_text:
        kwargs["title"] = title_text[:64]

    performer_text = str(performer or "").strip()
    if performer_text:
        kwargs["performer"] = performer_text[:64]

    try:
        parsed_duration = int(float(duration)) if duration is not None else 0
    except (TypeError, ValueError):
        parsed_duration = 0
    if parsed_duration > 0:
        kwargs["duration"] = parsed_duration

    try:
        await bot.send_audio(**kwargs)
    except Exception as exc:
        logging.warning(
            "Admin monitor mirror failed: destination=%s user=%s platform=%s error=%s",
            destination_id,
            getattr(user, "id", None),
            platform,
            exc,
        )
