"""BotHosting entry point for MusiX, without Docker/Render assumptions.

Configure BotHosting (Python 3.14) startup command:
    python -m pip install -r requirements-bothosting.txt && exec python -u scripts/bothosting_start.py

All checks happen BEFORE the Telegram webhook is deleted or polling begins.
No server is started by importing this module, making it safe to unit test.
"""
from __future__ import annotations

import importlib
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_MODULES = ("aiogram", "yt_dlp", "sqlalchemy", "asyncpg")


def environment_errors(env: dict[str, str]) -> list[str]:
    """Check configuration without printing any secrets or connecting to services."""
    errors = []
    token = env.get("BOT_TOKEN", "").strip()
    if not token or ":" not in token or not token.split(":", 1)[0].isdigit():
        errors.append("BOT_TOKEN must be a Telegram bot token from BotFather.")
    admin = env.get("ADMIN_ID", "").strip()
    if not admin.isdecimal() or int(admin or "0") <= 0:
        errors.append("ADMIN_ID must be your numeric Telegram user ID.")
    database = env.get("DATABASE_URL", "").strip()
    if not database:
        errors.append("DATABASE_URL is required for persistent music playlists and groups.")
    else:
        try:
            parsed = urlsplit(database)
            if parsed.scheme not in ("postgresql", "postgresql+asyncpg") or not parsed.hostname:
                errors.append("DATABASE_URL must be a PostgreSQL URL with a hostname.")
        except ValueError:
            errors.append("DATABASE_URL is malformed.")
    return errors


def _tool_ok(binary: str) -> bool:
    path = shutil.which(binary)
    if not path:
        return False
    try:
        result = subprocess.run(
            [path, "-version" if binary != "deno" else "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _install_binary_tools() -> list[str]:
    errors = []

    # Prefer native FFmpeg; otherwise the pinned manylinux wheel supplies
    # BOTH ffmpeg and ffprobe without root, apt-get, or Docker.
    if not (_tool_ok("ffmpeg") and _tool_ok("ffprobe")):
        try:
            ffmpeg_binaries = importlib.import_module("ffmpeg_binaries")
            ffmpeg_binaries.init()
            ffmpeg_binaries.add_to_path()
        except Exception as exc:
            errors.append(
                "FFmpeg/FFprobe unavailable and ffmpeg-binaries-compat could not initialize: "
                + type(exc).__name__
            )

    if not (_tool_ok("ffmpeg") and _tool_ok("ffprobe")):
        errors.append("Both ffmpeg and ffprobe must be executable on PATH.")

    # yt-dlp needs a JavaScript runtime for modern YouTube extraction.
    if not _tool_ok("deno"):
        try:
            deno_module = importlib.import_module("deno")
            deno_binary = Path(deno_module.find_deno_bin())
            if deno_binary.is_file():
                os.environ["PATH"] = str(deno_binary.parent) + os.pathsep + os.environ.get("PATH", "")
        except Exception as exc:
            errors.append("Deno is unavailable: " + type(exc).__name__)
    if not _tool_ok("deno"):
        errors.append("Deno executable unavailable; YouTube JS extraction would be unreliable.")

    return errors


def _configure_bothosting() -> None:
    os.chdir(PROJECT_ROOT)
    os.environ["PYTHONUNBUFFERED"] = "1"

    # BotHosting is a Python worker. Do not rely on the old Render HTTPS
    # health server, Docker /app paths, or a public webhook port.
    os.environ.pop("RENDER_EXTERNAL_URL", None)
    os.environ.pop("TELEGRAM_WEBHOOK_BASE_URL", None)

    # Constrain default concurrency to a shared ~3GB plan. Explicit overrides
    # remain possible through environment variables in BotHosting.
    defaults = {
        "BOT_POLLING_TASKS_CONCURRENCY_LIMIT": "24",
        "BOT_SESSION_CONNECTION_LIMIT": "60",
        "DB_POOL_SIZE": "3",
        "DB_MAX_OVERFLOW": "3",
        "DOWNLOAD_QUEUE_MIN_WORKERS": "1",
        "DOWNLOAD_QUEUE_MAX_WORKERS": "3",
        "DOWNLOAD_MAX_WORKERS_CAP": "3",
        "BATCH_LINKS_MAX_CONCURRENCY": "2",
        "YTDLP_YOUTUBE_JS_RUNTIMES": "deno",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    for directory in ("downloads", "logs", "cookies"):
        (PROJECT_ROOT / directory).mkdir(parents=True, exist_ok=True)


def check() -> list[str]:
    errors = environment_errors(dict(os.environ))
    if errors:
        return errors

    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append(f"Required Python module {module} failed: {type(exc).__name__}")

    if errors:
        return errors
    return _install_binary_tools()


def main() -> int:
    _configure_bothosting()
    if "--config-check" in sys.argv:
        problems = environment_errors(dict(os.environ))
    else:
        problems = check()

    if problems:
        print("[BOTHOSTING] PRE-FLIGHT FAILED", flush=True)
        for problem in problems:
            print("[BOTHOSTING] " + problem, flush=True)
        print("[BOTHOSTING] No polling or webhook changes were made.", flush=True)
        return 2

    if "--config-check" in sys.argv:
        print("[BOTHOSTING] Environment syntax OK (no network test).", flush=True)
        return 0

    print("[BOTHOSTING] Pre-flight OK: FFmpeg, FFprobe, Deno and Python deps available.", flush=True)
    print("[BOTHOSTING] Starting MusiX in Telegram long-polling mode.", flush=True)
    print("[BOTHOSTING] Stop the old Render instance before launching to avoid 409 conflicts.", flush=True)
    if "--check" in sys.argv:
        return 0

    runpy.run_path(str(PROJECT_ROOT / "main.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
