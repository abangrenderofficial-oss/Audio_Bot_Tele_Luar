"""Tests for quiet scheduled and manual MusiX cleanup decisions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import music_chat_cleanup as cleanup


def _track(mid, source, title="Song", performer="Artist", file_id=None):
    return SimpleNamespace(
        audio_message_id=mid,
        source_url=source, service="youtube", title=title,
        performer=performer, telegram_file_id=file_id or f"audio-{mid}",
    )


def _seen(mid, is_audio=False, audio_key=None):
    return SimpleNamespace(message_id=mid, is_audio=is_audio, audio_key=audio_key)


def test_cleanup_plan_preserves_one_original_song_and_audio_captions():
    tracks = [
        _track(20, "https://youtu.be/Ftffph3fVEs"),
        _track(23, "https://youtu.be/Ftffph3fVEs"),
        _track(27, "https://youtu.be/dQw4w9WgXcQ", "Another Song"),
    ]
    all_audio, duplicate_ids, keeper = cleanup.cleanup_plan(tracks, [
        _seen(10), _seen(20, True), _seen(23, True), _seen(21),
    ])
    assert all_audio == {20, 23, 27}
    assert duplicate_ids == {23}
    assert [t.audio_message_id for t in keeper] == [20, 27]


def test_cleanup_plan_keeps_voice_and_uploaded_audio_but_dedupes_exact_file():
    audio, duplicate_ids, keeper = cleanup.cleanup_plan([], [
        _seen(6, True, "telegram:unique-a"),
        _seen(8, True, "telegram:unique-b"),
        _seen(10, True, "telegram:unique-a"),
        _seen(11, True),  # unknown identity: preserve
    ])
    assert audio == {6, 8, 10, 11}
    assert duplicate_ids == {10}
    assert keeper == []


@pytest.mark.asyncio
async def test_private_cleanup_deletes_observed_text_and_one_duplicate_only(monkeypatch):
    from handlers import private_music
    from services import private_music_playlist

    rows = [
        _track(20, "https://youtu.be/Ftffph3fVEs"),
        _track(24, "https://youtu.be/Ftffph3fVEs"),
    ]
    mock_db = SimpleNamespace(
        get_music_chat_cleanup_messages=AsyncMock(return_value=[
            _seen(15), _seen(18, True, "telegram:user-audio"),
            _seen(24, True),
            _seen(29),
        ]),
        remove_music_chat_cleanup_messages=AsyncMock(),
    )
    delete = AsyncMock(return_value=({14, 15, 24, 29, 30}, set()))
    keep_titles = AsyncMock()
    monkeypatch.setattr(cleanup, "db", mock_db)
    monkeypatch.setattr(private_music_playlist, "list_private_tracks_raw", AsyncMock(return_value=rows))
    monkeypatch.setattr(private_music_playlist, "get_private_source_message_ids", AsyncMock(return_value={14}))
    monkeypatch.setattr(private_music_playlist, "delete_private_messages", delete)
    monkeypatch.setattr(private_music, "_clean_private_audio_message", keep_titles)

    stats = await cleanup.cleanup_music_chat(100, chat_type="private", command_message_id=30)

    targets = delete.await_args.args[1]
    assert targets == {14, 15, 24, 29, 30}
    assert 18 not in targets and 20 not in targets
    assert stats == {"deleted": 5, "duplicates": 1, "failed": 0}
    keep_titles.assert_awaited_once_with(100, rows[0])
    mock_db.remove_music_chat_cleanup_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_connected_group_cleanup_and_monitor_exclusion(monkeypatch):
    from handlers import group_music

    mock_db = SimpleNamespace(
        is_music_group_connected=AsyncMock(side_effect=[False, True]),
        list_music_group_tracks_raw=AsyncMock(return_value=[_track(20, "https://youtu.be/Ftffph3fVEs")]),
        get_music_group_source_message_ids=AsyncMock(return_value=[10]),
        get_music_group_cleanup_message_ids=AsyncMock(return_value=[11, 20]),
        get_music_chat_cleanup_messages=AsyncMock(return_value=[_seen(12), _seen(19, True)]),
        remove_music_chat_cleanup_messages=AsyncMock(),
        mark_music_group_links_cleared=AsyncMock(),
        remove_music_group_cleanup_messages=AsyncMock(),
    )
    delete = AsyncMock(return_value=({10, 11, 12, 13}, set()))
    update_audio = AsyncMock()
    monkeypatch.setattr(cleanup, "db", mock_db)
    monkeypatch.setattr(group_music, "_delete_group_messages", delete)
    monkeypatch.setattr(group_music, "_clean_group_audio_message", update_audio)

    skipped = await cleanup.cleanup_music_chat(-200, chat_type="group")
    assert skipped["deleted"] == 0
    delete.assert_not_awaited()

    result = await cleanup.cleanup_music_chat(-100, chat_type="group", command_message_id=13)
    assert delete.await_args.args[1] == {10, 11, 12, 13}
    assert result["deleted"] == 4
    update_audio.assert_awaited_once_with(-100, mock_db.list_music_group_tracks_raw.return_value[0])


@pytest.mark.asyncio
async def test_daily_autoclean_does_not_send_any_chat_message(monkeypatch):
    chat_cleanup = AsyncMock(return_value={"deleted": 2, "duplicates": 1, "failed": 0})
    mock_db = SimpleNamespace(list_music_chat_cleanup_targets=AsyncMock(return_value=[
        (101, "private"), (-100, "group"),
    ]))
    monkeypatch.setattr(cleanup, "db", mock_db)
    monkeypatch.setattr(cleanup, "cleanup_music_chat", chat_cleanup)
    monkeypatch.setattr(cleanup.asyncio, "sleep", AsyncMock())

    await cleanup.autoclean_music_chats()

    assert chat_cleanup.await_count == 2
    chat_cleanup.assert_any_await(101, chat_type="private")
    chat_cleanup.assert_any_await(-100, chat_type="group")
