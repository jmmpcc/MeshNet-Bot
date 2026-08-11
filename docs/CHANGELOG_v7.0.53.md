# MeshNet-Bot v7.0.53 — Geolocalización ligera de `/ver_nodos` y `/vecinos`

Fecha: 11/08/2026

## Objetivo

Eliminar la dependencia externa `reverse_geocoder`, que introducía NumPy/SciPy y un coste elevado de compilación en imágenes ARM, manteniendo sin cambios funcionales los comandos `/ver_nodos` y `/vecinos`.

La fase reutiliza la infraestructura geográfica validada en v7.0.52:

- `resolve_nearest_population()` para obtener el núcleo IGN/INE más cercano;
- `resolve_province()` para fallback provincial local;
- cartografía `provincias_espana.geojson` ya existente.

## Diseño de compatibilidad

No se reescriben los handlers históricos de Telegram.

`Telegram_Bot_Broker.py` ya usa imports diferidos:

```python
import reverse_geocoder as rg
rg.search(...)
```

v7.0.53 añade `source/reverse_geocoder.py`, un shim compatible con esa API. Al ejecutarse el bot desde `/app/source`, Python carga el módulo local y las funciones existentes continúan funcionando sin cambios en sus call-sites.

El shim acepta las dos formas presentes en el código actual:

```python
rg.search((lat, lon))
rg.search([(lat, lon)])
```

La respuesta mantiene las claves consumidas por el bot:

- `name`: núcleo de población IGN/INE más cercano;
- `admin2`: provincia resuelta con cartografía local;
- `admin1`: reservado por compatibilidad.

También conserva `lat`, `lon`, `cc` y añade metadatos no disruptivos de distancia, habitantes y CODINE.

## Fallo seguro

La resolución de población es `best-effort`.

Si el servicio IGN no está disponible:

1. no se propaga ninguna excepción al handler;
2. se mantiene la resolución provincial local;
3. `/ver_nodos` y `/vecinos` siguen mostrando el nodo y sus métricas;
4. distancia, hops, RSSI, SNR, alias y resto de comportamiento no se modifican.

La consulta remota puede desactivarse con:

```text
BOT_GEO_LOOKUP_ENABLED=0
```

Ajustes opcionales:

```text
BOT_GEO_LOOKUP_TIMEOUT_SEC=1.2
BOT_GEO_LOOKUP_MAX_RADIUS_KM=30
```

El shim mantiene una caché LRU de 1024 coordenadas para que ejecuciones posteriores de `/ver_nodos` y `/vecinos` no repitan consultas de posiciones ya resueltas dentro del mismo proceso.

## Docker / compilación

Se elimina `requirements/requirements.geo.txt`, cuyo único cometido era instalar `reverse_geocoder`.

El `Dockerfile` deja de instalar el stack específico necesario para SciPy/reverse-geocoder:

- `gfortran`;
- `libopenblas-dev`;
- `liblapack-dev`.

Se mantienen deliberadamente:

- `build-essential`;
- `python3-dev`;
- `pkg-config`;
- `libssl-dev`;
- `libffi-dev`;
- `rustc`;
- `cargo`.

Esas herramientas pueden seguir siendo necesarias en ARM por otras dependencias y no se eliminan sin una validación independiente.

La imagen copia únicamente el subconjunto geográfico que necesita el bot:

- `emergencias/__init__.py`;
- `emergencias/models.py`;
- `emergencias/geo_admin.py`;
- `data/provincias_espana.geojson`.

Además se ejecuta durante el build un smoke test sin red (`BOT_GEO_LOOKUP_ENABLED=0`) para garantizar que el shim y el fallback provincial están correctamente empaquetados.

## Compatibilidad funcional

No se modifica:

- `ver_nodos_cmd()`;
- `vecinos_cmd()`;
- filtros de hops;
- cálculo de distancia al HOME;
- RSSI/SNR y clasificación de calidad;
- alias e índices de nodo;
- acceso al broker;
- Meshtastic o MeshCore;
- Control Panel / WebAdmin;
- APRS / APRS-IS;
- Emergencias FIRMS.

## Regresiones

`tests/test_node_geolocation_v7053.py` comprueba:

1. compatibilidad con `rg.search((lat, lon))`;
2. compatibilidad con `rg.search([(lat, lon)])`;
3. núcleo de población + provincia;
4. fallback provincial si IGN falla;
5. desactivación de red sin perder provincia;
6. caché de posiciones repetidas;
7. ausencia de `requirements.geo.txt`;
8. ausencia de `gfortran`, OpenBLAS y LAPACK en el Dockerfile;
9. empaquetado del resolver y GeoJSON provincial.
