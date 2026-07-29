# Emergencias Guardia

Aplicación Python independiente que agrega incidencias oficiales, las normaliza,
mantiene una copia local y ofrece una API HTTP para consultas. Está inspirada en
`farmacias_guardia`, pero no importa código del broker ni altera servicios
existentes.

Esta primera fase usa únicamente la biblioteca estándar de Python y aporta:

- CLI para fuentes, áreas, categorías, consulta, diagnóstico e histórico.
- API local en el puerto `8789`.
- conectores configurables para DGT DATEX II y fuentes municipales JSON/GeoJSON;
- adaptación inicial verificada para la API de incidencias del Ayuntamiento de Zaragoza;
- terremotos del feed RSS/GeoRSS oficial del Instituto Geográfico Nacional (IGN);
- detecciones térmicas de incendios de NASA FIRMS (opcionales, requieren `MAP_KEY`);
- caché HTTP con `ETag` y `Last-Modified`;
- límites de tamaño y tiempo de descarga;
- rechazo de DTD y entidades en XML;
- deduplicación por identificador y huella estable;
- cambios `new`, `updated` y `resolved`;
- resolución solo tras dos lecturas correctas consecutivas sin el incidente;
- conservación de incidentes si una fuente falla;
- fragmentación UTF-8 para respuestas de radio.

No se incluye `.env` ni hay claves. Las fuentes vienen desactivadas. La URL
pública municipal de Zaragoza se incluye como valor conocido, pero requiere
habilitación explícita.

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

Configurar y probar DGT DATEX II:

```bash
python3 emergencias_guardia.py source set-url dgt_datex \
  'https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml'
python3 emergencias_guardia.py source test dgt_datex
python3 emergencias_guardia.py source enable dgt_datex
```

Por seguridad, `dgt_datex` exige al menos un área geográfica habilitada.
Configure, por ejemplo, `area add province Zaragoza` antes de probar o activar
esta fuente nacional.

Configurar una fuente municipal JSON:

```bash
python3 emergencias_guardia.py source set-url municipal_json \
  'https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json?rows=1000&srsname=wgs84'
python3 emergencias_guardia.py source test municipal_json
python3 emergencias_guardia.py source enable municipal_json
```

El valor inicial de `municipal_json` apunta al listado oficial:

```text
https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json?rows=1000&srsname=wgs84
```

Al asignar esta URL mediante `source set-url`, la CLI aplica también el mapeo
oficial conocido aunque `data/config.json` proceda de una prueba anterior.

El conector JSON admite en `data/config.json`:

- `records_path`: ruta con puntos hasta la lista de registros;
- `mapping`: correspondencia entre el modelo interno y campos de la fuente;
- GeoJSON `Feature` con geometría `Point`.

Ejemplo de mapeo:

```json
{
  "records_path": "result.items",
  "mapping": {
    "id": "identifier",
    "title": "title",
    "description": "description",
    "category": "category",
    "municipality": "location.municipality",
    "started_at": "startDate",
    "expected_end": "endDate"
  }
}
```

No habilite una fuente hasta confirmar su licencia, estabilidad y semántica.
`source test` descarga y normaliza; por ello actualiza la copia local y el
histórico, pero no envía mensajes por radio.

### Terremotos (IGN)

El conector `ign_earthquakes` consume el RSS oficial del IGN, extrae GeoRSS,
magnitud y fecha, y asigna severidad (`low` < 3,5; `medium` < 5; `high` < 6;
`critical` >= 6). Como el feed normalmente aporta coordenadas pero no provincia,
se recomienda un área circular en vez de un filtro administrativo:

```bash
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 150
python3 emergencias_guardia.py source test ign_earthquakes
python3 emergencias_guardia.py source enable ign_earthquakes
```

### Incendios (NASA FIRMS)

FIRMS no informa de incendios oficialmente confirmados: ofrece anomalías
térmicas observadas por satélite. Por ello los eventos se guardan siempre como
`satellite_detection`, el mensaje lo indica y **no se difunden por defecto**.
El conector consulta VIIRS SNPP NRT para la caja de España configurada en
`sources.nasa_firms.bbox` y después aplica las áreas locales:

```bash
export FIRMS_MAP_KEY='clave-obtenida-en-NASA-FIRMS'
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 150
python3 emergencias_guardia.py source test nasa_firms
python3 emergencias_guardia.py source enable nasa_firms
```

La clave solo se lee de la variable `FIRMS_MAP_KEY`; no se escribe en
`data/config.json`, la caché ni el histórico. Para propagar detecciones no
confirmadas hay que establecer deliberadamente
`notifications.allow_satellite_detection: true`, después de valorar falsos
positivos (industria, quemas controladas y otros focos térmicos).

La arquitectura ya admite otros riesgos oficiales mediante conectores `rss`
(RSS, Atom y GeoRSS) y `json` (JSON/GeoJSON). Se han añadido las categorías
`earthquake`, `tsunami`, `volcanic` y `landslide`; antes de incorporar una nueva
fuente se debe documentar su organismo, licencia, cobertura, frecuencia,
identificador estable y criterio de cierre. No se recomienda extraer HTML si
existe un feed o API oficial.

### Qué falta activar en una instalación existente

Para **recoger y consultar** terremotos basta con crear un área, probar el feed,
habilitarlo y dejar activo el temporizador. Para FIRMS hace falta además obtener
una MAP_KEY gratuita y hacerla visible al servicio systemd:

```bash
sudo install -d -m 0750 /etc/meshnet
sudo sh -c 'umask 077; printf "%s\n" "FIRMS_MAP_KEY=SU_MAP_KEY" > /etc/meshnet/emergencias_guardia.env'
sudo chown root:meshnet /etc/meshnet/emergencias_guardia.env

cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 150
python3 emergencias_guardia.py source test ign_earthquakes
sudo -u meshnet env FIRMS_MAP_KEY=SU_MAP_KEY python3 emergencias_guardia.py source test nasa_firms
python3 emergencias_guardia.py source enable ign_earthquakes
python3 emergencias_guardia.py source enable nasa_firms

sudo cp systemd/meshnet-emergencias-check.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-emergencias-check.timer
sudo systemctl start meshnet-emergencias-check.service
```

Después se comprueba la recolección con `source list`, `status` y `list`. La
**difusión automática por radio es opcional y separada**: requiere configurar el
transporte y el índice real del canal `emergencias`, crear primero una línea base
silenciosa y finalmente ejecutar `notify enable`. Los terremotos `high` o
`critical` oficiales pueden entrar en esa ruta. Las detecciones FIRMS siguen
bloqueadas aunque se habilite la ruta, salvo que el operador cambie expresamente
`notifications.allow_satellite_detection` por el riesgo de falsos positivos.

## Filtro de propagación

La recogida conserva todas las incidencias aceptadas. De forma independiente se
puede decidir qué severidades y categorías podrán difundirse:

```bash
python3 emergencias_guardia.py filters show
python3 emergencias_guardia.py filters set \
  --minimum-severity high \
  --categories wildfire,urban_fire,road_closed,traffic_collision
```

Las severidades son `low`, `medium`, `high` y `critical`. Una lista de categorías
vacía bloquea toda propagación. Cambiar el filtro no envía mensajes por sí mismo;
se aplica en las siguientes ejecuciones de `check --notify-changes`. El mismo
ajuste está disponible visualmente en MeshNet ControlPanel.

## Áreas y categorías

```bash
python3 emergencias_guardia.py area add province Zaragoza
python3 emergencias_guardia.py area add municipality Utebo
python3 emergencias_guardia.py area add radius entorno-zaragoza \
  --lat 41.6488 --lon -0.8891 --km 60
python3 emergencias_guardia.py area list
python3 emergencias_guardia.py area remove utebo
```

Los filtros de provincia y municipio usan el valor normalizado suministrado por
la fuente. El radio usa coordenadas. La incorporación futura de polígonos
oficiales permitirá resolver límites administrativos cuando una fuente solo
aporte coordenadas.

```bash
python3 emergencias_guardia.py category list
python3 emergencias_guardia.py category disable water_outage
python3 emergencias_guardia.py category enable road_closed
```

## Actualizar y consultar

```bash
python3 emergencias_guardia.py fetch
python3 emergencias_guardia.py fetch --source dgt_datex
python3 emergencias_guardia.py list
python3 emergencias_guardia.py list --province Zaragoza
python3 emergencias_guardia.py list --municipality Calatayud
python3 emergencias_guardia.py list --category wildfire
python3 emergencias_guardia.py list --road A-2
python3 emergencias_guardia.py list --json
python3 emergencias_guardia.py history --limit 50
python3 emergencias_guardia.py status
python3 emergencias_guardia.py doctor
```

Una descarga fallida se registra en `data/state.json`, pero no elimina
incidentes. Un incidente desaparecido se marca `resolved` después del número de
lecturas correctas configurado en `resolve_after_missing_fetches`.

## API local

```bash
python3 emergencias_guardia.py serve
```

Por defecto escucha solo en `127.0.0.1:8789`. Para que un broker en Docker
acceda desde otra interfaz:

```bash
python3 emergencias_guardia.py serve --host 0.0.0.0 --port 8789
```

Salud:

```bash
curl -s http://127.0.0.1:8789/health
```

Eventos:

```bash
curl -s 'http://127.0.0.1:8789/events?province=Zaragoza&road=A-2'
```

Consulta formateada:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"text":"emergencias A-2","max_bytes":140}' \
  http://127.0.0.1:8789/query
```

Comandos reconocidos:

```text
emergencias
emergencias incendios
emergencias trafico
emergencias carreteras
emergencias A-2
emergencias <texto de localidad>
```

La API genera mensajes, pero no abre conexiones de radio ni transmite.

## Integración con MeshNet-Broker

El broker incorpora `source/emergencias_commands.py` y consulta esta API desde
los receptores MeshCore y Meshtastic. La respuesta se encola siempre como DM
por la misma red de origen.

Configuración recomendada en el `.env` de MeshNet-Bot:

```env
EMERGENCIAS_COMMAND_ENABLED=true
EMERGENCIAS_SERVICE_URL=http://172.17.0.1:8789/query
EMERGENCIAS_SERVICE_TIMEOUT_SECONDS=3

EMERGENCIAS_MESHCORE_CHANNEL=-1
EMERGENCIAS_MESHTASTIC_CHANNEL=-1

EMERGENCIAS_MAX_REQUESTS_PER_HOUR=5
EMERGENCIAS_RATE_LIMIT_WINDOW_SECONDS=3600
EMERGENCIAS_DUPLICATE_WINDOW_SECONDS=20
EMERGENCIAS_RATE_LIMIT_SAVE_SECONDS=60
EMERGENCIAS_DM_MAX_MESSAGES_PER_RESPONSE=4
EMERGENCIAS_DM_INTER_MESSAGE_DELAY_SECONDS=1
EMERGENCIAS_MAX_EVENTS_PER_QUERY=5
EMERGENCIAS_MAX_TEXT_BYTES=140
```

La dirección `172.17.0.1` es solo un ejemplo: debe sustituirse por la puerta de
enlace real del host vista desde el contenedor. La integración viene desactivada
por defecto en código y los canales `-1` impiden consultas públicas. Con esa
configuración se puede validar primero únicamente por DM.

El broker aplica:

- límite independiente por red y contacto;
- rechazo de duplicados;
- timeout HTTP corto;
- máximo de cuatro partes por respuesta;
- mensajes de hasta 140 bytes UTF-8;
- ejecución en un hilo para no bloquear el receptor;
- `no_bridge` en respuestas Meshtastic.

Las consultas DM y los avisos automáticos son flujos independientes:

- el broker atiende las consultas DM mediante la API local;
- el temporizador de `emergencias_guardia` recolecta fuentes y publica
  únicamente novedades incrementales mediante el control del broker;
- la aplicación no abre una segunda conexión MeshCore o Meshtastic.

## Formato y enrutamiento por canales

Cada incidencia se convierte en un mensaje autocontenido de hasta 140 bytes.
No se divide una descripción larga en partes huérfanas: se conserva siempre
gravedad, categoría, ubicación, vigencia y fuente.

Cuando la fuente aporta coordenadas válidas, el mensaje incluye un enlace
directo y compacto a Google Maps:

```text
https://maps.google.com/?q=41.5801,-1.1187
```

El enlace se genera localmente, sin acortadores ni servicios intermedios, y se
omite si faltan las coordenadas o están fuera de rango. El formateador conserva
el límite máximo configurado de 140 bytes.

Ejemplo:

```text
SERV [1/2]
MEDIA · CARRETERA CORTADA
AV. VALENCIA · Zaragoza
Hasta 31/01 · Ayto. Zaragoza
```

Las reglas iniciales son:

- `SERVICIOS`: tráfico, carreteras, agua, luz, gas e incidencias municipales;
- `METEO`: tormenta, nieve, viento y temperaturas extremas;
- `EMERGENCIAS`: únicamente sucesos graves `high`/`critical`, oficiales y de
  categorías de emergencia;
- sin difusión: eventos futuros, caducados, resueltos, no verificados o
  detecciones satelitales sin confirmación.

El filtro de propagación permite habilitar cada severidad de forma independiente
(`low`, `medium`, `high` y `critical`). Por ejemplo, para propagar solo alertas
bajas y altas:

```bash
python3 emergencias_guardia.py filters set --severities low,high --categories road_closed,storm
```

La opción anterior `--minimum-severity` continúa disponible para configuraciones
y automatizaciones existentes, y selecciona esa severidad y todas las superiores.

La difusión está desactivada por defecto y todos los canales comienzan en `-1`.
Primero debe revisarse la salida:

```bash
python3 emergencias_guardia.py notify status
python3 emergencias_guardia.py notify preview
python3 emergencias_guardia.py notify preview --route servicios
python3 emergencias_guardia.py notify preview --route emergencias
```

`preview` no transmite. Muestra el destino resuelto y los mensajes exactos.

Una vez confirmados los índices reales:

```bash
python3 emergencias_guardia.py notify set-transport meshcore
python3 emergencias_guardia.py notify set-channel servicios meshcore INDICE
python3 emergencias_guardia.py notify set-channel emergencias meshcore INDICE
python3 emergencias_guardia.py notify set-channel meteo meshcore INDICE
python3 emergencias_guardia.py notify enable
```

En Meshtastic se sustituye `meshcore` por `meshtastic`.

Antes de automatizar, detenga temporalmente el temporizador, habilite las
notificaciones y pruebe el envío manual:

```bash
sudo systemctl stop meshnet-emergencias-check.timer
python3 emergencias_guardia.py notify enable
python3 emergencias_guardia.py notify send servicios
python3 emergencias_guardia.py notify send emergencias
python3 emergencias_guardia.py notify send meteo
```

Cada ruta publica como máximo tres eventos. Un hash persistente impide repetir
exactamente la misma difusión; `--force` permite repetirla deliberadamente:

```bash
python3 emergencias_guardia.py notify send emergencias --force
```

Una ruta sin eventos elegibles devuelve `no_eligible_events` y no transmite.
La automatización no debe reactivarse hasta verificar los índices y el
contenido real en cada canal.

## Avisos incrementales automáticos

El flujo automático utiliza:

```bash
python3 emergencias_guardia.py check --notify-changes
```

La primera ejecución crea una línea base silenciosa. No publica todos los
eventos ya existentes. Las siguientes ejecuciones detectan:

- `NUEVA`: evento que no existía en la línea base;
- `ACTUALIZACIÓN`: cambio significativo en gravedad, estado, ubicación o texto;
- `FINALIZADA`: resolución de un evento previamente difundido.

Los cambios de `SERVICIOS` y `METEO` se agrupan durante cinco minutos. Las
emergencias oficiales graves no tienen esa espera adicional. Si el broker
rechaza un mensaje o no responde, el elemento permanece en un spool persistente
y se reintenta con backoff progresivo entre 60 y 3600 segundos.

Si las notificaciones están desactivadas, `check --notify-changes` actualiza la
línea base sin acumular mensajes pendientes. Solo se encolan rutas cuyo canal
está configurado.

### Primera activación segura

```bash
# 1. Evitar que el temporizador se ejecute durante la configuración
sudo systemctl stop meshnet-emergencias-check.timer

# 2. Crear línea base sin transmitir
python3 emergencias_guardia.py notify disable
python3 emergencias_guardia.py check --notify-changes

# 3. Revisar estado y salida
python3 emergencias_guardia.py notify status
python3 emergencias_guardia.py notify preview

# 4. Configurar los canales reales
python3 emergencias_guardia.py notify set-channel servicios meshcore INDICE
python3 emergencias_guardia.py notify set-channel emergencias meshcore INDICE
python3 emergencias_guardia.py notify set-channel meteo meshcore INDICE

# 5. Habilitar y reanudar la recolección automática
python3 emergencias_guardia.py notify enable
sudo systemctl enable --now meshnet-emergencias-check.timer
sudo systemctl start meshnet-emergencias-check.service
```

`notify status` muestra si existe línea base, número de eventos observados,
eventos ya difundidos y elementos pendientes.

### Añadir DGT a una instalación municipal ya activa

No habilite DGT directamente mientras el temporizador transmite: los eventos
ya presentes en DGT podrían interpretarse como nuevos respecto a la línea base
municipal. Utilice esta secuencia para incorporarlos silenciosamente:

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia

sudo systemctl stop meshnet-emergencias-check.timer
python3 emergencias_guardia.py notify disable

python3 emergencias_guardia.py area add province Zaragoza
python3 emergencias_guardia.py source set-url dgt_datex \
  'https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml'
python3 emergencias_guardia.py source test dgt_datex
python3 emergencias_guardia.py source enable dgt_datex

# Actualiza la línea base con DGT sin encolar ni transmitir
python3 emergencias_guardia.py check --notify-changes

python3 emergencias_guardia.py notify enable
sudo systemctl enable --now meshnet-emergencias-check.timer
sudo systemctl start meshnet-emergencias-check.service
```

`source test dgt_datex` debe devolver `ok: true`, un total nacional en
`records` y un valor `accepted` mayor que cero cuando existan incidencias en el
área configurada. El número exacto varía con el estado de las carreteras.

Compruebe cuántos eventos DGT quedaron incorporados:

```bash
python3 emergencias_guardia.py list --json |
  jq '[.events[] | select(.source == "dgt_datex")] | length'
```

## systemd

Se incluyen:

```text
systemd/meshnet-emergencias-api.service
systemd/meshnet-emergencias-check.service
systemd/meshnet-emergencias-check.timer
```

Antes de habilitarlos, configure y pruebe las fuentes:

```bash
python3 emergencias_guardia.py doctor
python3 emergencias_guardia.py fetch
python3 emergencias_guardia.py list
```

Después:

```bash
sudo cp systemd/meshnet-emergencias-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-emergencias-api.service
sudo systemctl enable --now meshnet-emergencias-check.timer
```

La unidad de API incluida escucha en `0.0.0.0:8789` para permitir el acceso
desde Docker. El puerto no debe exponerse a Internet; restrínjalo al host y a
la red Docker mediante el cortafuegos del sistema.

El temporizador consulta cada dos minutos con un pequeño retardo aleatorio y
ejecuta `check --notify-changes`. Con notificaciones desactivadas solo mantiene
la API, el histórico y la línea base. Cuando se habilitan, procesa el spool
incremental según las reglas anteriores.

La unidad `meshnet-emergencias-check.service` es de tipo `oneshot`. Es normal
que aparezca como `inactive (dead)` después de una ejecución correcta. Debe
terminar con `status=0/SUCCESS`; la unidad que permanece activa es
`meshnet-emergencias-check.timer`.

### Comprobación operativa

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia

python3 emergencias_guardia.py doctor
python3 emergencias_guardia.py source list
python3 emergencias_guardia.py area list
python3 emergencias_guardia.py notify status

systemctl status meshnet-emergencias-api.service --no-pager
systemctl status meshnet-emergencias-check.timer --no-pager
systemctl list-timers --all | grep emergencias

journalctl -u meshnet-emergencias-check.service -n 100 --no-pager
curl -s http://127.0.0.1:8789/health
```

Una instalación preparada para emitir debe mostrar:

- `municipal_json.enabled: true` y, si se usa, `dgt_datex.enabled: true`;
- un área de provincia, municipio o radio habilitada;
- `notify status` con `enabled: true`, `initialized: true` y `pending: 0`;
- transporte `meshcore` o `meshtastic` y canales no negativos;
- temporizador `active (waiting)` con una fecha futura en `Trigger`/`NEXT`;
- cada fuente con `ok: true` en el diario;
- `new`, `updated`, `resolved` y `sent` a cero cuando no hay novedades.

La salida normal de cada ejecución contiene dos bloques:

```json
{
  "fetch": {
    "changes": {"new": 0, "updated": 0, "resolved": 0},
    "sources": {}
  },
  "notifications": {
    "queued": {"new": 0, "updated": 0, "resolved": 0},
    "sent": 0,
    "pending": 0
  }
}
```

Cuando exista una novedad, primero aumentará `queued`. Tras la entrega
confirmada aumentarán `sent` y `delivered`. Si falla el transporte,
`pending` conservará el aviso para reintentar.

### Diagnóstico

#### El temporizador muestra `active (elapsed)` y `Trigger: n/a`

El temporizador se inició después de que venciera `OnBootSec` y todavía no
tiene una ejecución de servicio desde la que calcular `OnUnitActiveSec`.
Habilítelo para futuros reinicios y fuerce una ejecución:

```bash
sudo systemctl enable --now meshnet-emergencias-check.timer
sudo systemctl start meshnet-emergencias-check.service
systemctl list-timers --all | grep emergencias
```

Debe pasar a `active (waiting)` y mostrar la próxima ejecución.

#### DGT devuelve `records` pero `accepted: 0`

Primero confirme que el área está habilitada:

```bash
python3 emergencias_guardia.py area list
```

El parser DATEX II v3.7 debe leer explícitamente el campo `province`. Verifique
que la copia desplegada contiene la revisión actual:

```bash
grep -nE 'province=first_text|xsi_type|laneclosures' \
  emergencias/sources/datex2.py
```

Debe incluir:

```python
province=first_text(record, "province", "administrativeArea"),
```

Si aún aparece:

```python
province=first_text(record, "administrativeArea") or self.config.get("default_province", ""),
```

la Raspberry conserva el parser anterior. Sustituya el archivo completo; no
cambie solo esa línea, porque la revisión v3.7 también interpreta `xsi:type` y
las categorías de cierre de carriles. Después:

```bash
python3 -m py_compile emergencias/sources/datex2.py
python3 emergencias_guardia.py notify disable
python3 emergencias_guardia.py source test dgt_datex
```

No reactive las notificaciones hasta obtener `ok: true` y revisar `accepted`.

#### La fuente falla temporalmente

Una descarga fallida conserva la última copia válida y no resuelve todos los
eventos. Revise `last_error` y el diario. La resolución solo se produce tras
dos lecturas correctas consecutivas en las que el evento ya no aparece.

#### Hay avisos pendientes

```bash
python3 emergencias_guardia.py notify status
journalctl -u meshnet-emergencias-check.service -n 100 --no-pager
```

`pending` mayor que cero indica que la novedad está en el spool. Compruebe el
broker, el índice del canal y el transporte; no borre `data/state.json`, porque
contiene la línea base, los entregados y los reintentos.

### Parada y reanudación

Detener únicamente las consultas automáticas mantiene disponibles la API y las
consultas DM:

```bash
sudo systemctl disable --now meshnet-emergencias-check.timer
```

Reanudar:

```bash
sudo systemctl enable --now meshnet-emergencias-check.timer
sudo systemctl start meshnet-emergencias-check.service
```

Detener también la API local:

```bash
sudo systemctl disable --now meshnet-emergencias-api.service
```

## Validación

```bash
python3 -m py_compile emergencias_guardia.py emergencias/*.py emergencias/sources/*.py
python3 -m unittest discover -s tests -v

cd /home/meshnet/MeshNet-Bot
python3 -m py_compile source/emergencias_commands.py source/Meshtastic_Broker.py
python3 -m unittest tests.test_emergencias_commands -v
```

## Alcance pendiente

- verificar y adaptar nuevas fuentes oficiales;
- geometrías oficiales de provincias y municipios;
- conectores AEMET, FIRMS, RAN, INFOAR y EFFIS;
- correlación entre fuentes;
- automatización de nuevas fuentes oficiales y correlación entre ellas.

NASA FIRMS, cuando se incorpore, deberá clasificarse siempre como
`satellite_detection` y describirse como foco térmico no confirmado, salvo
corroboración oficial.
