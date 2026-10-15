#!/usr/bin/env bash
set -euo pipefail

# Reproduce el timer histórico: primer check tras 10 min y después cada 3 h.
export FARMACIAS_DATA_DIR="${FARMACIAS_DATA_DIR:-/app/bot_data/farmacias_guardia}"
export BROKER_CTRL_HOST="${BROKER_CTRL_HOST:-127.0.0.1}"

boot_delay="${FARMACIAS_CHECK_BOOT_DELAY_SECONDS:-600}"
interval="${FARMACIAS_CHECK_INTERVAL_SECONDS:-10800}"

mkdir -p "${FARMACIAS_DATA_DIR}"
sleep "${boot_delay}"

while true; do
    python -u /app/tools/farmacias_guardia/farmacias_guardia_scoped.py check --send || true
    sleep "${interval}"
done
