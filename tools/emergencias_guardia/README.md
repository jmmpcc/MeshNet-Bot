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
python3 emergencias_guardia.py source set-url dgt_datex URL_OFICIAL
python3 emergencias_guardia.py source test dgt_datex
python3 emergencias_guardia.py source enable dgt_datex
```

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

No se realizan avisos automáticos. Las incidencias municipales actuales solo
se ofrecen bajo demanda.

## Formato y enrutamiento por canales

Cada incidencia se convierte en un mensaje autocontenido de hasta 140 bytes.
No se divide una descripción larga en partes huérfanas: se conserva siempre
gravedad, categoría, ubicación, vigencia y fuente.

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

Antes de automatizar, el envío se prueba manualmente:

```bash
python3 emergencias_guardia.py notify send servicios
python3 emergencias_guardia.py notify send emergencias
python3 emergencias_guardia.py notify send meteo
```

Cada ruta publica como máximo tres eventos. Un hash persistente impide repetir
exactamente la misma difusión; `--force` permite repetirla deliberadamente:

```bash
python3 emergencias_guardia.py notify send emergencias --force
```

La automatización no se habilitará hasta verificar los índices y el contenido
real en cada canal.

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

Secuencia segura de activación:

```bash
# 1. Crear línea base sin transmitir
python3 emergencias_guardia.py notify disable
python3 emergencias_guardia.py check --notify-changes

# 2. Revisar estado y salida
python3 emergencias_guardia.py notify status
python3 emergencias_guardia.py notify preview

# 3. Configurar canales, probar manualmente y habilitar
python3 emergencias_guardia.py notify set-channel servicios meshcore INDICE
python3 emergencias_guardia.py notify send servicios
python3 emergencias_guardia.py notify enable
```

`notify status` muestra si existe línea base, número de eventos observados,
eventos ya difundidos y elementos pendientes.

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
