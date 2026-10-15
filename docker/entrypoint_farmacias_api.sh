#!/usr/bin/env bash
set -euo pipefail

# API de Farmacias dentro del namespace de red del broker.
# La persistencia se mantiene fuera de la imagen mediante FARMACIAS_DATA_DIR.
export FARMACIAS_DATA_DIR="${FARMACIAS_DATA_DIR:-/app/bot_data/farmacias_guardia}"
export FARMACIAS_API_HOST="${FARMACIAS_API_HOST:-127.0.0.1}"
export BROKER_CTRL_HOST="${BROKER_CTRL_HOST:-127.0.0.1}"
export BROKER_EVENT_HOST="${BROKER_EVENT_HOST:-127.0.0.1}"

mkdir -p "${FARMACIAS_DATA_DIR}"

exec python -u /app/tools/farmacias_guardia/farmacias_guardia.py serve
