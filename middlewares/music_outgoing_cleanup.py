"""Record bot-sent messages for silent cleanup while preserving audio payloads.

Intercepts only completed outgoing Telegram API responses. A logging/storage
failure must never break a successful Telegram send or change its result.
"""
from __future__ import annotations

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import Message

from app_context import db
from services.logger import logger as logging

logging = logging.bind(service="music_outgoing_cleanup")


class MusicOutgoingCleanupMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if isinstance(result, Message):
            messages = [result]
        elif isinstance(result, list) and result and all(
            isinstance(item, Message) for item in result
        ):
            messages = result
        else:
            messages = []

        remember = getattr(db, "remember_music_chat_message", None)
        if not callable(remember):
            return result

        for item in messages:
            chat = getattr(item, "chat", None)
            chat_id = getattr(chat, "id", None)
            mid = getattr(item, "message_id", None)
            chat_type = str(getattr(chat, "type", "") or "").lower().split(".")[-1]
            if chat_id is None or mid is None or chat_type not in {
                "private", "group", "supergroup",
            }:
                continue
            document = getattr(item, "document", None)
            audio_document = bool(
                document and str(getattr(document, "mime_type", "") or "").lower().startswith("audio/")
            )
            payload = (
                getattr(item, "audio", None)
                or getattr(item, "voice", None)
                or (document if audio_document else None)
            )
            uid = getattr(payload, "file_unique_id", None)
            try:
                await remember(
                    chat_id=int(chat_id), message_id=int(mid),
                    chat_type=chat_type, is_audio=bool(payload),
                    audio_key=(f"telegram:{uid}" if uid else None),
                )
            except Exception as exc:
                logging.warning("Outgoing cleanup tracking failed: %s", exc)
        return result
