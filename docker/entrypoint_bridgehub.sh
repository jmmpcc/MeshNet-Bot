#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# Log mínimo del entorno efectivo
# ─────────────────────────────────────────────
echo "[bridgehub] MODE=${HUB_MODE:-tcp}"
echo "[bridgehub] A=${A_HOST:-via-broker}:${A_PORT:-4403}  B=${B_HOST:-?}:${B_PORT:-4403}  C=${C_HOST:-?}:${C_PORT:-4403}"
echo "[bridgehub] BROKER_CTRL=${BROKER_CTRL_HOST:-none}:${BROKER_CTRL_PORT:-8766}"

# ─────────────────────────────────────────────
# Construcción dinámica de argumentos
# (solo se pasan si existen)
# ─────────────────────────────────────────────
ARGS=()

[[ -n "${HUB_MODE:-}" ]]              && ARGS+=( "--mode" "$HUB_MODE" )

[[ -n "${A_HOST:-}" ]]                && ARGS+=( "--a-host" "$A_HOST" )
[[ -n "${A_PORT:-}" ]]                && ARGS+=( "--a-port" "$A_PORT" )

[[ -n "${B_HOST:-}" ]]                && ARGS+=( "--b-host" "$B_HOST" )
[[ -n "${B_PORT:-}" ]]                && ARGS+=( "--b-port" "$B_PORT" )

[[ -n "${C_HOST:-}" ]]                && ARGS+=( "--c-host" "$C_HOST" )
[[ -n "${C_PORT:-}" ]]                && ARGS+=( "--c-port" "$C_PORT" )

[[ -n "${BROKER_CTRL_HOST:-}" ]]       && ARGS+=( "--broker-ctrl-host" "$BROKER_CTRL_HOST" )
[[ -n "${BROKER_CTRL_PORT:-}" ]]       && ARGS+=( "--broker-ctrl-port" "$BROKER_CTRL_PORT" )

[[ -n "${FORWARD_TEXT:-}" ]]           && ARGS+=( "--forward-text" "$FORWARD_TEXT" )
[[ -n "${FORWARD_POSITION:-}" ]]       && ARGS+=( "--forward-position" "$FORWARD_POSITION" )
[[ -n "${REQUIRE_ACK:-}" ]]            && ARGS+=( "--require-ack" "$REQUIRE_ACK" )

[[ -n "${RATE_LIMIT_PER_SIDE:-}" ]]    && ARGS+=( "--rate-limit" "$RATE_LIMIT_PER_SIDE" )
[[ -n "${DEDUP_TTL:-}" ]]              && ARGS+=( "--dedup-ttl" "$DEDUP_TTL" )
[[ -n "${TAG_BRIDGE:-}" ]]             && ARGS+=( "--tag" "$TAG_BRIDGE" )

# ─────────────────────────────────────────────
# Ejecución REAL del proceso del contenedor
# (PID 1, sin shell intermedio)
# ─────────────────────────────────────────────
exec python /app/mesh_triple_bridge_brokerhub.py "${ARGS[@]}"
