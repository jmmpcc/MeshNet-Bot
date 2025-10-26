#!/usr/bin/env bash
set -euo pipefail

exec python -u /app/Meshtastic_Broker.py \
  --host "${MESHTASTIC_HOST:-192.168.1.201}" \
  --bind "0.0.0.0" \
  --port "${BROKER_PORT:-8765}" \
  --verbose
