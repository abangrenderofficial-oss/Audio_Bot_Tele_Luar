from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import private_music
from services import private_music_playlist


def _track(
    message_id: int,
    *,
    title: str = "Example Song",
    performer: str = "Example Artist",
    source_url: str = "https://youtu.be/Ftffph3fVEs",
    file_id: str = "file-id",
):
    return SimpleNamespace(
        id=message_id,
        service="youtube",
        source_url=source_url,
        title=title,
        performer=performer,
        telegram_file_id=file_id,
        duration_seconds=200,
        source_message_id=message_id - 1,
        audio_message_id=message_id,
    )


@pytest.mark.asyncio
async def test_private_clearall_preserves_all_audio_including_duplicates(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        message_id=30,
    )
    first = _track(20, file_id="file-a")
    duplicate = _track(22, file_id="file-b")

    delete_messages = AsyncMock(
        return_value=({19, 21, 30}, set())
    )
    clean_audio = AsyncMock()

    monkeypatch.setattr(
        private_music,
        "list_private_tracks_raw",
        AsyncMock(return_value=[first, duplicate]),
    )
    monkeypatch.setattr(
        private_music,
        "get_private_source_message_ids",
        AsyncMock(return_value={19, 21}),
    )
    monkeypatch.setattr(
        private_music,
        "delete_private_messages",
        delete_messages,
    )
    monkeypatch.setattr(
        private_music,
        "_clean_private_audio_message",
        clean_audio,
    )
    monkeypatch.setattr(private_music, "_CLEARALL_SWEEP_LIMIT", 50)

    await private_music.clear_all_private_music(message)

    target_ids = delete_messages.await_args.args[1]
    assert 20 not in target_ids
    assert 22 not in target_ids
    assert 19 in target_ids
    assert 21 in target_ids
    assert 30 in target_ids
    assert clean_audio.await_count == 2
    clean_audio.assert_any_await(message.chat.id, first)
    clean_audio.assert_any_await(message.chat.id, duplicate)


@pytest.mark.asyncio
async def test_private_clearall_without_audio_registry_does_not_blind_sweep(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        message_id=128,
    )
    delete_messages = AsyncMock(return_value=({120, 128}, set()))

    monkeypatch.setattr(
        private_music,
        "list_private_tracks_raw",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        private_music,
        "get_private_source_message_ids",
        AsyncMock(return_value={120}),
    )
    monkeypatch.setattr(
        private_music,
        "delete_private_messages",
        delete_messages,
    )
    monkeypatch.setattr(
        private_music,
        "_clean_private_audio_message",
        AsyncMock(),
    )

    await private_music.clear_all_private_music(message)

    target_ids = delete_messages.await_args.args[1]
    assert target_ids == {120, 128}
    assert 121 not in target_ids
    assert 127 not in target_ids


@pytest.mark.asyncio
async def test_private_playall_uses_deduped_playlist(monkeypatch):
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        answer=AsyncMock(return_value=status),
    )
    first = _track(20)
    second = _track(
        25,
        title="Another Song",
        source_url="https://youtu.be/dQw4w9WgXcQ",
        file_id="file-b",
    )

    monkeypatch.setattr(
        private_music,
        "list_private_tracks",
        AsyncMock(return_value=[first, second]),
    )
    sender = AsyncMock(return_value=2)
    monkeypatch.setattr(private_music, "_send_audio_batch", sender)
    monkeypatch.setattr(private_music.asyncio, "sleep", AsyncMock())

    await private_music.play_all_private_music(message)

    sender.assert_awaited_once()
    assert list(sender.await_args.args[1]) == [first, second]
    status.edit_text.assert_awaited_once_with(
        "✅ Playlist dihantar dari awal • 2/2 track."
    )


@pytest.mark.asyncio
async def test_private_search_checks_playlist_first(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        answer=AsyncMock(),
    )
    command = SimpleNamespace(args="example song")
    search_web = AsyncMock()

    monkeypatch.setattr(
        private_music,
        "search_private_tracks",
        AsyncMock(return_value=[_track(20)]),
    )
    monkeypatch.setattr(private_music, "_youtube_search", search_web)

    await private_music.search_private_music(message, command)

    search_web.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "Jumpa dalam playlist private" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_record_private_audio_deletes_new_duplicate(monkeypatch):
    existing = _track(20, file_id="file-old")
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1234, type="private"),
        from_user=SimpleNamespace(id=1234),
        message_id=21,
    )
    fake_bot = SimpleNamespace(delete_message=AsyncMock())

    monkeypatch.setattr(private_music_playlist, "bot", fake_bot)
    monkeypatch.setattr(
        private_music_playlist,
        "list_private_tracks_raw",
        AsyncMock(return_value=[existing]),
    )
    add_remote = AsyncMock()
    monkeypatch.setattr(
        private_music_playlist,
        "add_remote_music_group_track",
        add_remote,
    )

    stored = await private_music_playlist.record_private_audio(
        message,
        service="youtube",
        source_url=existing.source_url,
        file_id="file-new",
        audio_message_id=22,
        title=existing.title,
        performer=existing.performer,
        duration=200,
    )

    assert stored is False
    deleted_ids = {
        call.args[1] for call in fake_bot.delete_message.await_args_list
    }
    assert deleted_ids == {21, 22}
    add_remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_private_audio_persists_new_track(monkeypatch):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=4321, type="private"),
        from_user=SimpleNamespace(id=4321),
        message_id=40,
    )
    monkeypatch.setattr(
        private_music_playlist,
        "list_private_tracks_raw",
        AsyncMock(return_value=[]),
    )
    add_remote = AsyncMock(
        return_value={
            "id": 1,
            "service": "spotify",
            "source_url": "https://open.spotify.com/track/abc12345",
            "title": "Song",
            "performer": "Artist",
            "telegram_file_id": "spotify-file",
            "duration_seconds": 180,
            "source_message_id": 40,
            "audio_message_id": 41,
        }
    )
    monkeypatch.setattr(
        private_music_playlist,
        "add_remote_music_group_track",
        add_remote,
    )

    stored = await private_music_playlist.record_private_audio(
        message,
        service="spotify",
        source_url="https://open.spotify.com/track/abc12345",
        file_id="spotify-file",
        audio_message_id=41,
        title="Song",
        performer="Artist",
        duration=180,
    )

    assert stored is True
    add_remote.assert_awaited_once()
