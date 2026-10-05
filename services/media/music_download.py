from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
YOUTUBE_PUBLIC_FALLBACK_EXTRACTOR_ARGS = {
    "youtube": {
        "player_client": ["web_safari", "web_embedded", "tv"],
    },
}

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


def _extract_info_once(url: str, *, youtube_public_fallback: bool = False) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if youtube_public_fallback:
        overrides["extractor_args"] = YOUTUBE_PUBLIC_FALLBACK_EXTRACTOR_ARGS
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
    try:
        return _extract_info_once(url)
    except MusicDownloadError:
        raise
    except Exception as first_error:
        if source != "youtube":
            raise MusicDownloadError(str(first_error)) from first_error
        logging.warning(
            "Primary YouTube metadata extraction failed; trying public clients: error=%s",
            first_error,
        )
        try:
            return _extract_info_once(url, youtube_public_fallback=True)
        except MusicDownloadError:
            raise
        except Exception as fallback_error:
            raise MusicDownloadError(
                f"{first_error}\n--- YouTube public fallback ---\n{fallback_error}"
            ) from fallback_error


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
    youtube_public_fallback: bool = False,
) -> str:
    overrides: dict[str, Any] = {}
    if youtube_public_fallback:
        overrides["extractor_args"] = YOUTUBE_PUBLIC_FALLBACK_EXTRACTOR_ARGS
    options = build_ytdlp_youtube_options(
        format="bestaudio/best",
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


def _run_ytdlp_mp3_sync(
    url: str,
    out_template: str,
    bitrate_kbps: int,
    source: str,
) -> str:
    try:
        return _run_ytdlp_mp3_once(url, out_template, bitrate_kbps)
    except MusicDownloadError:
        raise
    except Exception as first_error:
        if source != "youtube":
            raise MusicDownloadError(str(first_error)) from first_error
        logging.warning(
            "Primary YouTube audio download failed; trying public clients: error=%s",
            first_error,
        )
        _clear_ytdlp_outputs(out_template)
        try:
            return _run_ytdlp_mp3_once(
                url,
                out_template,
                bitrate_kbps,
                youtube_public_fallback=True,
            )
        except MusicDownloadError:
            raise
        except Exception as fallback_error:
            raise MusicDownloadError(
                f"{first_error}\n--- YouTube public fallback ---\n{fallback_error}"
            ) from fallback_error


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
