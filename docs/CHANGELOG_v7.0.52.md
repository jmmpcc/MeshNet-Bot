# MeshNet-Bot v7.0.52 — Localización municipal NASA FIRMS

Fecha: 11/08/2026

## Objetivo

Añadir una localización humana a los eventos NASA FIRMS ya aceptados, reutilizando la infraestructura geográfica existente y sin alterar filtros, deduplicación, clasificación, DGT, IGN terremotos ni los transportes que ya funcionan.

## Cambios

### `geo_admin.py`

- Se conserva sin cambios funcionales `resolve_province()` y `enrich_event_province()`.
- Se añade `resolve_municipality()` mediante la colección oficial `administrativeunit` de OGC API-Features del Instituto Geográfico Nacional.
- La consulta es `best-effort`: cualquier error HTTP, timeout, respuesta inválida o ausencia de municipio devuelve `None`.
- Se valida localmente que la coordenada está dentro de la geometría Polygon/MultiPolygon devuelta por IGN antes de aceptar `nameunit`.
- La resolución municipal puede desactivarse con `EMERGENCIAS_GEO_MUNICIPALITY_ENABLED=0`.
- Timeout configurable con `EMERGENCIAS_GEO_MUNICIPALITY_TIMEOUT_SEC` (2.5 s por defecto).
- Endpoint configurable para pruebas mediante `EMERGENCIAS_GEO_MUNICIPALITY_ENDPOINT`.

### `engine.py`

- El enriquecimiento municipal se ejecuta únicamente para `source_id == "nasa_firms"`.
- Se ejecuta DESPUÉS de `event_matches()`: la disponibilidad del servicio IGN no interviene en la aceptación o rechazo de una emergencia.
- Sólo se consultan los eventos FIRMS que ya pasaron los filtros, evitando consultas innecesarias para todos los grupos satelitales recibidos.
- Ninguna otra fuente llama al resolver municipal nuevo.

### `formatters.py`

- El formato APRS FIRMS mantiene las coordenadas como primer dato operativo.
- Si hay municipio, se añade inmediatamente después de las coordenadas.
- En APRS-IS clásico (67 caracteres) se limita el nombre municipal a 12 caracteres para preservar espacio para DET/FRP/CONF.
- En APRS RF ampliado se usa el nombre completo y se añade la provincia cuando existe.
- Si el municipio no puede resolverse, se mantiene exactamente el formato operativo v7.0.51 basado en coordenadas.

## Ejemplo esperado

APRS-IS:

```text
EMERG INCENDIO SAT 42.4407,-0.7678 Bailo DET 42 FRP 17.89MW CONF N
```

APRS RF ampliado:

```text
EMERG INCENDIO SAT 42.4407,-0.7678 Bailo,Huesca DET 42 FRP 17.89MW CONF N Suomi-NPP FRP TOT 176.48MW
```

## Compatibilidad y seguridad

- No se modifica DGT DATEX.
- No se modifica el parser IGN de terremotos.
- No se modifica `event_matches()` ni `_area_matches()`.
- No se modifica deduplicación ni `MIN_INTERVAL`.
- No se modifica el gateway APRS, KISS ni Soundmodem.
- No se modifica la matriz de categorías APRS.
- No se modifica MeshCore ni Meshtastic.
- Un fallo del IGN nunca bloquea una emergencia FIRMS: las coordenadas siguen disponibles y el evento continúa su flujo normal.

## Pruebas añadidas

`tests/test_firms_locality_v7052.py` verifica:

1. que sólo FIRMS llama al enriquecimiento municipal;
2. que una respuesta OGC válida resuelve `nameunit` mediante point-in-polygon;
3. que un fallo del resolver deja intacto el evento;
4. que APRS incluye municipio y coordenadas cuando la localización está disponible;
5. que sin municipio se conserva el formato v7.0.51.

## Fuente cartográfica

Servicio oficial OGC API-Features del Instituto Geográfico Nacional / CNIG, colección de Unidades Administrativas (`administrativeunit`), CC BY 4.0 ign.es.
