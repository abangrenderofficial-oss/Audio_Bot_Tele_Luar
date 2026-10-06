from __future__ import annotations

import asyncio
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import FSInputFile

import messages as bm
from app_context import bot, db, send_analytics
from config import MAX_FILE_SIZE
from handlers.request_dedupe import claim_message_request
from handlers.user import update_info
from handlers.utils import (
    get_bot_avatar_thumbnail,
    get_bot_url,
    get_message_text,
    handle_download_backpressure_error,
    handle_download_error,
    load_user_settings,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    retry_async_operation,
    safe_delete_message,
    safe_edit_text,
    send_chat_action_if_needed,
    should_skip_duplicate_business_message,
    with_message_logging,
)
from handlers.youtube import (
    download_mp3_with_ytdlp_metrics,
    search_youtube_track_fast,
)
from services.logger import logger as logging, summarize_url_for_log
from services.media.audio_flow import run_audio_flow
from services.media.audio_metadata import (
    build_audio_filename,
    prepare_mp3_metadata,
)
from services.media.delivery import build_audio_cache_key, send_audio_with_thumbnail
from services.media.music_download import (
    MusicDownloadError,
    send_youtube_fast_to_telegram,
)
from services.storage.music_cache import (
    get_cached_audio,
    get_cached_social_audio,
    store_cached_audio,
    store_cached_social_audio,
)
from services.platforms.spotify_media import (
    SpotifyError,
    get_spotify_track,
    strip_spotify_url,
)
from utils.download_manager import (
    DownloadQueueBusyError,
    DownloadRateLimitError,
    DownloadTooLargeError,
)

logging = logging.bind(service="spotify")

SPOTIFY_URL_REGEX = r"https?://(?:(?:open|www)\.)?spotify\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]+(?:\?\S*)?"

router = Router()


def _extract_spotify_url(text: str) -> str | None:
    match = re.search(SPOTIFY_URL_REGEX, text or "", re.IGNORECASE)
    if not match:
        return None
    return strip_spotify_url(match.group(0).rstrip(".,;:!?)]}>\"'"))


@router.message(
    F.text.regexp(SPOTIFY_URL_REGEX, mode="search")
    | F.caption.regexp(SPOTIFY_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(SPOTIFY_URL_REGEX, mode="search")
    | F.caption.regexp(SPOTIFY_URL_REGEX, mode="search")
)
@with_message_logging("spotify", "message")
async def process_spotify(message: types.Message, direct_url: Optional[str] = None):
    source_url = strip_spotify_url(direct_url) if direct_url else _extract_spotify_url(
        get_message_text(message)
    )
    if not source_url:
        return

    chat_type_value = str(getattr(message.chat, "type", "")).lower().split(".")[-1]
    group_music_connected = False
    if chat_type_value in {"group", "supergroup"}:
        checker = getattr(db, "is_music_group_connected", None)
        if not callable(checker) or not await checker(message.chat.id):
            # Group auto-conversion is opt-in. /connectmusic activates it.
            return
        group_music_connected = True

    business_id = getattr(message, "business_connection_id", None)
    show_service_status = business_id is None
    status_message: Optional[types.Message] = None
    request_lease = None
    track: dict | None = None
    youtube_track: dict | None = None

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
                service="spotify",
                source_url=source_url,
                title=(str(title) if title else None),
                performer=(str(performer) if performer else None),
                telegram_file_id=str(file_id),
                duration_seconds=parsed_duration,
                source_message_id=getattr(message, "message_id", None),
                audio_message_id=int(audio_message_id),
            )
        except Exception as exc:
            logging.warning(
                "Spotify group playlist record failed: group=%s error=%s",
                message.chat.id,
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

    try:
        if await should_skip_duplicate_business_message(
            message, bot, service_name="Spotify", logger=logging
        ):
            return
        request_lease = await claim_message_request(
            message, service="spotify", url=source_url
        )
        if request_lease is None:
            return

        logging.debug(
            "Spotify track request: user_id=%s url=%s",
            message.from_user.id if message.from_user else 0,
            summarize_url_for_log(source_url),
        )
        await send_analytics(
            user_id=message.from_user.id if message.from_user else 0,
            chat_type=message.chat.type,
            action_name="spotify_audio",
        )
        await react_to_message(message, "👾", business_id=business_id)
        user_settings = await load_user_settings(db, message)
        bot_url = await get_bot_url(bot)
        if show_service_status:
            status_message = await message.answer(bm.downloading_audio_status())

        cache_key = build_audio_cache_key(source_url)

        # Persistent Spotify cache first: repeated links return immediately
        # using Telegram's stored file_id without touching Spotify/YouTube.
        spotify_fast, spotify_mp3 = await asyncio.gather(
            get_cached_social_audio(
                "spotify",
                source_url,
                variant="fast_original",
            ),
            get_cached_social_audio(
                "spotify",
                source_url,
                variant="mp3_320",
            ),
        )
        spotify_cached = spotify_fast or spotify_mp3
        if spotify_cached:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot,
                message.chat.id,
                "upload_audio",
                business_id,
            )
            await send_audio_with_thumbnail(
                audio_sender,
                audio=str(spotify_cached["telegram_file_id"]),
                title=str(spotify_cached.get("title") or "Audio"),
                performer=str(spotify_cached.get("performer") or "Spotify"),
                caption=bm.captions(
                    user_settings.get("captions", "off"),
                    None,
                    bot_url,
                ),
                bot_url=bot_url,
                duration=spotify_cached.get("duration_seconds"),
                parse_mode="HTML",
            )
            request_lease.mark_success()
            await maybe_delete_user_message(
                message,
                user_settings.get("delete_message"),
            )
            return

        async def _fetch_track() -> bool:
            nonlocal track, youtube_track
            if track is not None and youtube_track is not None:
                return True

            await safe_edit_text(
                status_message,
                "🎧 Spotify • sedang baca metadata...",
            )
            track = await get_spotify_track(source_url)
            query = " - ".join(
                value
                for value in (track.get("artists"), track.get("title"))
                if value and value != "Unknown artist"
            )

            await safe_edit_text(
                status_message,
                "🔎 Spotify • sedang cari padanan audio...",
            )
            search_started = asyncio.get_running_loop().time()
            youtube_track = await asyncio.to_thread(
                search_youtube_track_fast,
                query,
            )
            logging.info(
                "Spotify timing: stage=flat_youtube_search seconds=%.2f hit=%s",
                asyncio.get_running_loop().time() - search_started,
                bool(youtube_track),
            )
            if not youtube_track or not youtube_track.get("webpage_url"):
                await message.reply(bm.spotify_source_not_found())
                return False
            return True

        if not await _fetch_track():
            return

        youtube_url = str(youtube_track["webpage_url"])
        title = str(track.get("title") or "Audio")
        performer = str(track.get("artists") or "Spotify")
        duration = track.get("duration")

        # Reuse a globally cached YouTube file_id when this song was already
        # requested through a YouTube link by any user.
        youtube_fast, youtube_mp3 = await asyncio.gather(
            get_cached_audio(youtube_url, variant="fast_original"),
            get_cached_audio(youtube_url, variant="mp3_320"),
        )
        youtube_cached = youtube_fast or youtube_mp3
        if youtube_cached:
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot,
                message.chat.id,
                "upload_audio",
                business_id,
            )
            await send_audio_with_thumbnail(
                audio_sender,
                audio=str(youtube_cached["telegram_file_id"]),
                title=title,
                performer=performer,
                caption=bm.captions(
                    user_settings.get("captions", "off"),
                    None,
                    bot_url,
                ),
                bot_url=bot_url,
                duration=duration,
                parse_mode="HTML",
            )
            await store_cached_social_audio(
                "spotify",
                source_url,
                telegram_file_id=str(youtube_cached["telegram_file_id"]),
                variant="fast_original",
                title=title,
                performer=performer,
                duration_seconds=(
                    float(duration) if duration is not None else None
                ),
                file_size_bytes=(
                    int(youtube_cached.get("file_size_bytes"))
                    if youtube_cached.get("file_size_bytes") is not None
                    else None
                ),
            )
            request_lease.mark_success()
            await maybe_delete_user_message(
                message,
                user_settings.get("delete_message"),
            )
            return

        # Primary Spotify audio path: use Spotify only for exact metadata,
        # resolve a matching public YouTube source, then let the existing
        # external YouTube worker deliver the audio directly to Telegram.
        try:
            await safe_edit_text(
                status_message,
                "⬆️ Spotify • source jumpa, sedang hantar audio...",
            )
            worker_started = asyncio.get_running_loop().time()
            fast_result = await send_youtube_fast_to_telegram(
                youtube_url,
                chat_id=message.chat.id,
                title=title,
                performer=performer,
                duration=(
                    float(duration) if duration is not None else None
                ),
                business_connection_id=business_id,
            )
            logging.info(
                "Spotify timing: stage=fast_worker seconds=%.2f bytes=%s",
                asyncio.get_running_loop().time() - worker_started,
                fast_result.get("file_size"),
            )
        except Exception as exc:
            logging.warning(
                "Spotify fast worker path failed; using legacy MP3 fallback: %s",
                exc,
            )
        else:
            file_id = str(fast_result.get("file_id") or "")
            if file_id:
                file_size = fast_result.get("file_size")
                result_duration = (
                    fast_result.get("duration")
                    if fast_result.get("duration") is not None
                    else duration
                )
                try:
                    await asyncio.gather(
                        store_cached_social_audio(
                            "spotify",
                            source_url,
                            telegram_file_id=file_id,
                            variant="fast_original",
                            title=title,
                            performer=performer,
                            duration_seconds=(
                                float(result_duration)
                                if result_duration is not None
                                else None
                            ),
                            file_size_bytes=(
                                int(file_size)
                                if file_size is not None
                                else None
                            ),
                        ),
                        store_cached_audio(
                            youtube_url,
                            telegram_file_id=file_id,
                            variant="fast_original",
                            title=title,
                            performer=performer,
                            duration_seconds=(
                                float(result_duration)
                                if result_duration is not None
                                else None
                            ),
                            file_size_bytes=(
                                int(file_size)
                                if file_size is not None
                                else None
                            ),
                        ),
                    )
                except Exception as cache_error:
                    logging.warning(
                        "Spotify persistent cache store failed: %s",
                        cache_error,
                    )

                await _remember_group_audio(
                    file_id=file_id,
                    audio_message_id=(
                        int(fast_result["message_id"])
                        if fast_result.get("message_id") is not None
                        else None
                    ),
                    title=title,
                    performer=performer,
                    duration=result_duration,
                )
                request_lease.mark_success()
                await maybe_delete_user_message(
                    message,
                    user_settings.get("delete_message"),
                )
                return

        async def _send_cached(file_id: str):
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot, message.chat.id, "upload_audio", business_id
            )
            return await audio_sender(
                audio=file_id,
                caption=bm.captions(
                    user_settings.get("captions", "off"),
                    None,
                    bot_url,
                ),
                parse_mode="HTML",
            )

        async def _download_audio():
            base_name = f"{track['spotify_id']}_spotify_audio"
            return await retry_async_operation(
                lambda: download_mp3_with_ytdlp_metrics(
                    youtube_track["webpage_url"],
                    base_name,
                    "spotify_audio_mp3",
                    max_filesize=MAX_FILE_SIZE - 1,
                ),
                attempts=3,
                delay_seconds=2.0,
                should_retry_result=lambda result: result is None,
            )

        async def _prepare_metadata(path: str):
            return await prepare_mp3_metadata(path, track)

        async def _send_downloaded(path: str, prepared_metadata):
            await safe_edit_text(status_message, bm.uploading_status())
            await send_chat_action_if_needed(
                bot, message.chat.id, "upload_audio", business_id
            )
            thumbnail = (
                FSInputFile(str(prepared_metadata.thumbnail_path), filename="cover.jpg")
                if prepared_metadata.thumbnail_path
                else await get_bot_avatar_thumbnail(bot)
            )
            return await send_audio_with_thumbnail(
                audio_sender,
                audio=FSInputFile(
                    path,
                    filename=build_audio_filename(track.get("title")),
                ),
                title=track.get("title"),
                performer=track.get("artists"),
                caption=bm.captions(
                    user_settings.get("captions", "off"),
                    None,
                    bot_url,
                ),
                audio_path=path,
                bot_avatar=thumbnail,
                bot_url=bot_url,
                duration=track.get("duration"),
                embed_thumbnail=False,
                parse_mode="HTML",
            )

        async def _after_send(result):
            if result and result.file_id:
                try:
                    await store_cached_social_audio(
                        "spotify",
                        source_url,
                        telegram_file_id=str(result.file_id),
                        variant="mp3_320",
                        title=title,
                        performer=performer,
                        duration_seconds=(
                            float(duration) if duration is not None else None
                        ),
                    )
                except Exception as cache_error:
                    logging.warning(
                        "Spotify MP3 persistent cache store failed: %s",
                        cache_error,
                    )
            await maybe_delete_user_message(
                message,
                user_settings.get("delete_message"),
            )
            request_lease.mark_success()

        async def _on_missing_audio():
            await handle_download_error(message, business_id=business_id)

        async def _on_too_large():
            await message.reply(bm.audio_too_large())

        await run_audio_flow(
            cache_key=cache_key,
            db_service=db,
            send_cached=_send_cached,
            fetch_metadata=_fetch_track,
            download_audio=_download_audio,
            on_missing_audio=_on_missing_audio,
            max_file_size=MAX_FILE_SIZE,
            on_too_large=_on_too_large,
            prepare_metadata=_prepare_metadata,
            send_downloaded=_send_downloaded,
            cleanup_path=remove_file,
            on_after_send=_after_send,
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            chat_type=getattr(message.chat, "type", None),
            service="spotify",
            url=source_url,
            title=title,
        )
    except SpotifyError as exc:
        logging.warning("Spotify metadata error: url=%s error=%s", source_url, exc)
        await message.reply(bm.spotify_metadata_failed())
    except MusicDownloadError as exc:
        logging.warning("Spotify music pipeline error: %s", exc)
        await handle_download_error(message, business_id=business_id)
    except (DownloadRateLimitError, DownloadQueueBusyError, DownloadTooLargeError) as exc:
        await handle_download_backpressure_error(
            exc,
            message=message,
            show_service_status=show_service_status,
            too_large_text=bm.audio_too_large(),
        )
    except asyncio.TimeoutError:
        await handle_download_error(
            message, business_id=business_id, text=bm.timeout_error()
        )
    except Exception as exc:
        logging.exception("Spotify download failed: error=%s", exc)
        await handle_download_error(message, business_id=business_id)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await safe_delete_message(status_message)
        await update_info(message)


async def process_spotify_url(message: types.Message, url: Optional[str] = None):
    await process_spotify(message, direct_url=url)
