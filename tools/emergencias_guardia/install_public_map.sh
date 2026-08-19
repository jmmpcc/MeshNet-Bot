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
#
# El mapa público queda protegido por dos mecanismos complementarios:
#   1. systemd.path: publicación prácticamente inmediata cuando cambia current.json.
#   2. systemd.timer: comprobación de respaldo cada 30 s por si una sustitución
#      atómica del fichero no genera el evento esperado para el observador .path.
#
# Ambos mecanismos invocan el mismo servicio oneshot. publish_if_changed() compara la
# revisión pública y NO realiza ninguna subida FTPS cuando los datos no han cambiado.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/meshnet/MeshNet-Bot}"
SOURCE_DIR="$REPO_DIR/tools/emergencias_guardia/systemd"
SERVICE_NAME="meshnet-emergencias-public-map.service"
PATH_NAME="meshnet-emergencias-public-map.path"
TIMER_NAME="meshnet-emergencias-public-map.timer"
SYSTEMD_DIR="/etc/systemd/system"

for unit in "$SERVICE_NAME" "$PATH_NAME" "$TIMER_NAME"; do
    if [[ ! -f "$SOURCE_DIR/$unit" ]]; then
        echo "ERROR: no se encuentra la unidad systemd $unit en $SOURCE_DIR" >&2
        exit 1
    fi
done

# Verificación sintáctica previa. No necesita credenciales ni realiza conexiones FTPS.
/usr/bin/python3 -m py_compile \
    "$REPO_DIR/tools/emergencias_guardia/emergencias/public_map.py"

install -m 0644 "$SOURCE_DIR/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME"
install -m 0644 "$SOURCE_DIR/$PATH_NAME" "$SYSTEMD_DIR/$PATH_NAME"
install -m 0644 "$SOURCE_DIR/$TIMER_NAME" "$SYSTEMD_DIR/$TIMER_NAME"

systemctl daemon-reload
systemctl enable --now "$PATH_NAME" "$TIMER_NAME"

# Estado final. El service es oneshot y sólo queda activo durante cada comprobación o
# publicación; entre ejecuciones su estado normal será inactive/dead.
systemctl --no-pager --full status "$PATH_NAME" || true
systemctl --no-pager --full status "$TIMER_NAME" || true

echo
echo "Publicador instalado."
echo "Antes de activarlo configure tools/emergencias_guardia/.env con"
echo "EMERGENCIAS_PUBLIC_MAP_* y establezca EMERGENCIAS_PUBLIC_MAP_ENABLED=1."
echo "El mapa comprobará cambios inmediatamente mediante .path y, como respaldo,"
echo "cada 30 segundos mediante .timer. Si la revisión no cambia, no se sube nada."
echo "Para una primera publicación manual:"
echo "  sudo systemctl start $SERVICE_NAME"
