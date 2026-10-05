#!/bin/sh
set -eu

Xvfb :99 -ac -screen 0 1280x720x16 -nolisten tcp >/tmp/xvfb.log 2>&1 &

exec python potoken-generator.py \
  --bind 0.0.0.0 \
  --port "${PORT:-10000}" \
  --update-interval "${TOKEN_UPDATE_INTERVAL:-300}"
