from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

DEFAULT_USER_SETTINGS = {
    "captions": "off",
    "delete_message": "off",
    "info_buttons": "off",
    "url_button": "off",
    "audio_button": "off",
    "file_button": "off",
    "video_quality": "best",
    "as_document": "off",
    "audio_format": "mp3",
}


@dataclass(slots=True)
class _StatsSnapshot:
    totals_by_date: dict[str, int]
    by_service: dict[str, dict[str, int]]
    service_totals: dict[str, int]
    total_downloads: int = 0


class _DummyEngine:
    async def dispose(self) -> None:
        return None


class MemoryDataBase:
    """Transient compatibility layer used when no PostgreSQL DATABASE_URL is set."""

    def __init__(self) -> None:
        self.engine = _DummyEngine()
        self._settings: dict[int, dict[str, str]] = {}
        self._files: dict[str, tuple[str, str | None]] = {}
        self._status: dict[int, str] = {}
        self._users: dict[int, SimpleNamespace] = {}
        self._groups: dict[int, SimpleNamespace] = {}
        self._history: list[SimpleNamespace] = []
        self._music_groups: dict[int, dict[str, Any]] = {}
        self._music_tracks: dict[int, list[SimpleNamespace]] = {}

    async def init_db(self) -> None:
        return None

    def _settings_for(self, user_id: int) -> dict[str, str]:
        return self._settings.setdefault(int(user_id), dict(DEFAULT_USER_SETTINGS))

    async def user_settings(self, user_id: int) -> dict[str, str]:
        return dict(self._settings_for(user_id))

    async def get_user_setting(self, user_id: int, field: str) -> str | None:
        return self._settings_for(user_id).get(field)

    async def set_user_setting(self, user_id: int, field: str, value: str) -> None:
        self._settings_for(user_id)[field] = value

    async def add_file(self, url: str, file_id: str, file_type: str | None) -> None:
        self._files[str(url)] = (str(file_id), file_type)

    async def get_file_id(self, url: str) -> str | None:
        item = self._files.get(str(url))
        return item[0] if item else None

    def get_file_cache_stats(self) -> dict[str, float | int]:
        return {"entries": len(self._files), "hits": 0, "misses": 0, "hit_rate": 0.0}

    async def cleanup_expired_files(self, max_age_days: int = 30) -> int:
        count = len(self._files)
        self._files.clear()
        return count

    async def upsert_chat(
        self,
        user_id: int,
        user_name: str | None,
        user_username: str | None,
        chat_type: str | None,
        language: str | None = None,
        status: str = "active",
        referred_by: int | None = None,
        source: str | None = None,
    ) -> None:
        uid = int(user_id)
        record = SimpleNamespace(
            user_id=uid,
            user_name=user_name,
            user_username=user_username,
            chat_type=chat_type,
            language=language,
            status=status,
            referred_by=referred_by,
            source=source,
            has_dm=(chat_type == "private"),
        )
        self._users[uid] = record
        self._status[uid] = status

    async def status(self, user_id: int) -> str | None:
        return self._status.get(int(user_id), "active")

    async def set_active(self, user_id: int) -> None:
        self._status[int(user_id)] = "active"

    async def set_inactive(self, user_id: int) -> None:
        self._status[int(user_id)] = "inactive"

    async def delete_user(self, user_id: int) -> None:
        uid = int(user_id)
        self._users.pop(uid, None)
        self._settings.pop(uid, None)
        self._status.pop(uid, None)

    async def get_user_info(self, user_id: int) -> Any:
        return self._users.get(int(user_id))

    async def get_all_users_info(self) -> list[Any]:
        return list(self._users.values())

    async def get_users_for_reachability_check(self) -> list[Any]:
        return [u for u in self._users.values() if getattr(u, "status", "active") == "active"]

    async def get_user_counts(self) -> dict[str, int]:
        total = len(self._users)
        active = sum(1 for uid in self._users if self._status.get(uid, "active") == "active")
        return {"total": total, "active": active, "inactive": max(0, total - active)}

    async def get_community_stats(self) -> dict[str, int]:
        counts = await self.get_user_counts()
        counts["groups"] = len(self._groups)
        return counts

    async def update_group_status(self, group_id: int, status: str) -> None:
        gid = int(group_id)
        group = self._groups.get(gid) or SimpleNamespace(id=gid, member_count=0)
        group.status = status
        self._groups[gid] = group

    async def update_group_member_count(self, group_id: int, member_count: int) -> None:
        gid = int(group_id)
        group = self._groups.get(gid) or SimpleNamespace(id=gid, status="active")
        group.member_count = int(member_count)
        self._groups[gid] = group

    async def migrate_group_chat(
        self,
        old_group_id: int,
        new_group_id: int,
        *,
        new_title: str | None = None,
        new_username: str | None = None,
    ) -> None:
        group = self._groups.pop(int(old_group_id), SimpleNamespace(status="active", member_count=0))
        group.id = int(new_group_id)
        group.title = new_title
        group.username = new_username
        self._groups[int(new_group_id)] = group

    async def get_active_groups(self) -> list[Any]:
        return [g for g in self._groups.values() if getattr(g, "status", "active") == "active"]

    async def is_music_group_connected(self, group_id: int) -> bool:
        gid = int(group_id)
        try:
            from services.storage.music_cache import (
                get_remote_music_group_connected,
            )

            remote = await get_remote_music_group_connected(gid)
            if remote is not None:
                state = self._music_groups.setdefault(gid, {})
                state["connected"] = bool(remote)
                return bool(remote)
        except Exception:
            pass

        state = self._music_groups.get(gid)
        return bool(state and state.get("connected"))

    async def set_music_group_connected(
        self,
        group_id: int,
        *,
        connected: bool,
        connected_by_user_id: int | None = None,
    ) -> None:
        gid = int(group_id)
        state = self._music_groups.setdefault(gid, {})
        state["connected"] = bool(connected)
        if connected_by_user_id is not None:
            state["connected_by_user_id"] = int(connected_by_user_id)

        try:
            from services.storage.music_cache import (
                set_remote_music_group_connected,
            )

            await set_remote_music_group_connected(
                gid,
                connected=bool(connected),
                connected_by_user_id=connected_by_user_id,
            )
        except Exception:
            pass

    async def add_music_group_track(
        self,
        *,
        group_id: int,
        added_by_user_id: int | None,
        service: str,
        source_url: str,
        title: str | None,
        performer: str | None,
        telegram_file_id: str,
        duration_seconds: float | None,
        source_message_id: int | None,
        audio_message_id: int,
    ) -> Any:
        gid = int(group_id)
        audio_message_id = int(audio_message_id)
        tracks = self._music_tracks.setdefault(gid, [])

        try:
            from services.storage.music_cache import add_remote_music_group_track

            remote = await add_remote_music_group_track(
                group_id=gid,
                added_by_user_id=added_by_user_id,
                service=service,
                source_url=source_url,
                title=title,
                performer=performer,
                telegram_file_id=telegram_file_id,
                duration_seconds=duration_seconds,
                source_message_id=source_message_id,
                audio_message_id=audio_message_id,
            )
            if remote is not None:
                row = SimpleNamespace(**remote)
                for index, existing in enumerate(tracks):
                    if int(existing.audio_message_id) == audio_message_id:
                        tracks[index] = row
                        break
                else:
                    tracks.append(row)
                return row
        except Exception:
            pass

        for track in tracks:
            if int(track.audio_message_id) == audio_message_id:
                return track

        row = SimpleNamespace(
            id=len(tracks) + 1,
            group_id=gid,
            added_by_user_id=(
                int(added_by_user_id)
                if added_by_user_id is not None
                else None
            ),
            service=str(service or "unknown"),
            source_url=str(source_url or ""),
            title=title,
            performer=performer,
            telegram_file_id=str(telegram_file_id),
            duration_seconds=duration_seconds,
            source_message_id=(
                int(source_message_id)
                if source_message_id is not None
                else None
            ),
            audio_message_id=audio_message_id,
        )
        tracks.append(row)
        return row

    async def list_music_group_tracks(
        self,
        group_id: int,
        *,
        limit: int | None = None,
    ) -> list[Any]:
        gid = int(group_id)
        try:
            from services.storage.music_cache import (
                list_remote_music_group_tracks,
            )

            remote = await list_remote_music_group_tracks(gid, limit=limit)
            if remote is not None:
                rows = [SimpleNamespace(**item) for item in remote]
                if limit is None:
                    self._music_tracks[gid] = list(rows)
                return rows
        except Exception:
            pass

        rows = list(self._music_tracks.get(gid, []))
        if limit is not None:
            rows = rows[: max(1, int(limit))]
        return rows

    async def search_music_group_tracks(
        self,
        group_id: int,
        query: str,
        *,
        limit: int = 10,
    ) -> list[Any]:
        gid = int(group_id)
        try:
            from services.storage.music_cache import (
                search_remote_music_group_tracks,
            )

            remote = await search_remote_music_group_tracks(
                gid,
                query,
                limit=limit,
            )
            if remote is not None:
                return [SimpleNamespace(**item) for item in remote]
        except Exception:
            pass

        needle = str(query or "").strip().casefold()
        if not needle:
            return []
        rows = [
            row
            for row in self._music_tracks.get(gid, [])
            if needle in str(getattr(row, "title", "") or "").casefold()
            or needle in str(getattr(row, "performer", "") or "").casefold()
        ]
        return rows[: max(1, min(int(limit), 25))]

    async def get_music_group_source_message_ids(
        self,
        group_id: int,
    ) -> list[int]:
        gid = int(group_id)
        try:
            from services.storage.music_cache import (
                get_remote_music_group_source_message_ids,
            )

            remote = await get_remote_music_group_source_message_ids(gid)
            if remote is not None:
                return remote
        except Exception:
            pass

        values = {
            int(row.source_message_id)
            for row in self._music_tracks.get(gid, [])
            if getattr(row, "source_message_id", None) is not None
        }
        return sorted(values)

    async def mark_music_group_links_cleared(
        self,
        group_id: int,
        message_ids,
    ) -> None:
        gid = int(group_id)
        ids = {int(value) for value in message_ids}
        for row in self._music_tracks.get(gid, []):
            if getattr(row, "source_message_id", None) in ids:
                row.source_message_id = None

        try:
            from services.storage.music_cache import clear_remote_music_group_links

            await clear_remote_music_group_links(gid, sorted(ids))
        except Exception:
            pass

    async def get_music_group_track_count(self, group_id: int) -> int:
        gid = int(group_id)
        try:
            from services.storage.music_cache import (
                get_remote_music_group_track_count,
            )

            remote = await get_remote_music_group_track_count(gid)
            if remote is not None:
                return remote
        except Exception:
            pass

        return len(self._music_tracks.get(gid, []))

    async def record_download(
        self,
        *,
        user_id: int,
        chat_id: Optional[int] = None,
        chat_type: Optional[str] = None,
        service: str,
        url: str,
        title: Optional[str] = None,
        file_type: Optional[str] = None,
        file_id: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> Any:
        row = SimpleNamespace(
            id=len(self._history) + 1,
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            service=service,
            url=url,
            title=title,
            file_type=file_type,
            file_id=file_id,
            file_size_bytes=file_size_bytes,
            duration_seconds=duration_seconds,
            status=status,
            error_message=error_message,
        )
        self._history.append(row)
        return row

    async def get_download_history(
        self,
        *,
        page: int = 1,
        per_page: int = 15,
        user_id: Optional[int] = None,
        service: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Any]:
        rows = self._history
        if user_id is not None:
            rows = [r for r in rows if r.user_id == user_id]
        if service is not None:
            rows = [r for r in rows if r.service == service]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        start = max(0, (int(page) - 1) * int(per_page))
        return list(reversed(rows))[start : start + int(per_page)]

    async def get_download_history_count(
        self,
        *,
        user_id: Optional[int] = None,
        service: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        rows = await self.get_download_history(
            page=1,
            per_page=max(1, len(self._history) or 1),
            user_id=user_id,
            service=service,
            status=status,
        )
        return len(rows)

    async def get_download_stats(self, period: str) -> Any:
        totals: dict[str, int] = {}
        for row in self._history:
            totals[row.service] = totals.get(row.service, 0) + 1
        return _StatsSnapshot({}, {}, totals, len(self._history))
