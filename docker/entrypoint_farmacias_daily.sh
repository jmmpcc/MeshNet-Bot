#!/usr/bin/env bash
set -euo pipefail

# Compatibilidad con despliegues que todavía incluyen farmacias-daily.
# El envío completo diario está desactivado: nunca se llama a "send".
# La actualización y los avisos de altas permanecen en farmacias-check.
# El contenedor continúa en reposo para no provocar bucles de reinicio.
echo "[farmacias-daily] Difusión automática del listado completo desactivada." >&2
exec sleep infinity
