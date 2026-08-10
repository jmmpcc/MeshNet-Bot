# CHANGELOG v7.0.45

## Objetivo

Permitir que fuentes que aportan coordenadas pero no provincia, especialmente IGN y NASA FIRMS, utilicen las provincias seleccionadas en `emergencias_guardia` sin eliminar ni alterar el filtro por radio existente.

## Cambios

- Nuevo helper `tools/emergencias_guardia/emergencias/geo_admin.py`.
- Resolución local de provincia mediante geometrías provinciales derivadas de cartografía IGN/CNIG.
- Implementación Python puro de `Polygon`/`MultiPolygon`, huecos y point-in-polygon; no se añaden `shapely`, `geopandas`, `fiona` ni `pyproj`.
- La resolución solo se ejecuta si existe al menos un área de tipo `province` habilitada.
- Solo se enriquecen eventos con `province` vacía y coordenadas válidas.
- Una provincia proporcionada por DGT, Ayuntamiento de Zaragoza u otra fuente nunca se sobrescribe.
- El `raw_hash` de la fuente no se altera al añadir la provincia derivada, evitando falsos cambios `updated`.
- Si falta o está corrupta la cartografía local, se conserva el comportamiento anterior y el radio geográfico continúa funcionando.
- Se mantiene exactamente la semántica existente de áreas: provincia, municipio y radio se combinan mediante OR.
- ControlPanel: el radio pasa a describirse como cobertura adicional, no como requisito para IGN/FIRMS.

## Cartografía

`tools/emergencias_guardia/data/provincias_espana.geojson` contiene 52 unidades provinciales/ciudades autónomas para resolución local. El fichero se genera a partir de `es-atlas 0.6.0`, cuyo origen cartográfico es IGN/CNIG NGBE. Los datos fuente se distribuyen bajo CC-BY 4.0.

La cartografía se carga una sola vez por proceso y se prefiltra por bounding-box antes del cálculo point-in-polygon.

## Compatibilidad

No se modifica la lógica de `_area_matches()` ni `event_matches()`. Tampoco se modifican conectores DGT, Zaragoza, IGN, FIRMS, AEMET, APRS, MeshCore o Meshtastic.

Con provincias Zaragoza, Huesca y Teruel más un radio de 100 km, un evento se acepta si pertenece a cualquiera de esas provincias **o** si cae dentro del radio, igual que antes. La diferencia es que IGN/FIRMS ahora pueden obtener `Event.province` desde sus coordenadas antes de aplicar ese filtro.
