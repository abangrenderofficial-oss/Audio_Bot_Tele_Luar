from services.media.music_download import (
    BITRATE_CHOICES_KBPS,
    MIN_SINGLE_FILE_KBPS,
    SEGMENT_SECONDS,
    SPLIT_BITRATE_KBPS,
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
