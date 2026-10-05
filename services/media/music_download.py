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
from urllib.parse import parse_qs, urlparse

import httpx
from yt_dlp import YoutubeDL

from services.logger import logger as logging
from services.platforms.youtube_media import build_ytdlp_youtube_options
from services.platforms.ytdlp_helpers import mp3_extract_postprocessors

logging = logging.bind(service="music_download")

MAX_AUDIO_BYTES = 49 * 1024 * 1024
TARGET_AUDIO_BYTES = 47 * 1024 * 1024
BITRATE_CHOICES_KBPS = (320, 256, 224, 192, 160, 128)
MIN_SINGLE_FILE_KBPS = 128
SPLIT_BITRATE_KBPS = 128
SEGMENT_SECONDS = 2400
MUSIC_AUDIO_CACHE_VARIANT = "music_adaptive_mp3_v1"
PIPED_MAX_SOURCE_BYTES = 150 * 1024 * 1024
YOUTUBE_PUBLIC_FALLBACK_PROFILES: tuple[tuple[str, str], ...] = (
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
    piped_metadata_error: Exception | None = None

    if low_memory_mode and _configured_piped_api_urls():
        try:
            logging.info("YouTube low-memory mode: trying Piped metadata first")
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
    piped_first_error: Exception | None = None

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


async def _download_mp3(
    url: str,
    *,
    work_dir: str,
    bitrate_kbps: int,
    source: str,
) -> str:
    out_template = os.path.join(work_dir, "source.%(ext)s")
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
