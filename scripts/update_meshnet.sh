#!/usr/bin/env bash
set -euo pipefail

# Actualizador coordinado de MeshNet-Bot.
#
# Objetivo:
#   Actualizar de forma coherente los dos planos de despliegue del proyecto:
#   1) imágenes Docker publicadas en GHCR;
#   2) aplicaciones independientes que se ejecutan mediante systemd en el host.
#
# Uso normal (como usuario propietario del repositorio, sin sudo):
#   ./scripts/update_meshnet.sh
#
# Opciones:
#   --skip-docker       No ejecutar docker compose pull/up.
#
# Perfiles opcionales se controlan con COMPOSE_PROFILES en .env, por ejemplo:
#   COMPOSE_PROFILES=farmacias
#   COMPOSE_PROFILES=farmacias,emergencias
#   --from <commit>     Commit inicial para calcular cambios (modo diagnóstico).
#   --to <commit>       Commit final para calcular cambios (requiere --from).
#   --dry-run           Mostrar acciones sin modificar Git, Docker ni systemd.
#
# El script nunca descarta cambios locales, nunca hace reset y nunca habilita
# servicios que estuvieran deshabilitados/inactivos. Solo reinicia servicios
# host que ya estén instalados y activos.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_DIR}/docker-compose.rpi.yml"

DRY_RUN=false
SKIP_DOCKER=false
FROM_COMMIT=""
TO_COMMIT=""

log() { printf '[meshnet-update] %s\n' "$*"; }
die() { printf '[meshnet-update] ERROR: %s\n' "$*" >&2; exit 1; }

run() {
    if ${DRY_RUN}; then
        printf '[meshnet-update] DRY-RUN:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

run_root() {
    if [[ ${EUID} -eq 0 ]]; then
        run "$@"
    else
        run sudo "$@"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --skip-docker)
            SKIP_DOCKER=true
            shift
            ;;
        --from)
            [[ $# -ge 2 ]] || die "--from requiere un commit"
            FROM_COMMIT="$2"
            shift 2
            ;;
        --to)
            [[ $# -ge 2 ]] || die "--to requiere un commit"
            TO_COMMIT="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '3,22p' "$0"
            exit 0
            ;;
        *)
            die "opción desconocida: $1"
            ;;
    esac
done

if [[ -n "${FROM_COMMIT}" || -n "${TO_COMMIT}" ]]; then
    [[ -n "${FROM_COMMIT}" && -n "${TO_COMMIT}" ]] || die "--from y --to deben usarse juntos"
fi

if [[ ${EUID} -eq 0 && -n "${SUDO_USER:-}" ]]; then
    die "no ejecute el actualizador completo con sudo; use ./scripts/update_meshnet.sh como ${SUDO_USER}"
fi

command -v git >/dev/null 2>&1 || die "git no está disponible"
cd "${REPO_DIR}"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "${REPO_DIR} no es un repositorio Git"

OLD_COMMIT=""
NEW_COMMIT=""

if [[ -n "${FROM_COMMIT}" ]]; then
    git cat-file -e "${FROM_COMMIT}^{commit}" 2>/dev/null || die "commit inicial no válido: ${FROM_COMMIT}"
    git cat-file -e "${TO_COMMIT}^{commit}" 2>/dev/null || die "commit final no válido: ${TO_COMMIT}"
    OLD_COMMIT="$(git rev-parse "${FROM_COMMIT}^{commit}")"
    NEW_COMMIT="$(git rev-parse "${TO_COMMIT}^{commit}")"
    log "modo rango: ${OLD_COMMIT:0:12}..${NEW_COMMIT:0:12}"
else
    OLD_COMMIT="$(git rev-parse HEAD)"

    if ${DRY_RUN}; then
        log "DRY-RUN sin --from/--to: no se ejecuta git pull; se analizará HEAD..HEAD"
        NEW_COMMIT="${OLD_COMMIT}"
    else
        if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
            die "hay cambios locales versionados; no se ejecutará git pull"
        fi
        log "actualizando checkout con git pull --ff-only"
        git pull --ff-only
        NEW_COMMIT="$(git rev-parse HEAD)"
    fi
fi

CHANGED_FILES=""
if [[ "${OLD_COMMIT}" != "${NEW_COMMIT}" ]]; then
    CHANGED_FILES="$(git diff --name-only "${OLD_COMMIT}" "${NEW_COMMIT}")"
    log "cambios ${OLD_COMMIT:0:12}..${NEW_COMMIT:0:12}:"
    printf '%s\n' "${CHANGED_FILES}" | sed '/^$/d; s/^/  - /'
else
    log "checkout sin cambios de commit"
fi

changed_match() {
    local regex="$1"
    [[ -n "${CHANGED_FILES}" ]] && grep -E -q "${regex}" <<<"${CHANGED_FILES}"
}

# Perfiles Docker opcionales activos. Docker Compose lee COMPOSE_PROFILES también
# desde .env; aquí lo resolvemos para evitar reiniciar en paralelo servicios
# systemd equivalentes durante una migración.
COMPOSE_PROFILES_VALUE="${COMPOSE_PROFILES:-}"
if [[ -z "${COMPOSE_PROFILES_VALUE}" && -f "${REPO_DIR}/.env" ]]; then
    COMPOSE_PROFILES_VALUE="$(sed -n 's/^[[:space:]]*COMPOSE_PROFILES[[:space:]]*=[[:space:]]*//p' "${REPO_DIR}/.env" | tail -n1 | tr -d '"' | tr -d "'")"
fi

profile_enabled() {
    local wanted="$1"
    tr ',' '\n' <<<"${COMPOSE_PROFILES_VALUE}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -Fxq "${wanted}"
}

farmacias_docker_profile=false
emergencias_docker_profile=false
if profile_enabled farmacias; then farmacias_docker_profile=true; fi
if profile_enabled emergencias; then emergencias_docker_profile=true; fi

# Matriz de aplicaciones independientes del host.
restart_farmacias=false
restart_emergencias=false
restart_control_panel=false
restart_mobile_api=false
restart_voice_rf=false

sync_farmacias_units=false
sync_emergencias_units=false
sync_control_panel_units=false
sync_mobile_api_units=false
sync_voice_rf_units=false

if changed_match '^tools/farmacias_guardia/'; then restart_farmacias=true; fi
if changed_match '^tools/emergencias_guardia/'; then restart_emergencias=true; fi
if changed_match '^tools/ControlPanel/'; then restart_control_panel=true; fi
if changed_match '^tools/MobileAPI/'; then restart_mobile_api=true; fi
if changed_match '^tools/voice_rf_gateway/'; then restart_voice_rf=true; fi

# Código compartido cargado por procesos host. Reiniciar solo los procesos que
# actualmente dependen de shared/ y que estén instalados/activos.
if changed_match '^shared/'; then
    restart_farmacias=true
    restart_emergencias=true
    restart_control_panel=true
    restart_mobile_api=true
fi

if changed_match '^tools/farmacias_guardia/systemd/'; then sync_farmacias_units=true; fi
if changed_match '^tools/emergencias_guardia/systemd/'; then sync_emergencias_units=true; fi
if changed_match '^tools/ControlPanel/systemd/'; then sync_control_panel_units=true; fi
if changed_match '^tools/MobileAPI/systemd/'; then sync_mobile_api_units=true; fi
if changed_match '^systemd/meshnet-voice-rf\.service$'; then
    sync_voice_rf_units=true
    restart_voice_rf=true
fi

# Si la aplicación ya se ejecuta como perfil Docker, no se reinicia ni se
# resincroniza su versión systemd legacy. Se conserva el estado del host y se
# emite un aviso si todavía está activa para evitar planificadores duplicados.
if ${farmacias_docker_profile}; then
    restart_farmacias=false
    sync_farmacias_units=false
fi
if ${emergencias_docker_profile}; then
    restart_emergencias=false
    sync_emergencias_units=false
fi

unit_installed() {
    local unit="$1"
    [[ -e "/etc/systemd/system/${unit}" || -e "/lib/systemd/system/${unit}" || -e "/usr/lib/systemd/system/${unit}" ]]
}

sync_unit_if_installed() {
    local source="$1"
    local unit
    unit="$(basename "${source}")"

    if ! unit_installed "${unit}"; then
        log "unidad ${unit} no instalada; se conserva la topología actual"
        return 0
    fi

    [[ -f "${source}" ]] || die "unidad fuente inexistente: ${source}"
    log "sincronizando unidad instalada ${unit}"
    run_root install -m 0644 "${source}" "/etc/systemd/system/${unit}"
}

sync_polkit_if_installed() {
    local source="$1"
    local target="/etc/polkit-1/rules.d/$(basename "${source}")"

    if [[ ! -e "${target}" ]]; then
        log "regla polkit $(basename "${source}") no instalada; se conserva la topología actual"
        return 0
    fi

    log "sincronizando regla polkit instalada $(basename "${source}")"
    run_root install -m 0644 "${source}" "${target}"
}

need_daemon_reload=false

if ${sync_farmacias_units}; then
    for source in "${REPO_DIR}"/tools/farmacias_guardia/systemd/*.{service,timer}; do
        [[ -e "${source}" ]] || continue
        sync_unit_if_installed "${source}"
    done
    need_daemon_reload=true
fi

if ${sync_emergencias_units}; then
    for source in "${REPO_DIR}"/tools/emergencias_guardia/systemd/*.{service,timer,path}; do
        [[ -e "${source}" ]] || continue
        sync_unit_if_installed "${source}"
    done
    need_daemon_reload=true
fi

if ${sync_control_panel_units}; then
    sync_unit_if_installed "${REPO_DIR}/tools/ControlPanel/systemd/meshnet-control-panel.service"
    sync_polkit_if_installed "${REPO_DIR}/tools/ControlPanel/systemd/50-meshnet-control-panel.rules"
    need_daemon_reload=true
fi

if ${sync_mobile_api_units}; then
    sync_unit_if_installed "${REPO_DIR}/tools/MobileAPI/systemd/meshnet-mobile-api.service"
    need_daemon_reload=true
fi

if ${sync_voice_rf_units}; then
    sync_unit_if_installed "${REPO_DIR}/systemd/meshnet-voice-rf.service"
    need_daemon_reload=true
fi

if ${need_daemon_reload}; then
    log "recargando systemd por cambios en unidades instaladas"
    run_root systemctl daemon-reload
fi

warn_legacy_parallel_service() {
    local profile="$1"
    shift
    local unit
    for unit in "$@"; do
        if unit_installed "${unit}" && systemctl is-active --quiet "${unit}"; then
            log "AVISO: perfil Docker ${profile} activo y ${unit} también activo; desactive el servicio/timer legacy para evitar ejecuciones duplicadas"
        fi
    done
}

if ${farmacias_docker_profile}; then
    warn_legacy_parallel_service farmacias \
        meshnet-farmacias-api.service \
        meshnet-farmacias-check.timer \
        meshnet-farmacias-daily.timer
fi
if ${emergencias_docker_profile}; then
    warn_legacy_parallel_service emergencias \
        meshnet-emergencias-api.service \
        meshnet-emergencias-check.timer
fi

env_value_from_file() {
    local key="$1"
    [[ -f "${REPO_DIR}/.env" ]] || return 0
    sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "${REPO_DIR}/.env" | tail -n1 | tr -d '"' | tr -d "'"
}

if ${farmacias_docker_profile}; then
    farmacias_url="$(env_value_from_file FARMACIAS_SERVICE_URL)"
    if [[ "${farmacias_url}" == *"host.docker.internal"* ]]; then
        log "AVISO: perfil Docker farmacias activo pero FARMACIAS_SERVICE_URL apunta al host legacy; use http://127.0.0.1:8788/query para consumir el contenedor"
    fi
fi
if ${emergencias_docker_profile}; then
    emergencias_url="$(env_value_from_file EMERGENCIAS_SERVICE_URL)"
    if [[ "${emergencias_url}" == *"host.docker.internal"* ]]; then
        log "AVISO: perfil Docker emergencias activo pero EMERGENCIAS_SERVICE_URL apunta al host legacy; use http://127.0.0.1:8789/query para consumir el contenedor"
    fi
fi

# Docker se actualiza siempre en el flujo normal. Esto cubre imágenes GHCR que
# puedan haberse republicado aunque el checkout local ya estuviera al día.
if ! ${SKIP_DOCKER}; then
    command -v docker >/dev/null 2>&1 || die "docker no está disponible"
    [[ -f "${COMPOSE_FILE}" ]] || die "no existe ${COMPOSE_FILE}"

    log "actualizando imágenes Docker"
    run docker compose -f "${COMPOSE_FILE}" pull

    log "reconciliando contenedores"
    run docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans
else
    log "Docker omitido por --skip-docker"
fi

restart_if_active() {
    local unit="$1"

    if ! unit_installed "${unit}"; then
        log "${unit}: no instalado; sin cambios"
        return 0
    fi

    if systemctl is-active --quiet "${unit}"; then
        log "reiniciando ${unit}"
        run_root systemctl restart "${unit}"
        if ! ${DRY_RUN}; then
            systemctl is-active --quiet "${unit}" || die "${unit} no quedó activo tras reinicio"
        fi
    else
        log "${unit}: instalado pero inactivo; se respeta su estado"
    fi
}

restart_timer_or_path_if_active() {
    local unit="$1"
    if unit_installed "${unit}" && systemctl is-active --quiet "${unit}"; then
        log "reiniciando ${unit} por cambio de definición"
        run_root systemctl restart "${unit}"
    fi
}

if ${restart_farmacias}; then
    restart_if_active meshnet-farmacias-api.service
fi
if ${sync_farmacias_units}; then
    restart_timer_or_path_if_active meshnet-farmacias-daily.timer
    restart_timer_or_path_if_active meshnet-farmacias-check.timer
fi

if ${restart_emergencias}; then
    restart_if_active meshnet-emergencias-api.service
fi
if ${sync_emergencias_units}; then
    restart_timer_or_path_if_active meshnet-emergencias-check.timer
    restart_timer_or_path_if_active meshnet-emergencias-public-map.timer
    restart_timer_or_path_if_active meshnet-emergencias-public-map.path
fi

if ${restart_control_panel}; then
    restart_if_active meshnet-control-panel.service
fi
if ${restart_mobile_api}; then
    restart_if_active meshnet-mobile-api.service
fi
if ${restart_voice_rf}; then
    restart_if_active meshnet-voice-rf.service
fi

if ! ${SKIP_DOCKER}; then
    log "estado final de contenedores"
    run docker compose -f "${COMPOSE_FILE}" ps
fi

log "actualización coordinada completada"
