# Emergencias Guardia

Aplicación Python independiente que agrega incidencias oficiales, las normaliza,
mantiene una copia local y ofrece una API HTTP para consultas. Está inspirada en
`farmacias_guardia`, pero no importa código del broker ni altera servicios
existentes.

Esta primera fase usa únicamente la biblioteca estándar de Python y aporta:

- CLI para fuentes, áreas, categorías, consulta, diagnóstico e histórico.
- API local en el puerto `8789`.
- conectores configurables para DGT DATEX II y fuentes municipales JSON/GeoJSON;
- caché HTTP con `ETag` y `Last-Modified`;
- límites de tamaño y tiempo de descarga;
- rechazo de DTD y entidades en XML;
- deduplicación por identificador y huella estable;
- cambios `new`, `updated` y `resolved`;
- resolución solo tras dos lecturas correctas consecutivas sin el incidente;
- conservación de incidentes si una fuente falla;
- fragmentación UTF-8 para respuestas de radio.

No se incluye `.env`, no hay claves ni endpoints inventados. Las fuentes vienen
desactivadas hasta que un operador configure sus URL oficiales.

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
python3 emergencias_guardia.py source set-url municipal_json URL_OFICIAL
python3 emergencias_guardia.py source test municipal_json
python3 emergencias_guardia.py source enable municipal_json
```

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

La API genera mensajes, pero no abre conexiones de radio ni transmite. La
integración futura con el broker debe conservar sus controles de tasa,
duplicados, destino y DM, como hace Farmacias.

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

El temporizador consulta cada dos minutos con un pequeño retardo aleatorio. No
envía alertas: únicamente mantiene la API y el histórico actualizados.

## Validación

```bash
python3 -m py_compile emergencias_guardia.py emergencias/*.py emergencias/sources/*.py
python3 -m unittest discover -s tests -v
```

## Alcance pendiente

- verificar endpoints oficiales concretos y adaptar sus campos reales;
- geometrías oficiales de provincias y municipios;
- conectores AEMET, FIRMS, RAN, INFOAR y EFFIS;
- correlación entre fuentes;
- integración del comando con el broker;
- cola de avisos y envío por canal `EMERGENCIAS`.

NASA FIRMS, cuando se incorpore, deberá clasificarse siempre como
`satellite_detection` y describirse como foco térmico no confirmado, salvo
corroboración oficial.
