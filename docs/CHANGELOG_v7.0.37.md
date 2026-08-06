# MeshNet-Bot v7.0.37 — grupos APRS-IS para boletines de Emergencias

Fecha: 6 de agosto de 2026.

## Cambios

- Añadida `APRSIS_EMERGENCY_BULLETIN_GROUP`.
- Grupo normalizado a un máximo de cinco caracteres alfanuméricos.
- Compatibilidad total: variable vacía conserva `BLN0` a `BLN9`.
- `EMERG` genera `BLN0EMERG` a `BLN9EMERG`.
- Migración estable del número de boletín al activar o cambiar el grupo.
- El cambio de grupo no queda bloqueado por deduplicación ni rate limit previos.
- Sin cambios en APRS RF, Mesh, `/aprsis_push`, Voice RF ni Emergency Dispatcher.
- Actualizados tests, `.env_example`, README principal y guía APRS.
