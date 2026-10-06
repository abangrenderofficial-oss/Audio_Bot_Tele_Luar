from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import group_music
from services import admin_music_monitor


class DummyGroupMessage:
    def __init__(self, user_id: int):
        self.chat = SimpleNamespace(
            id=-1009876543210,
            type="supergroup",
            title="Admin Music Monitor",
        )
        self.from_user = SimpleNamespace(id=user_id)
        self.message_id = 77
        self.reply = AsyncMock(
            return_value=SimpleNamespace(message_id=78),
        )


@pytest.mark.asyncio
async def test_connectadminmusic_is_silent_and_deleted_for_non_owner(monkeypatch):
    message = DummyGroupMessage(123456)
    fake_bot = SimpleNamespace(delete_message=AsyncMock())
    setter = AsyncMock()

    monkeypatch.setattr(group_music, "bot", fake_bot)
    monkeypatch.setattr(group_music, "set_admin_music_monitor_group", setter)

    await group_music.connect_admin_music_monitor(message)

    fake_bot.delete_message.assert_awaited_once_with(
        message.chat.id,
        message.message_id,
    )
    setter.assert_not_awaited()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_connectadminmusic_connects_only_owner_destination(monkeypatch):
    message = DummyGroupMessage(admin_music_monitor.ADMIN_MUSIC_OWNER_ID)
    setter = AsyncMock()

    monkeypatch.setattr(group_music, "set_admin_music_monitor_group", setter)
    monkeypatch.setattr(group_music, "_remember_cleanup_message", AsyncMock())

    await group_music.connect_admin_music_monitor(message)

    setter.assert_awaited_once_with(
        message.chat.id,
        group_title=message.chat.title,
    )
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_audio_is_mirrored_with_user_details(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=998877, type="private"),
        from_user=SimpleNamespace(
            id=998877,
            username="listener",
            full_name="Test Listener",
        ),
        date=datetime(2026, 10, 6, 13, 44, tzinfo=timezone.utc),
    )
    fake_bot = SimpleNamespace(send_audio=AsyncMock())
    monkeypatch.setattr(admin_music_monitor, "bot", fake_bot)
    monkeypatch.setattr(
        admin_music_monitor,
        "get_admin_music_monitor_group",
        AsyncMock(return_value=(-1009876543210, "Admin Music Monitor")),
    )

    await admin_music_monitor.mirror_private_audio_to_admin_group(
        message,
        file_id="telegram-file-id",
        title="Example Song",
        performer="Example Artist",
        duration=205,
        platform="Spotify",
    )

    kwargs = fake_bot.send_audio.await_args.kwargs
    assert kwargs["chat_id"] == -1009876543210
    assert kwargs["audio"] == "telegram-file-id"
    assert kwargs["title"] == "Example Song"
    assert kwargs["performer"] == "Example Artist"
    assert kwargs["duration"] == 205
    assert "Username: @listener" in kwargs["caption"]
    assert "ID: 998877" in kwargs["caption"]
    assert "Nama: Test Listener" in kwargs["caption"]
    assert "Masa: 06/10/2026 21:44" in kwargs["caption"]
    assert "Platform: Spotify" in kwargs["caption"]


@pytest.mark.asyncio
async def test_owner_private_audio_is_not_mirrored(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(
            id=admin_music_monitor.ADMIN_MUSIC_OWNER_ID,
            type="private",
        ),
        from_user=SimpleNamespace(
            id=admin_music_monitor.ADMIN_MUSIC_OWNER_ID,
            username="owner",
            full_name="Owner",
        ),
        date=datetime.now(timezone.utc),
    )
    fake_bot = SimpleNamespace(send_audio=AsyncMock())
    monitor_lookup = AsyncMock(return_value=(-1009876543210, None))

    monkeypatch.setattr(admin_music_monitor, "bot", fake_bot)
    monkeypatch.setattr(
        admin_music_monitor,
        "get_admin_music_monitor_group",
        monitor_lookup,
    )

    await admin_music_monitor.mirror_private_audio_to_admin_group(
        message,
        file_id="telegram-file-id",
        title="Owner Song",
        platform="YouTube",
    )

    monitor_lookup.assert_not_awaited()
    fake_bot.send_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_destination_persists_through_remote_music_store(monkeypatch):
    admin_music_monitor._monitor_group_id = None
    admin_music_monitor._monitor_group_title = None

    set_connected = AsyncMock(return_value=True)
    add_track = AsyncMock(return_value={"ok": True})
    list_tracks = AsyncMock(
        return_value=[
            {
                "id": 1,
                "service": "admin_monitor",
                "title": "-100111",
                "performer": "Old Monitor",
            },
            {
                "id": 2,
                "service": "admin_monitor",
                "title": "-100222",
                "performer": "Latest Monitor",
            },
        ]
    )

    monkeypatch.setattr(
        admin_music_monitor,
        "set_remote_music_group_connected",
        set_connected,
    )
    monkeypatch.setattr(
        admin_music_monitor,
        "add_remote_music_group_track",
        add_track,
    )
    monkeypatch.setattr(
        admin_music_monitor,
        "list_remote_music_group_tracks",
        list_tracks,
    )

    await admin_music_monitor.set_admin_music_monitor_group(
        -100333,
        group_title="Runtime Monitor",
    )
    assert admin_music_monitor._monitor_group_id == -100333

    admin_music_monitor._monitor_group_id = None
    admin_music_monitor._monitor_group_title = None
    group_id, title = await admin_music_monitor.get_admin_music_monitor_group()

    assert group_id == -100222
    assert title == "Latest Monitor"
