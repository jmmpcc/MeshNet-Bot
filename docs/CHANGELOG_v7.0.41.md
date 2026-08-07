# CHANGELOG v7.0.41 — APRS RF de Emergencias: fragmentación real y compactación segura

## Cambios

- Nuevo modo UDP `aprs_preview` en el gateway APRS.
- El dispatcher de Emergencias consulta al gateway el número real de partes antes de RF.
- Límite específico por defecto de 3 partes mediante `EMERGENCIAS_APRS_RF_MAX_CHUNKS`.
- Compactación automática reutilizando `compact_messages()` cuando el texto original excede el límite.
- Segunda validación obligatoria antes de transmitir el resumen.
- Metadatos de diagnóstico: `original_parts`, `rf_parts`, `max_chunks` y `compacted`.

## Compatibilidad

- No se modifica `APPS_APRS_MAX_CHUNKS` de otras aplicaciones.
- No se cambia APRS-IS, Mesh, Voice RF, KISS, rutas AX.25 ni deduplicación.
- Los mensajes que ya cabían en 1-3 partes se transmiten sin modificar.
