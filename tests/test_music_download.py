import services.media.music_download as music_download

from services.media.music_download import (
    BITRATE_CHOICES_KBPS,
    MIN_SINGLE_FILE_KBPS,
    SEGMENT_SECONDS,
    SPLIT_BITRATE_KBPS,
    build_music_cache_key,
    build_music_metadata,
    choose_adaptive_bitrate,
    make_music_plan,
)


def test_music_quality_ladder_matches_music_bot_contract():
    assert BITRATE_CHOICES_KBPS == (320, 256, 224, 192, 160, 128)
    assert MIN_SINGLE_FILE_KBPS == 128
    assert SPLIT_BITRATE_KBPS == 128
    assert SEGMENT_SECONDS == 2400


def test_adaptive_bitrate_uses_highest_safe_quality():
    assert choose_adaptive_bitrate(60) == 320
    assert choose_adaptive_bitrate(1800) == 192
    assert choose_adaptive_bitrate(3000) == 128


def test_adaptive_bitrate_splits_instead_of_dropping_below_128():
    assert choose_adaptive_bitrate(3600) is None
    plan = make_music_plan(3600)
    assert plan.mode == "split"
    assert plan.bitrate_kbps == 128


def test_youtube_metadata_keeps_video_title_for_audio():
    metadata = build_music_metadata(
        {
            "title": "Example Song",
            "uploader": "Example Artist",
            "duration": 240,
        },
        source="youtube",
        source_url="https://youtu.be/demo",
    )

    assert metadata.title == "Example Song"
    assert metadata.file_base == "Example Song"
    assert metadata.performer == "Example Artist"


def test_social_metadata_prefers_real_sound_name_not_caption():
    metadata = build_music_metadata(
        {
            "title": "This is a long post caption #viral #fyp",
            "track": "Real Sound Name",
            "uploader_id": "creator123",
            "uploader": "Creator Name",
            "duration": 42,
        },
        source="tiktok",
        source_url="https://www.tiktok.com/@creator123/video/1",
    )

    assert metadata.title == "Real Sound Name"
    assert metadata.file_base == "Real Sound Name"
    assert "viral" not in metadata.title
    assert metadata.performer == "Creator Name"


def test_social_metadata_falls_back_to_original_sound_username():
    metadata = build_music_metadata(
        {
            "title": "Caption must not become the MP3 title",
            "uploader_id": "creator123",
            "uploader": "Creator Name",
            "duration": 42,
        },
        source="instagram",
        source_url="https://www.instagram.com/reel/demo/",
    )

    assert metadata.title == "Original sound — @creator123"
    assert metadata.file_base == "Original sound — @creator123"


def test_music_cache_key_is_isolated_from_legacy_audio_cache():
    key = build_music_cache_key("https://youtu.be/demo")
    assert key == "https://youtu.be/demo#music_adaptive_mp3_v1"
    assert "audio_artist_dedupe" not in key


def test_piped_api_urls_keep_https_urls_intact(monkeypatch):
    monkeypatch.setenv(
        "PIPED_API_URLS",
        "https://pipedapi.duck.party, https://api.piped.private.coffee",
    )

    assert music_download._configured_piped_api_urls() == [
        "https://pipedapi.duck.party",
        "https://api.piped.private.coffee",
    ]


def test_piped_api_urls_support_semicolon_and_whitespace(monkeypatch):
    monkeypatch.setenv(
        "PIPED_API_URLS",
        "https://one.example;https://two.example\nhttps://three.example/",
    )

    assert music_download._configured_piped_api_urls() == [
        "https://one.example",
        "https://two.example",
        "https://three.example",
    ]


def test_low_memory_youtube_mode_tries_piped_before_ytdlp(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "true")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    calls = []

    def fake_piped(url, out_template, bitrate_kbps):
        calls.append(("piped", url, bitrate_kbps))
        return "/tmp/piped.mp3"

    def fail_ytdlp(*args, **kwargs):
        raise AssertionError("yt-dlp should not run after successful Piped")

    monkeypatch.setattr(music_download, "_run_piped_mp3_sync", fake_piped)
    monkeypatch.setattr(music_download, "_run_ytdlp_mp3_once", fail_ytdlp)

    result = music_download._run_ytdlp_mp3_sync(
        "https://youtu.be/Ftffph3fVEs",
        "/tmp/audio.%(ext)s",
        192,
        "youtube",
    )

    assert result == "/tmp/piped.mp3"
    assert calls == [("piped", "https://youtu.be/Ftffph3fVEs", 192)]


def test_low_memory_youtube_mode_falls_back_to_one_primary_ytdlp(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "1")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    calls = []

    def fail_piped(*args, **kwargs):
        calls.append("piped")
        raise RuntimeError("piped unavailable")

    def primary_ytdlp(url, out_template, bitrate_kbps, **kwargs):
        calls.append(("yt-dlp", kwargs))
        return "/tmp/primary.mp3"

    monkeypatch.setattr(music_download, "_run_piped_mp3_sync", fail_piped)
    monkeypatch.setattr(music_download, "_run_ytdlp_mp3_once", primary_ytdlp)
    monkeypatch.setattr(music_download, "_clear_ytdlp_outputs", lambda _path: None)

    result = music_download._run_ytdlp_mp3_sync(
        "https://youtu.be/Ftffph3fVEs",
        "/tmp/audio.%(ext)s",
        192,
        "youtube",
    )

    assert result == "/tmp/primary.mp3"
    assert calls == ["piped", ("yt-dlp", {})]


def test_low_memory_metadata_prefers_piped(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "true")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    expected = {
        "id": "Ftffph3fVEs",
        "title": "Piped Song",
        "uploader": "Piped Artist",
        "duration": 240,
    }
    monkeypatch.setattr(
        music_download,
        "_extract_piped_info_sync",
        lambda _url: expected,
    )
    monkeypatch.setattr(
        music_download,
        "_extract_info_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("yt-dlp metadata should not run after successful Piped")
        ),
    )

    assert music_download._extract_info_sync(
        "https://youtu.be/Ftffph3fVEs",
        "youtube",
    ) == expected


def test_low_memory_metadata_falls_back_to_one_primary_ytdlp(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "yes")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    calls = []

    def fail_piped(_url):
        calls.append("piped")
        raise RuntimeError("piped metadata unavailable")

    def primary(_url, **kwargs):
        calls.append(("yt-dlp", kwargs))
        return {"id": "Ftffph3fVEs", "title": "Primary Song"}

    monkeypatch.setattr(music_download, "_extract_piped_info_sync", fail_piped)
    monkeypatch.setattr(music_download, "_extract_info_once", primary)

    result = music_download._extract_info_sync(
        "https://youtu.be/Ftffph3fVEs",
        "youtube",
    )

    assert result["title"] == "Primary Song"
    assert calls == ["piped", ("yt-dlp", {})]


def test_invidious_api_urls_keep_https_urls_intact(monkeypatch):
    monkeypatch.setenv(
        "INVIDIOUS_API_URLS",
        "https://inv.nadeko.net, https://invidious.nerdvpn.de/",
    )

    assert music_download._configured_invidious_api_urls() == [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
    ]


def test_pick_invidious_audio_stream_prefers_highest_bitrate_audio():
    data = {
        "adaptiveFormats": [
            {
                "url": "https://cdn.example/video",
                "type": "video/mp4",
                "bitrate": 2_000_000,
            },
            {
                "url": "https://cdn.example/audio-low",
                "type": "audio/webm",
                "audioQuality": "AUDIO_QUALITY_MEDIUM",
                "bitrate": 128_000,
            },
            {
                "url": "https://cdn.example/audio-high",
                "type": "audio/mp4",
                "audioQuality": "AUDIO_QUALITY_MEDIUM",
                "bitrate": 160_000,
            },
        ]
    }

    stream = music_download._pick_invidious_audio_stream(data)

    assert stream is not None
    assert stream["url"] == "https://cdn.example/audio-high"


def test_extract_invidious_info_maps_video_metadata(monkeypatch):
    monkeypatch.setattr(
        music_download,
        "_fetch_invidious_video_sync",
        lambda _url: (
            {
                "title": "Example Song",
                "author": "Example Artist",
                "lengthSeconds": 245,
                "videoThumbnails": [
                    {"url": "https://img.example/small.jpg", "width": 120, "height": 90},
                    {"url": "https://img.example/large.jpg", "width": 1280, "height": 720},
                ],
            },
            "https://inv.example",
        ),
    )

    metadata = music_download._extract_invidious_info_sync(
        "https://youtu.be/Ftffph3fVEs"
    )

    assert metadata["id"] == "Ftffph3fVEs"
    assert metadata["title"] == "Example Song"
    assert metadata["uploader"] == "Example Artist"
    assert metadata["duration"] == 245
    assert metadata["thumbnail"] == "https://img.example/large.jpg"


def test_low_memory_audio_prefers_invidious_before_piped(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "true")
    monkeypatch.setenv("INVIDIOUS_API_URLS", "https://inv.example")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    calls = []

    def invidious(url, out_template, bitrate_kbps):
        calls.append(("invidious", url, bitrate_kbps))
        return "/tmp/invidious.mp3"

    def fail_piped(*_args, **_kwargs):
        raise AssertionError("Piped should not run after successful Invidious")

    def fail_ytdlp(*_args, **_kwargs):
        raise AssertionError("yt-dlp should not run after successful Invidious")

    monkeypatch.setattr(music_download, "_run_invidious_mp3_sync", invidious)
    monkeypatch.setattr(music_download, "_run_piped_mp3_sync", fail_piped)
    monkeypatch.setattr(music_download, "_run_ytdlp_mp3_once", fail_ytdlp)

    result = music_download._run_ytdlp_mp3_sync(
        "https://youtu.be/Ftffph3fVEs",
        "/tmp/audio.%(ext)s",
        192,
        "youtube",
    )

    assert result == "/tmp/invidious.mp3"
    assert calls == [("invidious", "https://youtu.be/Ftffph3fVEs", 192)]


def test_low_memory_audio_falls_from_invidious_to_piped(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "true")
    monkeypatch.setenv("INVIDIOUS_API_URLS", "https://inv.example")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    calls = []

    def fail_invidious(*_args, **_kwargs):
        calls.append("invidious")
        raise RuntimeError("invidious unavailable")

    def piped(*_args, **_kwargs):
        calls.append("piped")
        return "/tmp/piped.mp3"

    def fail_ytdlp(*_args, **_kwargs):
        raise AssertionError("yt-dlp should not run after successful Piped")

    monkeypatch.setattr(music_download, "_run_invidious_mp3_sync", fail_invidious)
    monkeypatch.setattr(music_download, "_run_piped_mp3_sync", piped)
    monkeypatch.setattr(music_download, "_run_ytdlp_mp3_once", fail_ytdlp)
    monkeypatch.setattr(music_download, "_clear_ytdlp_outputs", lambda _path: None)

    result = music_download._run_ytdlp_mp3_sync(
        "https://youtu.be/Ftffph3fVEs",
        "/tmp/audio.%(ext)s",
        192,
        "youtube",
    )

    assert result == "/tmp/piped.mp3"
    assert calls == ["invidious", "piped"]


def test_low_memory_metadata_prefers_invidious_before_piped(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "true")
    monkeypatch.setenv("INVIDIOUS_API_URLS", "https://inv.example")
    monkeypatch.setenv("PIPED_API_URLS", "https://piped.example")

    expected = {
        "id": "Ftffph3fVEs",
        "title": "Invidious Song",
        "uploader": "Invidious Artist",
        "duration": 240,
    }
    monkeypatch.setattr(
        music_download,
        "_extract_invidious_info_sync",
        lambda _url: expected,
    )
    monkeypatch.setattr(
        music_download,
        "_extract_piped_info_sync",
        lambda _url: (_ for _ in ()).throw(
            AssertionError("Piped metadata should not run after successful Invidious")
        ),
    )
    monkeypatch.setattr(
        music_download,
        "_extract_info_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("yt-dlp metadata should not run after successful Invidious")
        ),
    )

    assert music_download._extract_info_sync(
        "https://youtu.be/Ftffph3fVEs",
        "youtube",
    ) == expected
