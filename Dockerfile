FROM ghcr.io/imputnet/yt-session-generator:webserver AS upstream

FROM python:3.12-alpine3.24

ARG TARGETARCH
ARG WGCF_VERSION=2.3.0
ARG WIREPROXY_VERSION=1.1.3

RUN apk add --no-cache \
      xvfb \
      nss \
      freetype \
      freetype-dev \
      harfbuzz \
      ca-certificates \
      ttf-freefont \
      chromium \
      chromium-chromedriver \
      curl \
      tar \
      gzip

WORKDIR /app

# Keep the official trusted-session generator source, but run it on a current
# browser/runtime instead of the upstream image's 2024-era Chromium.
COPY --from=upstream /app /app

RUN pip install --no-cache-dir -r /app/requirements.txt && \
    pip install --no-cache-dir --upgrade nodriver==0.50.3

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) \
        WGCF_SHA="01614e38c0eb5f3405232e71cfaf02d64d4809e4988ad8f5a8071af16d193405"; \
        WIREPROXY_SHA="e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c" ;; \
      arm64) \
        WGCF_SHA="dcadadc42bcc410a4032a6d1c0490ea510e199f0aaaee397dc1aa0fbd27038e8"; \
        WIREPROXY_SHA="370e00bd2167960d1ecd1c3c1439715bbaa94a0a110a2040468670c9af6021b6" ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/wgcf "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${TARGETARCH}"; \
    echo "${WGCF_SHA}  /tmp/wgcf" | sha256sum -c -; \
    install -m 0755 /tmp/wgcf /usr/local/bin/wgcf; \
    curl -fsSL -o /tmp/wireproxy.tar.gz "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${TARGETARCH}.tar.gz"; \
    echo "${WIREPROXY_SHA}  /tmp/wireproxy.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/wireproxy; \
    tar -xzf /tmp/wireproxy.tar.gz -C /tmp/wireproxy; \
    WIREPROXY_BIN="$(find /tmp/wireproxy -type f -name wireproxy -print -quit)"; \
    test -n "$WIREPROXY_BIN"; \
    install -m 0755 "$WIREPROXY_BIN" /usr/local/bin/wireproxy; \
    rm -rf /tmp/wgcf /tmp/wireproxy /tmp/wireproxy.tar.gz

# Current Cobalt asks POST /get_pot. The official generator exposes /token;
# add a compatibility alias while keeping the official response format.
RUN python - <<'PY'
from pathlib import Path
p = Path('/app/potoken_generator/server.py')
s = p.read_text()
needle = "            '/token': self.get_potoken,\n"
replacement = "            '/token': self.get_potoken,\n            '/get_pot': self.get_potoken,\n"
if needle not in s:
    raise SystemExit('official server.py route layout changed')
p.write_text(s.replace(needle, replacement, 1))
PY

# Modernize the browser launch, route Chromium through WARP, add safe page
# diagnostics, and detect BotGuard tokens by payload fields rather than one
# hard-coded YouTube endpoint.
RUN python - <<'PY'
from pathlib import Path
p = Path('/app/potoken_generator/extractor.py')
s = p.read_text()

old_start = """                browser = await nodriver.start(headless=False,
                                               browser_executable_path=self.browser_path,
                                               user_data_dir=self.profile_path)"""
new_start = """                browser = await nodriver.start(headless=False,
                                               browser_executable_path=self.browser_path,
                                               user_data_dir=self.profile_path,
                                               browser_args=[
                                                   "--proxy-server=socks5://127.0.0.1:1080",
                                                   "--autoplay-policy=no-user-gesture-required",
                                               ],
                                               sandbox=False)"""
if old_start not in s:
    raise SystemExit('official extractor.py nodriver.start layout changed')
s = s.replace(old_start, new_start, 1)

nav = "            await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw')\n"
diag = """            await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1')
            try:
                import re
                page_html = await tab.get_content()
                page_lower = page_html.lower()
                markers = [
                    marker for marker in (
                        'before you continue',
                        'consent.youtube.com',
                        'sign in to confirm',
                        'not a bot',
                        'video unavailable',
                        'unusual traffic',
                        'movie_player',
                    )
                    if marker in page_lower
                ]
                logger.warning(
                    f'page diagnostics markers={markers} html_len={len(page_html)}'
                )
            except Exception as diagnostic_error:
                logger.warning(f'page diagnostics failed: {diagnostic_error}')
"""
if nav not in s:
    raise SystemExit('official extractor.py navigation layout changed')
s = s.replace(nav, diag, 1)

old_handler = """    async def _send_handler(self, event: nodriver.cdp.network.RequestWillBeSent) -> None:
        if not event.request.method == 'POST':
            return
        if '/youtubei/v1/player' not in event.request.url:
            return
        token_info = self._extract_token(event.request)
        if token_info is None:
            return
        logger.info(f'new token: {token_info.to_json()}')
        self._token_info = token_info
        self._extraction_done.set()
"""
new_handler = """    async def _send_handler(self, event: nodriver.cdp.network.RequestWillBeSent) -> None:
        request = event.request
        if request.method != 'POST' or 'youtubei' not in request.url:
            return

        post_data = request.post_data
        if not post_data:
            return

        try:
            post_data_json = json.loads(post_data)
        except (json.JSONDecodeError, TypeError):
            return

        def find_token_fields(value):
            if isinstance(value, dict):
                context = value.get('context')
                integrity = value.get('serviceIntegrityDimensions')
                if isinstance(context, dict) and isinstance(integrity, dict):
                    client = context.get('client')
                    if isinstance(client, dict):
                        visitor_data = client.get('visitorData')
                        potoken = integrity.get('poToken')
                        if visitor_data and potoken:
                            return visitor_data, potoken

                for nested in value.values():
                    found = find_token_fields(nested)
                    if found:
                        return found
                return None

            if isinstance(value, list):
                for nested in value:
                    found = find_token_fields(nested)
                    if found:
                        return found
            return None

        from urllib.parse import urlsplit
        req_path = urlsplit(request.url).path
        found = find_token_fields(post_data_json)
        logger.info(
            f'network diagnostics youtubei_path={req_path} '
            f'payload_type={type(post_data_json).__name__} '
            f'has_trusted_session={bool(found)}'
        )

        if not found:
            return

        visitor_data, potoken = found
        token_info = TokenInfo(
            updated=int(time.time()),
            potoken=potoken,
            visitor_data=visitor_data,
        )
        logger.info(
            f'trusted session token captured '
            f'potoken_len={len(potoken)} visitor_len={len(visitor_data)}'
        )
        self._token_info = token_info
        self._extraction_done.set()
"""
if old_handler not in s:
    raise SystemExit('official extractor.py send-handler layout changed')
s = s.replace(old_handler, new_handler, 1)

p.write_text(s)
PY

RUN cat > /app/start-render-session.sh <<'SH'
#!/bin/sh
set -eu

echo "[BROWSER] $(chromium-browser --version 2>/dev/null || chromium --version 2>/dev/null || true)"
python - <<'PY'
import nodriver
print("[NODRIVER]", getattr(nodriver, "__version__", "unknown"))
PY

WARP_HOME=/app/.runtime/warp
mkdir -p "$WARP_HOME"
cd "$WARP_HOME"

if [ ! -f wgcf-account.toml ]; then
  wgcf register --accept-tos >/dev/null 2>&1
fi
if [ ! -f wgcf-profile.conf ]; then
  wgcf generate --keepalive=25 >/dev/null 2>&1
fi

if ! grep -q '^\[Socks5\]' wgcf-profile.conf; then
  cat >> wgcf-profile.conf <<'EOF'

[Socks5]
BindAddress = 127.0.0.1:1080
EOF
fi

wireproxy -c wgcf-profile.conf -s >/tmp/wireproxy.log 2>&1 &
WIREPID=$!

i=0
until curl -fsS --max-time 5 --socks5-hostname 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -Eq 'warp=(on|plus)'; do
  i=$((i+1))
  if [ "$i" -ge 12 ]; then
    echo "[WARP] verification failed" >&2
    cat /tmp/wireproxy.log >&2 || true
    kill "$WIREPID" 2>/dev/null || true
    exit 1
  fi
  sleep 2
done

echo "[WARP] verified for trusted-session generator"

Xvfb :99 -ac -screen 0 "${XVFB_WHD:-1280x720x16}" -nolisten tcp >/dev/null 2>&1 &
sleep 2
exec env DISPLAY=:99 python /app/potoken-generator.py --bind 0.0.0.0
SH

RUN chmod +x /app/start-render-session.sh

CMD ["/app/start-render-session.sh"]
