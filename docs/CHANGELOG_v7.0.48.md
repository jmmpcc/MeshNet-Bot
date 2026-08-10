# v7.0.48 — Journal común de mensajes emitidos

## Objetivo

Añadir observabilidad transversal para aplicaciones independientes sin introducir
ninguna dependencia en el camino crítico de transmisión.

## Cambios

- Nuevo `shared/delivery_audit.py` con SQLite WAL, `busy_timeout` y retención configurable.
- Cada fila representa un intento de entrega por un transporte concreto.
- `operation_id` agrupa MeshCore, Meshtastic, APRS/APRS-IS y otras salidas de una misma operación.
- Estados normalizados: `sent`, `failed`, `skipped`, `duplicate`, `rate_limited`.
- Integración best-effort en Emergencias para Mesh, APRS RF, APRS-IS y Voice RF.
- Integración best-effort en Farmacias para broadcast, respuestas DM y APRS.
- Nueva sección global **Mensajes emitidos** en ControlPanel.
- Filtros por periodo, aplicación, fuente, transporte, resultado y búsqueda libre.
- Detalle desplegable por transporte y exportación CSV.
- Retención predeterminada de 90 días.

## Seguridad operativa

El journal se escribe exclusivamente después de obtener el resultado de la salida
existente. `audit_delivery()` absorbe cualquier excepción de SQLite. No modifica
broker, gateway APRS, deduplicación, intervalos, reintentos ni decisiones de ruta.
