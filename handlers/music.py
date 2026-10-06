from __future__ import annotations

import asyncio
import html
import os
import re
import time
from typing import Optional

from aiogram import Router, types
from aiogram.types import FSInputFile

import messages as bm
from app_context import bot, db, send_analytics
from config import OUTPUT_DIR
from handlers.commands import update_info
from handlers.request_dedupe import claim_message_request
from handlers.utils import (
    build_queue_busy_text,
    build_queue_status,
    build_rate_limit_text,
    get_bot_avatar_thumbnail,
    get_bot_url,
    get_bot_username,
    get_message_text,
    load_user_settings,
    maybe_delete_user_message,
    react_to_message,
    safe_answer_inline_query,
    safe_delete_message,
    safe_edit_text,
    send_chat_action_if_needed,
    should_skip_duplicate_business_message,
)
from services.download.queue import (
    QueueBackpressureError,
    QueueRateLimitError,
    get_download_queue,
)
from services.inline.album_links import create_inline_album_request
from services.links.detection import extract_supported_link
from services.logger import logger as logging, summarize_url_for_log
from services.media.audio_metadata import build_audio_filename, prepare_mp3_metadata
from services.media.delivery import send_audio_with_thumbnail
from services.storage.music_cache import (
    get_cached_audio,
    get_cached_social_audio,
    social_media_key,
    store_cached_audio,
    store_cached_social_audio,
    youtube_video_id,
)
from services.platforms.threads_media import resolve_threads_share_fast
from services.media.music_download import (
    MusicDownloadError,
    MusicDownloadResult,
    build_music_cache_key,
    cleanup_music_result,
    download_music_files,
    fetch_music_metadata,
    make_music_plan,
    send_social_fast_to_telegram,
    send_youtube_fast_to_telegram,
)

logging = logging.bind(service="music")

router = Router(name=__name__)

_YOUTUBE_LYRIC_BRACKET_RE = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(?:lyrics?|lirik)\b[^\)\]\}]*[\)\]\}]",
    flags=re.IGNORECASE,
)
_YOUTUBE_LYRIC_WORD_RE = re.compile(
    r"\b(?:official\s+)?(?:lyrics?|lirik)(?:\s+video)?\b",
    flags=re.IGNORECASE,
)


def _clean_youtube_title(value: object) -> str:
    title = html.unescape(str(value or "Audio")).strip()
    title = _YOUTUBE_LYRIC_BRACKET_RE.sub(" ", title)
    title = _YOUTUBE_LYRIC_WORD_RE.sub(" ", title)
    title = re.sub(r"\s{2,}", " ", title)
    title = re.sub(r"\s*[-–—|•:·]+\s*$", "", title).strip()
    return title or "Audio"


async def _enforce_youtube_clean_fast_message(
    *,
    chat_id: int,
    message_id: object,
    file_id: object,
    title: object,
    performer: object,
    duration: object,
    business_connection_id: str | None,
) -> None:
    if message_id is None or not file_id:
        return

    clean_title = _clean_youtube_title(title)
    media_kwargs: dict[str, object] = {
        "media": str(file_id),
        "title": clean_title,
        "caption": f"🎵 {html.escape(clean_title)}",
        "parse_mode": "HTML",
    }
    if performer:
        media_kwargs["performer"] = str(performer)
    try:
        parsed_duration = int(float(duration)) if duration is not None else 0
    except (TypeError, ValueError):
        parsed_duration = 0
    if parsed_duration > 0:
        media_kwargs["duration"] = parsed_duration

    kwargs: dict[str, object] = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "media": types.InputMediaAudio(**media_kwargs),
    }
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id

    try:
        await bot.edit_message_media(**kwargs)
    except Exception as exc:
        logging.warning(
            "YouTube clean-title media edit failed: message_id=%s error=%s",
            message_id,
            exc,
        )
        caption_kwargs: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "caption": f"🎵 {html.escape(clean_title)}",
            "parse_mode": "HTML",
        }
        if business_connection_id:
            caption_kwargs["business_connection_id"] = business_connection_id
        try:
            await bot.edit_message_caption(**caption_kwargs)
        except Exception as caption_exc:
            logging.warning(
                "YouTube clean-title caption edit failed: message_id=%s error=%s",
                message_id,
                caption_exc,
            )


_FAST_YOUTUBE_INFLIGHT: dict[str, asyncio.Future[dict[str, object]]] = {}
_FAST_YOUTUBE_INFLIGHT_LOCK = asyncio.Lock()


async def _claim_fast_youtube_inflight(
    source_url: str,
) -> tuple[str | None, asyncio.Future[dict[str, object]] | None, bool]:
    video_id = youtube_video_id(source_url)
    if not video_id:
        return None, None, True

    async with _FAST_YOUTUBE_INFLIGHT_LOCK:
        existing = _FAST_YOUTUBE_INFLIGHT.get(video_id)
        if existing is not None:
            return video_id, existing, False

        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        _FAST_YOUTUBE_INFLIGHT[video_id] = future
        return video_id, future, True


async def _release_fast_youtube_inflight(
    video_id: str | None,
    future: asyncio.Future[dict[str, object]] | None,
) -> None:
    if not video_id or future is None:
        return
    async with _FAST_YOUTUBE_INFLIGHT_LOCK:
        if _FAST_YOUTUBE_INFLIGHT.get(video_id) is future:
            _FAST_YOUTUBE_INFLIGHT.pop(video_id, None)


_FAST_SOCIAL_INFLIGHT: dict[str, asyncio.Future[dict[str, object]]] = {}
_FAST_SOCIAL_INFLIGHT_LOCK = asyncio.Lock()


async def _claim_fast_social_inflight(
    service_name: str,
    source_url: str,
) -> tuple[str | None, asyncio.Future[dict[str, object]] | None, bool]:
    media_key = social_media_key(service_name, source_url)
    if not media_key:
        return None, None, True

    inflight_key = f"{service_name}:{media_key}"
    async with _FAST_SOCIAL_INFLIGHT_LOCK:
        existing = _FAST_SOCIAL_INFLIGHT.get(inflight_key)
        if existing is not None:
            return inflight_key, existing, False

        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        _FAST_SOCIAL_INFLIGHT[inflight_key] = future
        return inflight_key, future, True


async def _release_fast_social_inflight(
    inflight_key: str | None,
    future: asyncio.Future[dict[str, object]] | None,
) -> None:
    if not inflight_key or future is None:
        return
    async with _FAST_SOCIAL_INFLIGHT_LOCK:
        if _FAST_SOCIAL_INFLIGHT.get(inflight_key) is future:
            _FAST_SOCIAL_INFLIGHT.pop(inflight_key, None)


async def _resolve_threads_music_source(source_url: str) -> str:
    """Resolve Threads /share/... to the direct post before video extraction."""
    if "/share/" not in str(source_url):
        return source_url
    try:
        resolved = await resolve_threads_share_fast(source_url)
    except Exception as exc:
        logging.warning(
            "Threads music share resolve failed; worker will try original URL: %s",
            exc,
        )
        return source_url

    resolved = str(resolved or "").strip()
    if resolved and resolved != source_url:
        logging.info(
            "Threads music share resolved: input=%s resolved=%s",
            summarize_url_for_log(source_url),
            summarize_url_for_log(resolved),
        )
        return resolved
    return source_url


def _social_audio_caption(
    service_name: str,
    title: str,
    quality_label: str,
) -> str:
    escaped_title = html.escape(title)
    if service_name in {"threads", "twitter", "tiktok"}:
        return f"🎵 {escaped_title}"
    return f"🎵 {escaped_title}\n{html.escape(quality_label)}"


async def _enforce_worker_title_only_caption(
    *,
    service_name: str,
    chat_id: int,
    message_id: object,
    title: str,
    business_connection_id: str | None,
) -> None:
    if service_name not in {"threads", "twitter", "tiktok"} or message_id is None:
        return

    kwargs = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "caption": _social_audio_caption(service_name, title, ""),
        "parse_mode": "HTML",
    }
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id

    try:
        await bot.edit_message_caption(**kwargs)
    except Exception as exc:
        logging.warning(
            "Title-only caption enforcement failed: source=%s message_id=%s error=%s",
            service_name,
            message_id,
            exc,
        )


MUSIC_LINK_SERVICES = frozenset(
    {
        "youtube",
        "tiktok",
        "instagram",
        "threads",
        "twitter",
    }
)
MUSIC_BLOCKED_SERVICES = frozenset({"pinterest"})
_MUSIC_ROUTED_SERVICES = MUSIC_LINK_SERVICES | MUSIC_BLOCKED_SERVICES


def _music_link_filter(message: types.Message) -> bool:
    detected = extract_supported_link(get_message_text(message))
    return bool(detected and detected[0] in _MUSIC_ROUTED_SERVICES)


def _music_inline_filter(query: types.InlineQuery) -> bool:
    detected = extract_supported_link(getattr(query, "query", "") or "")
    return bool(detected and detected[0] in _MUSIC_ROUTED_SERVICES)


@router.inline_query(_music_inline_filter)
async def redirect_music_inline_to_private(query: types.InlineQuery) -> None:
    detected = extract_supported_link(query.query or "")
    if not detected:
        return
    service_name, source_url = detected
    if service_name in MUSIC_BLOCKED_SERVICES:
        result = types.InlineQueryResultArticle(
            id=f"music_unsupported_{service_name}_{query.from_user.id}",
            title="Pinterest belum disokong untuk MP3",
            description="MP3 Music Bot fokus pada sumber audio yang disokong.",
            input_message_content=types.InputTextMessageContent(
                message_text=bm.music_unsupported_link(),
                parse_mode="HTML",
            ),
        )
        await safe_answer_inline_query(
            query,
            [result],
            cache_time=1,
            is_personal=True,
        )
        return

    token = create_inline_album_request(
        query.from_user.id,
        service_name,
        source_url,
    )
    bot_username = await get_bot_username(bot)
    deep_link = f"https://t.me/{bot_username}?start=dl_{token}"
    result = types.InlineQueryResultArticle(
        id=f"music_{service_name}_{token}",
        title="🎵 Download MP3",
        description="Open MP3 Music Bot to convert this link.",
        input_message_content=types.InputTextMessageContent(
            message_text=(
                "🎵 <b>MP3 Music Bot</b>\n\n"
                "Tap the button below to convert this link to MP3."
            ),
            parse_mode="HTML",
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="🎧 Open MP3 Music Bot",
                        url=deep_link,
                    )
                ]
            ]
        ),
    )
    await safe_answer_inline_query(
        query,
        [result],
        cache_time=1,
        is_personal=True,
    )


def _friendly_music_error(exc: Exception) -> str:
    raw = str(exc)
    lower = raw.lower()
    if "threads_share_unavailable" in lower:
        return (
            "Link Threads ni dah tak dapat dibuka. Kemungkinan share link dah "
            "tak valid, post dah dipadam/private, atau Threads dah tamatkan "
            "share link itu. Cuba Copy Link semula dari post Threads yang masih "
            "public dan hantar link baru."
        )
    if "threads_share_unresolved" in lower:
        return (
            "Bot tak dapat buka share link Threads ini. Cuba buka post itu di "
            "Threads, tekan Share > Copy Link sekali lagi dan hantar link baru. "
            "Kalau boleh, hantar direct link @username/post/... ."
        )
    if "live_stream_not_supported" in lower:
        return "Live stream belum disokong. Hantar link video yang sudah siap/published."
    if (
        "confirm you're not a bot" in lower
        or "confirm you’re not a bot" in lower
        or "not a bot" in lower
        or "po token" in lower
        or "bot check" in lower
    ):
        return (
            "YouTube sedang challenge/block request server untuk video ini. "
            "Cuba link yang sama kemudian atau guna source lain buat sementara."
        )
    if (
        "private" in lower
        or "members-only" in lower
        or "sign in" in lower
        or "login" in lower
    ):
        return (
            "Media ini perlukan login/permission atau bukan public. "
            "Bot hanya boleh proses media public."
        )
    if "unsupported" in lower or "no formats" in lower or "no media" in lower:
        return (
            "Link ini tak dapat diextract sekarang. Pastikan post/video public "
            "dan link datang dari YouTube, TikTok, Instagram Reels, Threads atau X."
        )
    if "timed out" in lower or "timeout" in lower:
        return bm.timeout_error()
    return (
        "Conversion MP3 gagal untuk link ini. Source mungkin berubah, "
        "disekat, atau extractor sedang bermasalah."
    )


async def process_music_link(
    message: types.Message,
    *,
    service: Optional[str] = None,
    url: Optional[str] = None,
) -> None:
    detected = (service, url) if service and url else extract_supported_link(
        get_message_text(message)
    )
    if not detected:
        return

    service_name, source_url = detected

    if service_name == "threads":
        source_url = await _resolve_threads_music_source(source_url)

    chat_type_value = str(getattr(message.chat, "type", "")).lower().split(".")[-1]
    is_group_music_chat = chat_type_value in {"group", "supergroup"}
    group_music_connected = False
    if is_group_music_chat:
        checker = getattr(db, "is_music_group_connected", None)
        if not callable(checker) or not await checker(message.chat.id):
            # Group auto-conversion is opt-in. /connectmusic activates it.
            return
        group_music_connected = True

    async def _remember_group_audio(
        *,
        file_id: str | None,
        audio_message_id: int | None,
        title: object = None,
        performer: object = None,
        duration: object = None,
    ) -> None:
        if not group_music_connected or not file_id or audio_message_id is None:
            return
        try:
            parsed_duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            parsed_duration = None
        try:
            await db.add_music_group_track(
                group_id=message.chat.id,
                added_by_user_id=(
                    message.from_user.id if message.from_user else None
                ),
                service=service_name,
                source_url=source_url,
                title=(str(title) if title else None),
                performer=(str(performer) if performer else None),
                telegram_file_id=str(file_id),
                duration_seconds=parsed_duration,
                source_message_id=message.message_id,
                audio_message_id=int(audio_message_id),
            )
        except Exception as exc:
            logging.warning(
                "Group playlist record failed: group=%s source=%s error=%s",
                message.chat.id,
                service_name,
                exc,
            )

    async def _reply_audio_with_group_playlist(**kwargs):
        sent = await message.reply_audio(**kwargs)
        audio = getattr(sent, "audio", None)
        await _remember_group_audio(
            file_id=getattr(audio, "file_id", None),
            audio_message_id=getattr(sent, "message_id", None),
            title=(
                kwargs.get("title")
                or getattr(audio, "title", None)
                or getattr(audio, "file_name", None)
            ),
            performer=(
                kwargs.get("performer")
                or getattr(audio, "performer", None)
            ),
            duration=getattr(audio, "duration", None) or kwargs.get("duration"),
        )
        return sent

    audio_sender = (
        _reply_audio_with_group_playlist
        if group_music_connected
        else message.reply_audio
    )

    if service_name in MUSIC_BLOCKED_SERVICES:
        await message.reply(bm.music_unsupported_link(), parse_mode="HTML")
        await update_info(message)
        return
    if service_name not in MUSIC_LINK_SERVICES:
        return

    business_id = getattr(message, "business_connection_id", None)
    if await should_skip_duplicate_business_message(
        message,
        bot,
        service_name=f"{service_name} audio",
        logger=logging,
    ):
        await update_info(message)
        return

    request_lease = None
    status_message: Optional[types.Message] = None
    result: MusicDownloadResult | None = None

    try:
        request_lease = await claim_message_request(
            message,
            service=f"{service_name}_audio",
            url=source_url,
        )
        if request_lease is None:
            return

        logging.download_request(
            user_id=message.from_user.id if message.from_user else 0,
            username=getattr(message.from_user, "username", None),
            service=f"{service_name}_audio",
            url=source_url,
            chat_type=getattr(message.chat, "type", None),
        )

        await send_analytics(
            user_id=message.from_user.id,
            chat_type=message.chat.type,
            action_name=f"{service_name}_audio",
        )
        await react_to_message(message, "🎵", business_id=business_id)

        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        bot_avatar = await get_bot_avatar_thumbnail(bot)

        if business_id is None:
            status_message = await message.answer(
                "🎧 Sedang baca audio dan metadata..."
            )

        # Global persistent Telegram file_id cache. Do this before metadata so
        # a popular YouTube song can be returned almost immediately without
        # touching YouTube, Oregon, or FFmpeg at all.
        if service_name == "youtube":
            cache_started = time.perf_counter()
            remote_mp3, remote_fast = await asyncio.gather(
                get_cached_audio(source_url, variant="mp3_320"),
                get_cached_audio(source_url, variant="fast_original"),
            )
            remote_cached = remote_mp3 or remote_fast
            remote_variant = "mp3_320" if remote_mp3 else "fast_original"
            logging.info(
                "Music timing: stage=global_cache_lookup seconds=%.2f hit=%s variant=%s",
                time.perf_counter() - cache_started,
                bool(remote_cached),
                remote_variant if remote_cached else "miss",
            )
            if remote_cached:
                cached_title = _clean_youtube_title(
                    remote_cached.get("title") or "Audio"
                )
                cached_performer = str(
                    remote_cached.get("performer") or "YouTube"
                )
                cached_duration = remote_cached.get("duration_seconds")
                try:
                    await safe_edit_text(status_message, bm.uploading_status())
                    await send_chat_action_if_needed(
                        bot,
                        message.chat.id,
                        "upload_audio",
                        business_id,
                    )
                    send_started = time.perf_counter()
                    await send_audio_with_thumbnail(
                        audio_sender,
                        audio=str(remote_cached["telegram_file_id"]),
                        title=cached_title,
                        performer=cached_performer,
                        caption=f"🎵 {html.escape(cached_title)}",
                        bot_url=bot_url,
                        duration=cached_duration,
                        parse_mode="HTML",
                    )
                    logging.info(
                        "Music timing: stage=global_cache_send seconds=%.2f variant=%s",
                        time.perf_counter() - send_started,
                        remote_variant,
                    )
                except Exception as cache_send_error:
                    logging.warning(
                        "Persistent music cache file_id failed; continuing fresh path: %s",
                        cache_send_error,
                    )
                else:
                    request_lease.mark_success()
                    await maybe_delete_user_message(
                        message,
                        user_settings.get("delete_message"),
                    )
                    return

        if service_name in {"tiktok", "instagram", "threads", "twitter"}:
            cache_started = time.perf_counter()
            social_cached = await get_cached_social_audio(
                service_name,
                source_url,
                variant="fast_original",
            )
            logging.info(
                "Music timing: stage=social_global_cache_lookup "
                "seconds=%.2f source=%s hit=%s",
                time.perf_counter() - cache_started,
                service_name,
                bool(social_cached),
            )
            if social_cached:
                cached_title = str(social_cached.get("title") or "Audio")
                cached_performer = str(
                    social_cached.get("performer") or service_name.title()
                )
                if (
                    service_name == "instagram"
                    and re.fullmatch(
                        r"Original (?:sound|audio) — @\d+",
                        cached_title,
                        flags=re.IGNORECASE,
                    )
                ):
                    logging.info(
                        "Ignoring stale Instagram numeric-title cache: %s",
                        cached_title,
                    )
                    social_cached = None

            if social_cached:
                cached_title = str(social_cached.get("title") or "Audio")
                cached_performer = str(
                    social_cached.get("performer") or service_name.title()
                )
                cached_duration = social_cached.get("duration_seconds")
                try:
                    await safe_edit_text(status_message, bm.uploading_status())
                    await send_chat_action_if_needed(
                        bot,
                        message.chat.id,
                        "upload_audio",
                        business_id,
                    )
                    send_started = time.perf_counter()
                    await send_audio_with_thumbnail(
                        audio_sender,
                        audio=str(social_cached["telegram_file_id"]),
                        title=cached_title,
                        performer=cached_performer,
                        caption=_social_audio_caption(
                            service_name,
                            cached_title,
                            "Fast Audio",
                        ),
                        bot_url=bot_url,
                        duration=cached_duration,
                        parse_mode="HTML",
                    )
                    logging.info(
                        "Music timing: stage=social_global_cache_send "
                        "seconds=%.2f source=%s",
                        time.perf_counter() - send_started,
                        service_name,
                    )
                except Exception as cache_send_error:
                    logging.warning(
                        "Persistent social music cache file_id failed; "
                        "continuing fresh path: %s",
                        cache_send_error,
                    )
                else:
                    request_lease.mark_success()
                    await maybe_delete_user_message(
                        message,
                        user_settings.get("delete_message"),
                    )
                    return

        if service_name == "youtube":
            video_id, shared_fast_future, is_fast_leader = (
                await _claim_fast_youtube_inflight(source_url)
            )

            if not is_fast_leader and shared_fast_future is not None:
                try:
                    if status_message:
                        await safe_edit_text(
                            status_message,
                            "🎧 Lagu sama sedang diproses • guna hasil yang sama...",
                        )
                    shared_result = await asyncio.wait_for(
                        asyncio.shield(shared_fast_future),
                        timeout=190.0,
                    )
                    if not shared_result.get("ok"):
                        raise MusicDownloadError(
                            str(
                                shared_result.get("error")
                                or "shared fast path failed"
                            )
                        )

                    shared_title = _clean_youtube_title(
                        shared_result.get("title") or "Audio"
                    )
                    shared_performer = str(
                        shared_result.get("performer") or "YouTube"
                    )
                    shared_duration = shared_result.get("duration")

                    await safe_edit_text(status_message, bm.uploading_status())
                    await send_chat_action_if_needed(
                        bot,
                        message.chat.id,
                        "upload_audio",
                        business_id,
                    )
                    shared_send_started = time.perf_counter()
                    await send_audio_with_thumbnail(
                        audio_sender,
                        audio=str(shared_result["file_id"]),
                        title=shared_title,
                        performer=shared_performer,
                        caption=f"🎵 {html.escape(shared_title)}",
                        bot_url=bot_url,
                        duration=shared_duration,
                        parse_mode="HTML",
                    )
                    logging.info(
                        "Music timing: stage=inflight_shared_send "
                        "seconds=%.2f video_id=%s",
                        time.perf_counter() - shared_send_started,
                        video_id,
                    )
                    request_lease.mark_success()
                    await maybe_delete_user_message(
                        message,
                        user_settings.get("delete_message"),
                    )
                    return
                except Exception as shared_error:
                    logging.warning(
                        "Shared Fast Original result failed; "
                        "falling back to MP3 pipeline: %s",
                        shared_error,
                    )

            if is_fast_leader:
                fast_result: dict[str, object] | None = None
                fast_error: Exception | None = None
                try:
                    if status_message:
                        await safe_edit_text(
                            status_message,
                            "🎧 Fast Original • sedang sediakan audio...",
                        )
                    await send_chat_action_if_needed(
                        bot,
                        message.chat.id,
                        "upload_audio",
                        business_id,
                    )
                    fast_result = await send_youtube_fast_to_telegram(
                        source_url,
                        chat_id=message.chat.id,
                        business_connection_id=business_id,
                        caption_title_only=True,
                    )
                except Exception as exc:
                    fast_error = exc
                    logging.warning(
                        "Fast Original direct path failed; "
                        "falling back to MP3 pipeline: %s",
                        exc,
                    )
                    if (
                        shared_fast_future is not None
                        and not shared_fast_future.done()
                    ):
                        shared_fast_future.set_result(
                            {"ok": False, "error": str(exc)}
                        )
                else:
                    fast_title = _clean_youtube_title(
                        fast_result.get("title") or "Audio"
                    )
                    fast_performer = str(
                        fast_result.get("performer") or "YouTube"
                    )
                    fast_duration = fast_result.get("duration")

                    await _enforce_youtube_clean_fast_message(
                        chat_id=message.chat.id,
                        message_id=fast_result.get("message_id"),
                        file_id=fast_result.get("file_id"),
                        title=fast_title,
                        performer=fast_performer,
                        duration=fast_duration,
                        business_connection_id=business_id,
                    )

                    if (
                        shared_fast_future is not None
                        and not shared_fast_future.done()
                    ):
                        shared_fast_future.set_result(
                            {
                                "ok": True,
                                "file_id": str(fast_result["file_id"]),
                                "file_size": fast_result.get("file_size"),
                                "title": fast_title,
                                "performer": fast_performer,
                                "duration": fast_duration,
                            }
                        )

                    try:
                        await store_cached_audio(
                            source_url,
                            telegram_file_id=str(fast_result["file_id"]),
                            variant="fast_original",
                            title=fast_title,
                            performer=fast_performer,
                            duration_seconds=(
                                float(fast_duration)
                                if fast_duration is not None
                                else None
                            ),
                            file_size_bytes=(
                                int(fast_result.get("file_size"))
                                if fast_result.get("file_size") is not None
                                else None
                            ),
                        )
                    except Exception as exc:
                        logging.warning(
                            "Fast Original persistent cache store failed: %s",
                            exc,
                        )
                finally:
                    await _release_fast_youtube_inflight(
                        video_id,
                        shared_fast_future,
                    )

                if fast_result is not None and fast_error is None:
                    await _remember_group_audio(
                        file_id=str(fast_result.get("file_id") or "") or None,
                        audio_message_id=(
                            int(fast_result["message_id"])
                            if fast_result.get("message_id") is not None
                            else None
                        ),
                        title=fast_result.get("title"),
                        performer=fast_result.get("performer"),
                        duration=fast_result.get("duration"),
                    )
                    request_lease.mark_success()
                    await maybe_delete_user_message(
                        message,
                        user_settings.get("delete_message"),
                    )
                    return

        if service_name in {"tiktok", "instagram", "threads", "twitter"}:
            inflight_key, shared_social_future, is_social_leader = (
                await _claim_fast_social_inflight(service_name, source_url)
            )

            if not is_social_leader and shared_social_future is not None:
                try:
                    if status_message:
                        await safe_edit_text(
                            status_message,
                            "🎧 Audio sama sedang diproses • guna hasil yang sama...",
                        )
                    shared_result = await asyncio.wait_for(
                        asyncio.shield(shared_social_future),
                        timeout=200.0,
                    )
                    if not shared_result.get("ok"):
                        raise MusicDownloadError(
                            str(
                                shared_result.get("error")
                                or "shared social fast path failed"
                            )
                        )

                    shared_title = str(
                        shared_result.get("title") or "Audio"
                    )
                    shared_performer = str(
                        shared_result.get("performer")
                        or service_name.title()
                    )
                    shared_duration = shared_result.get("duration")
                    shared_quality = str(
                        shared_result.get("quality_label") or "Fast Audio"
                    )

                    await safe_edit_text(status_message, bm.uploading_status())
                    await send_chat_action_if_needed(
                        bot,
                        message.chat.id,
                        "upload_audio",
                        business_id,
                    )
                    send_started = time.perf_counter()
                    await send_audio_with_thumbnail(
                        audio_sender,
                        audio=str(shared_result["file_id"]),
                        title=shared_title,
                        performer=shared_performer,
                        caption=_social_audio_caption(
                            service_name,
                            shared_title,
                            shared_quality,
                        ),
                        bot_url=bot_url,
                        duration=shared_duration,
                        parse_mode="HTML",
                    )
                    logging.info(
                        "Music timing: stage=social_inflight_shared_send "
                        "seconds=%.2f source=%s key=%s",
                        time.perf_counter() - send_started,
                        service_name,
                        inflight_key,
                    )
                    request_lease.mark_success()
                    await maybe_delete_user_message(
                        message,
                        user_settings.get("delete_message"),
                    )
                    return
                except Exception as shared_error:
                    logging.warning(
                        "Shared social Fast Audio result failed; "
                        "falling back to legacy MP3 pipeline: %s",
                        shared_error,
                    )

            if is_social_leader:
                social_result: dict[str, object] | None = None
                social_error: Exception | None = None
                try:
                    if status_message:
                        await safe_edit_text(
                            status_message,
                            (
                                "🎧 Threads • sedang ambil audio dari post..."
                                if service_name == "threads"
                                else (
                                    f"🎧 {service_name.title()} Fast Audio • "
                                    "sedang sediakan audio..."
                                )
                            ),
                        )
                    await send_chat_action_if_needed(
                        bot,
                        message.chat.id,
                        "upload_audio",
                        business_id,
                    )
                    social_result = await send_social_fast_to_telegram(
                        source_url,
                        source=service_name,
                        chat_id=message.chat.id,
                        business_connection_id=business_id,
                    )
                except Exception as exc:
                    social_error = exc
                    error_text = str(exc)
                    logging.warning(
                        "Social Fast Audio direct path failed; "
                        "source=%s error=%s",
                        service_name,
                        exc,
                    )
                    if (
                        shared_social_future is not None
                        and not shared_social_future.done()
                    ):
                        shared_social_future.set_result(
                            {"ok": False, "error": error_text}
                        )

                    # Threads /share/ aliases that are explicitly unavailable or
                    # cannot be resolved should not enter the old metadata +
                    # conversion pipeline. That only repeats the same request
                    # for another minute before failing. Reply immediately.
                    if (
                        service_name == "threads"
                        and (
                            "THREADS_SHARE_UNAVAILABLE" in error_text
                            or "THREADS_SHARE_UNRESOLVED" in error_text
                        )
                    ):
                        raise MusicDownloadError(error_text) from exc
                else:
                    social_title = str(
                        social_result.get("title") or "Audio"
                    )
                    social_performer = str(
                        social_result.get("performer")
                        or service_name.title()
                    )
                    social_duration = social_result.get("duration")
                    social_quality = str(
                        social_result.get("quality_label") or "Fast Audio"
                    )

                    await _enforce_worker_title_only_caption(
                        service_name=service_name,
                        chat_id=message.chat.id,
                        message_id=social_result.get("message_id"),
                        title=social_title,
                        business_connection_id=business_id,
                    )

                    if (
                        shared_social_future is not None
                        and not shared_social_future.done()
                    ):
                        shared_social_future.set_result(
                            {
                                "ok": True,
                                "file_id": str(social_result["file_id"]),
                                "file_size": social_result.get("file_size"),
                                "title": social_title,
                                "performer": social_performer,
                                "duration": social_duration,
                                "quality_label": social_quality,
                            }
                        )

                    try:
                        await store_cached_social_audio(
                            service_name,
                            source_url,
                            telegram_file_id=str(social_result["file_id"]),
                            variant="fast_original",
                            title=social_title,
                            performer=social_performer,
                            duration_seconds=(
                                float(social_duration)
                                if social_duration is not None
                                else None
                            ),
                            file_size_bytes=(
                                int(social_result.get("file_size"))
                                if social_result.get("file_size") is not None
                                else None
                            ),
                        )
                    except Exception as exc:
                        logging.warning(
                            "Persistent social music cache store failed: "
                            "source=%s error=%s",
                            service_name,
                            exc,
                        )
                finally:
                    await _release_fast_social_inflight(
                        inflight_key,
                        shared_social_future,
                    )

                if social_result is not None and social_error is None:
                    await _remember_group_audio(
                        file_id=str(social_result.get("file_id") or "") or None,
                        audio_message_id=(
                            int(social_result["message_id"])
                            if social_result.get("message_id") is not None
                            else None
                        ),
                        title=social_result.get("title"),
                        performer=social_result.get("performer"),
                        duration=social_result.get("duration"),
                    )
                    request_lease.mark_success()
                    await maybe_delete_user_message(
                        message,
                        user_settings.get("delete_message"),
                    )
                    return

        stage_started = time.perf_counter()
        metadata = await fetch_music_metadata(
            source_url,
            source=service_name,
        )
        logging.info(
            "Music timing: stage=metadata seconds=%.2f source=%s",
            time.perf_counter() - stage_started,
            service_name,
        )
        plan = make_music_plan(metadata.duration)
        display_title = (
            _clean_youtube_title(metadata.title)
            if service_name == "youtube"
            else metadata.title
        )

        if status_message:
            if plan.mode == "single":
                await safe_edit_text(
                    status_message,
                    (
                        f"🎧 {display_title}\n\n"
                        f"Adaptive quality: {plan.bitrate_kbps} kbps. "
                        "Sedang sediakan MP3..."
                    ),
                )
            else:
                await safe_edit_text(
                    status_message,
                    (
                        f"🎧 {display_title}\n\n"
                        "Audio terlalu panjang untuk satu fail pada minimum 128 kbps. "
                        "Bot akan kekalkan 128 kbps dan split automatik."
                    ),
                )

        cache_key = build_music_cache_key(source_url)
        cached_file_id = await db.get_file_id(cache_key)
        if cached_file_id:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot,
                message.chat.id,
                "upload_audio",
                business_id,
            )
            await send_audio_with_thumbnail(
                audio_sender,
                audio=cached_file_id,
                title=display_title,
                performer=metadata.performer,
                caption=f"🎵 {html.escape(display_title)}",
                bot_url=bot_url,
                duration=metadata.duration,
                parse_mode="HTML",
            )
            request_lease.mark_success()
            await maybe_delete_user_message(
                message,
                user_settings.get("delete_message"),
            )
            return

        job_id = (
            f"{message.chat.id}-{message.message_id}-"
            f"{message.from_user.id if message.from_user else 0}"
        )
        async def _on_queued(ticket) -> None:
            if status_message:
                await safe_edit_text(
                    status_message,
                    build_queue_status("MP3 conversion", ticket),
                )

        async def _run_conversion() -> MusicDownloadResult:
            if status_message:
                await safe_edit_text(
                    status_message,
                    (
                        f"🎧 {display_title}\n\n"
                        f"Downloading audio • {plan.bitrate_kbps} kbps..."
                    ),
                )
            return await download_music_files(
                source_url,
                metadata=metadata,
                output_dir=OUTPUT_DIR,
                job_id=job_id,
            )

        stage_started = time.perf_counter()
        result = await get_download_queue().submit(
            _run_conversion,
            priority=30,
            source=f"{service_name}_audio",
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            request_id=job_id,
            on_queued=_on_queued,
        )
        logging.info(
            "Music timing: stage=download_convert seconds=%.2f source=%s bytes=%s",
            time.perf_counter() - stage_started,
            service_name,
            sum(os.path.getsize(path) for path in result.paths if os.path.isfile(path)),
        )

        await safe_edit_text(status_message, bm.uploading_status())
        await send_chat_action_if_needed(
            bot,
            message.chat.id,
            "upload_audio",
            business_id,
        )

        sent_file_id: str | None = None
        total_parts = len(result.paths)

        for index, path in enumerate(result.paths, start=1):
            if total_parts > 1:
                title = f"{display_title} — Part {index}"
                filename_title = f"{display_title} — Part {index}"
                caption = (
                    f"🎵 {html.escape(display_title)}\n"
                    f"Part {index}/{total_parts} • {result.bitrate_kbps} kbps"
                )
                duration = None
            else:
                title = display_title
                filename_title = display_title
                caption = f"🎵 {html.escape(display_title)}\n{result.bitrate_kbps} kbps"
                duration = metadata.duration

            stage_started = time.perf_counter()
            prepared = await prepare_mp3_metadata(
                path,
                {
                    "title": title,
                    "artist": metadata.performer,
                    "thumbnail": metadata.thumbnail,
                    "source_url": source_url,
                },
            )
            logging.info(
                "Music timing: stage=tag_cover seconds=%.2f part=%s/%s",
                time.perf_counter() - stage_started,
                index,
                total_parts,
            )
            try:
                audio_thumbnail = (
                    FSInputFile(
                        str(prepared.thumbnail_path),
                        filename="cover.jpg",
                    )
                    if prepared.thumbnail_path
                    else bot_avatar
                )
                upload_started = time.perf_counter()
                sent = await send_audio_with_thumbnail(
                    audio_sender,
                    audio=FSInputFile(
                        path,
                        filename=build_audio_filename(filename_title),
                    ),
                    title=title,
                    performer=metadata.performer,
                    caption=caption,
                    audio_path=path,
                    bot_avatar=audio_thumbnail,
                    bot_url=bot_url,
                    duration=duration,
                    embed_thumbnail=False,
                    parse_mode="HTML",
                )
                logging.info(
                    "Music timing: stage=telegram_upload seconds=%.2f part=%s/%s bytes=%s",
                    time.perf_counter() - upload_started,
                    index,
                    total_parts,
                    os.path.getsize(path) if os.path.isfile(path) else 0,
                )
            finally:
                prepared.cleanup()

            if (
                total_parts == 1
                and getattr(sent, "audio", None)
                and getattr(sent.audio, "file_id", None)
            ):
                sent_file_id = sent.audio.file_id

        if sent_file_id:
            try:
                await db.add_file(cache_key, sent_file_id, "audio")
            except Exception as exc:
                logging.debug(
                    "Failed to cache Music Bot audio: url=%s error=%s",
                    summarize_url_for_log(source_url),
                    exc,
                )

            # Persist normal 320 kbps YouTube singles globally so future users
            # can receive the Telegram file_id without another download.
            if (
                service_name == "youtube"
                and total_parts == 1
                and result.bitrate_kbps == 320
            ):
                try:
                    stored = await store_cached_audio(
                        source_url,
                        telegram_file_id=sent_file_id,
                        variant="mp3_320",
                        title=metadata.title,
                        performer=metadata.performer,
                        duration_seconds=metadata.duration,
                        file_size_bytes=(
                            os.path.getsize(result.paths[0])
                            if result.paths and os.path.isfile(result.paths[0])
                            else None
                        ),
                    )
                    logging.info(
                        "Persistent music cache store: success=%s",
                        stored,
                    )
                except Exception as exc:
                    logging.warning(
                        "Persistent music cache store failed: %s",
                        exc,
                    )

        request_lease.mark_success()
        await maybe_delete_user_message(
            message,
            user_settings.get("delete_message"),
        )

    except QueueRateLimitError as exc:
        logging.info(
            "Music Bot rate limit: user_id=%s retry_after=%.1f",
            message.from_user.id if message.from_user else None,
            exc.retry_after,
        )
        await message.reply(build_rate_limit_text(exc.retry_after))
    except QueueBackpressureError as exc:
        logging.info(
            "Music Bot queue busy: user_id=%s position=%s",
            message.from_user.id if message.from_user else None,
            exc.position,
        )
        await message.reply(build_queue_busy_text(exc.position))
    except asyncio.TimeoutError:
        logging.warning(
            "Music metadata timed out: service=%s url=%s",
            service_name,
            summarize_url_for_log(source_url),
        )
        await message.reply(bm.timeout_error())
    except MusicDownloadError as exc:
        logging.error(
            "Music conversion failed: service=%s url=%s error=%s",
            service_name,
            summarize_url_for_log(source_url),
            exc,
        )
        await message.reply(_friendly_music_error(exc))
    except Exception as exc:
        logging.exception(
            "Unexpected Music Bot failure: service=%s url=%s error=%s",
            service_name,
            summarize_url_for_log(source_url),
            exc,
        )
        await message.reply(_friendly_music_error(exc))
    finally:
        await safe_delete_message(status_message)
        await cleanup_music_result(result)
        if request_lease is not None:
            request_lease.finish()
        await update_info(message)


@router.message(_music_link_filter)
@router.business_message(_music_link_filter)
async def process_music(message: types.Message) -> None:
    await process_music_link(message)
