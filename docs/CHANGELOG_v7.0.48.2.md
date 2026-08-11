# MeshNet-Bot v7.0.48.2

Fecha: 2026-08-11

## Corrección

- Corregido el arranque directo de `tools/emergencias_guardia/emergencias_guardia.py` cuando es invocado por el ControlPanel.
- El punto de entrada añade explícitamente la raíz de MeshNet-Bot a `sys.path` antes de cargar `emergencias.cli`.
- Se resuelve `ModuleNotFoundError: No module named 'shared'` al abrir o guardar la matriz de propagación.
- No se modifica la lógica de la matriz, los filtros por severidad, `emergency_dispatcher`, la deduplicación ni los canales de salida.

## Validación

- Añadida prueba de regresión que ejecuta el CLI por ruta con `PYTHONPATH` eliminado deliberadamente.
- La prueba verifica que el CLI puede completar sus imports de `notifier`, `emergency_dispatcher` y `shared.delivery_audit` antes de mostrar `--help`.
