#!/bin/sh
set -eu

XVFB_WHD="${XVFB_WHD:-1280x720x16}"

echo "[TRUSTED-SESSION] starting Xvfb"
Xvfb :99 -ac -screen 0 "$XVFB_WHD" -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 2

echo "[TRUSTED-SESSION] starting official Chromium token generator"
export DISPLAY=:99
exec python trusted_session_server.py
