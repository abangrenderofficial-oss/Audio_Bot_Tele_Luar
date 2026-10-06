from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers import soundcloud
from utils.download_manager import DownloadMetrics


def test_strip_soundcloud_url_strips_tracking():
    url = "https://soundcloud.com/artist/track-name?si=abc123&utm_source=clipboard#frag"
    assert soundcloud.strip_soundcloud_url(url) == "https://soundcloud.com/artist/track-name"


def test_parse_soundcloud_track_tunnel():
    payload = {
        "status": "tunnel",
        "url": "https://cdn.example.com/audio.mp3",
        "filename": "artist-track.mp3",
    }
    track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/track")
    assert track is not None
    assert track.audio_url == "https://cdn.example.com/audio.mp3"
    assert track.title != ""
    same_track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/track")
    other_track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/other")
    assert same_track is not None and same_track.id == track.id
    assert other_track is not None and other_track.id != track.id


def test_parse_soundcloud_track_local_processing_with_cover():
    payload = {
        "status": "local-processing",
        "type": "audio",
        "tunnel": [
            "https://cdn.example.com/cover.jpg",
            "https://cdn.example.com/final.mp3",
        ],
        "output": {
            "type": "audio/mpeg",
            "filename": "final.mp3",
            "metadata": {
                "title": "Track Title",
                "artist": "Artist Name",
                "duration": "123.6",
            },
        },
        "audio": {"cover": True},
    }
    track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/track")
    assert track is not None
    assert track.audio_url == "https://cdn.example.com/final.mp3"
    assert track.thumbnail_url == "https://cdn.example.com/cover.jpg"
    assert track.title == "Track Title"
    assert track.artist == "Artist Name"
    assert track.duration_seconds == 124


def test_parse_soundcloud_track_deduplicates_repeated_artist_metadata():
    payload = {
        "status": "local-processing",
        "type": "audio",
        "tunnel": ["https://cdn.example.com/final.mp3"],
        "output": {
            "type": "audio/mpeg",
            "metadata": {
                "title": "Тону",
                "artist": "SUDNO, sudno, SUDNO, Sudno, SUDNO",
            },
        },
        "audio": {"cover": False},
    }

    track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/sudno_mp3/tonu")

    assert track is not None
    assert track.artist == "SUDNO"


def test_parse_soundcloud_track_recognizes_extensionless_cobalt_cover_tunnel():
    payload = {
        "status": "local-processing",
        "type": "audio",
        "tunnel": [
            "https://cobalt.example/tunnel/audio-token",
            "https://cobalt.example/tunnel/cover-token",
        ],
        "output": {
            "type": "audio/mpeg",
            "metadata": {"title": "Track", "artist": "Artist"},
        },
        "audio": {"cover": True},
    }

    track = soundcloud.parse_soundcloud_track(payload, "https://soundcloud.com/artist/track")

    assert track is not None
    assert track.audio_url.endswith("/audio-token")
    assert track.thumbnail_url.endswith("/cover-token")


@pytest.mark.asyncio
async def test_soundcloud_service_fetch_track_uses_cobalt_client(monkeypatch, tmp_path):
    captured = {}
    payload = {
        "status": "tunnel",
        "url": "https://cdn.example.com/audio.mp3",
        "filename": "track.mp3",
    }

    async def fake_fetch_cobalt_data(base_url, api_key, request_payload, **kwargs):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["payload"] = request_payload
        captured["kwargs"] = kwargs
        return payload

    monkeypatch.setattr(soundcloud, "COBALT_API_URL", "https://cobalt.test")
    monkeypatch.setattr(soundcloud, "COBALT_API_KEY", "test-key")
    monkeypatch.setattr(soundcloud, "fetch_cobalt_data", fake_fetch_cobalt_data)

    service = soundcloud.SoundCloudService(output_dir=str(tmp_path))
    track = await service.fetch_track("https://soundcloud.com/artist/track")

    assert track is not None
    assert captured["base_url"] == "https://cobalt.test"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["downloadMode"] == "audio"
    assert captured["kwargs"]["source"] == "soundcloud"


@pytest.mark.asyncio
async def test_soundcloud_service_download_media_success(monkeypatch, tmp_path):
    service = soundcloud.SoundCloudService(output_dir=str(tmp_path))

    async def fake_download(url, filename, **_kwargs):
        path = tmp_path / filename
        path.write_bytes(b"audio")
        return DownloadMetrics(
            url=url,
            path=str(path),
            size=path.stat().st_size,
            elapsed=0.01,
            used_multipart=False,
            resumed=False,
        )

    monkeypatch.setattr(service._downloader, "download", fake_download)
    metrics = await service.download_media("https://cdn.example.com/audio.mp3", "track.mp3")

    assert metrics is not None
    assert (tmp_path / "track.mp3").exists()


@pytest.mark.asyncio
async def test_soundcloud_service_download_media_handles_error(monkeypatch, tmp_path):
    service = soundcloud.SoundCloudService(output_dir=str(tmp_path))

    async def fake_download(*_args, **_kwargs):
        raise soundcloud.DownloadError("boom")

    monkeypatch.setattr(service._downloader, "download", fake_download)
    metrics = await service.download_media("https://cdn.example.com/audio.mp3", "track.mp3")

    assert metrics is None


@pytest.mark.asyncio
async def test_inline_soundcloud_uses_bot_avatar_thumbnail(monkeypatch, tmp_path):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    token = soundcloud.create_inline_video_request(
        "soundcloud",
        "https://soundcloud.com/artist/track",
        42,
        settings,
    )
    result = SimpleNamespace(
        result_id=f"soundcloud_inline:{token}",
        inline_message_id="inline-message-1",
        from_user=SimpleNamespace(full_name="Inline User"),
    )
    audio_path = tmp_path / "track.mp3"
    audio_path.write_bytes(b"audio")
    metrics = DownloadMetrics(
        url="https://cdn.example.com/audio.mp3",
        path=str(audio_path),
        size=audio_path.stat().st_size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )
    bot_avatar = object()

    monkeypatch.setattr(
        soundcloud.soundcloud_service,
        "fetch_track",
        AsyncMock(
            return_value=soundcloud.SoundCloudTrack(
                id="track-1",
                source_url="https://soundcloud.com/artist/track",
                audio_url="https://cdn.example.com/audio.mp3",
                title="Track Title",
                artist="Artist Name",
                thumbnail_url="https://cdn.example.com/cover.jpg",
                duration_seconds=199,
            )
        ),
    )
    monkeypatch.setattr(
        soundcloud.soundcloud_service,
        "download_media",
        AsyncMock(return_value=metrics),
    )
    monkeypatch.setattr(soundcloud.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(soundcloud.db, "add_file", AsyncMock())
    monkeypatch.setattr(soundcloud, "get_bot_url", AsyncMock(return_value="https://t.me/maxloadbot"))
    monkeypatch.setattr(soundcloud, "get_bot_avatar_thumbnail", AsyncMock(return_value=bot_avatar))
    prepared_metadata = SimpleNamespace(thumbnail_path=None, cleanup=Mock())
    monkeypatch.setattr(
        soundcloud,
        "prepare_mp3_metadata",
        AsyncMock(return_value=prepared_metadata),
    )
    monkeypatch.setattr(soundcloud, "safe_edit_inline_text", AsyncMock(return_value=True))
    monkeypatch.setattr(soundcloud, "safe_edit_inline_media", AsyncMock(return_value=True))
    monkeypatch.setattr(soundcloud, "remove_file", AsyncMock())
    monkeypatch.setattr(
        soundcloud.bot,
        "send_audio",
        AsyncMock(return_value=SimpleNamespace(audio=SimpleNamespace(file_id="cached-file-id"))),
    )

    await soundcloud.chosen_inline_soundcloud_result(result)

    send_kwargs = soundcloud.bot.send_audio.await_args.kwargs
    assert send_kwargs["thumbnail"] is bot_avatar
    assert send_kwargs["performer"] == "Artist Name"
    assert send_kwargs["duration"] == 199
    assert soundcloud.soundcloud_service.download_media.await_count == 1
    prepared_metadata.cleanup.assert_called_once_with()


@pytest.mark.asyncio
async def test_inline_soundcloud_query_prefers_track_thumbnail(monkeypatch):
    settings = {
        "captions": "on",
        "delete_message": "off",
        "info_buttons": "on",
        "url_button": "on",
        "audio_button": "on",
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat_type="inline",
        query="https://soundcloud.com/artist/track",
        answer=AsyncMock(),
    )
    track = soundcloud.SoundCloudTrack(
        id="track-1",
        source_url="https://soundcloud.com/artist/track",
        audio_url="https://cdn.example.com/audio.mp3",
        title="Track Title",
        artist="Artist Name",
        thumbnail_url="https://cdn.example.com/cover.jpg",
    )

    monkeypatch.setattr(soundcloud, "CHANNEL_ID", -1001234567890)
    monkeypatch.setattr(soundcloud, "send_analytics", AsyncMock())
    monkeypatch.setattr(soundcloud.db, "user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(soundcloud.soundcloud_service, "fetch_track", AsyncMock(return_value=track))

    await soundcloud.inline_soundcloud_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].thumbnail_url == "https://cdn.example.com/cover.jpg"



@pytest.mark.asyncio
async def test_soundcloud_fast_path_adds_connected_group_playlist(monkeypatch):
    message = SimpleNamespace(
        message_id=321,
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=-100777, type="supergroup"),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        reply_audio=AsyncMock(
            return_value=SimpleNamespace(
                message_id=654,
                audio=SimpleNamespace(
                    file_id="soundcloud-telegram-file-id-123456",
                    title="Track Title",
                    performer="Artist Name",
                    duration=124,
                ),
            )
        ),
        reply=AsyncMock(),
    )
    track = soundcloud.SoundCloudTrack(
        id="trackhash",
        source_url="https://on.soundcloud.com/t6e6Wwo57tyYeObzcG",
        audio_url="https://cobalt.example/tunnel/audio-token",
        title="Track Title",
        artist="Artist Name",
        thumbnail_url=None,
        duration_seconds=124,
    )

    monkeypatch.setattr(
        soundcloud.db,
        "is_music_group_connected",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(soundcloud.db, "add_music_group_track", AsyncMock())
    monkeypatch.setattr(
        soundcloud,
        "should_skip_duplicate_business_message",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(soundcloud, "react_to_message", AsyncMock())
    monkeypatch.setattr(soundcloud, "send_analytics", AsyncMock())
    monkeypatch.setattr(
        soundcloud,
        "load_user_settings",
        AsyncMock(return_value={"captions": "off", "delete_message": "off"}),
    )
    monkeypatch.setattr(
        soundcloud,
        "get_bot_url",
        AsyncMock(return_value="https://t.me/maxloadbot"),
    )
    monkeypatch.setattr(
        soundcloud,
        "get_bot_avatar_thumbnail",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        soundcloud,
        "get_cached_social_audio",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        soundcloud,
        "store_cached_social_audio",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        soundcloud.soundcloud_service,
        "fetch_track",
        AsyncMock(return_value=track),
    )
    monkeypatch.setattr(soundcloud, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(soundcloud, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(soundcloud, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(soundcloud, "update_info", AsyncMock())
    monkeypatch.setattr(soundcloud, "maybe_delete_user_message", AsyncMock())

    await soundcloud.process_soundcloud(
        message,
        direct_url="https://on.soundcloud.com/t6e6Wwo57tyYeObzcG",
    )

    soundcloud.soundcloud_service.fetch_track.assert_awaited_once_with(
        "https://on.soundcloud.com/t6e6Wwo57tyYeObzcG"
    )
    kwargs = message.reply_audio.await_args.kwargs
    assert kwargs["audio"] == "https://cobalt.example/tunnel/audio-token"
    assert kwargs["title"] == "Track Title"
    assert kwargs["performer"] == "Artist Name"
    assert kwargs["duration"] == 124

    soundcloud.store_cached_social_audio.assert_awaited_once()
    cache_kwargs = soundcloud.store_cached_social_audio.await_args.kwargs
    assert cache_kwargs["telegram_file_id"] == "soundcloud-telegram-file-id-123456"
    assert cache_kwargs["variant"] == "fast_original"

    soundcloud.db.add_music_group_track.assert_awaited_once()
    playlist_kwargs = soundcloud.db.add_music_group_track.await_args.kwargs
    assert playlist_kwargs["service"] == "soundcloud"
    assert playlist_kwargs["source_url"] == (
        "https://on.soundcloud.com/t6e6Wwo57tyYeObzcG"
    )
    assert playlist_kwargs["title"] == "Track Title"
    assert playlist_kwargs["performer"] == "Artist Name"
    assert playlist_kwargs["telegram_file_id"] == (
        "soundcloud-telegram-file-id-123456"
    )
    assert playlist_kwargs["source_message_id"] == 321
    assert playlist_kwargs["audio_message_id"] == 654


@pytest.mark.asyncio
async def test_soundcloud_group_requires_connectmusic(monkeypatch):
    message = SimpleNamespace(
        message_id=321,
        from_user=SimpleNamespace(id=7),
        business_connection_id=None,
        chat=SimpleNamespace(id=-100777, type="group"),
        reply_audio=AsyncMock(),
    )
    monkeypatch.setattr(
        soundcloud.db,
        "is_music_group_connected",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        soundcloud,
        "should_skip_duplicate_business_message",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(soundcloud, "update_info", AsyncMock())
    monkeypatch.setattr(soundcloud, "safe_delete_message", AsyncMock())

    await soundcloud.process_soundcloud(
        message,
        direct_url="https://on.soundcloud.com/t6e6Wwo57tyYeObzcG",
    )

    message.reply_audio.assert_not_awaited()
