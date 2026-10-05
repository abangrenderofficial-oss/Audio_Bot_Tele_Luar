FROM python:3.12-alpine3.19

RUN apk add --no-cache \
      bash \
      ca-certificates \
      git \
      xvfb \
      nss \
      freetype \
      freetype-dev \
      harfbuzz \
      ttf-freefont \
      chromium \
      chromium-chromedriver

WORKDIR /opt/yt-session-generator

RUN git clone --depth 1 https://github.com/imputnet/yt-session-generator.git . \
    && pip install --no-cache-dir -r requirements.txt \
    && python - <<'PY'
from pathlib import Path
p = Path("potoken_generator/server.py")
s = p.read_text()
needle = "            '/token': self.get_potoken,\n"
if needle not in s:
    raise SystemExit("token route not found")
s = s.replace(needle, needle + "            '/get_pot': self.get_potoken,\n")
p.write_text(s)
PY

COPY render/yt_session_start.sh /usr/local/bin/yt-session-start
RUN chmod +x /usr/local/bin/yt-session-start

ENV PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PORT=10000 \
    TOKEN_UPDATE_INTERVAL=300

EXPOSE 10000

CMD ["/usr/local/bin/yt-session-start"]
