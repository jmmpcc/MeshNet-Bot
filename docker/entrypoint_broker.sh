#!/usr/bin/env bash
set -euo pipefail

BROKER_SCRIPT=""

# Layout nuevo
for f in /app/source/Meshtastic_Broker.py /app/source/Meshtastic_Broker_v*.py /app/source/Meshtastic_Broker_*.py; do
  if [[ -f "$f" ]]; then
    BROKER_SCRIPT="$f"
    break
  fi
done

# Compatibilidad layout antiguo
if [[ -z "$BROKER_SCRIPT" && -f /app/Meshtastic_Broker.py ]]; then
  BROKER_SCRIPT="/app/Meshtastic_Broker.py"
fi

if [[ -z "$BROKER_SCRIPT" ]]; then
  echo "[broker] ERROR: no se encontró el script del broker"
  echo "[broker] /app:"; ls -la /app || true
  echo "[broker] /app/source:"; ls -la /app/source || true
  exit 2
fi

echo "[broker] Ejecutando: $BROKER_SCRIPT"

exec python -u "$BROKER_SCRIPT" \
  --host "${MESHTASTIC_HOST:-127.0.0.1}" \
  --bind "0.0.0.0" \
  --port "${BROKER_PORT:-8765}" \
  --verbose
