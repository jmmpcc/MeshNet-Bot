# MeshNet-Bot v7.0.38 — Catálogo de grupos APRS-IS

## Añadido

- Catálogo central de grupos APRS-IS: `EMERG`, `AEMET`, `FARMA`, `NEWS`, `MESH` y `TEST`.
- Variables de entorno reservadas para cada familia de boletines.
- Helper común de resolución y normalización por fuente.
- Pruebas de catálogo y comportamiento seguro para fuentes desconocidas.

## Compatibilidad

- Solo Emergencias publica automáticamente en esta versión.
- Las nuevas variables no activan tráfico ni modifican AEMET, Farmacias, Noticias o el sistema.
- El grupo vacío de Emergencias conserva `BLN0..BLN9`.
