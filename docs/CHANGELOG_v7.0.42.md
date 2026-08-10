# CHANGELOG v7.0.42 — 2026-08-10

## Objetivo

Cerrar correctamente el ciclo de vida de las emergencias en APRS-IS y reducir
el texto APRS sin perder el tipo de incidencia.

## Cambios

1. Los estados `resolved`, `cancelled`, `expired` y `closed` saltan únicamente
   `APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC`.
2. `APRSIS_EMERGENCY_BULLETIN_DEDUP_SEC` permanece activo y se evalúa antes.
3. El grupo de boletines continúa siendo configurable: variable vacía ->
   `BLN0..BLN9`; con `EMERG` -> `BLN0EMERG..BLN9EMERG`.
4. `.env_example` deja el grupo de Emergencias vacío por defecto.
5. Nuevo `aprs_emergency_text()` para APRS RF y APRS-IS. Genera ASCII de hasta
   67 caracteres y prioriza estado, categoría, carretera/km y municipio.
6. Nueva variable `EMERGENCIAS_APRS_TEXT_MAX_CHARS=67`.
7. No se modifica el texto que se envía por Meshtastic/MeshCore.

## Ejemplos

```text
EMERG INCENDIO | A-23 km 312 | Zuera
CRIT COLISION | A-2 km 315 | La Muela
FIN CORTE VIA | N-330 km 22 | Carinena
```

## Validación

- Alta seguida de cierre en segundos: el cierre se publica sin `rate_limited`.
- Segundo cierre idéntico: se bloquea como `duplicate`.
- Probados los cuatro estados terminales.
- Probados boletines estándar y agrupados.
- Suite dirigida v7.0.42: 60 pruebas correctas.
