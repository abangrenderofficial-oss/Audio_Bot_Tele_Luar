from types import SimpleNamespace

from services.music_group_dedupe import (
    clean_music_title,
    dedupe_music_group_tracks,
    duplicate_keeper_message_id,
)


def _track(
    message_id: int,
    *,
    service: str = "spotify",
    source_url: str = "https://open.spotify.com/track/13KXgSl4vs7h8y2G7V6GrB",
    title: str = "mejikuhibiniu",
    performer: str = "Tenxi, Suisei, Jemsii",
    file_id: str = "telegram-file",
):
    return SimpleNamespace(
        audio_message_id=message_id,
        service=service,
        source_url=source_url,
        title=title,
        performer=performer,
        telegram_file_id=file_id,
    )


def test_dedupe_keeps_first_copy_of_same_song():
    first = _track(200, file_id="file-a")
    second = _track(201, file_id="file-b")

    kept, duplicates = dedupe_music_group_tracks([first, second])

    assert [row.audio_message_id for row in kept] == [200]
    assert [row.audio_message_id for row in duplicates] == [201]


def test_dedupe_treats_lirik_suffix_as_same_song():
    first = _track(
        180,
        service="youtube",
        source_url="https://youtu.be/Ftffph3fVEs",
        title="énau feat. Ari Lesmana - Sesi Potret | Lirik",
        performer="HX Radio",
        file_id="youtube-a",
    )
    second = _track(
        181,
        service="youtube",
        source_url="https://www.youtube.com/watch?v=Ftffph3fVEs",
        title="énau feat. Ari Lesmana - Sesi Potret",
        performer="HX Radio",
        file_id="youtube-b",
    )

    kept, duplicates = dedupe_music_group_tracks([first, second])

    assert [row.audio_message_id for row in kept] == [180]
    assert [row.audio_message_id for row in duplicates] == [181]
    assert clean_music_title(first.title) == "énau feat. Ari Lesmana - Sesi Potret"


def test_duplicate_keeper_always_prefers_older_group_message():
    first = _track(300, file_id="file-a")
    keeper = duplicate_keeper_message_id(
        [first],
        candidate_audio_message_id=305,
        service="spotify",
        source_url=first.source_url,
        title=first.title,
        performer=first.performer,
        telegram_file_id="file-b",
    )

    assert keeper == 300
