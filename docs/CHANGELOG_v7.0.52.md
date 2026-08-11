# MeshNet-Bot v7.0.52 — Referencia de población cercana NASA FIRMS

Fecha: 11/08/2026

## Objetivo

Añadir una referencia geográfica humana útil a los eventos NASA FIRMS ya aceptados, manteniendo siempre las coordenadas como dato operativo principal y sin alterar filtros, deduplicación, clasificación, DGT, IGN terremotos ni transportes existentes.

## Contexto de la corrección

La primera implementación de v7.0.52 intentaba resolver el término municipal mediante la colección `administrativeunit` del IGN. Las pruebas reales mostraron dos inconvenientes:

1. una consulta con geometrías completas podía superar 140 MB;
2. focos próximos a fronteras administrativas podían intersectar más de un municipio.

Para evitar atribuciones administrativas dudosas, la referencia humana pasa a basarse en el núcleo de población IGN/INE más cercano.

## Cambios

### `geo_admin.py`

- Se conserva sin cambios funcionales `resolve_province()` y `enrich_event_province()`.
- Se añade `resolve_nearest_population()` usando la colección oficial `nuc` del IGN/INE.
- Se prueban radios crecientes de 5, 15 y 30 km por defecto.
- Se usa `skipGeometry=true` y se solicitan sólo los atributos necesarios: `nombre`, `latitud`, `longitud`, `habitantes`, `cpro` y `codine`.
- La distancia final se calcula localmente mediante Haversine.
- Se selecciona el núcleo realmente más cercano dentro del radio máximo configurado.
- La resolución es `best-effort`: cualquier error HTTP, timeout, respuesta inválida o ausencia de candidatos devuelve `None`.
- El resultado se guarda en `metadata` y NO se rellena `Event.municipality` con una población cercana.
- Variables nuevas opcionales:
  - `EMERGENCIAS_GEO_POPULATION_ENABLED` (1 por defecto)
  - `EMERGENCIAS_GEO_POPULATION_TIMEOUT_SEC` (2.5 por defecto)
  - `EMERGENCIAS_GEO_POPULATION_MAX_RADIUS_KM` (30 por defecto)
  - `EMERGENCIAS_GEO_POPULATION_ENDPOINT`

### `engine.py`

- El enriquecimiento geográfico sigue ejecutándose únicamente para `source_id == "nasa_firms"`.
- Se ejecuta DESPUÉS de `event_matches()`: el IGN nunca decide si una emergencia entra o sale del sistema.
- Ninguna otra fuente llama al resolver de población cercano.

### `formatters.py`

- El formato APRS FIRMS conserva siempre las coordenadas como primer dato operativo.
- Si existe referencia cercana, añade `CERCA <población>`.
- En APRS-IS clásico (67 caracteres), el nombre se compacta por palabras completas para evitar salidas incompletas como `CERCA Salinas de`.
- En RF ampliado se añade el nombre completo, provincia y distancia cuando caben.
- Sin población cercana se mantiene el formato operativo v7.0.51 basado en coordenadas.

## Prueba real Raspberry Pi

Con coordenadas FIRMS reales:

- `42.4407287,-0.7678461` → provincia `Huesca` → núcleo cercano `Salinas de Jaca` → distancia aproximada `3.6 km`.
- `42.4421958,-0.7610995` → provincia `Huesca` → núcleo cercano `Salinas de Jaca` → distancia aproximada `4.04 km`.

El campo `Event.municipality` permanece vacío en ambos casos, como debe ocurrir cuando sólo conocemos un núcleo cercano y no una pertenencia administrativa inequívoca.

Ejemplo APRS-IS tras compactación por palabras completas:

```text
EMERG INCENDIO SAT 42.4407,-0.7678 CERCA Salinas DET 42
```

Ejemplo APRS RF ampliado:

```text
EMERG INCENDIO SAT 42.4407,-0.7678 CERCA Salinas de Jaca,Huesca 3.6km DET 42 FRP 17.89MW CONF N Suomi-NPP FRP TOT 176.48MW
```

## Compatibilidad y seguridad

- No se modifica DGT DATEX.
- No se modifica el parser IGN de terremotos.
- No se modifica `event_matches()` ni `_area_matches()`.
- No se modifica deduplicación ni `MIN_INTERVAL`.
- No se modifica agrupamiento FIRMS.
- No se modifica dispatcher ni gateway APRS.
- No se modifica KISS ni Soundmodem.
- No se modifica MeshCore ni Meshtastic.
- No se modifica Control Panel.
- Un fallo del IGN nunca bloquea una emergencia FIRMS: las coordenadas siguen disponibles y el evento continúa su flujo normal.

## Pruebas

`tests/test_firms_locality_v7052.py` verifica:

1. aislamiento exclusivo de FIRMS;
2. selección Haversine del núcleo más cercano;
3. uso de `skipGeometry=true` y atributos mínimos;
4. fallo seguro;
5. almacenamiento en metadata sin alterar `municipality`;
6. APRS con `CERCA` + coordenadas;
7. compactación por palabras completas para `Salinas de Jaca`;
8. fallback v7.0.51.

## Fuente cartográfica

Servicio oficial OGC API-Features del Instituto Geográfico Nacional / CNIG, colección de núcleos de población `nuc`, datos IGN/INE.
