from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from yt_dlp import YoutubeDL

from services.logger import logger as logging
from services.platforms.youtube_media import build_ytdlp_youtube_options
from services.platforms.ytdlp_helpers import mp3_extract_postprocessors
from utils.cobalt_client import fetch_cobalt_data
from utils.cobalt_media import parse_cobalt_media_response

logging = logging.bind(service="music_download")

MAX_AUDIO_BYTES = 49 * 1024 * 1024
TARGET_AUDIO_BYTES = 47 * 1024 * 1024
BITRATE_CHOICES_KBPS = (320, 256, 224, 192, 160, 128)
MIN_SINGLE_FILE_KBPS = 128
SPLIT_BITRATE_KBPS = 128
SEGMENT_SECONDS = 2400
MUSIC_AUDIO_CACHE_VARIANT = "music_adaptive_mp3_v1"
PIPED_MAX_SOURCE_BYTES = 150 * 1024 * 1024
INVIDIOUS_MAX_SOURCE_BYTES = 150 * 1024 * 1024
COBALT_MAX_SOURCE_BYTES = 150 * 1024 * 1024
YOUTUBE_PUBLIC_FALLBACK_PROFILES: tuple[tuple[str, str], ...] = (
    ("web_creator", "bestaudio/best"),
    ("mweb", "bestaudio/best"),
    ("android_vr", "18/bestaudio/best"),
    ("web_embedded", "bestaudio/best"),
    ("tv", "bestaudio/best"),
    ("web_safari", "bestaudio/best"),
)

_SOURCE_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "threads": "Threads",
    "twitter": "X / Twitter",
}


@dataclass(frozen=True, slots=True)
class MusicMetadata:
    title: str
    performer: str
    file_base: str
    duration: float | None
    thumbnail: str | None
    source_url: str
    source: str


@dataclass(frozen=True, slots=True)
class MusicPlan:
    mode: str
    bitrate_kbps: int


@dataclass(slots=True)
class MusicDownloadResult:
    work_dir: str
    paths: list[str]
    bitrate_kbps: int
    mode: str


class MusicDownloadError(RuntimeError):
    pass


def build_music_cache_key(source_url: str) -> str:
    clean_url = (source_url or "").strip()
    if not clean_url:
        raise ValueError("source_url must not be empty")
    return f"{clean_url}#{MUSIC_AUDIO_CACHE_VARIANT}"


def _clean_text(value: object, fallback: str = "Audio", *, limit: int = 120) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or fallback))
    text = re.sub(r"\s+", " ", text).strip()
    return (text or fallback)[:limit].strip() or fallback


def safe_file_stem(value: object, fallback: str = "Audio") -> str:
    text = _clean_text(value, fallback, limit=140)
    text = re.sub(r'[\\/:*?"<>|]+', " ", text)
    text = re.sub(r"\.+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or fallback)[:96].strip() or fallback


def _social_creator_label(info: dict[str, Any], fallback: str) -> str:
    for key in ("uploader_id", "channel_id", "creator_id"):
        handle = str(info.get(key) or "").strip().lstrip("@")
        if handle:
            return f"@{handle}"
    for key in ("creator", "uploader", "channel", "artist"):
        value = info.get(key)
        if value:
            return _clean_text(value, fallback)
    return fallback


def build_music_metadata(
    info: dict[str, Any],
    *,
    source: str,
    source_url: str,
) -> MusicMetadata:
    source_label = _SOURCE_LABELS.get(source, source.title() or "Audio")
    performer = _clean_text(
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or info.get("channel")
        or source_label,
        source_label,
    )

    if source == "youtube":
        title = _clean_text(info.get("title") or info.get("fulltitle"), "Audio")
        file_base = safe_file_stem(title, "Audio")
    else:
        track = ""
        for key in ("track", "track_title", "song", "music_title", "audio_title"):
            value = info.get(key)
            if value:
                track = _clean_text(value, "", limit=120)
                if track:
                    break

        title = track or f"Original sound — {_social_creator_label(info, performer or source_label)}"
        title = _clean_text(title, "Original sound")
        file_base = safe_file_stem(title, "Original sound")

    raw_duration = info.get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError, OverflowError):
        duration = None
    if duration is not None and duration <= 0:
        duration = None

    thumbnail = info.get("thumbnail")
    if not thumbnail:
        thumbnails = info.get("thumbnails")
        if isinstance(thumbnails, list):
            candidates = [
                item for item in thumbnails
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ]
            if candidates:
                candidates.sort(
                    key=lambda item: (
                        int(item.get("width") or 0) * int(item.get("height") or 0),
                        int(item.get("preference") or 0),
                    ),
                    reverse=True,
                )
                thumbnail = candidates[0].get("url")

    return MusicMetadata(
        title=title,
        performer=performer,
        file_base=file_base,
        duration=duration,
        thumbnail=str(thumbnail) if thumbnail else None,
        source_url=source_url,
        source=source,
    )


def choose_adaptive_bitrate(duration_seconds: float | int | None) -> int | None:
    try:
        duration = float(duration_seconds or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if duration <= 0:
        return None

    max_kbps = int((TARGET_AUDIO_BYTES * 8) / duration / 1000)
    selected = next((kbps for kbps in BITRATE_CHOICES_KBPS if kbps <= max_kbps), None)
    if selected is None or selected < MIN_SINGLE_FILE_KBPS:
        return None
    return selected


def make_music_plan(duration_seconds: float | int | None) -> MusicPlan:
    bitrate = choose_adaptive_bitrate(duration_seconds)
    if bitrate is None:
        return MusicPlan(mode="split", bitrate_kbps=SPLIT_BITRATE_KBPS)
    return MusicPlan(mode="single", bitrate_kbps=bitrate)


def _youtube_extractor_args(client: str) -> dict[str, dict[str, list[str]]]:
    args: dict[str, list[str]] = {"player_client": [client]}
    if client in {"android_vr", "web_embedded", "tv"}:
        args["player_skip"] = ["webpage", "configs"]
    return {"youtube": args}


def _extract_info_once(url: str, *, youtube_client: str | None = None) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if youtube_client:
        overrides["extractor_args"] = _youtube_extractor_args(youtube_client)
    options = build_ytdlp_youtube_options(
        skip_download=True,
        ignore_no_formats_error=True,
        **overrides,
    )
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise MusicDownloadError("No media metadata returned")
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise MusicDownloadError("LIVE_STREAM_NOT_SUPPORTED")
    return info


def _extract_info_sync(url: str, source: str) -> dict[str, Any]:
    low_memory_mode = source == "youtube" and _youtube_low_memory_mode()
    invidious_metadata_error: Exception | None = None
    piped_metadata_error: Exception | None = None

    if low_memory_mode and _configured_invidious_api_urls():
        try:
            logging.info("YouTube low-memory mode: trying Invidious metadata first")
            return _extract_invidious_info_sync(url)
        except Exception as exc:
            invidious_metadata_error = exc
            logging.warning(
                "Invidious metadata failed; continuing low-memory fallbacks: error=%s",
                exc,
            )

    if low_memory_mode and _configured_piped_api_urls():
        try:
            logging.info("YouTube low-memory mode: trying Piped metadata")
            return _extract_piped_info_sync(url)
        except Exception as exc:
            piped_metadata_error = exc
            logging.warning(
                "Piped metadata failed; trying one primary yt-dlp metadata request: error=%s",
                exc,
            )

    try:
        return _extract_info_once(url)
    except MusicDownloadError:
        raise
    except Exception as first_error:
        if source != "youtube":
            raise MusicDownloadError(str(first_error)) from first_error

        errors: list[str] = []
        if invidious_metadata_error is not None:
            errors.append(f"invidious-metadata: {invidious_metadata_error}")
        if piped_metadata_error is not None:
            errors.append(f"piped-metadata: {piped_metadata_error}")
        errors.append(f"primary: {first_error}")
        last_error: Exception = first_error

        if low_memory_mode:
            logging.warning(
                "YouTube low-memory metadata mode exhausted without public-client fan-out: error=%s",
                first_error,
            )
            raise MusicDownloadError(
                "\n--- YouTube low-memory metadata retries ---\n" + "\n".join(errors)
            ) from last_error

        logging.warning(
            "Primary YouTube metadata extraction failed; trying public clients: error=%s",
            first_error,
        )
        for client, _format_spec in YOUTUBE_PUBLIC_FALLBACK_PROFILES:
            try:
                logging.info("Trying YouTube metadata client: %s", client)
                return _extract_info_once(url, youtube_client=client)
            except MusicDownloadError:
                raise
            except Exception as fallback_error:
                last_error = fallback_error
                errors.append(f"{client}: {fallback_error}")
                logging.warning(
                    "YouTube metadata client failed: client=%s error=%s",
                    client,
                    fallback_error,
                )
        raise MusicDownloadError(
            "\n--- YouTube client retries ---\n" + "\n".join(errors)
        ) from last_error


async def fetch_music_metadata(
    url: str,
    *,
    source: str,
    timeout_seconds: float = 45.0,
) -> MusicMetadata:
    info = await asyncio.wait_for(
        asyncio.to_thread(_extract_info_sync, url, source),
        timeout=max(1.0, float(timeout_seconds)),
    )
    return build_music_metadata(info, source=source, source_url=url)


def _clear_ytdlp_outputs(out_template: str) -> None:
    base_path = out_template.replace(".%(ext)s", "")
    for path in glob.glob(f"{base_path}.*"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _run_ytdlp_mp3_once(
    url: str,
    out_template: str,
    bitrate_kbps: int,
    *,
    youtube_client: str | None = None,
    format_spec: str = "bestaudio/best",
) -> str:
    overrides: dict[str, Any] = {}
    if youtube_client:
        overrides["extractor_args"] = _youtube_extractor_args(youtube_client)
    options = build_ytdlp_youtube_options(
        format=format_spec,
        outtmpl=out_template,
        postprocessors=mp3_extract_postprocessors(str(int(bitrate_kbps))),
        merge_output_format="mp3",
        **overrides,
    )
    with YoutubeDL(options) as ydl:
        ydl.download([url])

    base_path = out_template.replace(".%(ext)s", "")
    expected = f"{base_path}.mp3"
    if os.path.isfile(expected):
        return expected
    matches = sorted(glob.glob(f"{base_path}.*"))
    for match in matches:
        if os.path.isfile(match):
            return match
    raise MusicDownloadError(f"MP3 output file missing: {base_path}")



def _configured_piped_api_urls() -> list[str]:
    raw = (os.getenv("PIPED_API_URLS") or "").strip()
    if not raw:
        return []
    return [
        item.strip().rstrip("/")
        for item in re.split(r"[,;\s]+", raw)
        if item.strip()
    ]


def _configured_invidious_api_urls() -> list[str]:
    raw = (os.getenv("INVIDIOUS_API_URLS") or "").strip()
    if not raw:
        return []
    return [
        item.strip().rstrip("/")
        for item in re.split(r"[,;\s]+", raw)
        if item.strip()
    ]


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _youtube_low_memory_mode() -> bool:
    return _env_truthy("YOUTUBE_LOW_MEMORY_MODE")


def _extract_piped_info_sync(url: str) -> dict[str, Any]:
    api_urls = _configured_piped_api_urls()
    if not api_urls:
        raise MusicDownloadError("Piped metadata is not configured")

    video_id = _youtube_video_id(url)
    if not video_id:
        raise MusicDownloadError("Unable to extract YouTube video id for Piped metadata")

    headers = {
        "Accept": "application/json",
        "User-Agent": "AbangRender-MusicBot/1.0",
    }
    errors: list[str] = []
    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=8.0),
        follow_redirects=True,
    ) as client:
        for api_url in api_urls:
            try:
                logging.info("Trying Piped YouTube metadata: instance=%s", api_url)
                response = client.get(f"{api_url}/streams/{video_id}", headers=headers)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not data.get("title"):
                    raise MusicDownloadError("Piped returned incomplete metadata")
                return {
                    "id": video_id,
                    "title": data.get("title"),
                    "uploader": data.get("uploader") or data.get("uploaderName"),
                    "channel": data.get("uploader") or data.get("uploaderName"),
                    "duration": data.get("duration"),
                    "thumbnail": data.get("thumbnailUrl") or data.get("thumbnail"),
                    "webpage_url": url,
                }
            except Exception as exc:
                errors.append(f"{api_url}: {exc}")
                logging.warning(
                    "Piped YouTube metadata failed: instance=%s error=%s",
                    api_url,
                    exc,
                )

    raise MusicDownloadError(
        "Piped YouTube metadata failed\n" + "\n".join(errors)
    )


def _fetch_invidious_video_from_instance_sync(
    url: str,
    api_url: str,
    *,
    local: bool = False,
) -> dict[str, Any]:
    video_id = _youtube_video_id(url)
    if not video_id:
        raise MusicDownloadError("Unable to extract YouTube video id for Invidious")

    headers = {
        "Accept": "application/json",
        "User-Agent": "AbangRender-MusicBot/1.0",
    }
    logging.info(
        "Trying Invidious YouTube API: instance=%s local=%s",
        api_url,
        local,
    )
    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=8.0),
        follow_redirects=True,
    ) as client:
        response = client.get(
            f"{api_url}/api/v1/videos/{video_id}",
            params={"local": "true" if local else "false"},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, dict) or not data.get("title"):
        raise MusicDownloadError("Invidious returned incomplete video data")
    return data


def _fetch_invidious_video_sync(
    url: str,
    *,
    local: bool = False,
) -> tuple[dict[str, Any], str]:
    api_urls = _configured_invidious_api_urls()
    if not api_urls:
        raise MusicDownloadError("Invidious fallback is not configured")

    errors: list[str] = []
    for api_url in api_urls:
        try:
            data = _fetch_invidious_video_from_instance_sync(
                url,
                api_url,
                local=local,
            )
            return data, api_url
        except Exception as exc:
            errors.append(f"{api_url}: {exc}")
            logging.warning(
                "Invidious YouTube API failed: instance=%s local=%s error=%s",
                api_url,
                local,
                exc,
            )

    raise MusicDownloadError(
        "Invidious YouTube API failed\n" + "\n".join(errors)
    )


def _invidious_thumbnail_url(data: dict[str, Any]) -> str | None:
    thumbnails = data.get("videoThumbnails")
    if not isinstance(thumbnails, list):
        return None
    candidates = [
        item
        for item in thumbnails
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            int(item.get("width") or 0) * int(item.get("height") or 0),
            int(item.get("width") or 0),
        ),
        reverse=True,
    )
    return str(candidates[0]["url"])


def _extract_invidious_info_sync(url: str) -> dict[str, Any]:
    data, _api_url = _fetch_invidious_video_sync(url)
    video_id = _youtube_video_id(url)
    return {
        "id": video_id,
        "title": data.get("title"),
        "uploader": data.get("author"),
        "channel": data.get("author"),
        "duration": data.get("lengthSeconds"),
        "thumbnail": _invidious_thumbnail_url(data),
        "webpage_url": url,
    }


def _invidious_audio_streams(data: dict[str, Any]) -> list[dict[str, Any]]:
    streams = data.get("adaptiveFormats")
    if not isinstance(streams, list):
        return []

    candidates: list[dict[str, Any]] = []
    for item in streams:
        if not isinstance(item, dict):
            continue
        media_url = item.get("url")
        if not isinstance(media_url, str) or not media_url.strip():
            continue
        mime_type = str(item.get("type") or "").lower()
        audio_quality = str(item.get("audioQuality") or "").strip()
        if not audio_quality and not mime_type.startswith("audio/"):
            continue
        candidates.append(item)

    def _score(item: dict[str, Any]) -> int:
        try:
            return int(item.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(candidates, key=_score, reverse=True)


def _pick_invidious_audio_stream(data: dict[str, Any]) -> dict[str, Any] | None:
    candidates = _invidious_audio_streams(data)
    return candidates[0] if candidates else None


def _invidious_raw_extension(stream: dict[str, Any]) -> str:
    mime = str(stream.get("type") or "").lower()
    container = str(stream.get("container") or "").lower()
    encoding = str(stream.get("encoding") or "").lower()
    probe = " ".join((mime, container, encoding))
    if "webm" in probe or "opus" in probe:
        return "webm"
    if "mp4" in probe or "m4a" in probe or "aac" in probe:
        return "m4a"
    if "ogg" in probe:
        return "ogg"
    return "audio"


def _invidious_latest_version_url(api_url: str, video_id: str, itag: str) -> str:
    return (
        f"{api_url.rstrip('/')}/latest_version?"
        + urlencode({"id": video_id, "itag": itag, "local": "true"})
    )


def _curl_proxy_url() -> str | None:
    proxy = (os.getenv("YTDLP_YOUTUBE_PROXY") or "").strip()
    if not proxy:
        return None
    if proxy.startswith("socks5://"):
        return "socks5h://" + proxy[len("socks5://"):]
    return proxy


def _download_invidious_source(
    media_url: str,
    raw_path: str,
    *,
    use_youtube_proxy: bool = True,
) -> int:
    command = [
        "curl",
        "-fL",
        "--silent",
        "--show-error",
        "--retry",
        "2",
        "--connect-timeout",
        "10",
        "--max-time",
        "180",
        "--max-filesize",
        str(INVIDIOUS_MAX_SOURCE_BYTES),
        "--range",
        "0-",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "--referer",
        "https://www.youtube.com/",
        "--write-out",
        "\\n__AR_META__%{http_code}\\t%{content_type}\\t%{size_download}\\t%{url_effective}",
    ]
    proxy = _curl_proxy_url() if use_youtube_proxy else None
    if proxy:
        command.extend(["--proxy", proxy])
    command.extend(["--output", raw_path, media_url])

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=210,
        check=False,
    )

    meta_text = ""
    stdout_text = process.stdout or ""
    if "__AR_META__" in stdout_text:
        meta_text = stdout_text.rsplit("__AR_META__", 1)[-1].strip()

    status_code = ""
    content_type = ""
    reported_size = ""
    effective_url = ""
    if meta_text:
        parts = meta_text.split("\t", 3)
        if len(parts) == 4:
            status_code, content_type, reported_size, effective_url = parts

    effective = urlparse(effective_url or media_url)
    final_host = (effective.hostname or "").lower()
    final_path = effective.path or "/"
    logging.info(
        "Invidious media HTTP result: status=%s content_type=%s size=%s final_host=%s final_path=%s via_proxy=%s",
        status_code or "unknown",
        content_type or "unknown",
        reported_size or "unknown",
        final_host or "unknown",
        final_path,
        bool(proxy),
    )

    if process.returncode != 0:
        error_text = (process.stderr or "").strip()
        raise MusicDownloadError(
            "Invidious media download failed"
            + (f": {error_text[-300:]}" if error_text else "")
        )

    normalized_type = content_type.lower().split(";", 1)[0].strip()
    if (
        normalized_type.startswith("text/")
        or normalized_type in {
            "application/json",
            "application/problem+json",
            "application/xml",
            "application/xhtml+xml",
        }
    ):
        raise MusicDownloadError(
            "Invidious media endpoint returned non-media response: "
            f"status={status_code or 'unknown'} "
            f"content_type={content_type or 'unknown'} "
            f"final_host={final_host or 'unknown'} "
            f"final_path={final_path}"
        )

    try:
        total = os.path.getsize(raw_path)
    except OSError as exc:
        raise MusicDownloadError("Invidious media file was not created") from exc
    if total <= 0:
        raise MusicDownloadError("Invidious audio source was empty")
    if total > INVIDIOUS_MAX_SOURCE_BYTES:
        raise MusicDownloadError("Invidious audio source exceeded safety limit")
    return total


def _validate_audio_source(raw_path: str) -> None:
    try:
        with open(raw_path, "rb") as handle:
            prefix = handle.read(32).lstrip()
    except OSError as exc:
        raise MusicDownloadError("Unable to inspect Invidious audio source") from exc

    if prefix.startswith((b"<", b"{", b"[")):
        raise MusicDownloadError("Invidious media endpoint returned non-media content")

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            raw_path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0 or "audio" not in (probe.stdout or "").lower():
        error_text = (probe.stderr or "").strip()
        raise MusicDownloadError(
            "Invidious source validation failed"
            + (f": {error_text[-300:]}" if error_text else "")
        )


def _run_invidious_mp3_from_instance_sync(
    url: str,
    out_template: str,
    bitrate_kbps: int,
    api_url: str,
) -> str:
    data = _fetch_invidious_video_from_instance_sync(
        url,
        api_url,
        local=False,
    )
    streams = _invidious_audio_streams(data)
    if not streams:
        raise MusicDownloadError("Invidious returned no usable audio stream")

    local_streams_by_itag: dict[str, tuple[dict[str, Any], str]] = {}
    try:
        local_data = _fetch_invidious_video_from_instance_sync(
            url,
            api_url,
            local=True,
        )
        for local_stream in _invidious_audio_streams(local_data):
            local_itag = str(local_stream.get("itag") or "").strip()
            if local_itag:
                local_streams_by_itag[local_itag] = (local_stream, api_url)
        logging.info(
            "Loaded Invidious API-generated local audio URLs: instance=%s count=%s",
            api_url,
            len(local_streams_by_itag),
        )
    except Exception as local_api_error:
        logging.warning(
            "Invidious local=true API fetch failed; continuing with other paths: error=%s",
            local_api_error,
        )

    base_path = out_template.replace(".%(ext)s", "")
    expected = f"{base_path}.mp3"
    errors: list[str] = []

    video_id = _youtube_video_id(url)
    for index, stream in enumerate(streams, start=1):
        raw_path = f"{base_path}.invidious-{index}.{_invidious_raw_extension(stream)}"
        try:
            itag = str(stream.get("itag") or "").strip()
            source_candidates: list[tuple[str, str, bool]] = []

            local_entry = local_streams_by_itag.get(itag)
            if local_entry is not None:
                local_stream, local_api_url = local_entry
                local_url = urljoin(
                    f"{local_api_url.rstrip('/')}/",
                    str(local_stream["url"]),
                )
                local_host = (urlparse(local_api_url).hostname or "").lower()
                media_host = (urlparse(local_url).hostname or "").lower()
                source_candidates.append(
                    (
                        "api-local",
                        local_url,
                        bool(media_host and media_host != local_host),
                    )
                )

            if video_id and itag.isdigit():
                source_candidates.append(
                    (
                        "latest-version",
                        _invidious_latest_version_url(api_url, video_id, itag),
                        False,
                    )
                )

            source_candidates.append(
                (
                    "signed-warp",
                    urljoin(f"{api_url.rstrip('/')}/", str(stream["url"])),
                    True,
                )
            )

            total = 0
            selected_source = ""
            candidate_errors: list[str] = []
            for source_name, media_url, use_youtube_proxy in source_candidates:
                parsed = urlparse(media_url)
                media_host = (parsed.hostname or "").lower()
                if parsed.scheme != "https" or not media_host:
                    candidate_errors.append(f"{source_name}: non-HTTPS media URL")
                    continue
                if media_host in {"localhost", "127.0.0.1", "::1"}:
                    candidate_errors.append(f"{source_name}: unsafe local media URL")
                    continue

                try:
                    logging.info(
                        "Trying Invidious audio source: source=%s instance=%s stream=%s/%s itag=%s",
                        source_name,
                        api_url,
                        index,
                        len(streams),
                        itag or "unknown",
                    )
                    total = _download_invidious_source(
                        media_url,
                        raw_path,
                        use_youtube_proxy=use_youtube_proxy,
                    )
                    _validate_audio_source(raw_path)
                    selected_source = source_name
                    break
                except Exception as source_error:
                    candidate_errors.append(f"{source_name}: {source_error}")
                    logging.warning(
                        "Invidious audio source failed: source=%s instance=%s stream=%s/%s itag=%s error=%s",
                        source_name,
                        api_url,
                        index,
                        len(streams),
                        itag or "unknown",
                        source_error,
                    )
                    try:
                        os.remove(raw_path)
                    except OSError:
                        pass

            if not selected_source:
                raise MusicDownloadError(
                    "All Invidious source paths failed: " + " | ".join(candidate_errors)
                )

            process = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    raw_path,
                    "-map",
                    "0:a:0?",
                    "-vn",
                    "-ac",
                    "2",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    f"{int(bitrate_kbps)}k",
                    expected,
                ],
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            if process.returncode != 0 or not os.path.isfile(expected):
                error_text = (process.stderr or "").strip()
                raise MusicDownloadError(
                    "Invidious ffmpeg conversion failed"
                    + (f": {error_text[-300:]}" if error_text else "")
                )

            logging.info(
                "Invidious YouTube audio succeeded: instance=%s source=%s source_bytes=%s stream=%s/%s",
                api_url,
                selected_source,
                total,
                index,
                len(streams),
            )
            return expected
        except Exception as exc:
            errors.append(f"stream {index}: {exc}")
            logging.warning(
                "Invidious signed audio stream failed: instance=%s stream=%s/%s error=%s",
                api_url,
                index,
                len(streams),
                exc,
            )
            try:
                if os.path.isfile(expected):
                    os.remove(expected)
            except OSError:
                pass
        finally:
            try:
                os.remove(raw_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    raise MusicDownloadError(
        "Invidious signed audio streams failed\n" + "\n".join(errors)
    )


def _run_invidious_mp3_sync(
    url: str,
    out_template: str,
    bitrate_kbps: int,
) -> str:
    api_urls = _configured_invidious_api_urls()
    if not api_urls:
        raise MusicDownloadError("Invidious fallback is not configured")

    errors: list[str] = []
    for api_url in api_urls:
        _clear_ytdlp_outputs(out_template)
        try:
            logging.info("Trying Invidious audio instance: %s", api_url)
            return _run_invidious_mp3_from_instance_sync(
                url,
                out_template,
                bitrate_kbps,
                api_url,
            )
        except Exception as exc:
            errors.append(f"{api_url}: {exc}")
            logging.warning(
                "Invidious audio instance failed: instance=%s error=%s",
                api_url,
                exc,
            )

    raise MusicDownloadError(
        "Invidious audio instances exhausted\n" + "\n".join(errors)
    )


def _youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None

    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            candidate = parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"} else ""
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None
    return None


def _pick_piped_audio_stream(data: dict[str, Any]) -> dict[str, Any] | None:
    streams = data.get("audioStreams")
    if not isinstance(streams, list):
        return None
    candidates = [
        item
        for item in streams
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and str(item.get("url")).startswith("https://")
    ]
    if not candidates:
        return None

    def _score(item: dict[str, Any]) -> tuple[int, int]:
        try:
            bitrate = int(item.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        original = int(str(item.get("audioTrackType") or "").upper() == "ORIGINAL")
        return original, bitrate

    return max(candidates, key=_score)


def _piped_raw_extension(stream: dict[str, Any]) -> str:
    mime = str(stream.get("mimeType") or "").lower()
    fmt = str(stream.get("format") or "").lower()
    if "webm" in mime or "webm" in fmt or "opus" in mime or "opus" in fmt:
        return "webm"
    if "mp4" in mime or "m4a" in mime or fmt in {"m4a", "mpeg_4", "mp4"}:
        return "m4a"
    if "ogg" in mime or "ogg" in fmt:
        return "ogg"
    return "audio"


def _run_piped_mp3_sync(
    url: str,
    out_template: str,
    bitrate_kbps: int,
) -> str:
    api_urls = _configured_piped_api_urls()
    if not api_urls:
        raise MusicDownloadError("Piped fallback is not configured")

    video_id = _youtube_video_id(url)
    if not video_id:
        raise MusicDownloadError("Unable to extract YouTube video id for Piped fallback")

    base_path = out_template.replace(".%(ext)s", "")
    expected = f"{base_path}.mp3"
    errors: list[str] = []

    headers = {
        "Accept": "application/json",
        "User-Agent": "AbangRender-MusicBot/1.0",
    }
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True) as client:
        for api_url in api_urls:
            raw_path: str | None = None
            try:
                logging.info("Trying Piped YouTube fallback: instance=%s", api_url)
                response = client.get(f"{api_url}/streams/{video_id}", headers=headers)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MusicDownloadError("Piped returned invalid JSON payload")

                stream = _pick_piped_audio_stream(data)
                if stream is None:
                    raise MusicDownloadError("Piped returned no usable audio stream")

                media_url = str(stream["url"])
                media_host = (urlparse(media_url).hostname or "").lower()
                if media_host in {"localhost", "127.0.0.1", "::1"}:
                    raise MusicDownloadError("Piped returned unsafe local media URL")

                raw_path = f"{base_path}.piped.{_piped_raw_extension(stream)}"
                total = 0
                with client.stream(
                    "GET",
                    media_url,
                    headers={"User-Agent": headers["User-Agent"]},
                ) as media_response:
                    media_response.raise_for_status()
                    with open(raw_path, "wb") as handle:
                        for chunk in media_response.iter_bytes(1024 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > PIPED_MAX_SOURCE_BYTES:
                                raise MusicDownloadError("Piped audio source exceeded safety limit")
                            handle.write(chunk)

                if total <= 0:
                    raise MusicDownloadError("Piped audio source was empty")

                process = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        raw_path,
                        "-map",
                        "0:a:0?",
                        "-vn",
                        "-ac",
                        "2",
                        "-c:a",
                        "libmp3lame",
                        "-b:a",
                        f"{int(bitrate_kbps)}k",
                        expected,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=240,
                    check=False,
                )
                if process.returncode != 0 or not os.path.isfile(expected):
                    error_text = (process.stderr or "").strip()
                    raise MusicDownloadError(
                        "Piped ffmpeg conversion failed"
                        + (f": {error_text[-500:]}" if error_text else "")
                    )

                logging.info(
                    "Piped YouTube fallback succeeded: instance=%s source_bytes=%s",
                    api_url,
                    total,
                )
                return expected
            except Exception as exc:
                errors.append(f"{api_url}: {exc}")
                logging.warning(
                    "Piped YouTube fallback failed: instance=%s error=%s",
                    api_url,
                    exc,
                )
                try:
                    if os.path.isfile(expected):
                        os.remove(expected)
                except OSError:
                    pass
            finally:
                if raw_path:
                    try:
                        os.remove(raw_path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass

    raise MusicDownloadError(
        "Piped YouTube fallback failed\n" + "\n".join(errors)
    )


def _run_ytdlp_mp3_sync(
    url: str,
    out_template: str,
    bitrate_kbps: int,
    source: str,
) -> str:
    low_memory_mode = source == "youtube" and _youtube_low_memory_mode()
    invidious_first_error: Exception | None = None
    piped_first_error: Exception | None = None

    if low_memory_mode and _configured_invidious_api_urls():
        try:
            logging.info("YouTube low-memory mode: trying Invidious before Piped/yt-dlp")
            return _run_invidious_mp3_sync(url, out_template, bitrate_kbps)
        except Exception as exc:
            invidious_first_error = exc
            _clear_ytdlp_outputs(out_template)
            logging.warning(
                "Invidious-first YouTube attempt failed; continuing low-memory fallbacks: error=%s",
                exc,
            )

    if low_memory_mode and _configured_piped_api_urls():
        try:
            logging.info("YouTube low-memory mode: trying Piped before yt-dlp")
            return _run_piped_mp3_sync(url, out_template, bitrate_kbps)
        except Exception as exc:
            piped_first_error = exc
            _clear_ytdlp_outputs(out_template)
            logging.warning(
                "Piped-first YouTube attempt failed; trying one primary yt-dlp request: error=%s",
                exc,
            )

    try:
        return _run_ytdlp_mp3_once(url, out_template, bitrate_kbps)
    except MusicDownloadError:
        raise
    except Exception as first_error:
        if source != "youtube":
            raise MusicDownloadError(str(first_error)) from first_error

        errors: list[str] = []
        if invidious_first_error is not None:
            errors.append(f"invidious-first: {invidious_first_error}")
        if piped_first_error is not None:
            errors.append(f"piped-first: {piped_first_error}")
        errors.append(f"primary: {first_error}")
        last_error: Exception = first_error

        if low_memory_mode:
            logging.warning(
                "YouTube low-memory mode exhausted without public-client fan-out: error=%s",
                first_error,
            )
            raise MusicDownloadError(
                "\n--- YouTube low-memory retries ---\n" + "\n".join(errors)
            ) from last_error

        logging.warning(
            "Primary YouTube audio download failed; trying public clients: error=%s",
            first_error,
        )
        for client, format_spec in YOUTUBE_PUBLIC_FALLBACK_PROFILES:
            _clear_ytdlp_outputs(out_template)
            try:
                logging.info(
                    "Trying YouTube audio client: client=%s format=%s",
                    client,
                    format_spec,
                )
                return _run_ytdlp_mp3_once(
                    url,
                    out_template,
                    bitrate_kbps,
                    youtube_client=client,
                    format_spec=format_spec,
                )
            except MusicDownloadError:
                raise
            except Exception as fallback_error:
                last_error = fallback_error
                errors.append(f"{client}: {fallback_error}")
                logging.warning(
                    "YouTube audio client failed: client=%s error=%s",
                    client,
                    fallback_error,
                )

        invidious_urls = _configured_invidious_api_urls()
        if invidious_urls:
            _clear_ytdlp_outputs(out_template)
            try:
                return _run_invidious_mp3_sync(url, out_template, bitrate_kbps)
            except Exception as invidious_error:
                last_error = invidious_error
                errors.append(f"invidious: {invidious_error}")
                logging.warning(
                    "Invidious YouTube fallback exhausted: error=%s",
                    invidious_error,
                )

        piped_urls = _configured_piped_api_urls()
        if piped_urls:
            _clear_ytdlp_outputs(out_template)
            try:
                return _run_piped_mp3_sync(url, out_template, bitrate_kbps)
            except Exception as piped_error:
                last_error = piped_error
                errors.append(f"piped: {piped_error}")
                logging.warning(
                    "Piped YouTube fallback exhausted: error=%s",
                    piped_error,
                )

        raise MusicDownloadError(
            "\n--- YouTube client retries ---\n" + "\n".join(errors)
        ) from last_error


def _cobalt_music_configured() -> bool:
    return bool(
        (os.getenv("COBALT_API_URL") or "").strip()
        and (os.getenv("COBALT_API_KEY") or "").strip()
    )


def _convert_cobalt_audio_source_sync(
    media_url: str,
    out_template: str,
    bitrate_kbps: int,
) -> str:
    base_path = out_template.replace(".%(ext)s", "")
    raw_path = f"{base_path}.cobalt-source"
    expected = f"{base_path}.mp3"
    total = 0

    try:
        with httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=True,
        ) as client:
            with client.stream(
                "GET",
                media_url,
                headers={"User-Agent": "AbangRender-MusicBot/1.0"},
            ) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type.startswith("text/") or "json" in content_type:
                    raise MusicDownloadError(
                        f"Cobalt media endpoint returned non-media content: {content_type or 'unknown'}"
                    )
                with open(raw_path, "wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > COBALT_MAX_SOURCE_BYTES:
                            raise MusicDownloadError("Cobalt audio source exceeded safety limit")
                        handle.write(chunk)

        if total <= 0:
            raise MusicDownloadError("Cobalt audio source was empty")

        process = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                raw_path,
                "-map",
                "0:a:0?",
                "-vn",
                "-ac",
                "2",
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{int(bitrate_kbps)}k",
                expected,
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        if process.returncode != 0 or not os.path.isfile(expected):
            error_text = (process.stderr or "").strip()
            raise MusicDownloadError(
                "Cobalt ffmpeg conversion failed"
                + (f": {error_text[-500:]}" if error_text else "")
            )

        logging.info(
            "Cobalt YouTube Music fallback succeeded: source_bytes=%s",
            total,
        )
        return expected
    finally:
        try:
            os.remove(raw_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


async def _run_cobalt_mp3(
    url: str,
    out_template: str,
    bitrate_kbps: int,
) -> str:
    base_url = (os.getenv("COBALT_API_URL") or "").strip()
    api_key = (os.getenv("COBALT_API_KEY") or "").strip()
    if not base_url or not api_key:
        raise MusicDownloadError("Cobalt fallback is not configured")

    logging.info("Trying Cobalt YouTube Music fallback")
    data = await fetch_cobalt_data(
        base_url,
        api_key,
        {
            "url": url,
            "downloadMode": "audio",
            "videoQuality": "max",
            "alwaysProxy": True,
            "localProcessing": "disabled",
        },
        source="youtube_music",
        timeout=20,
        attempts=2,
    )
    if not data:
        raise MusicDownloadError("Cobalt returned no usable response")

    parsed = parse_cobalt_media_response(
        data,
        audio_only=True,
        allow_multi_tunnel=True,
        source="youtube_music",
    )
    if not parsed or not parsed.items:
        raise MusicDownloadError("Cobalt returned no usable audio media")

    media_url = parsed.items[0][0]
    return await asyncio.to_thread(
        _convert_cobalt_audio_source_sync,
        media_url,
        out_template,
        bitrate_kbps,
    )


async def _download_mp3(
    url: str,
    *,
    work_dir: str,
    bitrate_kbps: int,
    source: str,
) -> str:
    out_template = os.path.join(work_dir, "source.%(ext)s")

    if source == "youtube" and _cobalt_music_configured():
        try:
            return await _run_cobalt_mp3(url, out_template, bitrate_kbps)
        except Exception as cobalt_error:
            logging.warning(
                "Cobalt YouTube Music primary path failed; falling back to direct chain: error=%s",
                cobalt_error,
            )
            _clear_ytdlp_outputs(out_template)
            try:
                return await asyncio.to_thread(
                    _run_ytdlp_mp3_sync,
                    url,
                    out_template,
                    bitrate_kbps,
                    source,
                )
            except MusicDownloadError as direct_error:
                raise MusicDownloadError(
                    f"Cobalt primary: {cobalt_error}\n--- Direct fallback ---\n{direct_error}"
                ) from direct_error

    if source == "youtube":
        logging.warning(
            "Cobalt YouTube Music primary path unavailable: COBALT_API_URL/COBALT_API_KEY not configured"
        )

    return await asyncio.to_thread(
        _run_ytdlp_mp3_sync,
        url,
        out_template,
        bitrate_kbps,
        source,
    )


async def _transcode_to_128k(source_path: str, target_path: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_path,
        "-map",
        "0:a:0?",
        "-vn",
        "-ac",
        "2",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{SPLIT_BITRATE_KBPS}k",
        target_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0 or not os.path.isfile(target_path):
        raise MusicDownloadError(
            "FFmpeg 128 kbps transcode failed: "
            + stderr.decode("utf-8", errors="ignore").strip()
        )
    return target_path


async def _split_mp3(source_path: str, work_dir: str) -> list[str]:
    pattern = os.path.join(work_dir, "part-%03d.mp3")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_path,
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_format",
        "mp3",
        "-segment_time",
        str(SEGMENT_SECONDS),
        "-reset_timestamps",
        "1",
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise MusicDownloadError(
            "FFmpeg split failed: " + stderr.decode("utf-8", errors="ignore").strip()
        )

    parts = sorted(glob.glob(os.path.join(work_dir, "part-*.mp3")))
    if not parts:
        raise MusicDownloadError("No MP3 parts were generated")
    too_large = [path for path in parts if os.path.getsize(path) > MAX_AUDIO_BYTES]
    if too_large:
        raise MusicDownloadError("Generated MP3 part exceeded Telegram-safe size")
    return parts


async def download_music_files(
    url: str,
    *,
    metadata: MusicMetadata,
    output_dir: str,
    job_id: str,
) -> MusicDownloadResult:
    clean_job_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id).strip("-") or "job"
    work_dir = os.path.join(output_dir, f"music-{clean_job_id}")
    await asyncio.to_thread(os.makedirs, work_dir, exist_ok=True)

    try:
        plan = make_music_plan(metadata.duration)
        source_path = await _download_mp3(
            url,
            work_dir=work_dir,
            bitrate_kbps=plan.bitrate_kbps,
            source=metadata.source,
        )
        source_size = os.path.getsize(source_path)

        if plan.mode == "single" and source_size <= MAX_AUDIO_BYTES:
            return MusicDownloadResult(
                work_dir=work_dir,
                paths=[source_path],
                bitrate_kbps=plan.bitrate_kbps,
                mode="single",
            )

        split_source = source_path
        if plan.bitrate_kbps != SPLIT_BITRATE_KBPS:
            split_source = os.path.join(work_dir, "split-source-128.mp3")
            await _transcode_to_128k(source_path, split_source)

        parts = await _split_mp3(split_source, work_dir)
        return MusicDownloadResult(
            work_dir=work_dir,
            paths=parts,
            bitrate_kbps=SPLIT_BITRATE_KBPS,
            mode="split",
        )
    except Exception:
        await asyncio.to_thread(shutil.rmtree, work_dir, True)
        raise


async def cleanup_music_result(result: MusicDownloadResult | None) -> None:
    if result is None:
        return
    path = Path(result.work_dir)
    if path.exists():
        await asyncio.to_thread(shutil.rmtree, path, True)
