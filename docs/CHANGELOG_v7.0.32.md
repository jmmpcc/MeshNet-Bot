# MeshNet-Bot v7.0.32 — Fase 2A

## Boletines públicos APRS-IS para emergencias

- Añade publicación opcional de emergencias `high`/`critical` como boletines `BLN0`–`BLN9`.
- Reutiliza la conexión APRS-IS persistente del gateway.
- No transmite estas publicaciones por RF ni utiliza `APRSIS_PUSH_TO`.
- Mantiene líneas BLN estables por `event_id`, deduplicación y rate limit persistentes.
- La salida queda desactivada por defecto y requiere doble autorización.
- Los fallos APRS-IS no revierten ni repiten envíos Mesh correctos.
