#!/usr/bin/env bash
set -e

# Valores por defecto (se pueden sobreescribir desde .env o docker-compose)
: "${WEB_BIND:=0.0.0.0}"
: "${WEB_PORT:=8080}"

# Variables del broker (si no vienen del .env, usamos estos defaults)
: "${BROKER_HOST:=broker}"           # nombre del servicio broker en la red compose
: "${BROKER_PORT:=8765}"
: "${BROKER_CTRL_HOST:=${BROKER_HOST}}"
: "${BROKER_CTRL_PORT:=$((BROKER_PORT+1))}"

# Info
echo "[web] Arrancando panel en http://${WEB_BIND}:${WEB_PORT} -> broker=${BROKER_HOST}:${BROKER_PORT} (ctrl=${BROKER_CTRL_HOST}:${BROKER_CTRL_PORT})"

# Lanzamos Uvicorn contra el objeto `app` definido en web_admin.py
exec uvicorn web_admin:app --host "${WEB_BIND}" --port "${WEB_PORT}" --no-server-header --proxy-headers
