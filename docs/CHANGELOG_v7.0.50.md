# MeshNet-Bot v7.0.50

Fecha: 2026-08-11

## Objetivo

Añadir al ControlPanel control explícito, por categoría de emergencia, sobre las
salidas automáticas **APRS-IS** y **APRS RF**, sin modificar la lógica de
propagación Mesh que ya funciona.

## ControlPanel

La matriz **Emergencias → Propagación** conserva sin cambios funcionales las
columnas:

- Baja;
- Media;
- Alta;
- Crítica.

Estas cuatro columnas continúan determinando la elegibilidad de cada combinación
categoría/severidad para el flujo Mesh existente.

Se añaden al final dos columnas nuevas e independientes:

- **APRS-IS**;
- **APRS RF**.

Las nuevas columnas autorizan únicamente la categoría para la salida secundaria
correspondiente. No seleccionan MeshCore/Meshtastic, ya que esa decisión continúa
en el selector de transporte global existente.

## Persistencia

El ControlPanel reutiliza sus helpers atómicos de `.env` y guarda las selecciones
en dos variables independientes:

```env
EMERGENCIAS_APRSIS_CATEGORIES=road_closed,traffic_collision
EMERGENCIAS_APRS_RF_CATEGORIES=road_closed
```

Semántica de compatibilidad:

- variable inexistente: se conserva el comportamiento anterior a v7.0.50;
- variable presente con lista: sólo se autorizan esas categorías;
- variable presente y vacía: no se autoriza ninguna categoría para esa salida.

## Dispatcher

`emergency_dispatcher.py` incorpora una única comprobación común reutilizable de
categoría antes de cada salida APRS.

Orden de decisión para APRS RF:

1. autorizaciones generales APRS existentes;
2. categoría autorizada por `EMERGENCIAS_APRS_RF_CATEGORIES`;
3. `EMERGENCIAS_APRS_RF_MIN_LEVEL`;
4. preview, límite de partes, deduplicación y transmisión históricos.

Orden equivalente para APRS-IS:

1. autorizaciones generales APRS/APRS-IS existentes;
2. categoría autorizada por `EMERGENCIAS_APRSIS_CATEGORIES`;
3. `APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL`;
4. controles y transmisión del boletín existentes.

Una categoría no autorizada devuelve `category_not_allowed` y queda registrada
por el journal como salida omitida, sin contactar con el gateway APRS.

## Garantías de compatibilidad

No se modifica:

- `route_event()`;
- la clasificación `EMERG` / `SERV` / `METEO`;
- `send_route()`;
- el selector MeshCore / Meshtastic / ambos;
- canales Mesh;
- formateadores de mensajes;
- APRS RF preview/troceado;
- APRS-IS `MIN_INTERVAL`;
- deduplicación;
- tratamiento especial de estados terminales;
- Voz RF;
- esquema SQLite ni auditoría de entregas.

Marcar APRS-IS o APRS RF tampoco convierte una incidencia `SERV` en `EMERG`.
Las salidas secundarias continúan ejecutándose únicamente desde el flujo
`emergencias` que ya existía.

## Implementación segura del ControlPanel

Para minimizar regresiones, la funcionalidad se incorpora mediante
`tools/ControlPanel/aprs_category_matrix.py`, una extensión que reutiliza la app
FastAPI existente, la autenticación, los endpoints históricos de filtros y la
CLI actual.

El módulo sustituye exclusivamente las rutas GET/PUT de filtros en tiempo de
arranque y delega el guardado de la matriz de severidades al endpoint original.
Los clientes antiguos que no envían `secondary_transports` siguen funcionando y
no alteran las listas APRS.

## Validación

Se añaden pruebas específicas para comprobar:

- compatibilidad cuando las nuevas variables no existen;
- listas vacías;
- independencia APRS-IS / APRS RF;
- bloqueo antes de contactar con el gateway;
- conservación independiente de los `MIN_LEVEL`;
- renderizado de las dos columnas en ControlPanel;
- persistencia atómica de ambas listas;
- regresiones existentes de ControlPanel y delivery audit.
