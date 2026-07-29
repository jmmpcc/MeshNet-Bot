#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
UNIT_NAME="meshnet-control-panel.service"
UNIT_SOURCE="${SCRIPT_DIR}/systemd/${UNIT_NAME}"
POLKIT_SOURCE="${SCRIPT_DIR}/systemd/50-meshnet-control-panel.rules"

if [[ ! -f "${UNIT_SOURCE}" || ! -f "${POLKIT_SOURCE}" ]]; then
    echo >&2 "[control-panel] No se encontraron los archivos de instalación."
    exit 2
fi

temporary="$(mktemp)"
trap 'rm -f "${temporary}"' EXIT
sed "s|/home/meshnet/MeshNet-Bot|${REPO_DIR}|g" "${UNIT_SOURCE}" > "${temporary}"

echo "[control-panel] Instalando ${UNIT_NAME} para ${REPO_DIR}"
sudo install -o root -g root -m 0644 "${temporary}" "/etc/systemd/system/${UNIT_NAME}"
sudo install -d -o root -g root -m 0755 "/etc/polkit-1/rules.d"
sudo install -o root -g root -m 0644 "${POLKIT_SOURCE}" \
    "/etc/polkit-1/rules.d/50-meshnet-control-panel.rules"
sudo systemctl daemon-reload
sudo systemctl enable --now "${UNIT_NAME}"

echo "[control-panel] Servicio instalado y habilitado."
sudo systemctl status "${UNIT_NAME}" --no-pager
