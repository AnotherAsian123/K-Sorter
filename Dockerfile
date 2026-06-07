# K-Sorter — single self-hosted container for Unraid.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
LABEL org.opencontainers.image.title="K-Sorter" \
      org.opencontainers.image.description="Calm K-pop video sorter for Unraid" \
      org.opencontainers.image.source="https://github.com/AnotherAsian123/K-Sorter"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KSORTER_CONFIG_DIR=/config \
    KSORTER_PORT=8080 \
    PUID=99 PGID=100 UMASK=022

# gosu lets us drop to the Unraid PUID/PGID at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
WORKDIR /app
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('KSORTER_PORT','8080')+'/healthz')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
