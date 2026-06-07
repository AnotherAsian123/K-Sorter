#!/bin/sh
# Unraid-friendly entrypoint: apply UMASK, fix /config ownership, drop to PUID/PGID.
set -e

CONFIG="${KSORTER_CONFIG_DIR:-/config}"
umask "${UMASK:-022}"
mkdir -p "${CONFIG}/logs"
chown -R "${PUID:-99}:${PGID:-100}" "${CONFIG}" 2>/dev/null || true

echo "[K-Sorter] PUID=${PUID:-99} PGID=${PGID:-100} UMASK=${UMASK:-022} port=${KSORTER_PORT:-8080}"
echo "[K-Sorter] Sort once, sort TWICE."

exec gosu "${PUID:-99}:${PGID:-100}" \
  uvicorn app.main:app --host 0.0.0.0 --port "${KSORTER_PORT:-8080}"
