from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers import spotify
from utils.download_manager import DownloadMetrics


@pytest.mark.asyncio
async def test_process_spotify_downloads_matching_track_with_metadata(monkeypatch, tmp_path):
    audio_path = tmp_path / "spotify.mp3"
    audio_path.write_bytes(b"audio")
    metrics = DownloadMetrics(
        url="https://youtube.com/watch?v=abcdefghijk",
        path=str(audio_path),
        size=audio_path.stat().st_size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        reply_audio=AsyncMock(
            return_value=SimpleNamespace(audio=SimpleNamespace(file_id="telegram-audio-id"))
        ),
        reply=AsyncMock(),
    )
    track = {
        "spotify_id": "abc123",
        "title": "Track Name",
        "artists": "Artist Name",
        "thumbnail": "https://i.scdn.co/image/cover",
        "duration": 201,
        "source_url": "https://open.spotify.com/track/abc123",
    }

    monkeypatch.setattr(spotify, "should_skip_duplicate_business_message", AsyncMock(return_value=False))
    monkeypatch.setattr(spotify, "react_to_message", AsyncMock())
    monkeypatch.setattr(spotify, "send_analytics", AsyncMock())
    monkeypatch.setattr(spotify, "load_user_settings", AsyncMock(return_value={"captions": "off", "delete_message": "off"}))
    monkeypatch.setattr(spotify, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(spotify, "get_bot_avatar_thumbnail", AsyncMock(return_value=None))
    monkeypatch.setattr(spotify.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(spotify.db, "add_file", AsyncMock())
    monkeypatch.setattr(spotify, "get_cached_social_audio", AsyncMock(return_value=None))
    monkeypatch.setattr(spotify, "get_cached_audio", AsyncMock(return_value=None))
    monkeypatch.setattr(spotify, "store_cached_social_audio", AsyncMock(return_value=True))
    monkeypatch.setattr(spotify, "store_cached_audio", AsyncMock(return_value=True))
    monkeypatch.setattr(
        spotify,
        "send_youtube_fast_to_telegram",
        AsyncMock(side_effect=RuntimeError("fast worker unavailable")),
    )
    monkeypatch.setattr(spotify, "get_spotify_track", AsyncMock(return_value=track))
    monkeypatch.setattr(spotify, "search_youtube_track", lambda _query: {"webpage_url": metrics.url})
    monkeypatch.setattr(spotify, "download_mp3_with_ytdlp_metrics", AsyncMock(return_value=metrics))
    prepared_metadata = SimpleNamespace(thumbnail_path=None, cleanup=Mock())
    monkeypatch.setattr(
        spotify,
        "prepare_mp3_metadata",
        AsyncMock(return_value=prepared_metadata),
    )
    monkeypatch.setattr(spotify, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(spotify, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(spotify, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(spotify, "remove_file", AsyncMock())
    monkeypatch.setattr(spotify, "update_info", AsyncMock())

    await spotify.process_spotify(
        message, direct_url="https://open.spotify.com/track/abc123?si=demo"
    )

    spotify.download_mp3_with_ytdlp_metrics.assert_awaited_once()
    spotify.prepare_mp3_metadata.assert_awaited_once_with(str(audio_path), track)
    prepared_metadata.cleanup.assert_called_once_with()
    kwargs = message.reply_audio.await_args.kwargs
    assert kwargs["audio"].filename == "Track Name.mp3"
    assert kwargs["title"] == "Track Name"
    assert kwargs["performer"] == "Artist Name"
    assert kwargs["duration"] == 201
    spotify.db.add_file.assert_awaited_once()



@pytest.mark.asyncio
async def test_process_spotify_fast_worker_adds_group_playlist(monkeypatch):
    message = SimpleNamespace(
        message_id=321,
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=-100777, type="supergroup"),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        reply_audio=AsyncMock(),
        reply=AsyncMock(),
    )
    track = {
        "spotify_id": "2IuAckXwlp3GUOD812FDSC",
        "title": "Gila Bayang",
        "artists": "Ammar Haikal",
        "thumbnail": "https://i.scdn.co/image/cover",
        "duration": 198,
        "source_url": "https://open.spotify.com/track/2IuAckXwlp3GUOD812FDSC",
    }
    youtube_url = "https://www.youtube.com/watch?v=abcdefghijk"
    fast_result = {
        "file_id": "telegram-fast-file-id-123456789",
        "message_id": 654,
        "file_size": 4567890,
        "duration": 198,
        "title": "Gila Bayang",
        "performer": "Ammar Haikal",
    }

    monkeypatch.setattr(
        spotify.db,
        "is_music_group_connected",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(spotify.db, "add_music_group_track", AsyncMock())
    monkeypatch.setattr(
        spotify,
        "should_skip_duplicate_business_message",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(spotify, "react_to_message", AsyncMock())
    monkeypatch.setattr(spotify, "send_analytics", AsyncMock())
    monkeypatch.setattr(
        spotify,
        "load_user_settings",
        AsyncMock(return_value={"captions": "off", "delete_message": "off"}),
    )
    monkeypatch.setattr(
        spotify,
        "get_bot_url",
        AsyncMock(return_value="https://t.me/maxloadbot"),
    )
    monkeypatch.setattr(
        spotify,
        "get_cached_social_audio",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(spotify, "get_cached_audio", AsyncMock(return_value=None))
    monkeypatch.setattr(
        spotify,
        "store_cached_social_audio",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        spotify,
        "store_cached_audio",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        spotify,
        "get_spotify_track",
        AsyncMock(return_value=track),
    )
    monkeypatch.setattr(
        spotify,
        "search_youtube_track",
        lambda _query: {"webpage_url": youtube_url},
    )
    monkeypatch.setattr(
        spotify,
        "send_youtube_fast_to_telegram",
        AsyncMock(return_value=fast_result),
    )
    monkeypatch.setattr(
        spotify,
        "download_mp3_with_ytdlp_metrics",
        AsyncMock(),
    )
    monkeypatch.setattr(spotify, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(spotify, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(spotify, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(spotify, "update_info", AsyncMock())
    monkeypatch.setattr(spotify, "maybe_delete_user_message", AsyncMock())

    await spotify.process_spotify(
        message,
        direct_url=(
            "https://open.spotify.com/track/"
            "2IuAckXwlp3GUOD812FDSC?si=65c3444e6930420a"
        ),
    )

    spotify.send_youtube_fast_to_telegram.assert_awaited_once_with(
        youtube_url,
        chat_id=-100777,
        title="Gila Bayang",
        performer="Ammar Haikal",
        duration=198.0,
        business_connection_id=None,
    )
    spotify.download_mp3_with_ytdlp_metrics.assert_not_awaited()
    spotify.store_cached_social_audio.assert_awaited()
    spotify.store_cached_audio.assert_awaited()
    spotify.db.add_music_group_track.assert_awaited_once()
    kwargs = spotify.db.add_music_group_track.await_args.kwargs
    assert kwargs["service"] == "spotify"
    assert kwargs["source_url"] == (
        "https://open.spotify.com/track/2IuAckXwlp3GUOD812FDSC"
    )
    assert kwargs["title"] == "Gila Bayang"
    assert kwargs["performer"] == "Ammar Haikal"
    assert kwargs["telegram_file_id"] == "telegram-fast-file-id-123456789"
    assert kwargs["source_message_id"] == 321
    assert kwargs["audio_message_id"] == 654
