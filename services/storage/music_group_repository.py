from __future__ import annotations

from typing import Sequence

from sqlalchemy import delete, func, or_, select, update

from services.logger import logger as logging
from services.music_group_dedupe import dedupe_music_group_tracks
from services.storage.models import MusicGroupCleanupMessage, MusicGroupSettings, MusicGroupTrack

logging = logging.bind(service="db_group_music")


class MusicGroupRepositoryMixin:
    async def is_music_group_connected(self, group_id: int) -> bool:
        async with self.SessionLocal() as session:
            row = await session.get(MusicGroupSettings, int(group_id))
            return bool(row and row.connected)

    async def set_music_group_connected(
        self,
        group_id: int,
        *,
        connected: bool,
        connected_by_user_id: int | None = None,
    ) -> None:
        group_id = int(group_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                row = await session.get(MusicGroupSettings, group_id)
                if row is None:
                    row = MusicGroupSettings(
                        group_id=group_id,
                        connected=bool(connected),
                        connected_by_user_id=connected_by_user_id,
                    )
                    session.add(row)
                else:
                    row.connected = bool(connected)
                    if connected_by_user_id is not None:
                        row.connected_by_user_id = int(connected_by_user_id)
                    row.updated_at = func.now()

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
    ) -> MusicGroupTrack | None:
        group_id = int(group_id)
        audio_message_id = int(audio_message_id)

        async with self.SessionLocal() as session:
            async with session.begin():
                existing = await session.execute(
                    select(MusicGroupTrack).where(
                        MusicGroupTrack.group_id == group_id,
                        MusicGroupTrack.audio_message_id == audio_message_id,
                    )
                )
                current = existing.scalar_one_or_none()
                if current is not None:
                    return current

                row = MusicGroupTrack(
                    group_id=group_id,
                    added_by_user_id=(
                        int(added_by_user_id)
                        if added_by_user_id is not None
                        else None
                    ),
                    service=str(service or "unknown"),
                    source_url=str(source_url or ""),
                    title=(str(title) if title else None),
                    performer=(str(performer) if performer else None),
                    telegram_file_id=str(telegram_file_id),
                    duration_seconds=(
                        float(duration_seconds)
                        if duration_seconds is not None
                        else None
                    ),
                    source_message_id=(
                        int(source_message_id)
                        if source_message_id is not None
                        else None
                    ),
                    audio_message_id=audio_message_id,
                )
                session.add(row)
            await session.refresh(row)
            return row

    async def list_music_group_tracks_raw(
        self,
        group_id: int,
        *,
        limit: int | None = None,
    ) -> Sequence[MusicGroupTrack]:
        async with self.SessionLocal() as session:
            stmt = (
                select(MusicGroupTrack)
                .where(MusicGroupTrack.group_id == int(group_id))
                .order_by(
                    MusicGroupTrack.created_at.asc(),
                    MusicGroupTrack.id.asc(),
                )
            )
            if limit is not None:
                stmt = stmt.limit(max(1, int(limit)))
            result = await session.execute(stmt)
            return result.scalars().all()

    async def list_music_group_tracks(
        self,
        group_id: int,
        *,
        limit: int | None = None,
    ) -> Sequence[MusicGroupTrack]:
        rows = list(
            await self.list_music_group_tracks_raw(
                int(group_id),
                limit=None,
            )
        )
        rows, _duplicates = dedupe_music_group_tracks(rows)
        if limit is not None:
            rows = rows[: max(1, int(limit))]
        return rows

    async def search_music_group_tracks(
        self,
        group_id: int,
        query: str,
        *,
        limit: int = 10,
    ) -> Sequence[MusicGroupTrack]:
        value = f"%{str(query or '').strip()}%"
        if value == "%%":
            return []
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(MusicGroupTrack)
                .where(
                    MusicGroupTrack.group_id == int(group_id),
                    or_(
                        MusicGroupTrack.title.ilike(value),
                        MusicGroupTrack.performer.ilike(value),
                    ),
                )
                .order_by(MusicGroupTrack.created_at.asc(), MusicGroupTrack.id.asc())
                .limit(max(1, min(int(limit), 25)))
            )
            rows, _duplicates = dedupe_music_group_tracks(result.scalars().all())
            return rows[: max(1, min(int(limit), 25))]

    async def get_music_group_source_message_ids(
        self,
        group_id: int,
    ) -> list[int]:
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(MusicGroupTrack.source_message_id)
                .where(
                    MusicGroupTrack.group_id == int(group_id),
                    MusicGroupTrack.source_message_id.is_not(None),
                )
                .distinct()
                .order_by(MusicGroupTrack.source_message_id.asc())
            )
            return [int(value) for value in result.scalars().all() if value is not None]

    async def mark_music_group_links_cleared(
        self,
        group_id: int,
        message_ids: Sequence[int],
    ) -> None:
        ids = [int(value) for value in message_ids]
        if not ids:
            return
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(MusicGroupTrack)
                    .where(
                        MusicGroupTrack.group_id == int(group_id),
                        MusicGroupTrack.source_message_id.in_(ids),
                    )
                    .values(source_message_id=None)
                )

    async def get_music_group_track_count(self, group_id: int) -> int:
        rows = await self.list_music_group_tracks(int(group_id))
        return len(rows)


    async def add_music_group_cleanup_message(
        self,
        *,
        group_id: int,
        message_id: int,
        kind: str,
    ) -> None:
        group_id = int(group_id)
        message_id = int(message_id)
        async with self.SessionLocal() as session:
            async with session.begin():
                existing = await session.execute(
                    select(MusicGroupCleanupMessage.id).where(
                        MusicGroupCleanupMessage.group_id == group_id,
                        MusicGroupCleanupMessage.message_id == message_id,
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    return
                session.add(
                    MusicGroupCleanupMessage(
                        group_id=group_id,
                        message_id=message_id,
                        kind=str(kind or "bot_text"),
                    )
                )

    async def get_music_group_cleanup_message_ids(
        self,
        group_id: int,
    ) -> list[int]:
        async with self.SessionLocal() as session:
            result = await session.execute(
                select(MusicGroupCleanupMessage.message_id)
                .where(MusicGroupCleanupMessage.group_id == int(group_id))
                .order_by(MusicGroupCleanupMessage.message_id.asc())
            )
            return [int(value) for value in result.scalars().all()]

    async def remove_music_group_cleanup_messages(
        self,
        group_id: int,
        message_ids: Sequence[int],
    ) -> None:
        ids = [int(value) for value in message_ids]
        if not ids:
            return
        async with self.SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    delete(MusicGroupCleanupMessage).where(
                        MusicGroupCleanupMessage.group_id == int(group_id),
                        MusicGroupCleanupMessage.message_id.in_(ids),
                    )
                )
