"""Shared yt-dlp download scaffolding used by the TikTok and YouTube services."""

import glob
import os
from typing import Any, Callable

from utils.download_manager import DownloadError


def run_ytdlp_download(
    youtube_dl_factory: Callable[[dict[str, Any]], Any],
    url: str,
    ydl_opts: dict[str, Any],
) -> None:
    with youtube_dl_factory(ydl_opts) as ydl:
        ydl.download([url])


def resolve_downloaded_path(expected_path: str) -> str:
    if os.path.exists(expected_path):
        return expected_path
    stem, ext = os.path.splitext(expected_path)
    matches = sorted(glob.glob(f"{stem}*{ext}") + glob.glob(f"{stem}.*"))
    for match in matches:
        if os.path.isfile(match):
            return match
    raise DownloadError(f"yt-dlp output file missing: {expected_path}")


def mp3_extract_postprocessors(preferred_quality: str = "192") -> list[dict[str, str]]:
    return [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": str(preferred_quality),
    }]
