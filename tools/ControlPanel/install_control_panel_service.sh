#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
UNIT_NAME="meshnet-control-panel.service"
UNIT_SOURCE="${SCRIPT_DIR}/systemd/${UNIT_NAME}"
POLKIT_SOURCE="${SCRIPT_DIR}/systemd/50-meshnet-control-panel.rules"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

if [[ ! -f "${UNIT_SOURCE}" || ! -f "${POLKIT_SOURCE}" ]]; then
    echo >&2 "[control-panel] No se encontraron los archivos de instalación."
    exit 2
fi
if [[ ! "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo >&2 "[control-panel] Usuario de servicio no válido: ${SERVICE_USER}"
    exit 2
fi
if [[ "$(id -u "${SERVICE_USER}")" -eq 0 ]]; then
    echo >&2 "[control-panel] No se instalará el panel como root."
    echo >&2 "[control-panel] Ejecute este asistente desde la cuenta que administrará MeshNet-Bot, sin anteponer sudo."
    exit 2
fi

temporary_dir="$(mktemp -d)"
trap 'rm -rf "${temporary_dir}"' EXIT
temporary_unit="${temporary_dir}/${UNIT_NAME}"
temporary_polkit="${temporary_dir}/50-meshnet-control-panel.rules"
escaped_repo="${REPO_DIR//\\/\\\\}"
escaped_repo="${escaped_repo//&/\\&}"
escaped_repo="${escaped_repo//|/\\|}"
sed \
    -e "s|/home/meshnet/MeshNet-Bot|${escaped_repo}|g" \
    -e "s|^User=meshnet$|User=${SERVICE_USER}|" \
    -e "s|^Group=meshnet$|Group=${SERVICE_GROUP}|" \
    "${UNIT_SOURCE}" > "${temporary_unit}"
sed \
    -e "s|subject.user == \"meshnet\"|subject.user == \"${SERVICE_USER}\"|" \
    "${POLKIT_SOURCE}" > "${temporary_polkit}"

if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    echo "[control-panel] Preparando el entorno virtual..."
    python3 -m venv "${SCRIPT_DIR}/.venv"
fi
"${SCRIPT_DIR}/.venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"
if [[ ! -r "${SCRIPT_DIR}/.venv/bin/python" || ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    echo >&2 "[control-panel] El intérprete del entorno virtual no es accesible."
    exit 2
fi

echo "[control-panel] Instalando ${UNIT_NAME} para ${SERVICE_USER}:${SERVICE_GROUP} en ${REPO_DIR}"
sudo install -o root -g root -m 0644 "${temporary_unit}" "/etc/systemd/system/${UNIT_NAME}"
sudo install -d -o root -g root -m 0755 "/etc/polkit-1/rules.d"
sudo install -o root -g root -m 0644 "${temporary_polkit}" \
    "/etc/polkit-1/rules.d/50-meshnet-control-panel.rules"
sudo systemctl daemon-reload
sudo systemctl enable "${UNIT_NAME}"
# `enable --now` no reinicia una unidad que ya estaba activa. Es necesario
# reiniciarla para aplicar cambios de código, usuario, ruta o dirección de escucha.
sudo systemctl restart "${UNIT_NAME}"

echo "[control-panel] Esperando la comprobación de salud..."
"${SCRIPT_DIR}/.venv/bin/python" - <<'PY'
import json
import time
from urllib.error import URLError
from urllib.request import urlopen

url = "http://127.0.0.1:8790/health"
for attempt in range(20):
    try:
        with urlopen(url, timeout=1) as response:
            payload = json.load(response)
        if response.status == 200 and payload.get("ok") is True:
            print("[control-panel] Salud correcta:", json.dumps(payload, ensure_ascii=False))
            break
    except (OSError, URLError, ValueError):
        pass
    time.sleep(0.5)
else:
    raise SystemExit("[control-panel] El servicio no respondió correctamente en /health")
PY

echo "[control-panel] Servicio instalado y habilitado."
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "[control-panel] Panel local: http://127.0.0.1:8790"
if [[ -n "${LAN_IP}" ]]; then
    echo "[control-panel] Panel en red: http://${LAN_IP}:8790"
fi
sudo systemctl status "${UNIT_NAME}" --no-pager
