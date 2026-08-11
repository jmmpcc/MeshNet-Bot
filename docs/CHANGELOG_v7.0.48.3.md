# MeshNet-Bot v7.0.48.3

Fecha: 2026-08-11

## Corrección

- Corregida la columna **Medios** de `Mensajes emitidos` en el ControlPanel.
- La fila principal muestra ahora exclusivamente transportes cuya entrega física quedó registrada como `sent`.
- Los transportes evaluados pero omitidos (`skipped`), duplicados (`duplicate`), limitados (`rate_limited`) o fallidos (`failed`) dejan de presentarse como si hubieran transmitido.
- Se conserva `attempted_transports` con todos los medios evaluados para diagnóstico y compatibilidad informativa.
- El detalle desplegable de cada operación continúa mostrando todas las entregas físicas y su resultado real.
- Los filtros/facetas por transporte y la exportación CSV conservan todos los registros del journal.

## Alcance de seguridad

Esta corrección afecta únicamente a la representación agregada del journal.

No se modifica:

- `emergency_dispatcher`;
- selección o clasificación de emergencias;
- rutas `emergencias`, `servicios` o `meteo`;
- APRS RF ni APRS-IS;
- Voz RF;
- MeshCore ni Meshtastic;
- deduplicación;
- `MIN_INTERVAL`;
- formato de mensajes;
- esquema SQLite ni escritura de auditoría.

## Validación

- Se mantiene la prueba de operación parcial: MeshCore `sent` + APRS-IS `failed` produce resultado global `partial`, mostrando únicamente MeshCore como medio enviado y conservando ambos en el detalle.
- Añadida regresión para MeshCore `sent` + APRS RF/APRS-IS/Voz RF `skipped`: la fila muestra únicamente MeshCore y el detalle conserva los cuatro medios con sus estados.
