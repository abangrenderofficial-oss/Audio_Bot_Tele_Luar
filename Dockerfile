# syntax=docker/dockerfile:1.7

FROM denoland/deno:2.9.6 AS deno

FROM brainicism/bgutil-ytdlp-pot-provider:2.0.1-deno AS pot_provider

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV TZ=UTC \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      gcc \
      build-essential \
      libffi-dev \
      libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

COPY requirements.txt ./

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --python "$VIRTUAL_ENV/bin/python" -r requirements.txt

FROM python:3.14-slim
ARG TARGETARCH
ARG WGCF_VERSION=2.3.0
ARG WIREPROXY_VERSION=1.1.3
# Pin to a digest for reproducible builds:
#   docker pull python:3.14-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.14-slim
#   Then use: FROM python:3.14-slim@sha256:...

ENV TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      curl \
      ca-certificates \
      tar \
      gzip \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system appgroup \
    && adduser --system --ingroup appgroup --home /app appuser \
    && usermod -a -G 1000 appuser

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

COPY --from=builder /opt/venv /opt/venv
COPY --from=deno /usr/bin/deno /usr/local/bin/deno
COPY --from=pot_provider /app /opt/bgutil-pot
COPY . .

RUN mkdir -p /app/downloads /app/logs /app/cookies && \
    chown -R appuser:appgroup /app

EXPOSE 8080

ENTRYPOINT ["python", "back4app_entrypoint.py"]
CMD ["python", "main.py"]
