from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request

import aiohttp
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import urljoin, urlparse

from services.logger import logger as logging
from services.platforms import CobaltMediaService
from utils.download_manager import (
    DownloadError as DownloadError,  # noqa: F401  (re-exported for handlers)
    DownloadMetrics,
)
from utils.http_client import get_http_session

logging = logging.bind(service="threads_media")

THREADS_POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?threads\.(?:com|net)/@(?P<username>[A-Za-z0-9._-]+)/post/(?P<code>[A-Za-z0-9_-]+)/*$",
    re.IGNORECASE,
)
THREADS_SHARE_URL_RE = re.compile(
    r"^https?://(?:www\.)?threads\.(?:com|net)/share/[A-Za-z0-9_-]+/*$",
    re.IGNORECASE,
)
THREADS_POST_URL_SCAN_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:com|net)/@[A-Za-z0-9._-]+/post/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
THREADS_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
}
# Important: /share/<id>/ behaves differently for crawler UAs. A normal browser
# UA receives the redirect to /@user/post/<code>; Googlebot can stay on the
# wrapper page, which is exactly why yt-dlp was reporting "No video post found".
THREADS_SHARE_HEADERS = {
    "Accept": THREADS_PAGE_HEADERS["Accept"],
    "Accept-Language": "en",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    ),
}
THREADS_MEDIA_HEADERS = {
    "Referer": "https://www.threads.com/",
    "User-Agent": THREADS_PAGE_HEADERS["User-Agent"],
}
THREADS_SHARE_CRAWLER_UAS = (
    ("telegram", "TelegramBot (like TwitterBot)"),
    ("facebook", "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"),
    ("whatsapp", "WhatsApp/2.24.7.81 A"),
    ("twitter", "Twitterbot/1.0"),
    ("discord", "Discordbot/2.0"),
)


def strip_threads_url(url: str) -> str:
    """Canonicalize a public Threads post URL for dedupe and file caching."""
    candidate = (url or "").strip()
    try:
        parsed = urlparse(candidate)
    except Exception:
        return candidate

    normalized = f"{parsed.scheme.lower() or 'https'}://{parsed.netloc.lower()}{parsed.path}"
    match = THREADS_POST_URL_RE.fullmatch(normalized)
    if not match:
        return candidate
    return f"https://www.threads.com/@{match.group('username')}/post/{match.group('code')}"


def extract_threads_post_code(url: str) -> str | None:
    match = THREADS_POST_URL_RE.fullmatch(strip_threads_url(url))
    return match.group("code") if match else None


def _threads_path_only(url: str) -> str:
    candidate = (url or "").strip()
    try:
        parsed = urlparse(candidate)
    except Exception:
        return candidate
    return f"{parsed.scheme.lower() or 'https'}://{parsed.netloc.lower()}{parsed.path}"


def _extract_threads_post_url_from_html(page: str) -> str | None:
    # Threads share pages can expose the destination in og/canonical markup or
    # inside JSON where slashes are escaped. Normalize both forms before scan.
    normalized_page = unescape(page or "").replace("\\/", "/")
    match = THREADS_POST_URL_SCAN_RE.search(normalized_page)
    if match:
        return strip_threads_url(match.group(0))

    # Newer /share/<id>/ pages may keep the share URL as og:url while embedding
    # the actual post object in Threads' data-sjs JSON. Recover code + username.
    for payload in _iter_json_blobs(page):
        for node in _walk_json(payload):
            code = node.get("code")
            user = node.get("user")
            username = user.get("username") if isinstance(user, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
                continue
            if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", username):
                continue
            if not (
                node.get("caption")
                or node.get("text_post_app_info")
                or node.get("video_versions")
                or node.get("image_versions2")
                or node.get("carousel_media")
            ):
                continue
            return f"https://www.threads.com/@{username}/post/{code}"
    return None


async def fetch_threads_share_page(url: str) -> tuple[str, str]:
    session = await get_http_session()
    # Threads currently resolves /share/<token>/ server-side for crawler UAs,
    # while ordinary browser UAs can be left on the generic JS/login shell.
    async with session.get(
        url,
        headers=THREADS_PAGE_HEADERS,
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        final_url = str(response.url)
        page = await response.text()
        logging.info(
            "Threads Googlebot share fetch: status=%s final=%s bytes=%s",
            response.status,
            final_url[:220],
            len(page),
        )
        return final_url, page


async def resolve_threads_share_via_headerless_fetch(url: str) -> str | None:
    """Mirror browser extension fetch: no explicit/automatic User-Agent header."""
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        ) as session:
            async with session.get(
                url,
                headers={"Accept-Language": "en"},
                allow_redirects=True,
            ) as response:
                final_url = str(response.url)
                page = await response.text(errors="replace")
    except Exception as exc:
        logging.info("Threads headerless resolver request failed: error=%s", exc)
        return None

    resolved = strip_threads_url(final_url)
    if extract_threads_post_code(resolved):
        logging.info(
            "Resolved Threads share URL via headerless browser-style fetch: %s -> %s",
            url,
            resolved,
        )
        return resolved

    resolved = _extract_threads_post_url_from_html(page)
    if resolved:
        logging.info(
            "Resolved Threads share URL via headerless page metadata: %s -> %s",
            url,
            resolved,
        )
        return resolved

    logging.info(
        "Threads headerless resolver miss: status=%s final=%s bytes=%s",
        response.status,
        final_url[:220],
        len(page),
    )
    return None


async def resolve_threads_share_via_manual_redirect(url: str) -> str | None:
    """Walk Threads' raw GET redirect chain with a non-browser user agent."""
    session = await get_http_session()
    current = (url or "").strip()

    for hop in range(5):
        try:
            async with session.get(
                current,
                headers={
                    "Accept": "*/*",
                    "User-Agent": "curl/8.5.0",
                },
                allow_redirects=False,
                timeout=10,
            ) as response:
                location = (response.headers.get("Location") or "").strip()
                status = response.status
        except Exception as exc:
            logging.info(
                "Threads manual redirect resolver request failed: hop=%s error=%s",
                hop,
                exc,
            )
            return None

        if not location:
            logging.info(
                "Threads manual redirect resolver stopped: hop=%s status=%s no_location=true",
                hop,
                status,
            )
            return None

        next_url = urljoin(current, location)
        try:
            parsed = urlparse(next_url)
        except Exception:
            return None
        host = parsed.netloc.lower().split(":", 1)[0]
        if parsed.scheme != "https" or host not in {
            "threads.com",
            "www.threads.com",
            "threads.net",
            "www.threads.net",
        }:
            logging.info(
                "Threads manual redirect resolver rejected target: hop=%s host=%s",
                hop,
                host,
            )
            return None

        canonical = strip_threads_url(next_url)
        if extract_threads_post_code(canonical) or re.match(
            r"^/t/[A-Za-z0-9_-]+/?$",
            parsed.path,
            re.IGNORECASE,
        ):
            logging.info(
                "Resolved Threads share URL via manual redirect: %s -> %s",
                url,
                canonical if extract_threads_post_code(canonical) else next_url,
            )
            return canonical if extract_threads_post_code(canonical) else next_url

        if next_url == current:
            return None
        current = next_url

    return None


async def resolve_threads_share_via_head(url: str) -> str | None:
    """Probe HEAD redirects for /share/ tokens before heavier resolution paths."""
    try:
        path = urlparse(url).path
    except Exception:
        return None
    if not path or "/share/" not in path:
        return None

    session = await get_http_session()
    user_agents = (
        THREADS_SHARE_HEADERS["User-Agent"],
        "TelegramBot (like TwitterBot)",
        "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "WhatsApp/2.24.7.81 A",
    )
    for origin in ("https://www.threads.com", "https://www.threads.net"):
        target = f"{origin}{path}"
        for user_agent in user_agents:
            try:
                async with session.head(
                    target,
                    headers={"User-Agent": user_agent, "Accept": "*/*"},
                    allow_redirects=True,
                    timeout=8,
                ) as response:
                    final_url = strip_threads_url(str(response.url))
            except Exception:
                continue

            final_path = urlparse(final_url).path
            if extract_threads_post_code(final_url) or "/t/" in final_path:
                logging.info(
                    "Resolved Threads share URL via HEAD: %s -> %s",
                    url,
                    final_url,
                )
                return final_url
    return None


def _extract_threads_lsd_token(page: str) -> str | None:
    """Recover the current anonymous LSD token embedded in Threads HTML."""
    text = unescape(page or "").replace("\\/", "/")
    patterns = (
        r'"LSD"\s*,\s*\[\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
        r'"token"\s*:\s*"([^"]+)"[^{}]{0,300}"LSD"',
        r'name=["\']lsd["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']lsd["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            token = match.group(1).strip()
            if 6 <= len(token) <= 256 and re.fullmatch(r"[A-Za-z0-9._-]+", token):
                return token
    return None


def _threads_shortcode_from_numeric_id(value: object) -> str | None:
    """Convert Threads' numeric media id back to its URL shortcode."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0:
        return None

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    chars: list[str] = []
    while number:
        number, remainder = divmod(number, 64)
        chars.append(alphabet[remainder])
    return "".join(reversed(chars)) or None


async def resolve_threads_share_via_bulk_route(
    url: str,
    *,
    bootstrap_page: str = "",
) -> str | None:
    """Ask Threads' route-definition endpoint which post a /share/ path targets.

    This avoids relying on browser redirects. The endpoint returns a numeric
    post_id even when the public share page itself is only a login shell.
    """
    try:
        path = urlparse(url).path
    except Exception:
        return None
    if not path or "/share/" not in path:
        return None

    session = await get_http_session()
    lsd_token = _extract_threads_lsd_token(bootstrap_page)
    if not lsd_token:
        # Fall back to the previous anonymous token only when the current shell
        # does not expose one. A stale token can return a valid-looking route
        # response that omits the share target.
        lsd_token = "XudMkvWGqcnLxbgeR25f3V"
    logging.info(
        "Threads bulk-route LSD token source=%s len=%s",
        "page" if _extract_threads_lsd_token(bootstrap_page) else "fallback",
        len(lsd_token),
    )
    form = {
        "route_urls[0]": path,
        "__a": "1",
        "__comet_req": "29",
        "lsd": lsd_token,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
            "Gecko/20100101 Firefox/115.0"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-FB-LSD": lsd_token,
        "X-ASBD-ID": "129477",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    for origin in ("https://www.threads.com", "https://www.threads.net"):
        try:
            async with session.post(
                f"{origin}/ajax/bulk-route-definitions/",
                data=form,
                headers=headers,
                allow_redirects=True,
                timeout=15,
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logging.info(
                        "Threads bulk-route resolver HTTP miss: origin=%s status=%s",
                        origin,
                        response.status,
                    )
                    continue
        except Exception as exc:
            logging.info(
                "Threads bulk-route resolver request failed: origin=%s error=%s",
                origin,
                exc,
            )
            continue

        payload_text = body.strip()
        if payload_text.startswith("for (;;);"):
            payload_text = payload_text[len("for (;;);"):].lstrip()

        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            logging.info(
                "Threads bulk-route resolver returned non-JSON payload: origin=%s bytes=%s",
                origin,
                len(body),
            )
            continue

        payloads = (
            payload.get("payload", {}).get("payloads", {})
            if isinstance(payload, dict)
            else {}
        )
        route_payload = None
        if isinstance(payloads, dict):
            route_payload = payloads.get(path) or payloads.get(path.rstrip("/"))
            if route_payload is None:
                target_path = path.rstrip("/")
                for route_key, route_value in payloads.items():
                    if str(route_key).rstrip("/") == target_path:
                        route_payload = route_value
                        break

        result = (
            route_payload.get("result")
            if isinstance(route_payload, dict)
            else None
        )

        # The route-definition schema changes often. Recover either a canonical
        # post URL/path or any post_id from the selected route payload instead
        # of depending on one exact redirect_result nesting.
        post_id = None
        canonical_candidate = None

        def _scan_route_value(value: Any, key_hint: str = "") -> None:
            nonlocal post_id, canonical_candidate
            if post_id is not None and canonical_candidate is not None:
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    _scan_route_value(child, str(key))
                return
            if isinstance(value, list):
                for child in value:
                    _scan_route_value(child, key_hint)
                return
            if isinstance(value, str):
                normalized = unescape(value).replace("\\/", "/")
                match = THREADS_POST_URL_SCAN_RE.search(normalized)
                if match and canonical_candidate is None:
                    canonical_candidate = strip_threads_url(match.group(0))
                    return
                path_match = re.search(
                    r"/@([A-Za-z0-9._-]+)/post/([A-Za-z0-9_-]+)",
                    normalized,
                )
                if path_match and canonical_candidate is None:
                    canonical_candidate = (
                        f"https://www.threads.com/@{path_match.group(1)}/post/{path_match.group(2)}"
                    )
                    return
                if key_hint.lower() in {"post_id", "postid"} and value.isdigit():
                    post_id = value
            elif key_hint.lower() in {"post_id", "postid"} and isinstance(value, int):
                post_id = value

        _scan_route_value(result if result is not None else route_payload)
        # If Meta moved the redirect target outside the route's result object,
        # scan the complete response as a schema-change fallback.
        if post_id is None or canonical_candidate is None:
            _scan_route_value(payload)

        if canonical_candidate and extract_threads_post_code(canonical_candidate):
            logging.info(
                "Resolved Threads share URL via bulk-route canonical: %s -> %s",
                url,
                canonical_candidate,
            )
            return canonical_candidate

        shortcode = _threads_shortcode_from_numeric_id(post_id)
        if shortcode:
            resolved = f"https://www.threads.com/t/{shortcode}"
            logging.info(
                "Resolved Threads share URL via bulk route post_id: %s -> %s",
                url,
                resolved,
            )
            return resolved

        route_keys = (
            list(route_payload.keys())[:12]
            if isinstance(route_payload, dict)
            else []
        )
        result_keys = list(result.keys())[:12] if isinstance(result, dict) else []
        logging.info(
            "Threads bulk-route resolver had no usable target: origin=%s path=%s "
            "payload_keys=%s route_keys=%s result_keys=%s",
            origin,
            path,
            list(payloads.keys())[:8] if isinstance(payloads, dict) else [],
            route_keys,
            result_keys,
        )

    return None


async def resolve_threads_share_via_vxthreads(url: str) -> str | None:
    """Resolve /share/<id>/ through the current vxThreads preview service."""
    try:
        path = urlparse(url).path
    except Exception:
        return None
    match = re.fullmatch(r"/share/([A-Za-z0-9_-]+)/?", path)
    if not match:
        return None

    share_id = match.group(1)
    mirror_url = f"https://www.vxthreads.com/share/{share_id}/"
    session = await get_http_session()
    try:
        async with session.get(
            mirror_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
            },
            allow_redirects=False,
            timeout=12,
        ) as response:
            status = response.status
            location = response.headers.get("Location") or response.headers.get("location")
            page = await response.text() if status == 200 else ""
    except Exception as exc:
        logging.info("vxThreads share resolver failed: error=%s", exc)
        return None

    if location:
        absolute_location = location
        if location.startswith("/"):
            absolute_location = f"https://www.vxthreads.com{location}"

        resolved = strip_threads_url(absolute_location)
        if extract_threads_post_code(resolved):
            logging.info(
                "Resolved Threads share URL via vxThreads redirect: %s -> %s",
                url,
                resolved,
            )
            return resolved

        try:
            redirected = urlparse(absolute_location)
        except Exception:
            redirected = None
        if redirected and redirected.netloc.lower() in {"vxthreads.com", "www.vxthreads.com"}:
            local_match = re.fullmatch(
                r"/@([A-Za-z0-9._-]+)/post/([A-Za-z0-9_-]+)/?",
                redirected.path,
            )
            if local_match:
                resolved = (
                    f"https://www.threads.com/@{local_match.group(1)}/post/{local_match.group(2)}"
                )
                logging.info(
                    "Resolved Threads share URL via vxThreads local redirect: %s -> %s",
                    url,
                    resolved,
                )
                return resolved

    if status == 200 and page:
        resolved = _extract_threads_post_url_from_html(page)
        if resolved:
            logging.info(
                "Resolved Threads share URL via vxThreads page: %s -> %s",
                url,
                resolved,
            )
            return resolved

        local_match = re.search(
            r"https?://(?:www\.)?vxthreads\.com/@([A-Za-z0-9._-]+)/post/([A-Za-z0-9_-]+)",
            unescape(page).replace("\\/", "/"),
            re.IGNORECASE,
        )
        if local_match:
            resolved = (
                f"https://www.threads.com/@{local_match.group(1)}/post/{local_match.group(2)}"
            )
            logging.info(
                "Resolved Threads share URL via vxThreads embedded canonical: %s -> %s",
                url,
                resolved,
            )
            return resolved

    logging.info(
        "vxThreads share resolver miss: status=%s location=%s bytes=%s",
        status,
        (location or "-")[:240],
        len(page),
    )
    return None


async def resolve_threads_share_via_railway(url: str) -> str | None:
    """Resolve opaque Threads share aliases from the existing Railway worker."""
    # Do not use RAILWAY_YOUTUBE_WORKER_URL here: production currently
    # repoints that variable at the separate Render social worker. This resolver
    # must hit the Railway function whose /threads-resolve route we control.
    base_url = "https://music-youtube-audio-worker-production.up.railway.app"
    token = (
        (os.getenv("BOT_TOKEN") or "").strip()
        or (os.getenv("AR_MUSIC_WORKER_KEY_V3") or "").strip()
        or (os.getenv("YOUTUBE_WORKER_API_KEY") or "").strip()
        or (os.getenv("RAILWAY_YOUTUBE_WORKER_API_KEY") or "").strip()
    )
    if not base_url or not token:
        logging.info(
            "Railway Threads resolver skipped: endpoint=%s token_present=%s",
            bool(base_url),
            bool(token),
        )
        return None

    session = await get_http_session()
    try:
        async with session.post(
            f"{base_url}/threads-resolve",
            json={"url": url},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "AbangRender-MusicBot/1.0",
            },
            allow_redirects=False,
            timeout=45,
        ) as response:
            status = response.status
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = None
    except Exception as exc:
        logging.info("Railway Threads resolver request failed: error=%s", exc)
        return None

    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        logging.info(
            "Railway Threads resolver miss: status=%s error=%s",
            status,
            payload.get("error") if isinstance(payload, dict) else "invalid_payload",
        )
        return None

    resolved = strip_threads_url(str(payload.get("url") or "").strip())
    if extract_threads_post_code(resolved):
        logging.info(
            "Resolved Threads share URL via Railway edge: %s -> %s",
            url,
            resolved,
        )
        return resolved

    try:
        parsed = urlparse(resolved)
    except Exception:
        parsed = None
    if (
        parsed
        and parsed.scheme == "https"
        and parsed.netloc.lower() in {"threads.com", "www.threads.com"}
        and re.fullmatch(r"/t/[A-Za-z0-9_-]+/?", parsed.path)
    ):
        logging.info(
            "Resolved Threads share URL via Railway edge short form: %s -> %s",
            url,
            resolved,
        )
        return resolved

    logging.info("Railway Threads resolver returned unsupported target")
    return None


async def resolve_threads_share_via_fzthreads(url: str) -> str | None:
    """Resolve a Threads share link through FzThreads' independent resolver."""
    try:
        path = urlparse(url).path
    except Exception:
        return None
    match = re.fullmatch(r"/share/([A-Za-z0-9_-]+)/?", path)
    if not match:
        return None

    share_id = match.group(1)
    session = await get_http_session()
    try:
        async with session.get(
            f"https://fzthreads.com/share/{share_id}",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "TelegramBot (like TwitterBot)",
            },
            allow_redirects=False,
            timeout=12,
        ) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location") or response.headers.get("location")
                if location:
                    resolved_redirect = strip_threads_url(location)
                    if extract_threads_post_code(resolved_redirect):
                        logging.info(
                            "Resolved Threads share URL via FzThreads redirect: %s -> %s",
                            url,
                            resolved_redirect,
                        )
                        return resolved_redirect
                return None
            if response.status != 200:
                logging.info(
                    "FzThreads resolver returned HTTP %s for share=%s",
                    response.status,
                    share_id,
                )
                return None
            page = await response.text()
    except Exception as exc:
        logging.info("FzThreads resolver failed: error=%s", exc)
        return None

    resolved = _extract_threads_post_url_from_html(page)
    if resolved:
        logging.info(
            "Resolved Threads share URL via FzThreads page: %s -> %s",
            url,
            resolved,
        )
        return resolved
    return None


async def resolve_threads_share_via_fixembed(url: str) -> str | None:
    """Use FixEmbed's Threads resolver when Meta blocks this server's share redirect."""
    session = await get_http_session()
    try:
        async with session.get(
            "https://fixembed.app/api/embed",
            params={"url": url},
            headers={
                "Accept": "application/json",
                "User-Agent": "AbangRender-MusicBot/1.0",
            },
            allow_redirects=True,
            timeout=12,
        ) as response:
            if response.status != 200:
                logging.info(
                    "FixEmbed Threads resolver returned HTTP %s",
                    response.status,
                )
                return None
            payload = await response.json(content_type=None)
    except Exception as exc:
        logging.info("FixEmbed Threads resolver failed: error=%s", exc)
        return None

    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    if str(payload.get("platform") or "").lower() != "threads":
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    resolved = strip_threads_url(str(data.get("url") or "").strip())
    if extract_threads_post_code(resolved):
        logging.info(
            "Resolved Threads share URL via FixEmbed: %s -> %s",
            url,
            resolved,
        )
        return resolved

    return None


async def resolve_threads_share_via_crawlers(url: str) -> str | None:
    """Try link-preview crawler UAs; Threads often serves them richer share metadata."""
    session = await get_http_session()
    for label, user_agent in THREADS_SHARE_CRAWLER_UAS:
        try:
            async with session.get(
                url,
                headers={
                    "Accept": THREADS_PAGE_HEADERS["Accept"],
                    "Accept-Language": "en-US,en;q=0.8",
                    "User-Agent": user_agent,
                },
                allow_redirects=True,
                timeout=12,
            ) as response:
                final_url = str(response.url)
                page = await response.text()
        except Exception as exc:
            logging.info(
                "Threads crawler resolver request failed: ua=%s error=%s",
                label,
                exc,
            )
            continue

        resolved = strip_threads_url(final_url)
        if extract_threads_post_code(resolved):
            logging.info(
                "Resolved Threads share URL via crawler redirect: ua=%s %s -> %s",
                label,
                url,
                resolved,
            )
            return resolved

        resolved = _extract_threads_post_url_from_html(page)
        if resolved:
            logging.info(
                "Resolved Threads share URL via crawler page: ua=%s %s -> %s",
                label,
                url,
                resolved,
            )
            return resolved

        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            page or "",
            re.IGNORECASE | re.DOTALL,
        )
        logging.info(
            "Threads crawler resolver miss: ua=%s status=%s final=%s bytes=%s title=%s",
            label,
            response.status,
            final_url[:220],
            len(page or ""),
            (
                re.sub(r"\s+", " ", unescape(title_match.group(1))).strip()[:120]
                if title_match
                else "-"
            ),
        )
    return None


def _resolve_threads_share_with_chromium_sync(url: str) -> str | None:
    """Resolve /share/<id>/ using Chromium's real browser navigation.

    The plain HTTP page can be only a login shell on server IPs. A real browser
    still performs Threads' client-side navigation. Read the active tab URL
    from Chrome DevTools instead of relying on page metadata.
    """
    binary = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if not binary:
        logging.warning("Threads Chromium fallback unavailable: browser binary not found")
        return None

    profile_dir = tempfile.mkdtemp(prefix="threads-share-chrome-")
    process: subprocess.Popen | None = None
    try:
        command = [
            binary,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--proxy-server=socks5://127.0.0.1:1080",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile_dir}",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
            url,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        port_file = os.path.join(profile_dir, "DevToolsActivePort")
        port: int | None = None
        startup_deadline = time.monotonic() + 6.0
        while time.monotonic() < startup_deadline:
            if process.poll() is not None:
                break
            try:
                with open(port_file, "r", encoding="utf-8") as handle:
                    first_line = handle.readline().strip()
                if first_line.isdigit():
                    port = int(first_line)
                    break
            except (FileNotFoundError, OSError):
                pass
            time.sleep(0.1)

        if not port:
            logging.warning(
                "Threads Chromium fallback could not open DevTools: rc=%s",
                process.poll(),
            )
            return None

        last_url = url
        navigation_deadline = time.monotonic() + 12.0
        endpoint = f"http://127.0.0.1:{port}/json"
        while time.monotonic() < navigation_deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(endpoint, timeout=1.0) as response:
                    targets = json.loads(response.read().decode("utf-8", errors="replace"))
                if isinstance(targets, list):
                    for target in targets:
                        if not isinstance(target, dict) or target.get("type") != "page":
                            continue
                        current_url = str(target.get("url") or "").strip()
                        if current_url:
                            last_url = current_url
                        resolved = strip_threads_url(current_url)
                        if extract_threads_post_code(resolved):
                            return resolved
            except Exception:
                pass
            time.sleep(0.25)

        logging.warning(
            "Threads Chromium fallback did not navigate to canonical post: final=%s",
            last_url[:300],
        )
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("Threads Chromium fallback failed to run: error=%s", exc)
        return None
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass
        shutil.rmtree(profile_dir, ignore_errors=True)


async def fetch_fxthreads_canonical(share_id: str) -> str | None:
    """Fallback resolver for Threads /share/ links when Meta returns the login shell.

    FxThreads resolves the short link in a real browser and returns the canonical
    public post URL. This is used only after direct Threads resolution fails.
    """
    share_id = (share_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", share_id):
        return None

    session = await get_http_session()
    url = f"https://fx.akitsuki.me/api/share/{share_id}"
    try:
        async with session.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AbangRender-MusicBot/1.0",
            },
            allow_redirects=True,
            timeout=20,
        ) as response:
            if response.status != 200:
                logging.warning(
                    "FxThreads share resolver returned HTTP %s for share=%s",
                    response.status,
                    share_id,
                )
                return None
            payload = await response.json(content_type=None)
    except Exception as exc:
        logging.warning(
            "FxThreads share resolver failed: share=%s error=%s",
            share_id,
            exc,
        )
        return None

    if not isinstance(payload, dict):
        return None

    resolved = strip_threads_url(str(payload.get("url") or ""))
    if extract_threads_post_code(resolved):
        return resolved

    post_id = str(payload.get("id") or "").strip()
    author = payload.get("author")
    username = (
        str(author.get("username") or "").strip()
        if isinstance(author, dict)
        else ""
    )
    if re.fullmatch(r"[A-Za-z0-9_-]+", post_id) and re.fullmatch(
        r"[A-Za-z0-9._-]+", username
    ):
        return f"https://www.threads.com/@{username}/post/{post_id}"
    return None


async def resolve_threads_url(
    url: str,
    *,
    fetch_share_func: Callable[[str], Awaitable[tuple[str, str]]] | None = None,
) -> str:
    """Resolve Threads /share/... links to the canonical /@user/post/... URL."""
    candidate = (url or "").strip()
    canonical = strip_threads_url(candidate)
    if extract_threads_post_code(canonical):
        return canonical

    if not THREADS_SHARE_URL_RE.fullmatch(_threads_path_only(candidate)):
        return candidate

    headerless_resolved = await resolve_threads_share_via_headerless_fetch(candidate)
    if headerless_resolved:
        return headerless_resolved

    manual_resolved = await resolve_threads_share_via_manual_redirect(candidate)
    if manual_resolved:
        return manual_resolved

    head_resolved = await resolve_threads_share_via_head(candidate)
    if head_resolved:
        return head_resolved

    fetcher = fetch_share_func or fetch_threads_share_page
    try:
        final_url, page = await fetcher(candidate)
    except Exception as exc:
        logging.warning("Threads share resolve request failed: url=%s error=%s", candidate, exc)
        return candidate

    resolved_final = strip_threads_url(final_url)
    if extract_threads_post_code(resolved_final):
        logging.info("Resolved Threads share URL via redirect: %s -> %s", candidate, resolved_final)
        return resolved_final

    resolved_page = _extract_threads_post_url_from_html(page)
    if resolved_page:
        logging.info("Resolved Threads share URL via page metadata: %s -> %s", candidate, resolved_page)
        return resolved_page

    bulk_resolved = await resolve_threads_share_via_bulk_route(
        candidate,
        bootstrap_page=page,
    )
    if bulk_resolved:
        return bulk_resolved

    vxthreads_resolved = await resolve_threads_share_via_vxthreads(candidate)
    if vxthreads_resolved:
        return vxthreads_resolved

    railway_resolved = await resolve_threads_share_via_railway(candidate)
    if railway_resolved:
        return railway_resolved

    fzthreads_resolved = await resolve_threads_share_via_fzthreads(candidate)
    if fzthreads_resolved:
        return fzthreads_resolved

    fixembed_resolved = await resolve_threads_share_via_fixembed(candidate)
    if fixembed_resolved:
        return fixembed_resolved

    crawler_resolved = await resolve_threads_share_via_crawlers(candidate)
    if crawler_resolved:
        return crawler_resolved

    share_match = THREADS_SHARE_URL_RE.fullmatch(_threads_path_only(candidate))
    share_id = ""
    if share_match:
        share_id = _threads_path_only(candidate).rstrip("/").split("/")[-1]
    if share_id:
        resolved_proxy = await fetch_fxthreads_canonical(share_id)
        if resolved_proxy:
            logging.info(
                "Resolved Threads share URL via FxThreads fallback: %s -> %s",
                candidate,
                resolved_proxy,
            )
            return resolved_proxy

    chromium_resolved = await asyncio.to_thread(
        _resolve_threads_share_with_chromium_sync,
        candidate,
    )
    if chromium_resolved:
        logging.info(
            "Resolved Threads share URL via Chromium fallback: %s -> %s",
            candidate,
            chromium_resolved,
        )
        return chromium_resolved

    title_match = re.search(r"<title[^>]*>(.*?)</title>", page or "", re.IGNORECASE | re.DOTALL)
    canonical_match = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
        page or "",
        re.IGNORECASE,
    )
    og_url_match = re.search(
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)',
        page or "",
        re.IGNORECASE,
    )
    usernames = list(dict.fromkeys(re.findall(r'"username"\s*:\s*"([A-Za-z0-9._-]+)"', page or "")))[:8]
    codes = list(dict.fromkeys(re.findall(r'"code"\s*:\s*"([A-Za-z0-9_-]+)"', page or "")))[:8]
    logging.warning(
        "Threads share URL did not expose a canonical post: url=%s final=%s bytes=%s "
        "title=%s canonical=%s og_url=%s usernames=%s codes=%s data_sjs=%s",
        candidate,
        final_url,
        len(page or ""),
        re.sub(r"\s+", " ", unescape(title_match.group(1))).strip()[:180] if title_match else "-",
        canonical_match.group(1)[:240] if canonical_match else "-",
        og_url_match.group(1)[:240] if og_url_match else "-",
        usernames,
        codes,
        (page or "").count("data-sjs"),
    )
    return candidate


@dataclass(slots=True)
class ThreadsMedia:
    url: str
    type: str
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class ThreadsPost:
    id: str
    description: str
    author: str
    media_list: list[ThreadsMedia]


def get_threads_preview_url(media: ThreadsMedia | None) -> str | None:
    if not media or media.type != "photo":
        return None
    return media.url


class _JsonScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._attrs: dict[str, str | None] | None = None
        self._body: list[str] | None = None
        self.scripts: list[tuple[dict[str, str | None], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._attrs = dict(attrs)
            self._body = []

    def handle_data(self, data: str) -> None:
        if self._body is not None:
            self._body.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._body is not None:
            self.scripts.append((self._attrs or {}, "".join(self._body)))
            self._attrs = None
            self._body = None


def _iter_json_blobs(page: str, *, contains: str | None = None) -> Iterator[dict[str, Any]]:
    parser = _JsonScriptParser()
    parser.feed(page)
    for attrs, body in parser.scripts:
        if attrs.get("type") != "application/json" or "data-sjs" not in attrs:
            continue
        if not body.lstrip().startswith("{"):
            continue
        if contains is not None and contains not in body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _walk_json(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _find_post(page: str, post_code: str) -> dict[str, Any] | None:
    needle = f'"code":"{post_code}"'
    # Threads blobs are minified, so the needle usually matches the raw script
    # text; pre-filter on it to avoid parsing unrelated blobs, then fall back
    # to parsing every blob in case the raw-text match missed.
    for contains in (needle, None):
        for payload in _iter_json_blobs(page, contains=contains):
            for node in _walk_json(payload):
                if node.get("code") == post_code:
                    return node
    return None


def _has_media(node: dict[str, Any]) -> bool:
    return bool(node.get("carousel_media") or node.get("video_versions") or node.get("image_versions2"))


def _media_source(post: dict[str, Any]) -> dict[str, Any]:
    if _has_media(post):
        return post

    app_info = post.get("text_post_app_info")
    if isinstance(app_info, dict):
        linked_media = app_info.get("linked_inline_media")
        if isinstance(linked_media, dict) and _has_media(linked_media):
            return linked_media

        share_info = app_info.get("share_info")
        if isinstance(share_info, dict):
            quoted_post = share_info.get("quoted_attachment_post")
            if isinstance(quoted_post, dict) and _has_media(quoted_post):
                return quoted_post
    return post


def _best_variant(items: Any) -> dict[str, Any] | None:
    candidates = [item for item in items or [] if isinstance(item, dict) and isinstance(item.get("url"), str)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0),
    )


def _extract_media(post: dict[str, Any]) -> list[ThreadsMedia]:
    source = _media_source(post)
    items = source.get("carousel_media") or [source]
    media: list[ThreadsMedia] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        video = _best_variant(item.get("video_versions"))
        if video:
            media.append(
                ThreadsMedia(
                    url=video["url"],
                    type="video",
                    width=video.get("width"),
                    height=video.get("height"),
                )
            )
            continue

        image_versions = item.get("image_versions2")
        image = _best_variant(image_versions.get("candidates") if isinstance(image_versions, dict) else None)
        if image:
            media.append(
                ThreadsMedia(
                    url=image["url"],
                    type="photo",
                    width=image.get("width"),
                    height=image.get("height"),
                )
            )
    return media


def parse_threads_post_html(page: str, post_code: str) -> ThreadsPost | None:
    post = _find_post(page, post_code)
    if not post:
        return None

    caption = post.get("caption")
    description = caption.get("text", "") if isinstance(caption, dict) else ""
    description = description.strip() if isinstance(description, str) else ""
    media_list = _extract_media(post)
    if not media_list and not description:
        return None

    user = post.get("user")
    author = user.get("username", "threads") if isinstance(user, dict) else "threads"
    return ThreadsPost(
        id=post_code,
        description=description,
        author=author.strip() if isinstance(author, str) and author.strip() else "threads",
        media_list=media_list,
    )


async def fetch_threads_post_html(url: str) -> str:
    session = await get_http_session()
    async with session.get(url, headers=THREADS_PAGE_HEADERS, allow_redirects=True) as response:
        response.raise_for_status()
        return await response.text()


class ThreadsMediaService(CobaltMediaService):
    def __init__(
        self,
        output_dir: str,
        *,
        fetch_page_func: Callable[[str], Awaitable[str]] = fetch_threads_post_html,
        retry_async_operation_func: Callable[..., Awaitable[DownloadMetrics | None]],
    ) -> None:
        super().__init__(
            output_dir,
            source="threads",
            retry_async_operation_func=retry_async_operation_func,
            logger=logging,
            download_error_message="Threads media download failed: url=%s error=%s",
            download_headers=THREADS_MEDIA_HEADERS,
        )
        self._fetch_page = fetch_page_func

    async def fetch_post(self, url: str) -> ThreadsPost | None:
        source_url = strip_threads_url(url)
        post_code = extract_threads_post_code(source_url)
        if not post_code:
            logging.warning("Unsupported Threads URL: url=%s", url)
            return None
        try:
            page = await self._fetch_page(source_url)
        except Exception as exc:
            logging.warning("Threads page request failed: post=%s error=%s", post_code, exc)
            return None

        post = parse_threads_post_html(page, post_code)
        if not post:
            logging.warning("Threads post has no extractable content: post=%s", post_code)
        return post
