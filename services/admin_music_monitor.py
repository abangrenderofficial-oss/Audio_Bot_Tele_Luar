from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import types
from sqlalchemy import func, select

from app_context import bot, db
from services.storage.models import AdminMusicMonitorSettings, MusicPrivateAudioDelivery
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
    """Confirm connection only if a durable destination has been saved."""
    global _monitor_group_id, _monitor_group_title

    gid = int(group_id)
    title = str(group_title or "").strip() or None
    database_saved = False
    remote_saved = False

    try:
        async with db.SessionLocal() as session:
            async with session.begin():
                row = await session.get(AdminMusicMonitorSettings, 1)
                if row is None:
                    session.add(
                        AdminMusicMonitorSettings(
                            id=1,
                            group_id=gid,
                            group_title=title,
                        )
                    )
                else:
                    row.group_id = gid
                    row.group_title = title
        database_saved = True
    except Exception as exc:
        logging.warning(
            "Admin monitor database persistence failed: group=%s error=%s",
            gid,
            exc,
        )

    # Keep compatibility with installations that previously stored monitor
    # destinations in remote music-cache track records. The database above
    # is the authoritative store after this change.
    try:
        remote_connected = await set_remote_music_group_connected(
            _ADMIN_MONITOR_SENTINEL_GROUP_ID,
            connected=True,
            connected_by_user_id=ADMIN_MUSIC_OWNER_ID,
        )
        if remote_connected:
            stored = await add_remote_music_group_track(
                group_id=_ADMIN_MONITOR_SENTINEL_GROUP_ID,
                added_by_user_id=ADMIN_MUSIC_OWNER_ID,
                service=_ADMIN_MONITOR_SERVICE,
                source_url=f"admin-monitor://{gid}",
                title=str(gid),
                performer=title,
                telegram_file_id=_ADMIN_MONITOR_FILE_ID,
                duration_seconds=None,
                source_message_id=None,
                audio_message_id=int(time.time() * 1000),
            )
            remote_saved = bool(stored)
    except Exception as exc:
        logging.warning(
            "Admin monitor legacy cache persistence failed: group=%s error=%s",
            gid,
            exc,
        )

    if not database_saved and not remote_saved:
        raise RuntimeError(
            "Admin Music Monitor destination could not be saved. "
            "Check database and remote cache connectivity."
        )

    _monitor_group_id = gid
    _monitor_group_title = title
    logging.info(
        "Admin music monitor connected: group=%s database=%s remote=%s",
        gid,
        database_saved,
        remote_saved,
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

    # Survives Render instance restarts and works when the remote cache is down.
    try:
        async with db.SessionLocal() as session:
            row = await session.scalar(
                select(AdminMusicMonitorSettings).where(
                    AdminMusicMonitorSettings.id == 1
                )
            )
            if row is not None:
                _monitor_group_id = int(row.group_id)
                _monitor_group_title = (
                    str(row.group_title or "").strip() or None
                )
                return _monitor_group_id, _monitor_group_title
    except Exception as exc:
        logging.warning("Admin monitor database lookup failed: %s", exc)

    # Legacy compatibility: recover a destination registered before the
    # database migration. A new /connectadminmusic will save it to PostgreSQL.
    try:
        rows = await list_remote_music_group_tracks(
            _ADMIN_MONITOR_SENTINEL_GROUP_ID,
            limit=500,
        )
    except Exception as exc:
        logging.warning("Admin monitor legacy lookup failed: %s", exc)
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


async def record_music_private_audio_delivery(
    *, user_id: int, audio_message_id: int, platform: str,
) -> None:
    """Idempotent MusiX-only success record, not a group-message counter."""
    if int(user_id) <= 0 or int(audio_message_id) <= 0:
        return
    async with db.SessionLocal() as session:
        async with session.begin():
            stmt = db._insert(MusicPrivateAudioDelivery).values(
                user_id=int(user_id),
                audio_message_id=int(audio_message_id),
                platform=str(platform or "Unknown")[:100],
            )
            await session.execute(stmt.on_conflict_do_nothing(
                index_elements=[
                    MusicPrivateAudioDelivery.user_id,
                    MusicPrivateAudioDelivery.audio_message_id,
                ],
            ))


async def count_unique_music_audio_users() -> int:
    """Only verified private audio delivery rows from the MusiX service."""
    async with db.SessionLocal() as session:
        result = await session.scalar(
            select(func.count(func.distinct(MusicPrivateAudioDelivery.user_id)))
        )
    return int(result or 0)


def _user_detail_lines(
    message: types.Message,
    platform: str,
    source_url: str | None = None,
) -> list[str]:
    user = getattr(message, "from_user", None)
    username = f"@{html.escape(str(user.username))}" if user and getattr(user, "username", None) else "—"
    full_name = html.escape(str(getattr(user, "full_name", "") or "").strip()) or "—"
    user_id = getattr(user, "id", "—")
    msg_date = getattr(message, "date", None)
    if isinstance(msg_date, datetime):
        when = msg_date if msg_date.tzinfo else msg_date.replace(tzinfo=timezone.utc)
    else:
        when = datetime.now(timezone.utc)
    local_time = when.astimezone(_KL_TZ)
    lines = [
        f"👤 Username: {username}",
        f"🆔 ID: {user_id}",
        f"📛 Nama: {full_name}",
        f"📅 Tarikh: {local_time:%d/%m/%Y}",
        f"🕒 Masa: {local_time:%H:%M} (Malaysia)",
        f"🌐 Source: {html.escape(str(platform or 'Unknown'))}",
    ]
    if source_url:
        lines.append(f"🔗 Link: {html.escape(str(source_url).strip()[:350])}")
    return lines

async def mirror_private_audio_to_admin_group(
    message: types.Message,
    *,
    file_id: object,
    source_message_id: object = None,
    title: object = None,
    performer: object = None,
    duration: object = None,
    platform: str,
    source_url: str | None = None,
) -> None:
    chat_type = str(getattr(message.chat, "type", "")).lower().split(".")[-1]
    if chat_type != "private":
        return

    user = getattr(message, "from_user", None)
    if user is None:
        return

    file_id_text = str(file_id or "").strip()
    if not file_id_text:
        return

    # Record a real MusiX private audio delivery even if monitor forwarding is
    # unavailable. The same audio message cannot be counted twice on retries.
    try:
        delivered_id = int(source_message_id) if source_message_id is not None else 0
    except (ValueError, TypeError):
        delivered_id = 0
    if delivered_id > 0:
        try:
            await record_music_private_audio_delivery(
                user_id=int(user.id), audio_message_id=delivered_id, platform=platform,
            )
        except Exception as exc:
            logging.error("MusiX audio usage persistence failed: user=%s error=%s", user.id, exc)

    destination_id, _destination_title = await get_admin_music_monitor_group()
    if destination_id is None:
        logging.warning(
            "Admin monitor destination missing; run /connectadminmusic "
            "in the monitoring group again: user=%s platform=%s",
            getattr(user, "id", None),
            platform,
        )
        return

    caption = "\n".join(_user_detail_lines(message, platform, source_url))
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

    copied = False
    try:
        parsed_source_message_id = int(source_message_id) if source_message_id is not None else 0
    except (TypeError, ValueError):
        parsed_source_message_id = 0

    if parsed_source_message_id > 0:
        try:
            await bot.copy_message(
                chat_id=destination_id,
                from_chat_id=message.chat.id,
                message_id=parsed_source_message_id,
                caption=caption,
            )
            copied = True
            logging.info(
                "Admin monitor mirror copied: destination=%s user=%s platform=%s source_message=%s",
                destination_id,
                getattr(user, "id", None),
                platform,
                parsed_source_message_id,
            )
        except Exception as exc:
            logging.warning(
                "Admin monitor copy_message failed; falling back to file_id: "
                "destination=%s user=%s platform=%s source_message=%s error=%s",
                destination_id,
                getattr(user, "id", None),
                platform,
                parsed_source_message_id,
                exc,
            )

    if copied:
        return

    try:
        await bot.send_audio(**kwargs)
        logging.info(
            "Admin monitor mirror sent: destination=%s user=%s platform=%s",
            destination_id,
            getattr(user, "id", None),
            platform,
        )
    except Exception as exc:
        logging.warning(
            "Admin monitor mirror failed: destination=%s user=%s platform=%s error=%s",
            destination_id,
            getattr(user, "id", None),
            platform,
            exc,
        )
