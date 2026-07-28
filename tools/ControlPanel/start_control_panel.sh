#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${CONTROLPANEL_VENV:-${SCRIPT_DIR}/.venv}"
HOST="${CONTROLPANEL_HOST:-0.0.0.0}"
PORT="${CONTROLPANEL_PORT:-8790}"

cd "${SCRIPT_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "[control-panel] Creando entorno virtual..."
    python3 -m venv "${VENV_DIR}"
fi

if ! "${VENV_DIR}/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo "[control-panel] Instalando dependencias..."
    "${VENV_DIR}/bin/pip" install -r requirements.txt
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "[control-panel] Panel local: http://127.0.0.1:${PORT}"
if [[ -n "${LAN_IP}" ]]; then
    echo "[control-panel] Panel en red: http://${LAN_IP}:${PORT}"
fi
echo "[control-panel] Pulsa Ctrl+C para detenerlo."

if command -v xdg-open >/dev/null 2>&1 && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    (
        sleep 2
        xdg-open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true
    ) &
fi

exec "${VENV_DIR}/bin/python" control_panel.py --host "${HOST}" --port "${PORT}"
