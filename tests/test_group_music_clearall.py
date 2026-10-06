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
async def test_clearall_sweeps_untracked_text_and_preserves_playlist_audio(monkeypatch):
    message = DummyMessage()
    message.message_id = 220
    fake_db = SimpleNamespace(
        is_music_group_connected=AsyncMock(return_value=True),
        list_music_group_tracks=AsyncMock(
            return_value=[
                SimpleNamespace(audio_message_id=200),
                SimpleNamespace(audio_message_id=201),
            ]
        ),
        get_music_group_cleanup_message_ids=AsyncMock(return_value=[101, 102, 200]),
        get_music_group_source_message_ids=AsyncMock(return_value=[103]),
        mark_music_group_links_cleared=AsyncMock(),
        remove_music_group_cleanup_messages=AsyncMock(),
    )
    fake_bot = SimpleNamespace(
        delete_messages=AsyncMock(return_value=True),
        delete_message=AsyncMock(),
    )

    monkeypatch.setattr(group_music, "db", fake_db)
    monkeypatch.setattr(group_music, "bot", fake_bot)
    monkeypatch.setattr(group_music, "_CLEARALL_SWEEP_LIMIT", 20)
    monkeypatch.setattr(group_music, "_CLEARALL_SWEEP_MARGIN", 3)
    monkeypatch.setattr(
        group_music,
        "_require_group_admin",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(group_music.asyncio, "sleep", AsyncMock())

    await group_music.clear_all_group_text(message)

    deleted_ids = {
        message_id
        for call in fake_bot.delete_messages.await_args_list
        for message_id in call.kwargs["message_ids"]
    }
    # Explicit tracked/source ids survive deploy gaps and the range sweep also
    # catches untracked bot errors/text between the song audios and /clearall.
    assert {101, 102, 103}.issubset(deleted_ids)
    assert set(range(202, 221)).issubset(deleted_ids)
    assert 200 not in deleted_ids
    assert 201 not in deleted_ids
    fake_bot.delete_message.assert_not_awaited()

    fake_db.mark_music_group_links_cleared.assert_awaited_once_with(
        message.chat.id,
        [103],
    )
    fake_db.remove_music_group_cleanup_messages.assert_awaited_once()


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
    monkeypatch.setattr(group_music, "_require_group_admin", admin_gate)

    await group_music.connect_music_group(message)

    admin_gate.assert_not_awaited()
    fake_db.set_music_group_connected.assert_awaited_once_with(
        message.chat.id,
        connected=True,
        connected_by_user_id=message.from_user.id,
    )
    message.reply.assert_awaited_once()

