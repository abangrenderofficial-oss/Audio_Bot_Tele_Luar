FROM ghcr.io/imputnet/yt-session-generator:webserver

ARG TARGETARCH
ARG WGCF_VERSION=2.3.0
ARG WIREPROXY_VERSION=1.1.3

USER root

RUN apk add --no-cache curl tar gzip ca-certificates

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

# Cobalt currently requests POST /get_pot while the official generator
# exposes /token. Keep the official generator and add only a compatible alias.
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

# Run Chromium without its container sandbox and force all YouTube browser
# traffic through the local WARP SOCKS5 endpoint.
RUN python - <<'PY'
from pathlib import Path
p = Path('/app/potoken_generator/extractor.py')
s = p.read_text()
old = """                browser = await nodriver.start(headless=False,
                                               browser_executable_path=self.browser_path,
                                               user_data_dir=self.profile_path)"""
new = """                browser = await nodriver.start(headless=False,
                                               browser_executable_path=self.browser_path,
                                               user_data_dir=self.profile_path,
                                               browser_args=[\"--proxy-server=socks5://127.0.0.1:1080\"],
                                               sandbox=False)"""
if old not in s:
    raise SystemExit('official extractor.py nodriver.start layout changed')
p.write_text(s.replace(old, new, 1))
PY

RUN cat > /app/start-render-session.sh <<'SH'
#!/bin/sh
set -eu

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

RUN python - <<'PY'
from pathlib import Path
p = Path('/app/potoken_generator/extractor.py')
s = p.read_text()
needle = "            await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw')\n"
replacement = """            await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw')
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
                text_preview = re.sub(r'<[^>]+>', ' ', page_html)
                text_preview = re.sub(r'\\s+', ' ', text_preview).strip()[:600]
                logger.warning(f'page diagnostics markers={markers} preview={text_preview}')
            except Exception as diagnostic_error:
                logger.warning(f'page diagnostics failed: {diagnostic_error}')
"""
if needle not in s:
    raise SystemExit('official extractor.py page navigation layout changed')
p.write_text(s.replace(needle, replacement, 1))
PY

RUN chmod +x /app/start-render-session.sh

CMD ["/app/start-render-session.sh"]
