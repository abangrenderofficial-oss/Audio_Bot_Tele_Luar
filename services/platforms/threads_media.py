from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import aiohttp
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import quote, urljoin, urlparse

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
# Threads currently exposes /share/<id>/ redirects and SSR post data to a
# crawler UA. Ordinary browser/server requests can receive only the SPA shell.
# Keep the Googlebot request shape as the primary public resolver.
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
        headers={
            "User-Agent": THREADS_PAGE_HEADERS["User-Agent"],
            "Accept-Language": "en-US,en;q=0.9",
        },
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


async def resolve_threads_share_via_oembed(url: str) -> str | None:
    """Ask Meta's official tokenless Threads oEmbed endpoint for the permalink.

    Public Threads oEmbed is an official server-side surface. Although the
    documented input is a permanent post URL, Meta may normalize share wrappers
    internally; when it does, the returned embed HTML contains the canonical
    data-text-post-permalink URL.
    """
    session = await get_http_session()
    endpoints = (
        "https://graph.threads.com/oembed",
        "https://graph.threads.net/v1.0/oembed",
    )
    for endpoint in endpoints:
        try:
            async with session.get(
                endpoint,
                params={"url": url, "omitscript": "true"},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AbangRender-MusicBot/1.0",
                },
                allow_redirects=True,
                timeout=12,
            ) as response:
                raw = await response.text(errors="replace")
                status = response.status
        except Exception as exc:
            logging.info(
                "Threads oEmbed resolver request failed: endpoint=%s error=%s",
                endpoint,
                exc,
            )
            continue

        if status != 200:
            logging.info(
                "Threads oEmbed resolver miss: endpoint=%s status=%s body=%s",
                endpoint,
                status,
                re.sub(r"\s+", " ", raw)[:220],
            )
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            candidates = [
                str(payload.get("url") or ""),
                str(payload.get("permalink") or ""),
                str(payload.get("html") or ""),
            ]
            for candidate in candidates:
                resolved = _extract_threads_post_url_from_html(candidate)
                if resolved:
                    logging.info(
                        "Resolved Threads share URL via official oEmbed: %s -> %s",
                        url,
                        resolved,
                    )
                    return resolved

        resolved = _extract_threads_post_url_from_html(raw)
        if resolved:
            logging.info(
                "Resolved Threads share URL via official oEmbed body: %s -> %s",
                url,
                resolved,
            )
            return resolved

        logging.info(
            "Threads oEmbed resolver returned no permalink: endpoint=%s bytes=%s",
            endpoint,
            len(raw),
        )
    return None


async def resolve_threads_share_via_extension_fetch(url: str) -> str | None:
    """Mirror a real Chrome extension fetch as closely as possible.

    The open-source Threads Clean Link extension resolves /share/<id>/ with a
    normal browser GET, credentials omitted, redirects followed, and an
    extension Origin. Render's plain HTTP clients receive the SPA/login shell,
    so try the browser-extension request shape before heavier fallbacks.
    """
    timeout = aiohttp.ClientTimeout(total=12)
    variants = (
        {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
            "Origin": "chrome-extension://hehokicokbgajpanjcajhmflaennnmdj",
        },
        {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
            "Origin": "chrome-extension://hehokicokbgajpanjcajhmflaennnmdj",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "none",
        },
    )
    for index, headers in enumerate(variants, 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                ) as response:
                    final_url = str(response.url)
                    page = await response.text(errors="replace")
                    status = response.status
        except Exception as exc:
            logging.info(
                "Threads extension-style resolver request failed: variant=%s error=%s",
                index,
                exc,
            )
            continue

        resolved = strip_threads_url(final_url)
        if extract_threads_post_code(resolved):
            logging.info(
                "Resolved Threads share URL via extension-style fetch: variant=%s %s -> %s",
                index,
                url,
                resolved,
            )
            return resolved

        resolved = _extract_threads_post_url_from_html(page)
        if resolved:
            logging.info(
                "Resolved Threads share URL via extension-style page metadata: variant=%s %s -> %s",
                index,
                url,
                resolved,
            )
            return resolved

        logging.info(
            "Threads extension-style resolver miss: variant=%s status=%s final=%s bytes=%s",
            index,
            status,
            final_url[:220],
            len(page),
        )
    return None


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
    """Resolve Threads share links with Meta's plain-client redirect path first.

    A current Threads extractor verified in 2026 uses an exact curl/8.0 request
    with redirects disabled; Meta returns the canonical post in Location for
    public /share/ links. Keep the aiohttp walk as a fallback.
    """

    def _urllib_probe(target: str) -> tuple[int | None, str]:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: object, **kwargs: object) -> None:
                return None

        request = urllib.request.Request(target, headers={"User-Agent": "curl/8.0"})
        try:
            response = urllib.request.build_opener(_NoRedirect).open(request, timeout=12)
            return getattr(response, "status", None), (response.headers.get("Location") or "").strip()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return exc.code, (exc.headers.get("Location") or "").strip()
            return exc.code, ""
        except Exception:
            return None, ""

    status, location = await asyncio.to_thread(_urllib_probe, (url or "").strip())
    if location:
        next_url = urljoin(url, location)
        canonical = strip_threads_url(next_url)
        parsed = urlparse(next_url)
        if extract_threads_post_code(canonical) or re.match(
            r"^/t/[A-Za-z0-9_-]+/?$",
            parsed.path,
            re.IGNORECASE,
        ):
            logging.info(
                "Resolved Threads share URL via plain curl redirect: status=%s %s -> %s",
                status,
                url,
                canonical if extract_threads_post_code(canonical) else next_url,
            )
            return canonical if extract_threads_post_code(canonical) else next_url

    logging.info(
        "Threads plain curl redirect probe miss: status=%s location=%s",
        status,
        (location or "-")[:220],
    )

    session = await get_http_session()
    current = (url or "").strip()
    for hop in range(5):
        try:
            async with session.get(
                current,
                headers={
                    "Accept": "*/*",
                    "User-Agent": "curl/8.0",
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


def _resolve_threads_share_via_real_extension_page_sync(url: str) -> str | None:
    """Resolve a Threads share alias by executing fetch() inside a real
    chrome-extension:// page.

    The Threads Clean Link extension succeeds because the request originates
    from an extension page with host permission. Run that exact fetch through
    Chromium DevTools and read response.url directly instead of relying on
    normal server-side HTTP semantics.
    """
    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as exc:
        logging.warning(
            "Threads real-extension resolver unavailable: websocket client error=%s",
            exc,
        )
        return None

    binary = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if not binary:
        logging.warning("Threads real-extension resolver unavailable: browser binary not found")
        return None

    # Headless Chromium does not fully mirror extension networking on Debian;
    # run the extension in a normal Chromium window under Xvfb when available.
    # This matches the working desktop-extension execution model while staying
    # entirely inside the Render Music Bot container.
    xvfb_run = shutil.which("xvfb-run")

    extension_id = "hehokicokbgajpanjcajhmflaennnmdj"
    extension_key = (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwsulpvef7Tggdw39ft9kn/"
        "AmboE4U5U+16uEir9kdo2CGvLqe2WbKLWnShqQj0XbDSMqASr8RgsSl6fkhSRfEW"
        "t3qEuQ2QA9wQaeftPwoRGBUanuFTwIoeA6sNAoHJ8rhf+WTiwkA6IIBoYBNmNQrVg"
        "PHicnkPkATbX2+yYTOD2Zwd78yAW4Wpd9kefIVr9TBVEtvq6xqvifm+tC6Y+/kKPY"
        "CFUltUDoq+2ct9Yg1toVM/bWrhSiM+CX5jWEUSmRdFFid8dcjQDZ+HaIp5ALDHHeN"
        "uo/xhM/X2bHZbsBLcUNgXQdskVB/D9qn9eIHrQ5l2OEdKOGktmh0KBXA1iCnwIDAQAB"
    )

    for mode, proxy in (
        ("direct", None),
        ("warp", "socks5://127.0.0.1:1080"),
    ):
        root_dir = tempfile.mkdtemp(prefix=f"threads-real-ext-{mode}-")
        extension_dir = os.path.join(root_dir, "extension")
        profile_dir = os.path.join(root_dir, "profile")
        os.makedirs(extension_dir, exist_ok=True)
        os.makedirs(profile_dir, exist_ok=True)
        process: subprocess.Popen | None = None
        try:
            manifest = {
                "manifest_version": 3,
                "name": "Threads Share Resolver",
                "version": "1.0",
                "key": extension_key,
                "background": {"service_worker": "background.js"},
                "host_permissions": [
                    "https://*.threads.com/*",
                    "https://*.threads.net/*",
                ],
            }
            with open(
                os.path.join(extension_dir, "manifest.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(manifest, handle)

            with open(
                os.path.join(extension_dir, "resolver.html"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("<!doctype html><meta charset=\"utf-8\"><title>resolver</title>")

            background_script = """
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "resolveThreadsShare") return false;
  (async () => {
    try {
      const response = await fetch(String(message.url || ""), {
        method: "GET",
        credentials: "omit",
        redirect: "follow",
        headers: { "Accept-Language": "en" }
      });
      sendResponse({
        ok: true,
        url: response.url,
        status: response.status,
        type: response.type
      });
    } catch (error) {
      sendResponse({
        ok: false,
        error: String(error && error.message || error || "fetch_failed")
      });
    }
  })();
  return true;
});
"""
            with open(
                os.path.join(extension_dir, "background.js"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(background_script)

            resolver_url = f"chrome-extension://{extension_id}/resolver.html"
            command = (
                [xvfb_run, "-a", binary]
                if xvfb_run
                else [binary, "--headless=new"]
            ) + [
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--no-first-run",
                "--lang=en-US",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile_dir}",
                f"--disable-extensions-except={extension_dir}",
                f"--load-extension={extension_dir}",
            ]
            if proxy:
                command.append(f"--proxy-server={proxy}")
            else:
                command.append("--no-proxy-server")
            command.append(resolver_url)

            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            port_file = os.path.join(profile_dir, "DevToolsActivePort")
            port: int | None = None
            startup_deadline = time.monotonic() + 7.0
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
                    "Threads real-extension resolver could not open DevTools: mode=%s rc=%s",
                    mode,
                    process.poll(),
                )
                continue

            endpoint = f"http://127.0.0.1:{port}/json"

            # Startup navigation can race unpacked-extension registration.
            # Re-open the page only after DevTools is ready.
            try:
                create_url = (
                    f"http://127.0.0.1:{port}/json/new?"
                    f"{quote(resolver_url, safe='')}"
                )
                request = urllib.request.Request(create_url, method="PUT")
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    created = json.loads(
                        response.read().decode("utf-8", errors="replace")
                    )
                logging.info(
                    "Threads real-extension DevTools target created: mode=%s url=%s",
                    mode,
                    str(created.get("url") or "-")[:220]
                    if isinstance(created, dict)
                    else "-",
                )
            except Exception as exc:
                logging.info(
                    "Threads real-extension DevTools target create miss: "
                    "mode=%s error=%s",
                    mode,
                    exc,
                )
            time.sleep(0.5)

            target: dict[str, object] | None = None
            target_deadline = time.monotonic() + 6.0
            while time.monotonic() < target_deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(endpoint, timeout=1.0) as response:
                        targets = json.loads(
                            response.read().decode("utf-8", errors="replace")
                        )
                    if isinstance(targets, list):
                        for item in targets:
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "page"
                                and str(item.get("url") or "").startswith(resolver_url)
                                and item.get("webSocketDebuggerUrl")
                            ):
                                target = item
                                break
                    if target is not None:
                        break
                except Exception:
                    pass
                time.sleep(0.2)

            if target is None:
                logging.warning(
                    "Threads real-extension resolver found no extension page target: mode=%s",
                    mode,
                )
                continue

            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            expression = f"""
(async () => {{
  const target = {json.dumps(url)};
  const diag = {{
    href: location.href,
    chromeType: typeof chrome,
    runtimeType: (
      typeof chrome !== "undefined"
        ? typeof chrome.runtime
        : "undefined"
    )
  }};

  try {{
    const response = await fetch(target, {{
      method: "GET",
      credentials: "omit",
      redirect: "follow",
      headers: {{ "Accept-Language": "en" }}
    }});
    return JSON.stringify({{
      ok: true,
      url: response.url,
      status: response.status,
      type: response.type,
      via: "extension_page_fetch",
      diag
    }});
  }} catch (directError) {{
    if (
      typeof chrome !== "undefined" &&
      chrome.runtime &&
      typeof chrome.runtime.sendMessage === "function"
    ) {{
      try {{
        const result = await chrome.runtime.sendMessage({{
          type: "resolveThreadsShare",
          url: target
        }});
        return JSON.stringify({{
          ...(result || {{ok: false, error: "empty_extension_response"}}),
          via: "extension_service_worker",
          diag
        }});
      }} catch (messageError) {{
        return JSON.stringify({{
          ok: false,
          error:
            "page_fetch=" +
            String(directError && directError.message || directError) +
            "; runtime_message=" +
            String(messageError && messageError.message || messageError),
          via: "extension_page_then_worker",
          diag
        }});
      }}
    }}

    return JSON.stringify({{
      ok: false,
      error: String(directError && directError.message || directError || "fetch_failed"),
      via: "extension_page_fetch",
      diag
    }});
  }}
}})()
"""

            with ws_connect(
                ws_url,
                open_timeout=4,
                close_timeout=1,
            ) as websocket:
                enable_id = 31
                websocket.send(
                    json.dumps(
                        {
                            "id": enable_id,
                            "method": "Runtime.enable",
                        }
                    )
                )
                context_id = None
                context_deadline = time.monotonic() + 6.0
                while time.monotonic() < context_deadline and context_id is None:
                    try:
                        raw = websocket.recv(timeout=2)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw)
                    if payload.get("method") != "Runtime.executionContextCreated":
                        continue
                    context = payload.get("params", {}).get("context", {})
                    aux = context.get("auxData") or {}
                    if aux.get("isDefault") is True and context.get("id") is not None:
                        context_id = int(context["id"])

                if context_id is None:
                    logging.warning(
                        "Threads real-extension resolver found no execution context: mode=%s",
                        mode,
                    )
                    continue

                request_id = 32
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expression,
                                "contextId": context_id,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }
                    )
                )
                response_payload = None
                response_deadline = time.monotonic() + 18.0
                while time.monotonic() < response_deadline:
                    try:
                        raw = websocket.recv(timeout=2)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw)
                    if payload.get("id") == request_id:
                        response_payload = payload
                        break

            if not isinstance(response_payload, dict):
                logging.warning(
                    "Threads real-extension fetch timed out: mode=%s",
                    mode,
                )
                continue
            if response_payload.get("error"):
                logging.warning(
                    "Threads real-extension CDP command failed: mode=%s error=%s",
                    mode,
                    str(response_payload.get("error"))[:300],
                )
                continue

            remote = (
                response_payload.get("result", {})
                .get("result", {})
                .get("value")
            )
            try:
                fetch_result = json.loads(remote) if isinstance(remote, str) else {}
            except json.JSONDecodeError:
                fetch_result = {}

            final_url = str(fetch_result.get("url") or "").strip()
            resolved = strip_threads_url(final_url)
            if extract_threads_post_code(resolved):
                logging.info(
                    "Resolved Threads share URL via real Chromium extension: "
                    "mode=%s status=%s %s -> %s",
                    mode,
                    fetch_result.get("status"),
                    url,
                    resolved,
                )
                return resolved

            logging.warning(
                "Threads real-extension fetch did not resolve canonical post: "
                "mode=%s status=%s type=%s final=%s error=%s",
                mode,
                fetch_result.get("status"),
                fetch_result.get("type"),
                final_url[:300] or "-",
                str(fetch_result.get("error") or "-")[:200],
            )
        except Exception as exc:
            logging.warning(
                "Threads real-extension resolver failed: mode=%s error=%s",
                mode,
                exc,
            )
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
            shutil.rmtree(root_dir, ignore_errors=True)

    return None


def _resolve_threads_share_via_extension_worker_sync(url: str) -> str | None:
    """Resolve a Threads /share/ alias inside a real MV3 extension service worker.

    Threads Clean Link resolves these aliases from an extension service-worker
    fetch with host permissions. Reproduce that exact origin instead of a normal
    page/server request, which Meta currently serves as the generic SPA shell.
    """
    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as exc:
        logging.warning("Threads extension-worker resolver unavailable: %s", exc)
        return None

    binary = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if not binary:
        logging.warning("Threads extension-worker resolver unavailable: browser binary not found")
        return None

    extension_id = "hehokicokbgajpanjcajhmflaennnmdj"
    extension_key = (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwsulpvef7Tggdw39ft9kn/"
        "AmboE4U5U+16uEir9kdo2CGvLqe2WbKLWnShqQj0XbDSMqASr8RgsSl6fkhSRfEW"
        "t3qEuQ2QA9wQaeftPwoRGBUanuFTwIoeA6sNAoHJ8rhf+WTiwkA6IIBoYBNmNQrVg"
        "PHicnkPkATbX2+yYTOD2Zwd78yAW4Wpd9kefIVr9TBVEtvq6xqvifm+tC6Y+/kKPY"
        "CFUltUDoq+2ct9Yg1toVM/bWrhSiM+CX5jWEUSmRdFFid8dcjQDZ+HaIp5ALDHHeN"
        "uo/xhM/X2bHZbsBLcUNgXQdskVB/D9qn9eIHrQ5l2OEdKOGktmh0KBXA1iCnwIDAQAB"
    )

    root_dir = tempfile.mkdtemp(prefix="threads-ext-worker-")
    extension_dir = os.path.join(root_dir, "extension")
    profile_dir = os.path.join(root_dir, "profile")
    os.makedirs(extension_dir, exist_ok=True)
    os.makedirs(profile_dir, exist_ok=True)
    process: subprocess.Popen | None = None

    try:
        manifest = {
            "manifest_version": 3,
            "name": "Threads Share Resolver",
            "version": "1.0",
            "key": extension_key,
            "background": {"service_worker": "background.js"},
            "host_permissions": [
                "https://*.threads.com/*",
                "https://*.threads.net/*",
            ],
        }
        with open(os.path.join(extension_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

        background = """
chrome.runtime.onInstalled.addListener(() => {});
chrome.runtime.onStartup.addListener(() => {});
globalThis.__threadsResolverReady = true;
"""
        with open(os.path.join(extension_dir, "background.js"), "w", encoding="utf-8") as handle:
            handle.write(background)

        xvfb_run = shutil.which("xvfb-run")
        command = (
            [xvfb_run, "-a", binary]
            if xvfb_run
            else [binary, "--headless=new"]
        ) + [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
            "--no-proxy-server",
            "about:blank",
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        port_file = os.path.join(profile_dir, "DevToolsActivePort")
        port: int | None = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
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
                "Threads extension-worker resolver could not open DevTools rc=%s",
                process.poll(),
            )
            return None

        endpoint = f"http://127.0.0.1:{port}/json"
        worker = None
        worker_deadline = time.monotonic() + 8.0
        while time.monotonic() < worker_deadline:
            try:
                with urllib.request.urlopen(endpoint, timeout=1.0) as response:
                    targets = json.loads(response.read().decode("utf-8", errors="replace"))
                if isinstance(targets, list):
                    for item in targets:
                        if (
                            isinstance(item, dict)
                            and item.get("type") == "service_worker"
                            and str(item.get("url") or "").startswith(
                                f"chrome-extension://{extension_id}/"
                            )
                            and item.get("webSocketDebuggerUrl")
                        ):
                            worker = item
                            break
                if worker:
                    break
            except Exception:
                pass
            time.sleep(0.2)

        if not worker:
            logging.warning("Threads extension-worker target not found")
            return None

        ws_url = str(worker.get("webSocketDebuggerUrl") or "")
        expression = f"""
(async () => {{
  try {{
    const response = await fetch({json.dumps(url)}, {{
      method: "GET",
      credentials: "omit",
      redirect: "follow",
      headers: {{ "Accept-Language": "en" }}
    }});
    return JSON.stringify({{
      ok: true,
      url: response.url,
      status: response.status,
      type: response.type
    }});
  }} catch (error) {{
    return JSON.stringify({{
      ok: false,
      error: String(error && error.message || error || "fetch_failed")
    }});
  }}
}})()
"""

        with ws_connect(ws_url, open_timeout=4, close_timeout=1) as websocket:
            request_id = 71
            websocket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expression,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    }
                )
            )
            payload = None
            response_deadline = time.monotonic() + 15.0
            while time.monotonic() < response_deadline:
                try:
                    raw = websocket.recv(timeout=2)
                except TimeoutError:
                    continue
                candidate = json.loads(raw)
                if candidate.get("id") == request_id:
                    payload = candidate
                    break

        if not isinstance(payload, dict) or payload.get("error"):
            logging.warning(
                "Threads extension-worker CDP evaluation failed: %s",
                str((payload or {}).get("error") or "timeout")[:300],
            )
            return None

        remote = payload.get("result", {}).get("result", {}).get("value")
        try:
            result = json.loads(remote) if isinstance(remote, str) else {}
        except json.JSONDecodeError:
            result = {}

        final_url = str(result.get("url") or "").strip()
        resolved = strip_threads_url(final_url)
        if extract_threads_post_code(resolved):
            logging.info(
                "Resolved Threads share URL via extension worker: %s -> %s",
                url,
                resolved,
            )
            return resolved

        logging.warning(
            "Threads extension-worker fetch did not resolve: status=%s type=%s final=%s error=%s",
            result.get("status"),
            result.get("type"),
            final_url[:300] or "-",
            str(result.get("error") or "-")[:300],
        )
        return None
    except Exception as exc:
        logging.warning("Threads extension-worker resolver failed: %s", exc)
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
        shutil.rmtree(root_dir, ignore_errors=True)


def _resolve_threads_share_via_cdp_fetch_sync(url: str) -> str | None:
    """Resolve /share/<id>/ by running fetch() inside real Chromium.

    Threads' share redirect currently depends on browser request semantics.
    Server-side aiohttp requests and normal document navigation can receive the
    generic SPA shell, so execute the same credentials=omit, redirect=follow
    fetch inside Chromium and read response.url through the DevTools protocol.
    """
    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as exc:
        logging.warning("Threads CDP resolver unavailable: websocket client error=%s", exc)
        return None

    binary = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if not binary:
        logging.warning("Threads CDP resolver unavailable: browser binary not found")
        return None

    variants = (
        ("share_origin", url, False),
        ("blank_relaxed", "about:blank", True),
    )
    for mode, bootstrap_url, relax_cors in variants:
        profile_dir = tempfile.mkdtemp(prefix=f"threads-cdp-{mode}-")
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
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--lang=en-US",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile_dir}",
            ]
            if relax_cors:
                command.extend(
                    [
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )
            command.append(bootstrap_url)

            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            port_file = os.path.join(profile_dir, "DevToolsActivePort")
            port: int | None = None
            startup_deadline = time.monotonic() + 7.0
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
                    "Threads CDP resolver could not open DevTools: mode=%s rc=%s",
                    mode,
                    process.poll(),
                )
                continue

            target = None
            target_deadline = time.monotonic() + 6.0
            endpoint = f"http://127.0.0.1:{port}/json"
            while time.monotonic() < target_deadline:
                try:
                    with urllib.request.urlopen(endpoint, timeout=1.0) as response:
                        targets = json.loads(
                            response.read().decode("utf-8", errors="replace")
                        )
                    if isinstance(targets, list):
                        pages = [
                            item
                            for item in targets
                            if isinstance(item, dict)
                            and item.get("type") == "page"
                            and item.get("webSocketDebuggerUrl")
                        ]
                        if pages:
                            target = pages[0]
                            current_url = str(target.get("url") or "")
                            if (
                                mode != "share_origin"
                                or "threads.com" in current_url
                                or "threads.net" in current_url
                            ):
                                break
                except Exception:
                    pass
                time.sleep(0.2)

            if not target:
                logging.warning("Threads CDP resolver found no page target: mode=%s", mode)
                continue

            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                continue

            expression = f"""
(async () => {{
  try {{
    const response = await fetch({json.dumps(url)}, {{
      method: "GET",
      credentials: "omit",
      redirect: "follow",
      headers: {{ "Accept-Language": "en" }}
    }});
    return JSON.stringify({{
      ok: true,
      url: response.url,
      status: response.status,
      type: response.type
    }});
  }} catch (error) {{
    return JSON.stringify({{
      ok: false,
      error: String(error && error.message || error || "fetch_failed")
    }});
  }}
}})()
"""

            with ws_connect(
                ws_url,
                open_timeout=4,
                close_timeout=1,
            ) as websocket:
                # Runtime.enable replays the page's existing execution contexts.
                # Wait for the default document context instead of evaluating
                # while Chromium is still between provisional navigations.
                enable_id = 16
                websocket.send(
                    json.dumps(
                        {
                            "id": enable_id,
                            "method": "Runtime.enable",
                        }
                    )
                )
                context_id = None
                context_deadline = time.monotonic() + 8.0
                while time.monotonic() < context_deadline and context_id is None:
                    try:
                        raw = websocket.recv(timeout=2)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw)
                    if payload.get("method") != "Runtime.executionContextCreated":
                        continue
                    context = payload.get("params", {}).get("context", {})
                    aux = context.get("auxData") or {}
                    if aux.get("isDefault") is True and context.get("id") is not None:
                        context_id = int(context["id"])

                if context_id is None:
                    logging.warning(
                        "Threads CDP resolver found no default execution context: mode=%s",
                        mode,
                    )
                    continue

                request_id = 17
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expression,
                                "contextId": context_id,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }
                    )
                )
                response_payload = None
                response_deadline = time.monotonic() + 15.0
                while time.monotonic() < response_deadline:
                    try:
                        raw = websocket.recv(timeout=2)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw)
                    if payload.get("id") == request_id:
                        response_payload = payload
                        break

            if not isinstance(response_payload, dict):
                logging.warning("Threads CDP fetch timed out: mode=%s", mode)
                continue
            if response_payload.get("error"):
                logging.warning(
                    "Threads CDP command failed: mode=%s error=%s",
                    mode,
                    str(response_payload.get("error"))[:300],
                )
                continue

            remote = (
                response_payload.get("result", {})
                .get("result", {})
                .get("value")
            )
            try:
                fetch_result = json.loads(remote) if isinstance(remote, str) else {}
            except json.JSONDecodeError:
                fetch_result = {}

            final_url = str(fetch_result.get("url") or "").strip()
            resolved = strip_threads_url(final_url)
            if extract_threads_post_code(resolved):
                logging.info(
                    "Resolved Threads share URL via Chromium CDP fetch: "
                    "mode=%s status=%s %s -> %s",
                    mode,
                    fetch_result.get("status"),
                    url,
                    resolved,
                )
                return resolved

            logging.warning(
                "Threads CDP fetch did not resolve canonical post: "
                "mode=%s status=%s type=%s final=%s error=%s",
                mode,
                fetch_result.get("status"),
                fetch_result.get("type"),
                final_url[:300] or "-",
                str(fetch_result.get("error") or "-")[:200],
            )
        except Exception as exc:
            logging.warning(
                "Threads CDP resolver failed: mode=%s error=%s",
                mode,
                exc,
            )
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

    return None


def _resolve_threads_share_with_extension_fetch_sync(url: str) -> str | None:
    """Resolve a Threads /share/ URL using Chromium's extension fetch path.

    Threads currently treats a browser-extension fetch differently from a
    server-side HTTP client or normal document navigation. Reproduce the same
    anonymous fetch shape used by Threads Clean Link: credentials omitted,
    redirects followed, and only Accept-Language set explicitly. The temporary
    extension calls back to a loopback-only HTTP server with response.url.
    """
    binary = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if not binary:
        return None

    root_dir = tempfile.mkdtemp(prefix="threads-ext-resolver-")
    extension_dir = os.path.join(root_dir, "extension")
    profile_dir = os.path.join(root_dir, "profile")
    os.makedirs(extension_dir, exist_ok=True)
    os.makedirs(profile_dir, exist_ok=True)

    result: dict[str, str] = {"url": "", "error": ""}

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/done":
                self.send_response(404)
                self.end_headers()
                return
            from urllib.parse import parse_qs

            query = parse_qs(parsed.query, keep_blank_values=True)
            result["url"] = str((query.get("url") or [""])[0]).strip()
            result["error"] = str((query.get("error") or [""])[0]).strip()
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = 0.35
    callback_port = int(server.server_address[1])

    manifest = {
        "manifest_version": 3,
        "name": "Threads Share Resolver",
        "version": "1.0",
        "background": {"service_worker": "background.js"},
        "host_permissions": [
            "https://*.threads.com/*",
            "https://*.threads.net/*",
            "http://127.0.0.1/*",
        ],
    }
    with open(
        os.path.join(extension_dir, "manifest.json"),
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(manifest, handle)

    callback_base = f"http://127.0.0.1:{callback_port}/done"
    background = f"""
(async () => {{
  const source = {json.dumps(url)};
  const callback = {json.dumps(callback_base)};
  try {{
    const response = await fetch(source, {{
      method: "GET",
      credentials: "omit",
      redirect: "follow",
      headers: {{ "Accept-Language": "en" }}
    }});
    await fetch(callback + "?url=" + encodeURIComponent(response.url));
  }} catch (error) {{
    await fetch(
      callback + "?error=" +
      encodeURIComponent(String(error && error.message || error || "fetch_failed"))
    );
  }}
}})();
"""
    with open(
        os.path.join(extension_dir, "background.js"),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(background)

    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                f"--user-data-dir={profile_dir}",
                f"--disable-extensions-except={extension_dir}",
                f"--load-extension={extension_dir}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and not result["url"] and not result["error"]:
            if process.poll() is not None:
                break
            server.handle_request()

        final_url = result["url"]
        if final_url:
            resolved = strip_threads_url(final_url)
            if extract_threads_post_code(resolved):
                logging.info(
                    "Resolved Threads share URL via Chromium extension fetch: %s -> %s",
                    url,
                    resolved,
                )
                return resolved
            logging.warning(
                "Threads extension fetch did not resolve canonical post: final=%s",
                final_url[:300],
            )
        elif result["error"]:
            logging.warning(
                "Threads extension fetch failed: error=%s",
                result["error"][:300],
            )
        else:
            logging.warning("Threads extension fetch timed out without callback")
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("Threads extension resolver failed to run: error=%s", exc)
        return None
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass
        shutil.rmtree(root_dir, ignore_errors=True)


def _resolve_threads_share_with_chromium_sync(url: str) -> str | None:
    """Resolve /share/<id>/ with a real Chromium navigation.

    Meta can serve different results depending on the HTTP/TLS/browser path.
    Try Render's direct egress first (closest to a normal browser request), then
    fall back to the local WARP SOCKS tunnel. This stays entirely inside Render.
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

    attempts: list[tuple[str, str | None, str]] = [
        (
            "direct-mobile",
            None,
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
            "Mobile/15E148 Safari/604.1",
        ),
        (
            "direct",
            None,
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        ),
        (
            "warp-mobile",
            "socks5://127.0.0.1:1080",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
            "Mobile/15E148 Safari/604.1",
        ),
    ]

    for mode, proxy_url, user_agent in attempts:
        profile_dir = tempfile.mkdtemp(prefix=f"threads-share-chrome-{mode}-")
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
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--lang=en-US",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile_dir}",
                f"--user-agent={user_agent}",
            ]
            if proxy_url:
                command.append(f"--proxy-server={proxy_url}")
            else:
                command.append("--no-proxy-server")
            command.append(url)

            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            port_file = os.path.join(profile_dir, "DevToolsActivePort")
            port: int | None = None
            startup_deadline = time.monotonic() + 7.0
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
                    "Threads Chromium resolver could not open DevTools: mode=%s rc=%s",
                    mode,
                    process.poll(),
                )
                continue

            last_url = url
            last_title = "-"
            navigation_deadline = time.monotonic() + 22.0
            endpoint = f"http://127.0.0.1:{port}/json"
            while time.monotonic() < navigation_deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(endpoint, timeout=1.0) as response:
                        targets = json.loads(
                            response.read().decode("utf-8", errors="replace")
                        )
                    if isinstance(targets, list):
                        for target in targets:
                            if (
                                not isinstance(target, dict)
                                or target.get("type") != "page"
                            ):
                                continue
                            current_url = str(target.get("url") or "").strip()
                            current_title = str(target.get("title") or "").strip()
                            if current_url:
                                last_url = current_url
                            if current_title:
                                last_title = current_title
                            resolved = strip_threads_url(current_url)
                            if extract_threads_post_code(resolved):
                                logging.info(
                                    "Resolved Threads share URL via Chromium: "
                                    "mode=%s %s -> %s",
                                    mode,
                                    url,
                                    resolved,
                                )
                                return resolved
                except Exception:
                    pass
                time.sleep(0.25)

            logging.warning(
                "Threads Chromium resolver did not navigate to canonical post: "
                "mode=%s final=%s title=%s",
                mode,
                last_url[:300],
                last_title[:160],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logging.warning(
                "Threads Chromium resolver failed to run: mode=%s error=%s",
                mode,
                exc,
            )
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

    return None


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


def _resolve_threads_share_via_warp_googlebot_sync(url: str) -> str | None:
    """Try the same public Googlebot request through the local WARP SOCKS path."""
    curl = shutil.which("curl")
    if not curl:
        return None

    marker = "__THREADS_FINAL_URL__="
    command = [
        curl,
        "-sS",
        "-L",
        "--max-time",
        "20",
        "--proxy",
        "socks5h://127.0.0.1:1080",
        "-A",
        THREADS_PAGE_HEADERS["User-Agent"],
        "-H",
        "Accept-Language: en",
        "-H",
        f"Accept: {THREADS_PAGE_HEADERS['Accept']}",
        "-w",
        f"\\n{marker}%{{url_effective}}",
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=24,
            check=False,
        )
    except Exception as exc:
        logging.info("Threads WARP Googlebot resolver failed to run: %s", exc)
        return None

    output = completed.stdout or ""
    if marker not in output:
        logging.info(
            "Threads WARP Googlebot resolver miss: rc=%s stderr=%s",
            completed.returncode,
            re.sub(r"\\s+", " ", completed.stderr or "")[:220],
        )
        return None

    page, final_url = output.rsplit(marker, 1)
    final_url = final_url.strip()
    resolved = strip_threads_url(final_url)
    if extract_threads_post_code(resolved):
        logging.info(
            "Resolved Threads share URL via WARP Googlebot redirect: %s -> %s",
            url,
            resolved,
        )
        return resolved

    resolved = _extract_threads_post_url_from_html(page)
    if resolved:
        logging.info(
            "Resolved Threads share URL via WARP Googlebot SSR: %s -> %s",
            url,
            resolved,
        )
        return resolved

    logging.info(
        "Threads WARP Googlebot resolver miss: rc=%s final=%s bytes=%s",
        completed.returncode,
        final_url[:220] or "-",
        len(page),
    )
    return None


async def resolve_threads_share_fast(url: str) -> str:
    """Resolve a Threads /share/<id>/ with the current public SSR path.

    Threads' crawler response is intermittent, so retry a small number of
    cheap Googlebot requests. A public post normally resolves on the first
    request; retries are only paid for stubborn aliases.
    """
    candidate = (url or "").strip()
    if not THREADS_SHARE_URL_RE.fullmatch(_threads_path_only(candidate)):
        return candidate

    for attempt in range(1, 6):
        try:
            final_url, page = await fetch_threads_share_page(candidate)
            resolved = strip_threads_url(final_url)
            if extract_threads_post_code(resolved):
                logging.info(
                    "Resolved Threads share URL via Googlebot redirect: %s -> %s attempt=%s",
                    candidate,
                    resolved,
                    attempt,
                )
                return resolved

            resolved = _extract_threads_post_url_from_html(page)
            if resolved:
                logging.info(
                    "Resolved Threads share URL via Googlebot SSR: %s -> %s attempt=%s",
                    candidate,
                    resolved,
                    attempt,
                )
                return resolved
        except Exception as exc:
            logging.info(
                "Threads Googlebot resolver failed: url=%s attempt=%s error=%s",
                candidate,
                attempt,
                exc,
            )

        if attempt < 5:
            await asyncio.sleep(1.5)

    # One alternate egress check. WARP is cheap and sometimes avoids an
    # anonymous-serving decision tied to the Render IP.
    resolved = await asyncio.to_thread(
        _resolve_threads_share_via_warp_googlebot_sync,
        candidate,
    )
    if resolved:
        return resolved

    logging.warning(
        "Threads share alias remains unresolved after public crawler checks: %s",
        candidate,
    )
    return candidate

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

    plain_redirect_resolved = await resolve_threads_share_via_manual_redirect(candidate)
    if plain_redirect_resolved:
        return plain_redirect_resolved

    oembed_resolved = await resolve_threads_share_via_oembed(candidate)
    if oembed_resolved:
        return oembed_resolved

    real_extension_resolved = await asyncio.to_thread(
        _resolve_threads_share_via_real_extension_page_sync,
        candidate,
    )
    if real_extension_resolved:
        return real_extension_resolved

    cdp_resolved = await asyncio.to_thread(
        _resolve_threads_share_via_cdp_fetch_sync,
        candidate,
    )
    if cdp_resolved:
        return cdp_resolved

    # The old temporary extension service-worker probe is intentionally skipped
    # here: Chromium headless doesn't wake that MV3 worker reliably. Keep the
    # lightweight HTTP request-shape fallback below for diagnostics.
    extension_resolved = await resolve_threads_share_via_extension_fetch(candidate)
    if extension_resolved:
        return extension_resolved

    headerless_resolved = await resolve_threads_share_via_headerless_fetch(candidate)
    if headerless_resolved:
        return headerless_resolved

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
