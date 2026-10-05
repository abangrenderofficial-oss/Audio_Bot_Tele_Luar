#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARP_HOME="$ROOT/.runtime/session-warp"
WGCF_BIN="$ROOT/.session-bin/wgcf"
WIREPROXY_BIN="$ROOT/.session-bin/wireproxy"
PROFILE="$WARP_HOME/wgcf-profile.conf"

mkdir -p "$WARP_HOME"
cd "$WARP_HOME"

if [[ ! -f wgcf-account.toml ]]; then
  "$WGCF_BIN" register --accept-tos >/dev/null 2>&1
fi

if [[ ! -f wgcf-profile.conf ]]; then
  "$WGCF_BIN" generate --keepalive=25 >/dev/null 2>&1
fi

if ! grep -q '^\[Socks5\]' "$PROFILE"; then
  printf '\n[Socks5]\nBindAddress = 127.0.0.1:1080\n' >> "$PROFILE"
fi

"$WIREPROXY_BIN" -c "$PROFILE" -s >/tmp/session-wireproxy.log 2>&1 &
WIREPROXY_PID=$!

for _ in $(seq 1 60); do
  if (echo >/dev/tcp/127.0.0.1/1080) >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! (echo >/dev/tcp/127.0.0.1/1080) >/dev/null 2>&1; then
  echo "[SESSION] WARP SOCKS failed to start" >&2
  exit 1
fi

echo "[SESSION] WARP SOCKS ready at 127.0.0.1:1080"

cd "$ROOT"
node .bgutil/server/build/main.js --host 127.0.0.1 --port 4416 >/tmp/session-bgutil.log 2>&1 &
BGUTIL_PID=$!

for _ in $(seq 1 60); do
  if (echo >/dev/tcp/127.0.0.1/4416) >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! (echo >/dev/tcp/127.0.0.1/4416) >/dev/null 2>&1; then
  echo "[SESSION] bgutil provider failed to start" >&2
  cat /tmp/session-bgutil.log >&2 || true
  exit 1
fi

echo "[SESSION] bgutil provider ready at 127.0.0.1:4416"

export POT_ADAPTER_HOST="${POT_ADAPTER_HOST:-0.0.0.0}"
export POT_ADAPTER_PORT="${PORT:-10000}"
export POT_PROVIDER_URL="${POT_PROVIDER_URL:-http://127.0.0.1:4416/get_pot}"
export POT_PROVIDER_PROXY="${POT_PROVIDER_PROXY:-socks5h://127.0.0.1:1080}"
export POT_ADAPTER_ATTEMPTS="${POT_ADAPTER_ATTEMPTS:-30}"
export POT_ADAPTER_RETRY_MS="${POT_ADAPTER_RETRY_MS:-500}"

cleanup() {
  kill "$BGUTIL_PID" "$WIREPROXY_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec node render/cobalt_pot_adapter.cjs
