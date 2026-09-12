FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ffmpeg fontconfig fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /usr/local/share/fonts && \
    curl -L --fail --retry 3 "https://raw.githubusercontent.com/google/fonts/main/ofl/tiktoksans/TikTokSans%5Bopsz%2Cslnt%2Cwdth%2Cwght%5D.ttf" -o /usr/local/share/fonts/TikTokSans.ttf && \
    fc-cache -f
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY backend ./backend
COPY scripts ./scripts

# API service default. provider_api extends the existing API with the explicit
# Google Flow / Kling 3.0 video-provider selector. For the worker service,
# override command with: python -m backend.worker
CMD ["sh", "-c", "uvicorn backend.provider_api:app --host 0.0.0.0 --port ${PORT:-8000}"]
