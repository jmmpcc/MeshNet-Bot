# MeshNet-Bot v7.0.51 — NASA FIRMS y coordenadas APRS

Fecha: 2026-08-11

## Objetivo

Corregir y hacer operativa la fuente NASA FIRMS sin alterar DGT, IGN, el motor común de filtros, la deduplicación ni las rutas de transporte existentes.

## NASA FIRMS

- Se mantiene `VIIRS_SNPP_NRT` como dataset configurado.
- Se normaliza la confianza VIIRS oficial:
  - `l` → `low`
  - `n` → `nominal`
  - `h` → `high`
- Una detección con confianza `h` pasa a considerarse severidad `high`, además del criterio histórico `FRP >= 100 MW`.
- Se normalizan los códigos de satélite, incluyendo `N` → `Suomi-NPP`, `N20` → `NOAA-20` y `N21` → `NOAA-21`.
- Las filas FIRMS dejan de convertirse indiscriminadamente en un aviso por píxel. Por defecto se agrupan espacial y temporalmente:
  - radio: 5 km;
  - ventana temporal: 90 minutos.
- El evento agrupado conserva:
  - centro geográfico ponderado por FRP;
  - número de detecciones;
  - FRP máximo y total;
  - confianza máxima;
  - satélites participantes.
- `cluster_enabled=false` conserva el modo diagnóstico fila=evento.
- Se mantiene `verification=satellite_detection`: FIRMS continúa representándose como detección térmica satelital y no como incendio confirmado oficialmente.

## APRS-IS

- El boletín automático continúa limitado a un cuerpo APRS clásico de 67 caracteres.
- Para `nasa_firms/wildfire`, las coordenadas pasan a ser el primer dato después de `INCENDIO SAT`.
- La prioridad queda:
  1. estado y tipo;
  2. coordenadas;
  3. número de detecciones;
  4. FRP máximo;
  5. confianza;
  6. satélite, si cabe.
- No se habilitan boletines largos automáticos incompatibles con clientes que esperan el formato APRS clásico.

Ejemplo:

```text
EMERG INCENDIO SAT 42.4235,-0.7543 DET 12 FRP 150MW CONF H
```

## APRS RF

- Para fuentes distintas de FIRMS se mantiene exactamente el límite histórico de 67 caracteres.
- FIRMS puede construir un resumen RF más completo, con 160 caracteres por defecto mediante:

```env
EMERGENCIAS_APRS_RF_FIRMS_TEXT_MAX_CHARS=160
```

- El gateway APRS existente realiza el troceado RF real.
- Se mantiene como barrera dura `EMERGENCIAS_APRS_RF_MAX_CHUNKS` —3 por defecto—.
- Si el resumen FIRMS superase ese número, el dispatcher vuelve a compactarlo y previsualizarlo antes de transmitir.

Ejemplo lógico antes del troceado:

```text
EMERG INCENDIO SAT 42.4235,-0.7543 DET 12 FRP 150MW CONF H Suomi-NPP FRP TOT 430.5MW
```

## Compatibilidad

No se modifican:

- DGT DATEX;
- IGN;
- `event_matches()` / `_area_matches()`;
- Control Panel y su matriz de categorías APRS;
- deduplicación;
- `MIN_INTERVAL` de boletines;
- resolución de estados terminales;
- MeshCore;
- Meshtastic;
- Voice RF;
- gateway KISS/APRS ni sus algoritmos de troceado.

## Validación

- Compilación Python de los tres módulos modificados: correcta.
- Suite existente de `emergencias_guardia`: 35 pruebas superadas.
- Nuevas regresiones v7.0.51:
  - confianza `h`;
  - nombre de satélite;
  - agrupación espacial/temporal;
  - modo diagnóstico sin agrupación;
  - coordenadas FIRMS dentro de 67 caracteres;
  - detalle RF extendido;
  - multipart RF respetando el máximo de tramas;
  - APRS-IS clásico manteniendo coordenadas.
