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
