FROM python:3.12-alpine3.19

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
      git

WORKDIR /app

RUN git clone --depth 1 https://github.com/imputnet/yt-session-generator.git /app/yt-session-generator

WORKDIR /app/yt-session-generator

RUN pip install --no-cache-dir -r requirements.txt \
    && sed -i 's/await self.sleep(0.5)/await self.sleep(2)/' /usr/local/lib/python3.12/site-packages/nodriver/core/browser.py

COPY render/trusted_session_server.py ./trusted_session_server.py
COPY render/trusted_session_start.sh /usr/local/bin/start-trusted-session

RUN chmod +x /usr/local/bin/start-trusted-session

ENV PYTHONUNBUFFERED=1 \
    TOKEN_UPDATE_INTERVAL=300

EXPOSE 10000

CMD ["/usr/local/bin/start-trusted-session"]
