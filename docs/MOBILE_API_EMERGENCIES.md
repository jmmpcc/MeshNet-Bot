# Mobile API — incidencias de Emergencias

## Objetivo

Exponer a MeshNet-Mobile la instantánea actual de `emergencias_guardia` sin duplicar datos ni introducir operaciones mutantes.

## Endpoint

`GET /api/v1/emergencies`

Parámetros opcionales:

- `source`: fuente exacta.
- `severity`: `low`, `medium`, `high` o `critical`.
- `status`: estado exacto del evento.
- `q`: búsqueda textual en identificador, fuente, título, descripción, carretera, municipio, provincia y comunidad autónoma.
- `limit`: entre 1 y 500; por defecto 200.

## Fuente de verdad

El endpoint reutiliza `tools.emergencias_guardia.emergencias.storage.load_current()` y, por tanto, lee exactamente el mismo `current.json` que mantiene el motor de Emergencias.

No ejecuta una nueva recogida, no recalcula severidades, no modifica deduplicación y no dispara notificaciones.

## Respuesta

La respuesta contiene:

- `events`: eventos completos serializados mediante `Event.to_dict()`;
- `summary.total`;
- `summary.with_coordinates`;
- resumen por severidad;
- `limit`;
- `has_more`.

Las coordenadas `latitude` y `longitude` se mantienen para que MeshNet-Mobile pueda incorporarlas al mapa en una fase posterior sin cambiar de nuevo el contrato.

## Capabilities

`mobile_api_v7054.py` declara:

- `emergencies_read: true`;
- `emergencies_coordinates: true`.

La aplicación Android debe comprobar `emergencies_read` antes de mostrar el acceso a la pantalla.

## Seguridad

El endpoint hereda la autenticación Bearer de Mobile API. No expone `.env`, claves, configuraciones privadas ni operaciones de escritura.
