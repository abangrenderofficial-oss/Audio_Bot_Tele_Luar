"""Outgoing Telegram request tracking must never change the send result."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Audio, Chat, Message

from middlewares import music_outgoing_cleanup


def _message(mid: int, *, audio=False):
    kwargs = {}
    if audio:
        kwargs["audio"] = Audio(
            file_id="test-audio", file_unique_id="unique-123", duration=90
        )
    else:
        kwargs["text"] = "Converting..."
    return Message(
        message_id=mid, date=datetime.now(timezone.utc),
        chat=Chat(id=321, type="private"), **kwargs,
    )


@pytest.mark.asyncio
async def test_outgoing_text_is_registered_as_non_audio(monkeypatch):
    tracker = AsyncMock()
    monkeypatch.setattr(music_outgoing_cleanup, "db", SimpleNamespace(
        remember_music_chat_message=tracker,
    ))
    message = _message(10)
    wrapped = AsyncMock(return_value=message)
    result = await music_outgoing_cleanup.MusicOutgoingCleanupMiddleware()(
        wrapped, None, None,
    )
    assert result is message
    tracker.assert_awaited_once_with(
        chat_id=321, message_id=10, chat_type="private",
        is_audio=False, audio_key=None,
    )


@pytest.mark.asyncio
async def test_outgoing_audio_is_protected(monkeypatch):
    tracker = AsyncMock()
    monkeypatch.setattr(music_outgoing_cleanup, "db", SimpleNamespace(
        remember_music_chat_message=tracker,
    ))
    message = _message(12, audio=True)
    result = await music_outgoing_cleanup.MusicOutgoingCleanupMiddleware()(
        AsyncMock(return_value=message), None, None,
    )
    assert result is message
    tracker.assert_awaited_once_with(
        chat_id=321, message_id=12, chat_type="private",
        is_audio=True, audio_key="telegram:unique-123",
    )


@pytest.mark.asyncio
async def test_outgoing_db_failure_cannot_break_delivery(monkeypatch):
    tracker = AsyncMock(side_effect=RuntimeError("db unavailable"))
    monkeypatch.setattr(music_outgoing_cleanup, "db", SimpleNamespace(
        remember_music_chat_message=tracker,
    ))
    message = _message(13)
    result = await music_outgoing_cleanup.MusicOutgoingCleanupMiddleware()(
        AsyncMock(return_value=message), None, None,
    )
    assert result is message
