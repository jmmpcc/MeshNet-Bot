#!/usr/bin/env bash
set -euo pipefail

# Reproduce el timer diario histórico de las 08:30 Europe/Madrid.
export FARMACIAS_DATA_DIR="${FARMACIAS_DATA_DIR:-/app/bot_data/farmacias_guardia}"
export BROKER_CTRL_HOST="${BROKER_CTRL_HOST:-127.0.0.1}"

mkdir -p "${FARMACIAS_DATA_DIR}"

while true; do
    sleep_seconds="$(python - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

tz = ZoneInfo("Europe/Madrid")
now = datetime.now(tz)
target = now.replace(hour=8, minute=30, second=0, microsecond=0)
if target <= now:
    target += timedelta(days=1)
print(max(1, int((target - now).total_seconds())))
PY
)"
    sleep "${sleep_seconds}"
    python -u /app/tools/farmacias_guardia/farmacias_guardia_scoped.py send || true
done
