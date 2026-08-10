# Emergencias Guardia

Aplicación Python independiente que agrega incidencias oficiales, las normaliza,
mantiene una copia local y ofrece una API HTTP para consultas. Está inspirada en
`farmacias_guardia`, pero no importa código del broker ni altera servicios
existentes.

Esta fase mantiene únicamente biblioteca estándar de Python y aporta:

- CLI para fuentes, áreas, categorías, consulta, diagnóstico e histórico.
- API local en el puerto `8789`.
- DGT DATEX II para tráfico y carreteras.
- API municipal JSON/GeoJSON del Ayuntamiento de Zaragoza.
- terremotos del RSS/GeoRSS oficial del Instituto Geográfico Nacional (IGN).
- detecciones térmicas de incendios de NASA FIRMS (opcionales, requieren `MAP_KEY`).
- avisos meteorológicos oficiales AEMET CAP 1.2 mediante AEMET OpenData.
- comunicaciones hidrológicas CHE / SAIH Ebro sobre crecidas, cauces, barrancos e inundaciones.
- caché HTTP con `ETag` y `Last-Modified` en los conectores que la soportan.
- límites de tamaño y tiempo de descarga.
- rechazo de DTD y entidades en XML.
- deduplicación por identificador y huella estable.
- cambios `new`, `updated` y `resolved`.
- resolución solo tras dos lecturas correctas consecutivas sin el incidente.
- conservación de incidentes si una fuente falla.
- fragmentación UTF-8 para respuestas de radio.

Las fuentes vienen desactivadas por defecto. Las claves API permanecen en el
`.env` local y no se escriben en `data/config.json`, caché ni histórico.

## Instalación

Ubicación prevista en Raspberry:

```text
/home/meshnet/MeshNet-Bot/tools/emergencias_guardia
```

Requiere Python 3.10 o posterior. No instala dependencias externas.

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
python3 emergencias_guardia.py init
python3 emergencias_guardia.py doctor
```

`init` crea `data/config.json`. El directorio `data/` es estado de ejecución y
está excluido de Git.

## Configurar fuentes

Consultar conectores:

```bash
python3 emergencias_guardia.py source list
```

### DGT DATEX II

```bash
python3 emergencias_guardia.py area add province Zaragoza
python3 emergencias_guardia.py source test dgt_datex
python3 emergencias_guardia.py source enable dgt_datex
```

URL predeterminada:

```text
https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml
```

DGT exige al menos un área habilitada para impedir una descarga nacional sin
filtro operativo.

### Ayuntamiento de Zaragoza

```bash
python3 emergencias_guardia.py source test municipal_json
python3 emergencias_guardia.py source enable municipal_json
```

URL predeterminada:

```text
https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json?rows=1000&srsname=wgs84
```

El conector JSON admite `records_path`, mapeo de campos y GeoJSON `Feature` con
geometría `Point`.

### Terremotos — IGN

El conector `ign_earthquakes` extrae GeoRSS, magnitud y fecha. La magnitud se
normaliza a severidad: `low` < 3,5; `medium` < 5; `high` < 6; `critical` >= 6.
Como normalmente aporta coordenadas pero no provincia, se recomienda un radio:

```bash
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 150
python3 emergencias_guardia.py source test ign_earthquakes
python3 emergencias_guardia.py source enable ign_earthquakes
```

### Incendios — NASA FIRMS

FIRMS informa de anomalías térmicas observadas por satélite, no de incendios
oficialmente confirmados. Los eventos se guardan como `satellite_detection` y
no se difunden por defecto.

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
umask 077
printf '%s\n' 'FIRMS_MAP_KEY=SU_MAP_KEY' > .env
set -a; . ./.env; set +a
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 150
python3 emergencias_guardia.py source test nasa_firms
python3 emergencias_guardia.py source enable nasa_firms
```

Para propagar detecciones no confirmadas hay que establecer expresamente
`notifications.allow_satellite_detection: true`.

### Avisos meteorológicos — AEMET CAP

v7.0.43 incorpora `aemet_cap`. El conector consume el endpoint oficial AEMET
OpenData de avisos CAP, resuelve la URL temporal `datos` que devuelve la API y
normaliza cada mensaje CAP 1.2 al modelo `Event` existente.

Datos utilizados:

- `identifier` para identidad estable;
- `msgType` para estado y cancelación;
- `event`, `headline`, `description` e `instruction` para tipo y texto;
- `severity`, `urgency` y `certainty`;
- `onset`, `effective`, `expires` y `sent`;
- `areaDesc` y parámetros CAP para cobertura geográfica.

Clasificación meteorológica:

```text
Tormentas / rayos        -> storm
Nieve / aludes           -> snow
Viento                   -> strong_wind
Calor / frío             -> extreme_temperature
Lluvia / inundación      -> flood
```

Severidad CAP:

```text
Extreme  -> critical
Severe   -> high
Moderate -> medium
resto    -> low
```

La fuente requiere una clave gratuita de AEMET OpenData y al menos una provincia
configurada:

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
umask 077
printf '%s\n' 'AEMET_API_KEY=SU_API_KEY' >> .env
set -a; . ./.env; set +a
python3 emergencias_guardia.py area add province Zaragoza
python3 emergencias_guardia.py source test aemet_cap
python3 emergencias_guardia.py source enable aemet_cap
```

Un CAP de cancelación se normaliza como `resolved`. La deduplicación y el flujo
de cierre posterior son los mismos que para el resto de fuentes.

### Crecidas e inundaciones — CHE / SAIH Ebro

v7.0.43 incorpora `che_saih` sobre el canal oficial de comunicaciones de la
Confederación Hidrográfica del Ebro:

```text
https://cph.chebro.es/es/notas-de-prensa-rss
```

La CHE publica contenido de distintas materias. El conector no transforma cada
nota en una emergencia: solo acepta entradas con semántica hidrológica explícita,
como crecidas, avenidas, inundaciones, desbordamientos, cauces, barrancos o
vigilancia SAIH.

```bash
python3 emergencias_guardia.py area add province Zaragoza
python3 emergencias_guardia.py source test che_saih
python3 emergencias_guardia.py source enable che_saih
```

Los eventos se normalizan como `flood`, `verification=official`. Si una
comunicación menciona varias provincias, se conserva la primera en
`Event.province` por compatibilidad y la lista completa en `metadata.provinces`,
de modo que el filtro del motor pueda aceptar cualquiera de las provincias
seleccionadas.

### RAN y 112 Aragón

RAN / Protección Civil y 112 Aragón son fuentes de interés, pero **no se integran
en v7.0.43 mediante scraping HTML**. Permanecen pendientes hasta disponer de un
endpoint público estructurado y estable que permita identidad, actualización y
cierre fiables.

## ControlPanel

En **Emergencias -> Fuentes y cobertura** aparecen seis fuentes:

```text
Ayuntamiento de Zaragoza
DGT — tráfico y carreteras
IGN — terremotos
NASA FIRMS — focos térmicos
AEMET CAP — avisos meteorológicos
CHE / SAIH Ebro — crecidas e inundaciones
```

El panel conserva categorías, provincias, radio y matriz categoría × severidad.
Añade un campo protegido `AEMET_API_KEY` junto a `FIRMS_MAP_KEY`. Ninguna clave
existente se devuelve al navegador; dejar el campo vacío conserva el valor local.

Para AEMET y CHE debe existir al menos una provincia seleccionada. IGN y FIRMS
continúan requiriendo radio por su naturaleza geográfica basada principalmente
en coordenadas.

## Filtro de propagación

La recogida y la difusión son independientes. El ControlPanel mantiene una matriz
**categoría × severidad**. Una incidencia puede almacenarse e historizarse sin
ser transmitida.

```bash
python3 emergencias_guardia.py filters show
python3 emergencias_guardia.py filters set \
  --minimum-severity high \
  --categories wildfire,urban_fire,road_closed,traffic_collision
```

Las severidades son `low`, `medium`, `high` y `critical`.

## Áreas y categorías

```bash
python3 emergencias_guardia.py area add province Zaragoza
python3 emergencias_guardia.py area add municipality Utebo
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 60
python3 emergencias_guardia.py area list
python3 emergencias_guardia.py area remove utebo

python3 emergencias_guardia.py category list
python3 emergencias_guardia.py category disable water_outage
python3 emergencias_guardia.py category enable road_closed
```

Los filtros provinciales consultan `Event.province` y, para fuentes multizona de
v7.0.43, también `metadata.provinces`, `metadata.cap_area` y parámetros CAP. Los
conectores históricos conservan su comportamiento original.

## Actualizar y consultar

```bash
python3 emergencias_guardia.py fetch
python3 emergencias_guardia.py fetch --source dgt_datex
python3 emergencias_guardia.py fetch --source aemet_cap
python3 emergencias_guardia.py fetch --source che_saih
python3 emergencias_guardia.py list
python3 emergencias_guardia.py list --province Zaragoza
python3 emergencias_guardia.py list --category flood
python3 emergencias_guardia.py history --limit 50
python3 emergencias_guardia.py status
python3 emergencias_guardia.py doctor
```

Una descarga fallida se registra en `data/state.json` y no elimina incidentes.
Un incidente desaparecido se marca `resolved` después del número de lecturas
correctas configurado en `resolve_after_missing_fetches`.

## API local

```bash
python3 emergencias_guardia.py serve
```

Por defecto escucha en `127.0.0.1:8789`. Para un broker Docker:

```bash
python3 emergencias_guardia.py serve --host 0.0.0.0 --port 8789
```

Salud:

```bash
curl -s http://127.0.0.1:8789/health
```

Eventos:

```bash
curl -s 'http://127.0.0.1:8789/events?province=Zaragoza'
```

Consulta formateada:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"text":"emergencias Zaragoza","max_bytes":140}' \
  http://127.0.0.1:8789/query
```

## Integración con MeshNet-Broker

El broker usa `source/emergencias_commands.py` y consulta la API desde MeshCore
y Meshtastic. Las consultas DM y los avisos automáticos permanecen separados.
La aplicación no abre una segunda conexión de radio.

Configuración recomendada en `.env` principal:

```env
EMERGENCIAS_COMMAND_ENABLED=true
EMERGENCIAS_SERVICE_URL=http://172.17.0.1:8789/query
EMERGENCIAS_SERVICE_TIMEOUT_SECONDS=3
EMERGENCIAS_MESHCORE_CHANNEL=-1
EMERGENCIAS_MESHTASTIC_CHANNEL=-1
EMERGENCIAS_MAX_REQUESTS_PER_HOUR=5
EMERGENCIAS_RATE_LIMIT_WINDOW_SECONDS=3600
EMERGENCIAS_DUPLICATE_WINDOW_SECONDS=20
EMERGENCIAS_DM_MAX_MESSAGES_PER_RESPONSE=4
EMERGENCIAS_MAX_EVENTS_PER_QUERY=5
EMERGENCIAS_MAX_TEXT_BYTES=140
```

## Avisos automáticos y APRS

El temporizador de Emergencias recolecta las fuentes y publica únicamente cambios
incrementales autorizados. Las salidas MeshCore/Meshtastic, APRS RF y APRS-IS
siguen usando el dispatcher existente.

v7.0.43 no modifica el comportamiento validado en v7.0.42: los estados
terminales `resolved`, `cancelled`, `expired` y `closed` pueden omitir únicamente
el `MIN_INTERVAL` de boletines APRS-IS, manteniendo la deduplicación.

## systemd

El servicio de comprobación carga primero el `.env` general y después el `.env`
local de Emergencias:

```text
EnvironmentFile=-/home/meshnet/MeshNet-Bot/.env
EnvironmentFile=-/home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env
```

Por ello `FIRMS_MAP_KEY` y `AEMET_API_KEY` guardadas por el ControlPanel quedan
disponibles para `meshnet-emergencias-check.service` sin introducir secretos en
Git.

Instalación o actualización:

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
sudo cp systemd/meshnet-emergencias-check.service /etc/systemd/system/
sudo cp systemd/meshnet-emergencias-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-emergencias-check.timer
sudo systemctl start meshnet-emergencias-check.service
sudo systemctl status meshnet-emergencias-check.service --no-pager
```

## Pruebas v7.0.43

```bash
cd /home/meshnet/MeshNet-Bot
python3 -m compileall -q tools/emergencias_guardia tools/ControlPanel
python3 -m pytest tests/test_emergency_sources_v7043.py tools/ControlPanel/tests/test_web_admin.py -v
```

La nueva batería cubre normalización CAP, cancelación CAP, filtrado hidrológico
CHE y exposición de las nuevas fuentes en el ControlPanel.
