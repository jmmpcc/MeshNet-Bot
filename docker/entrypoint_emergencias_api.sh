#!/usr/bin/env bash
set -euo pipefail

# API de Emergencias dentro del namespace de red del broker.
export EMERGENCIAS_DATA_DIR="${EMERGENCIAS_DATA_DIR:-/app/bot_data/emergencias_guardia}"

mkdir -p "${EMERGENCIAS_DATA_DIR}"

# Si todavía no existe configuración persistente, se crea con los valores
# históricos por defecto antes de iniciar la API.
if [[ ! -f "${EMERGENCIAS_DATA_DIR}/config.json" ]]; then
    python -u /app/tools/emergencias_guardia/emergencias_guardia.py init >/dev/null
fi

api_host="${EMERGENCIAS_API_HOST:-0.0.0.0}"
api_port="${EMERGENCIAS_API_PORT:-8789}"

exec python -u /app/tools/emergencias_guardia/emergencias_guardia.py serve --host "${api_host}" --port "${api_port}"
