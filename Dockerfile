FROM ghcr.io/imputnet/yt-session-generator:webserver

# The upstream webserver image launches Chromium as root on Render.
# nodriver 0.32 needs Chromium sandbox disabled in this environment.
RUN python - <<'PY'
from pathlib import Path

path = Path("/app/potoken_generator/extractor.py")
source = path.read_text(encoding="utf-8")
needle = "browser = await nodriver.start(headless=False,"
replacement = "browser = await nodriver.start(headless=False, sandbox=False,"
if needle not in source:
    raise SystemExit("yt-session-generator extractor launch pattern not found")
path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
PY

EXPOSE 8080
