FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=10000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /var/data/udemy_archives /app/archive_work \
    && chown -R appuser:appuser /app /var/data /ms-playwright

COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 10000

CMD ["python", "bot_with_healthcheck.py"]
