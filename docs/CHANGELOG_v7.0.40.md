# MeshNet-Bot v7.0.40

## Corrección operativa

- El control UDP APRS sigue publicado únicamente en `127.0.0.1:9464/udp`.
- `meshnet-emergencias-check.service` carga ahora el `.env` principal antes del `.env` local de Emergencias.
- Las autorizaciones APRS/APRS-IS configuradas en el proyecto llegan realmente al proceso `check --notify-changes`.
- El `.env` local de Emergencias se carga después y mantiene prioridad.
- No se altera MeshCore, Meshtastic, APRS RF, APRS-IS ni la API de Emergencias.
