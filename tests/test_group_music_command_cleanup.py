"""Regression coverage for the reduced MusiX group command surface."""

from config import BOT_COMMANDS, GROUP_MUSIC_COMMANDS
from handlers import group_music, private_music


REMOVED_GROUP_COMMANDS = {
    "clearlink",
    "playlist",
    "playall",
    "search",
    "playsync",
    "stopsync",
}


def _registered_callbacks(router):
    return {handler.callback.__name__ for handler in router.message.handlers}


def test_group_command_menu_only_lists_connect_and_clearall():
    names = [command["command"] for command in GROUP_MUSIC_COMMANDS]
    assert names == ["connectmusic", "clearall"]
    assert REMOVED_GROUP_COMMANDS.isdisjoint(names)


def test_removed_group_handlers_are_not_registered():
    names = _registered_callbacks(group_music.router)
    assert {
        "connect_music_group",
        "connect_admin_music_monitor",
        "clear_all_group_text",
    }.issubset(names)
    assert {
        "show_group_playlist",
        "clear_music_links",
        "play_all_group_music",
        "search_music",
        "play_sync_info",
        "stop_sync_info",
    }.isdisjoint(names)


def test_private_search_and_playall_stay_available():
    names = _registered_callbacks(private_music.router)
    assert "play_all_private_music" in names
    assert "search_private_music" in names
    default_names = {command["command"] for command in BOT_COMMANDS}
    assert {"search", "playall"}.issubset(default_names)


def test_group_audio_registry_and_cleanup_remain_available():
    """Audio collection and /clearall are distinct from the removed commands."""
    names = _registered_callbacks(group_music.router)
    assert "connect_music_group" in names
    assert "clear_all_group_text" in names
