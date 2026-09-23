# Render (runtime: docker). Google Chrome + Xvfb so nodriver can clear
# mkvbase's Cloudflare Turnstile with a real, headful browser.
FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    CHROME_BIN=/usr/bin/google-chrome-stable \
    MKVBASE_CF_CACHE=/tmp/mkvbase_cf.json \
    # Chrome in tiny container /dev/shm
    CHROME_ARGS=--disable-dev-shm-usage

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      wget \
      gnupg \
      xvfb \
      xdg-utils \
      fonts-liberation \
      fonts-noto-color-emoji \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libgtk-3-0 \
      libnspr4 \
      libnss3 \
      libx11-xcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && google-chrome-stable --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Wait for Xvfb before starting the bot (Turnstile needs a real display).
# Plain Xvfb, not xvfb-run: xvfb-run hangs when it is the container's PID 1.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x900x24 -nolisten tcp -ac & sleep 2; exec python bot_with_healthcheck.py"]
