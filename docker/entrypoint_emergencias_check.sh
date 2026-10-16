#!/usr/bin/env bash
set -euo pipefail

# Reproduce el timer histórico: primer check tras 2 min y después cada 2 min.
export EMERGENCIAS_DATA_DIR="${EMERGENCIAS_DATA_DIR:-/app/bot_data/emergencias_guardia}"

boot_delay="${EMERGENCIAS_CHECK_BOOT_DELAY_SECONDS:-120}"
interval="${EMERGENCIAS_CHECK_INTERVAL_SECONDS:-120}"

mkdir -p "${EMERGENCIAS_DATA_DIR}"

if [[ ! -f "${EMERGENCIAS_DATA_DIR}/config.json" ]]; then
    python -u /app/tools/emergencias_guardia/emergencias_guardia.py init >/dev/null
fi

sleep "${boot_delay}"

while true; do
    # Un check correcto no vuelca el JSON completo en Docker.
    # El wrapper conserva stderr y los avisos de fuentes/envíos fallidos.
    python -u /app/tools/emergencias_guardia/emergencias_check_log.py || true
    sleep "${interval}"
done
