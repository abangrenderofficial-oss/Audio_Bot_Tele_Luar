from __future__ import annotations

import asyncio
import html
import os
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
from services.storage.music_cache import get_cached_audio, store_cached_audio
from services.media.music_download import (
    MusicDownloadError,
    MusicDownloadResult,
    build_music_cache_key,
    cleanup_music_result,
    download_music_files,
    fetch_music_metadata,
    make_music_plan,
)

logging = logging.bind(service="music")

router = Router(name=__name__)

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
            remote_cached = await get_cached_audio(
                source_url,
                variant="mp3_320",
            )
            logging.info(
                "Music timing: stage=global_cache_lookup seconds=%.2f hit=%s",
                time.perf_counter() - cache_started,
                bool(remote_cached),
            )
            if remote_cached:
                cached_title = str(remote_cached.get("title") or "Audio")
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
                        message.reply_audio,
                        audio=str(remote_cached["telegram_file_id"]),
                        title=cached_title,
                        performer=cached_performer,
                        caption=f"🎵 {html.escape(cached_title)}\n320 kbps",
                        bot_url=bot_url,
                        duration=cached_duration,
                        parse_mode="HTML",
                    )
                    logging.info(
                        "Music timing: stage=global_cache_send seconds=%.2f",
                        time.perf_counter() - send_started,
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

        if status_message:
            if plan.mode == "single":
                await safe_edit_text(
                    status_message,
                    (
                        f"🎧 {metadata.title}\n\n"
                        f"Adaptive quality: {plan.bitrate_kbps} kbps. "
                        "Sedang sediakan MP3..."
                    ),
                )
            else:
                await safe_edit_text(
                    status_message,
                    (
                        f"🎧 {metadata.title}\n\n"
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
                message.reply_audio,
                audio=cached_file_id,
                title=metadata.title,
                performer=metadata.performer,
                caption=f"🎵 {html.escape(metadata.title)}",
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
                        f"🎧 {metadata.title}\n\n"
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
                title = f"{metadata.title} — Part {index}"
                filename_title = f"{metadata.file_base} — Part {index}"
                caption = (
                    f"🎵 {html.escape(metadata.title)}\n"
                    f"Part {index}/{total_parts} • {result.bitrate_kbps} kbps"
                )
                duration = None
            else:
                title = metadata.title
                filename_title = metadata.file_base
                caption = f"🎵 {html.escape(metadata.title)}\n{result.bitrate_kbps} kbps"
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
                    message.reply_audio,
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
