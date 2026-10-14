from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import group_music


class DummyMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=-100123, type="supergroup")
        self.from_user = SimpleNamespace(id=42)
        self.message_id = 104
        self.reply = AsyncMock()


@pytest.mark.asyncio
async def test_group_clearall_reuses_shared_cleanup_without_admin_gate(monkeypatch):
    from services import music_chat_cleanup

    message = DummyMessage()
    fake_db = SimpleNamespace(is_music_group_connected=AsyncMock(return_value=True))
    cleanup = AsyncMock(return_value={"deleted": 2, "duplicates": 0, "failed": 0})
    admin_gate = AsyncMock(side_effect=AssertionError("member cleanup should not need admin"))

    monkeypatch.setattr(group_music, "db", fake_db)
    monkeypatch.setattr(group_music, "_is_monitor_group", AsyncMock(return_value=False))
    monkeypatch.setattr(group_music, "_require_group_admin", admin_gate)
    monkeypatch.setattr(music_chat_cleanup, "cleanup_music_chat", cleanup)

    await group_music.clear_all_group_text(message)

    admin_gate.assert_not_awaited()
    cleanup.assert_awaited_once_with(
        message.chat.id, chat_type="group", command_message_id=message.message_id,
    )


@pytest.mark.asyncio
async def test_reply_tracked_records_bot_text(monkeypatch):
    message = DummyMessage()
    sent = SimpleNamespace(message_id=555)
    message.reply = AsyncMock(return_value=sent)
    fake_db = SimpleNamespace(add_music_group_cleanup_message=AsyncMock())

    monkeypatch.setattr(group_music, "db", fake_db)

    await group_music._reply_tracked(message, "hello")

    fake_db.add_music_group_cleanup_message.assert_awaited_once_with(
        group_id=message.chat.id,
        message_id=555,
        kind="bot_text",
    )


@pytest.mark.asyncio
async def test_connectmusic_can_be_used_by_regular_group_member(monkeypatch):
    message = DummyMessage()
    message.reply = AsyncMock(return_value=SimpleNamespace(message_id=700))
    fake_db = SimpleNamespace(
        set_music_group_connected=AsyncMock(),
        get_music_group_track_count=AsyncMock(return_value=0),
        add_music_group_cleanup_message=AsyncMock(),
    )
    admin_gate = AsyncMock(side_effect=AssertionError("admin gate must not be called"))

    monkeypatch.setattr(group_music, "db", fake_db)
    monkeypatch.setattr(group_music, "_ensure_group_record", AsyncMock())
    monkeypatch.setattr(group_music, "_is_monitor_group", AsyncMock(return_value=False))
    monkeypatch.setattr(group_music, "_require_group_admin", admin_gate)

    await group_music.connect_music_group(message)

    admin_gate.assert_not_awaited()
    fake_db.set_music_group_connected.assert_awaited_once_with(
        message.chat.id,
        connected=True,
        connected_by_user_id=message.from_user.id,
    )
    message.reply.assert_awaited_once()



@pytest.mark.asyncio
async def test_group_clearall_requires_connected_music_group(monkeypatch):
    from services import music_chat_cleanup
    message = DummyMessage()
    message.reply = AsyncMock(return_value=SimpleNamespace(message_id=1000))
    db = SimpleNamespace(
        is_music_group_connected=AsyncMock(return_value=False),
        add_music_group_cleanup_message=AsyncMock(),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(group_music, "db", db)
    monkeypatch.setattr(group_music, "_is_monitor_group", AsyncMock(return_value=False))
    monkeypatch.setattr(music_chat_cleanup, "cleanup_music_chat", cleanup)
    await group_music.clear_all_group_text(message)
    cleanup.assert_not_awaited()
    message.reply.assert_awaited_once()
