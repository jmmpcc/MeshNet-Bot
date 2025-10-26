#!/usr/bin/env bash
set -euo pipefail

BROKER_H="${BROKER_HOST:-127.0.0.1}"
BROKER_P="${BROKER_PORT:-8765}"

# ⬇️ segundos extra tras ver el puerto abierto (ajustable por env)
BOT_START_DELAY="${BOT_START_DELAY:-15}"

export BROKER_H BROKER_P

echo "[bot] Esperando a broker en ${BROKER_H}:${BROKER_P}…"

# Espera hasta que el puerto del broker responda
until python - <<'PY'
import os, socket, sys
h=os.environ.get("BROKER_H") or os.environ.get("BROKER_HOST","broker")
p=int(os.environ.get("BROKER_P") or os.environ.get("BROKER_PORT","8765"))
try:
    s=socket.create_connection((h,p),2); s.close(); sys.exit(0)
except Exception:
    sys.exit(1)
PY
do
  sleep 2
done

echo "[bot] Puerto del broker OK. Esperando ${BOT_START_DELAY}s para que termine de enlazar con el nodo…"
sleep "${BOT_START_DELAY}"

# --- AÑADIDO: asegurar permisos del directorio de datos del bot ---
mkdir -p /app/bot_data
chmod 0777 /app/bot_data || true

echo "[bot] ✅ Broker listo. Arrancando bot…"
exec python -u /app/Telegram_Bot_Broker.py
