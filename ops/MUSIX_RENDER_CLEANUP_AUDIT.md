# MusiX Render service cleanup — risk-controlled audit (2026-10-09)

Purpose: free Render service capacity **without changing the working MusiX download paths**. This document is an inventory/checklist, not authorization to delete or reroute a service.

## Absolute invariant

Preserve public audio download behavior for **Spotify, SoundCloud, YouTube, Instagram, Threads, TikTok**. Do not change Telegram webhook, Render bot service, worker tokens, downstream worker URLs, proxy/WARP, cookies or DB, or other live deployment resources during inventory.

The active bot is `abangrender-music-bot` (Render Singapore, main); `main.py` registers `/telegram/webhook`.

## Dependencies established from current source and logs

- `services/media/music_download.py` uses Railway YouTube worker as a fast path (default `music-youtube-audio-worker-production.up.railway.app`) and supports an ENV override `RAILWAY_YOUTUBE_WORKER_URL`. Spotify ultimately resolves audio via YouTube, so check Spotify and YouTube together.
- Early October 2026 bot logs reported the *social fast worker* endpoint `https://abangrender-youtube-session-oregon.onrender.com`. **Do not delete** this worker.
- The main container showed a functioning local WARP proxy for YouTube/Threads at startup. This does not prove a remote session service is unused.
- `utils/cobalt_client.py`, `handlers/instagram.py`, `handlers/soundcloud.py` use `COBALT_API_URL` when configured. The connected Render service interface exposes no secret ENV read. Therefore it is impossible to verify which Cobalt endpoint is selected solely from connected metadata.
- `threads-share-resolver-sg` has a live deployment, and its intended relationship to live Threads resolution must be tested before deletion.

## Deployment-status observations, not deletion decisions

| Service | Latest deployment | Meaning |
| --- | --- | --- |
| `abangrender-cobalt-api` | build_failed | Candidate, never proven to serve traffic |
| `abangrender-cobalt-api2` | update_failed | Candidate, never proven to serve traffic |
| `abangrender-cobalt-clean` | build_failed | Candidate, never proven to serve traffic |
| `abangrender-cobalt-api3` | live | Retain until ENV/actual calls checked |
| `abangrender-cobalt-api4` | live | Retain until ENV/actual calls checked |
| `abangrender-cobalt-final` | live | Retain until ENV/actual calls checked |
| `abangrender-youtube-session-oregon` | live | Retain; reference observed |
| `abangrender-youtube-session-warp` | live | Retain until verified unused |
| `threads-share-resolver-sg` | live | Retain until tested |
| `abangrender-music-bot` | live | **Never delete for cleanup** |

**Important:** Render request logs and metrics were empty for both known-active and possibly-inactive services. Empty data must **not** be treated as proof of no traffic.

## Six-platform release gate

Before each deletion, and after each deletion, test a fresh public link (non-cached) through Telegram and confirm an audio file is delivered:

- [ ] Spotify track — returns track title, downloadable audio (YouTube worker path/fallback)
- [ ] SoundCloud track and on.soundcloud.com share — downloadable audio
- [ ] YouTube video and Shorts — downloadable audio
- [ ] Instagram Reel/Post with music — downloadable audio and correct title
- [ ] Threads video-with-music and image-with-music share — downloadable audio
- [ ] TikTok public video with sound — downloadable audio

Also confirm Telegram `getWebhookInfo` still points to Render MusiX, admin music-monitor group copy behavior is unaffected, and PayPing interactions unaffected.

Run the existing CI `tests.yml` against this branch; tests check regressions but **do not** replace end-to-end Telegram audio delivery.

## Grouping

Create/choose one Render Project `MusiX` and move the confirmed-retained services into it through the Render dashboard. Grouping is an organizational feature; it does not reduce consumed service slots. The available connected Render tool cannot create/move Render Projects or delete services, so no live cleanup changes are performed by this branch.

Do not remove backend services by service-name similarity alone. Verify the configured `COBALT_API_URL`, `YOUTUBE_SESSION_SERVER`, worker URL and Threads resolver env values via Render dashboard's protected Environment UI **without copying secrets to chat**.

## Planned cleanup order

1. Protect the main bot, current railway YouTube worker, Oregon worker, active WARP/session and Threads resolver.
2. Verify ENV URLs and green six-platform end-to-end checks.
3. Only then delete failed-deploy experiment services, **one service at a time**. Verify all platforms between deletions.
4. Evaluate other replicas separately, only after evidence they are not referenced.
5. Move remaining components to `MusiX` Render Project; reserve freed slots for MediaX disaster-recovery service.

Do not assert Spotify or YouTube independence from Railway: current source explicitly uses a Railway YouTube worker.
