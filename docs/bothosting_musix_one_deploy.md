# MusiX — Bot Hosting one-deploy preparation

**Status:** preparation only; DO NOT deploy or run MusiX yet. All changes stay on the `prepare/musix-one-deploy-20261014` Git branch until the owner approves the complete repair batch.

## Scope of current group cleanup

Removed group-only commands: `/clearlink`, `/playlist`, `/playall`, `/search`, `/playsync`, `/stopsync`. Their group handlers and registered group menu entries are gone. Private-chat `/playall` and `/search` are also removed, along with their default command-menu entries.

Retained: `/connectmusic`, `/connectadminmusic` (owner-only), `/clearall`, normal link-to-audio group handling, duplicate checks, audio history and Telegram monitoring. No database schema or existing stored audio has been deleted.

## Prepare Bot-Hosting.net (not yet executed)

1. First complete all requested bot fixes, review the final diff and run tests. Merge into the intended final branch only once.
2. The repo **is private**. Bot Hosting's dashboard supports importing a private repo after connecting GitHub and selecting the repo/branch. Its MCP create-deployment operation accepts **public** GitHub URLs only; do not try that API option for this private repo. Use the linked-GitHub UI or a separately authorized private-source workflow.
3. Select an **Application** with **Python 3.14** (supported by Bot Hosting); entry file `main.py`. Keep `requirements.txt` at the deployment root; Bot Hosting automatically installs its Python requirements at startup.
4. Add required secrets using the hosting **ENV** screen (never commit them): `BOT_TOKEN`, `ADMIN_ID` and a persistent `DATABASE_URL` for PostgreSQL. Confirm the existing production database is the intended one before connection. Configure any YouTube cookies via a secret file or `YTDLP_YOUTUBE_COOKIES_B64` only after verifying hosting support. Additional endpoints (`CUSTOM_API_URL`, `COBALT_API_URL`, tokens) are optional/configuration-dependent.
5. Before starting, verify the **ffmpeg / ffprobe**, **yt-dlp**, and YouTube JavaScript runtime prerequisites on Bot Hosting. The repo's Dockerfile installs OS utilities, but the managed Python runtime has not yet been verified to include them. Do not assume full media-download capability without a preflight.
6. Use a modest dedicated allocation from the Pro+ resource pool and limit downloader concurrency according to what the plan can support. Confirm logs, Python dependencies and disk usage before opening traffic to users.
7. Avoid running the *same Telegram bot token* concurrently on old Render hosting and Bot Hosting, especially for long polling. Schedule the cutover only after the new deployment is staged/ready and the old process can be stopped safely.
8. At the one authorized launch, verify startup, `/connectmusic`, group link -> audio, owner-only `/connectadminmusic`, no old group command menu, and that private `/search` and `/playall` no longer respond. Observe logs after launch.

No hosting deployment, project, resources, ENV or Telegram webhook should be changed in this preparation stage.

## Quiet chat cleanup (prepared; NOT LIVE YET)

- Automatic cleanup runs every day at 00:15 **server time**, separately from temporary download-file cleanup at 00:00. Both only write to server logs, never direct messages.
- Applies to private chats that have supplied messages since the cleanup registry was installed, and groups explicitly connected with `/connectmusic`. The `/connectadminmusic` monitoring group remains excluded unless separately connected for music.
- Incoming and outgoing messages are recorded as text/non-audio or audio/voice/audio document. The cleanup removes recorded text, commands, source links and status messages; never guesses unknown Telegram history IDs.
- Music track registry protects bot-generated audios and their captions. Exact file IDs protect user-uploaded audio. Same-song duplicates in the music registry, and exact-file duplicates, are removed where Telegram permits deletion; oldest/known registered keeper remains.
- Manual `/clearall` invokes the same cleanup engine immediately in private and connected group chats; group members can call it but the bot must have Delete Messages rights. Manual runs also normalise the retained audio title/caption.
- Telegram normally disallows deleting messages older than 48 hours, and bot privacy mode in groups may prevent observation of ordinary user messages. Unseen history cannot safely be auto-deleted; no unsafe numeric-ID sweeps.
- Schema adds `music_chat_cleanup_messages` via Alembic migration 017. The migration is applied only when the bot is launched with the approved deployment. Ensure PostgreSQL backup and migration readiness before the one-time cutover.

## Admin Music Monitor isolation + unique users

- `/connectmusic` is the ONLY group mode eligible for music group `/clearall` and automatic chat cleanup. Private chats remain eligible for the separate private auto-clean schedule.
- `/connectadminmusic` is owner-only, configures exactly one admin monitoring destination and **never** runs group auto-clean or group `/clearall`. This group may safely also host MediaX's bot/logs. Connecting as admin monitor deactivates any old music-room mode; cleaning additionally checks monitor ID as a hard safety gate.
- Each successful **nonduplicate** private MusiX audio delivery is mirrored to the admin group. Each copy includes Telegram user ID, username, name, source platform, original source link and date/time (Asia/Kuala_Lumpur, derived from the request message timestamp). A copy failure does not fabricate a success or interrupt the user audio.
- Private music success events are logged to `music_private_audio_deliveries` (Alembic 018) with composite key `(user_id, audio_message_id)` for retry idempotency.
- Hidden owner-only `/totalusermusic` works exclusively in the configured admin monitor group and counts `COUNT(DISTINCT user_id)` from this **MusiX-only** table, not the Telegram group messages, all database users, MediaX users, or unsuccessful downloads.
- **Historical note:** The count starts when this dedicated usage ledger is deployed. Do not claim it includes earlier MusiX usage without a verified backfill from MusiX-exclusive audio events.
- Never import MediaX user tables or its analytics. Mixing two bot log streams in one Telegram admin group does not change the source of the count.
- Before launch, exercise `/connectadminmusic` in a group shared with MediaX and verify group auto-clean and `/clearall` cannot erase any monitoring messages. Verify `/totalusermusic` is silent for non-owners and when issued outside the connected monitor group.
