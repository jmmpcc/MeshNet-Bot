# MeshNet-Bot v7.0.52 — Referencia de población cercana para NASA FIRMS

Fecha: 11/08/2026

## Objetivo

Añadir una referencia humana útil a los eventos NASA FIRMS ya aceptados, manteniendo las coordenadas como dato principal y sin alterar filtros, deduplicación, clasificación, DGT, IGN terremotos ni los transportes que ya funcionan.

## Decisión de diseño

La primera iteración intentó resolver el término municipal mediante `administrativeunit`. Las pruebas reales demostraron dos problemas:

1. una consulta con geometrías administrativas podía superar 140 MB;
2. un punto FIRMS próximo a una frontera podía intersectar más de un término municipal.

Para evitar asignaciones administrativas incorrectas, v7.0.52 utiliza la colección oficial IGN/INE `nuc` de núcleos de población y calcula localmente el núcleo más cercano mediante Haversine. La referencia se presenta como `CERCA <población>`; nunca se escribe artificialmente en `Event.municipality`.

## Cambios

### `geo_admin.py`

- Se conserva sin cambios funcionales `resolve_province()` y `enrich_event_province()`.
- Se añade `resolve_nearest_population()` sobre la colección oficial `nuc` del OGC API-Features del IGN.
- La colección proporciona `nombre`, `latitud`, `longitud`, `habitantes`, `cpro` y `codine`.
- Se consultan radios crecientes de 5 km, 15 km y un máximo configurable de 30 km por defecto.
- La consulta usa `skipGeometry=true` y solicita sólo los atributos necesarios.
- La distancia real se calcula localmente con Haversine y se selecciona el núcleo más cercano.
- La resolución es `best-effort`: error HTTP, timeout, JSON inválido o ausencia de candidatos devuelve `None`.
- Se mantiene el símbolo `enrich_event_municipality()` únicamente por compatibilidad interna con la primera iteración de la rama; ya no modifica `event.municipality` y sólo añade metadatos `nearest_population*`.

Variables nuevas opcionales:

- `EMERGENCIAS_GEO_POPULATION_ENABLED=1`
- `EMERGENCIAS_GEO_POPULATION_TIMEOUT_SEC=2.5`
- `EMERGENCIAS_GEO_POPULATION_MAX_RADIUS_KM=30`
- `EMERGENCIAS_GEO_POPULATION_ENDPOINT=https://api-features.ign.es/collections/nuc/items`

### `engine.py`

- El enriquecimiento se sigue ejecutando únicamente para `source_id == "nasa_firms"`.
- Se ejecuta DESPUÉS de `event_matches()`: la disponibilidad del IGN no interviene en la aceptación o rechazo de una emergencia.
- Sólo se consultan eventos FIRMS ya aceptados; ninguna otra fuente utiliza el resolver nuevo.

### `formatters.py`

- Las coordenadas FIRMS siguen siendo el primer dato operativo.
- Si existe `metadata['nearest_population']`, se añade `CERCA <nombre>`.
- APRS RF ampliado añade también provincia y distancia cuando caben.
- El término `CERCA` evita afirmar que el foco pertenece administrativamente al núcleo mostrado.
- Sin referencia de población se conserva el formato operativo v7.0.51 basado en coordenadas.

## Ejemplo esperado

APRS-IS clásico:

```text
EMERG INCENDIO SAT 42.4407,-0.7678 CERCA Bailo DET 42 FRP 17.89MW
```

APRS RF ampliado:

```text
EMERG INCENDIO SAT 42.4407,-0.7678 CERCA Bailo,Huesca 7.4km DET 42 FRP 17.89MW CONF N Suomi-NPP FRP TOT 176.48MW
```

La población y distancia anteriores son sólo un ejemplo de formato; el valor real se obtiene en cada ejecución desde el IGN.

## Compatibilidad y seguridad

- No se modifica DGT DATEX.
- No se modifica el parser IGN de terremotos.
- No se modifica `event_matches()` ni `_area_matches()`.
- No se modifica deduplicación ni `MIN_INTERVAL`.
- No se modifica el agrupamiento FIRMS v7.0.51.
- No se modifica el gateway APRS, KISS ni Soundmodem.
- No se modifica la matriz de categorías APRS.
- No se modifica MeshCore ni Meshtastic.
- `Event.municipality` permanece intacto.
- Un fallo del IGN nunca bloquea una emergencia FIRMS: las coordenadas siguen disponibles y el evento continúa su flujo normal.

## Pruebas

`tests/test_firms_locality_v7052.py` verifica:

1. que sólo FIRMS llama al enriquecimiento geográfico posterior al filtrado;
2. que se selecciona el núcleo más cercano mediante Haversine;
3. que la consulta solicita `skipGeometry=true` y sólo atributos necesarios;
4. que un fallo del resolver deja intacto el evento;
5. que la población cercana se guarda en metadata y no en `municipality`;
6. que APRS mantiene coordenadas y añade `CERCA` cuando hay referencia;
7. que sin población se conserva el formato v7.0.51.

## Fuente geográfica

Servicio oficial OGC API-Features del Instituto Geográfico Nacional / CNIG, colección `nuc` —núcleos de población identificados por el Instituto Nacional de Estadística—, licencia CC BY 4.0 ign.es.
