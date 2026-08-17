# MeshNet-Bot v7.0.59 — Alerta temprana y evolución NASA FIRMS

## Objetivo

Reducir al mínimo el desfase de los avisos de focos térmicos NASA FIRMS sin eliminar ni sustituir la agrupación espacial/temporal implantada en v7.0.51.

## Cambios

- Se mantiene intacto `FirmsSource` como parser y agrupador FIRMS validado.
- El tipo de fuente `firms` pasa por `FirmsTrackedSource`, una subclase que añade continuidad entre pasadas satelitales.
- Una primera detección válida puede convertirse inmediatamente en un evento con `firms_phase=initial` y texto `Inicio de posible foco de incendio satelital`.
- Pasadas posteriores próximas en espacio y tiempo conservan el mismo `event_id`, evitando tratarlas como incendios independientes.
- La evolución se evalúa por cuatro señales:
  - aumento del número de detecciones;
  - crecimiento significativo de FRP total;
  - crecimiento significativo de la extensión térmica observada;
  - aumento de confianza FIRMS.
- Cuando existe crecimiento se establece `firms_phase=growth` y el evento pasa a `Aumento del foco de incendio satelital`.
- Cuando no existe crecimiento significativo se establece `firms_phase=stable`, se actualizan los metadatos de seguimiento y se conserva el `raw_hash` anterior para no provocar una notificación redundante.
- La extensión del foco se calcula como el diámetro Haversine máximo entre detecciones del cluster de la pasada.
- Se mantienen `verification=satellite_detection`, los filtros geográficos, la matriz de propagación, la deduplicación, la resolución tras ausencias y todas las salidas secundarias existentes.

## Valores predeterminados

```text
incident_tracking_enabled = true
incident_radius_km = 8.0
incident_max_gap_hours = 24.0
growth_frp_ratio = 0.25
growth_frp_min_mw = 5.0
growth_extent_ratio = 0.20
growth_extent_min_km = 0.5
```

El crecimiento por FRP o extensión exige simultáneamente superar el incremento relativo y el incremento absoluto configurados, reduciendo avisos producidos por pequeñas oscilaciones instrumentales.

## Compatibilidad

- `tools/emergencias_guardia/emergencias/sources/firms.py` no se modifica.
- Las pruebas históricas que importan `FirmsSource` continúan ejercitando el conector original.
- `SOURCE_TYPES["firms"]` utiliza la nueva subclase únicamente en el flujo operativo.
- La ruta `emergencias` mantiene `batch_window_seconds=0`; no se introduce ningún bypass ni temporizador adicional.

## Pruebas añadidas

`tests/test_firms_early_growth_v7059.py` cubre:

- primera detección individual;
- continuidad del `event_id` entre pasadas;
- crecimiento por detecciones, FRP y extensión;
- estabilidad sin cambio de `raw_hash`;
- filtrado de pequeñas oscilaciones de FRP;
- separación de focos alejados;
- creación de un foco nuevo al superar la ventana temporal;
- conservación de la ventana cero de la ruta de emergencias.
