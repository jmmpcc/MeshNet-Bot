#!/usr/bin/env bash
set -euo pipefail

# Opcional: log mínimamente el entorno útil
echo "[aprs] KISS=${KISS_HOST:-127.0.0.1}:${KISS_PORT:-8100}  CALL=${APRS_CALL:-NOCALL}  PATH=${APRS_PATH:-WIDE1-1,WIDE2-1}"
echo "[aprs] BROKER_CTRL=${BROKER_CTRL_HOST:-meshtastic-broker}:${BROKER_CTRL_PORT:-8766}  MESHTASTIC_HOST=${MESHTASTIC_HOST:-127.0.0.1}"

# Construye args para APRS-IS solo si hay credenciales
APRSCFG=()
if [[ -n "${APRSIS_USER:-}" && -n "${APRSIS_PASSCODE:-}" ]]; then
  APRSCFG+=( "--aprsis-user" "$APRSIS_USER" "--aprsis-passcode" "$APRSIS_PASSCODE" )
  [[ -n "${APRSIS_HOST:-}" ]]   && APRSCFG+=( "--aprsis-host" "$APRSIS_HOST" )
  [[ -n "${APRSIS_PORT:-}" ]]   && APRSCFG+=( "--aprsis-port" "$APRSIS_PORT" )
  [[ -n "${APRSIS_FILTER:-}" ]] && APRSCFG+=( "--aprsis-filter" "$APRSIS_FILTER" )
fi

# Ejecuta el puente APRS
exec python /app/source/meshtastic_to_aprs.py "${APRSCFG[@]}"

