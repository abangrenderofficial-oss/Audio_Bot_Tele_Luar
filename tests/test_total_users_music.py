"""Admin monitor and unique MusiX private audio accounting never touch MediaX."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import group_music
from services import admin_music_monitor
from services.storage.models import MusicPrivateAudioDelivery


class MonitorMessage:
    def __init__(self, user_id=admin_music_monitor.ADMIN_MUSIC_OWNER_ID):
        self.chat = SimpleNamespace(id=-100999, type="supergroup")
        self.from_user = SimpleNamespace(id=user_id)
        self.message_id = 50
        self.reply = AsyncMock()


@pytest.mark.asyncio
async def test_totalusermusic_only_owner_in_connected_monitor(monkeypatch):
    count = AsyncMock(return_value=42)
    is_monitor = AsyncMock(return_value=True)
    monkeypatch.setattr(group_music, "count_unique_music_audio_users", count)
    monkeypatch.setattr(group_music, "_is_monitor_group", is_monitor)

    admin = MonitorMessage()
    await group_music.total_users_music(admin)
    assert "42" in admin.reply.await_args.args[0]
    assert "MusiX" in admin.reply.await_args.args[0]
    assert "bukan MediaX" in admin.reply.await_args.args[0]
    count.assert_awaited_once_with()

    other = MonitorMessage(user_id=123)
    await group_music.total_users_music(other)
    other.reply.assert_not_awaited()
    assert count.await_count == 1

    is_monitor.return_value = False
    await group_music.total_users_music(admin)
    assert count.await_count == 1


@pytest.mark.asyncio
async def test_connectmusic_denied_for_monitor_group(monkeypatch):
    message = MonitorMessage(user_id=123)
    fake_db = SimpleNamespace(set_music_group_connected=AsyncMock())
    monkeypatch.setattr(group_music, "db", fake_db)
    monkeypatch.setattr(group_music, "_is_monitor_group", AsyncMock(return_value=True))
    await group_music.connect_music_group(message)
    fake_db.set_music_group_connected.assert_not_awaited()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_clearall_ignored_in_monitor_group(monkeypatch):
    message = MonitorMessage()
    fake_db = SimpleNamespace(is_music_group_connected=AsyncMock(return_value=True))
    monkeypatch.setattr(group_music, "db", fake_db)
    monkeypatch.setattr(group_music, "_is_monitor_group", AsyncMock(return_value=True))
    await group_music.clear_all_group_text(message)
    fake_db.is_music_group_connected.assert_not_awaited()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_mirror_records_only_musix_success_and_full_source(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=8855, type="private"),
        from_user=SimpleNamespace(id=8855, username="musixtester", full_name="Alice & Bob"),
        date=datetime(2026, 10, 14, 5, 34, tzinfo=timezone.utc),
    )
    record = AsyncMock()
    copy = AsyncMock()
    monkeypatch.setattr(admin_music_monitor, "record_music_private_audio_delivery", record)
    monkeypatch.setattr(admin_music_monitor, "bot", SimpleNamespace(copy_message=copy, send_audio=AsyncMock()))
    monkeypatch.setattr(admin_music_monitor, "get_admin_music_monitor_group",
                        AsyncMock(return_value=(-100999, "Monitor MediaX + MusiX")))

    await admin_music_monitor.mirror_private_audio_to_admin_group(
        message,
        file_id="test-mp3",
        source_message_id=100,
        title="An original music title",
        platform="TikTok",
        source_url="https://www.tiktok.com/@example/video/123",
    )
    record.assert_awaited_once_with(user_id=8855, audio_message_id=100, platform="TikTok")
    caption = copy.await_args.kwargs["caption"]
    assert "ID: 8855" in caption
    assert "Username: @musixtester" in caption
    assert "Nama: Alice &amp; Bob" in caption
    assert "Tarikh: 14/10/2026" in caption
    assert "Masa: 13:34 (Malaysia)" in caption
    assert "Source: TikTok" in caption
    assert "Link: https://www.tiktok.com/@example/video/123" in caption


@pytest.mark.asyncio
async def test_musix_counter_counts_distinct_users_only(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def scalar(self, statement):
            # The query targets the dedicated MusiX delivery table, NOT
            # group messages, generic downloads or any MediaX table.
            assert "music_private_audio_deliveries" in str(statement)
            assert "DISTINCT" in str(statement).upper()
            return 7

    monkeypatch.setattr(admin_music_monitor, "db", SimpleNamespace(SessionLocal=Session))
    assert await admin_music_monitor.count_unique_music_audio_users() == 7


def test_musix_delivery_key_idempotent_per_user_and_message():
    pkey = {column.name for column in MusicPrivateAudioDelivery.__table__.primary_key.columns}
    assert pkey == {"user_id", "audio_message_id"}
    assert MusicPrivateAudioDelivery.__tablename__ == "music_private_audio_deliveries"
