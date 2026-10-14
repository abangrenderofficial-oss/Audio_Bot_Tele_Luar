# MusiX / BotHosting: one-deployment checklist

SOURCE BRANCH: bothosting-ready (NOT main)
TARGET: BotHosting Python 3.14 worker (Linux x86_64)

This does not use Docker, Render, GitHub Codespaces, or a webhook HTTP port.

## 1. Create one BotHosting deployment when approved

GitHub repo: abangrenderofficial-oss/Audio_Bot_Tele_Luar
Branch: bothosting-ready
Runtime: python
Version: 3.14
Entry file: scripts/bothosting_start.py

START COMMAND (one single line):
python -m pip install -r requirements-bothosting.txt && exec python -u scripts/bothosting_start.py

Keep GitHub auto-pull OFF so later code pushes cannot restart the production bot.
Use your paid Pro+ 3GB allocation (or the allocated RAM/CPU for the bot).

This command uses pinned FFmpeg, FFprobe and Deno Python binary wheels; no
root, apt-get, Dockerfile or system package installation is needed for x86_64.
It performs a fail-fast preflight before the Telegram poller is started.

## 2. Set ENV in BotHosting, NOT on GitHub

REQUIRED:
- BOT_TOKEN: existing MusiX BotFather token (do not regenerate).
- ADMIN_ID: Telegram numeric user id of the owner.
- DATABASE_URL: externally reachable persistent PostgreSQL connection URL.
  Use postgresql://USER:PASSWORD@HOST:PORT/DB with URL-escaped password.

The original bot can use an in-memory database if DATABASE_URL is omitted.
For BotHosting we reject that mode to avoid losing playlists after restarts.

OPTIONAL: copy from your known-good hosting settings only where needed:
- CUSTOM_API_URL: normally https://api.telegram.org
- MUSIC_CACHE_API_URL, MUSIC_CACHE_API_KEY
- COBALT_API_URL, COBALT_API_KEY
- SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, CHANNEL_ID
- Any PayPing integration settings used by your current music bot
- Other platform-specific settings only if already used in production.

Do not copy old Render-only localhost URLs.
Do not set RENDER_EXTERNAL_URL or TELEGRAM_WEBHOOK_BASE_URL: the
BotHosting entrypoint forces long polling.
Never commit secrets, bot tokens or exported YouTube cookies.

## 3. Safe migration sequence

1) Set branch, startup command, and ENV in BotHosting.
2) Check external PostgreSQL is reachable from the new host. Reusing the
   current database helps retain playlists/group state.
3) Stop the old Render MusiX process just before starting BotHosting.
   Two processes polling the same BOT_TOKEN cause Telegram 409 conflicts.
4) Start the new BotHosting worker. Look for:

[BOTHOSTING] Pre-flight OK: FFmpeg, FFprobe, Deno and Python deps available.
[BOTHOSTING] Starting MusiX in Telegram long-polling mode.
[STARTUP] Bot started successfully. Listening for updates...

If any preflight check fails, the bot does NOT delete a webhook or poll.

## 4. Post-deploy smoke tests (must perform on real hosting)

- /start, /search, and send a new YouTube URL; verify actual playable MP3.
- TikTok, Instagram, Threads video AND photo+music, SoundCloud, Spotify, X.
- /connectmusic, playlist, /playall, /search, /clearlink, /clearall.
- /connectadminmusic monitor: verify actual private downloads are forwarded.
- Verify deduplication, MP3 title quality, Telegram upload and large files.
- Restart once: confirm PostgreSQL-backed playlists/groups remain.
- Confirm no Telegram 409 conflict and the old instance remains stopped.
- Verify PayPing link and manual-only follow-up separately.

DO NOT treat offline preflight as proof of all these runtime functions.

## 5. Diagnostics (in BotHosting shell; no Telegram changes)

Check syntax of required ENV:
python scripts/bothosting_start.py --config-check

Check Python packages, FFmpeg/FFprobe and Deno without polling:
python scripts/bothosting_start.py --check

Normal production command is the START COMMAND above, not python main.py
and not the Render Docker entrypoint.

## Remaining platform limitations

- The Docker WARP/wireproxy helpers and Chromium/Xvfb browser-based
  PO-token provider are NOT recreated by the Python BotHosting entrypoint.
- YouTube extraction may still need cookies, trusted session,
  proxies/Cobalt, or other fixes; this cannot be proven before real tests.
- The prebuilt FFmpeg/FFprobe wheels used here target Linux x86_64.
- A live external PostgreSQL service is required; shared plan RAM/CPU,
  inbound/outbound internet connectivity and external service access must
  still be validated after deployment.

## GitHub safety

The existing GitHub Actions push workflow only matches main, master,
and music-bot-v2-* branches. This bothosting-ready branch does not match.
Opening a PR, however, triggers its pull_request workflow. Avoid PRs
if saving GitHub Actions minutes is required.

No changes to GitHub main, Render, PayPing or Telegram were made in the
preparation branch.
