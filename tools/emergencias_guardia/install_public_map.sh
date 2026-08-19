#!/usr/bin/env bash
# Instala exclusivamente el observador/publicador del mapa público de emergencias.
#
# Uso:
#   cd /home/meshnet/MeshNet-Bot
#   sudo bash tools/emergencias_guardia/install_public_map.sh
#
# No modifica el recolector de emergencias, sus timers, APRS, broker ni Control Panel.
# Tampoco modifica /public_html/.htaccess: la política global del dominio debe fusionarse
# de forma consciente con la configuración real del hosting.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/meshnet/MeshNet-Bot}"
SOURCE_DIR="$REPO_DIR/tools/emergencias_guardia/systemd"
SERVICE_NAME="meshnet-emergencias-public-map.service"
PATH_NAME="meshnet-emergencias-public-map.path"
SYSTEMD_DIR="/etc/systemd/system"

if [[ ! -f "$SOURCE_DIR/$SERVICE_NAME" || ! -f "$SOURCE_DIR/$PATH_NAME" ]]; then
    echo "ERROR: no se encuentran las unidades systemd del mapa público en $SOURCE_DIR" >&2
    exit 1
fi

# Verificación sintáctica previa. No necesita credenciales ni realiza conexiones FTPS.
/usr/bin/python3 -m py_compile \
    "$REPO_DIR/tools/emergencias_guardia/emergencias/public_map.py"

install -m 0644 "$SOURCE_DIR/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME"
install -m 0644 "$SOURCE_DIR/$PATH_NAME" "$SYSTEMD_DIR/$PATH_NAME"

systemctl daemon-reload
systemctl enable --now "$PATH_NAME"

# Estado final: path activo. El service es oneshot y sólo queda activo durante cada
# publicación, por lo que su estado normal entre cambios será inactive/dead.
systemctl --no-pager --full status "$PATH_NAME" || true

echo
echo "Publicador instalado."
echo "Antes de activarlo configure tools/emergencias_guardia/.env con"
echo "EMERGENCIAS_PUBLIC_MAP_* y establezca EMERGENCIAS_PUBLIC_MAP_ENABLED=1."
echo "Para una primera publicación manual:"
echo "  sudo systemctl start $SERVICE_NAME"
