# Farmacias de guardia

Aplicación Python independiente que descarga, normaliza y conserva las farmacias de guardia, ofrece una API local para las consultas `farma` recibidas por el broker y puede publicar automáticamente el listado en un canal MeshCore o Meshtastic.

La aplicación **no forma parte de MeshNet-Bot**, no importa módulos del broker, no necesita Docker y utiliza su propio archivo `.env`.

## Arquitectura

```text
Nodo MeshCore/Meshtastic
        │
        │ farma / farma zaragoza
        ▼
MeshNet-Broker
        │ HTTP POST /query
        ▼
Aplicación farmacias_guardia
        │
        └── respuesta formateada
                │
                ▼
Broker → DM al contacto solicitante
```

Para las difusiones automáticas:

```text
farmacias_guardia.py send (listado completo a las 08:30)
farmacias_guardia.py check --send (solo altas nuevas cada 3 horas)
        │
        │ puerto de control TCP
        ▼
MeshNet-Broker :8766
        │
        ▼
Canal FARMACIA configurado
```

## Ubicación recomendada

```text
/home/meshnet/MeshNet-Bot/tools/farmacias_guardia/
```

No debe instalarse dentro del árbol de código de MeshNet-Bot. Puede estar en cualquier ruta, siempre que los servicios `systemd` y su `.env` apunten a la ubicación correcta.

## Requisitos

- Python 3.10 o posterior.
- Acceso HTTP a la fuente configurada de farmacias.
- Broker MeshNet activo para las funciones de difusión.
- Puerto de control del broker accesible, normalmente `127.0.0.1:8766`.
- Para consultas recibidas por radio, el broker debe poder acceder a la API HTTP de esta aplicación.

La implementación utiliza módulos de la biblioteca estándar de Python. No requiere crear un entorno virtual salvo que se añadan adaptadores externos posteriormente.

## Instalación

```bash
sudo mkdir -p /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
sudo cp -a tools/farmacias_guardia/. /home/meshnet/MeshNet-Bot/tools/farmacias_guardia/
sudo chown -R meshnet:meshnet /home/meshnet/MeshNet-Bot/tools/farmacias_guardia

cd /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
cp .env.example .env
nano .env
```

Comprueba la sintaxis:

```bash
python3 -m py_compile farmacias_guardia.py
```

## Configuración de la aplicación

La aplicación lee exclusivamente su propio `.env`.

### Conexión con el broker

```env
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766
BROKER_TIMEOUT_SECONDS=10
```

- `BROKER_CTRL_HOST`: dirección del broker vista desde la aplicación.
- `BROKER_CTRL_PORT`: puerto de control JSONL del broker.
- `BROKER_TIMEOUT_SECONDS`: tiempo máximo de espera de cada petición al broker.

Como la aplicación se ejecuta directamente en la Raspberry y el puerto del broker está publicado en el host, normalmente se utiliza `127.0.0.1`.

### Perfil y destino de difusión

```env
RADIO_PROFILE=meshcore_only
FARMACIAS_BROADCAST_TRANSPORT=auto
FARMACIAS_MIXED_PROFILE_BROADCAST=meshcore
FARMACIAS_MESHCORE_CHANNEL=1
FARMACIAS_MESHTASTIC_CHANNEL=3
```

- `RADIO_PROFILE`: perfil de radio operativo.
- `FARMACIAS_BROADCAST_TRANSPORT`: `auto`, `meshcore` o `meshtastic`.
- `FARMACIAS_MIXED_PROFILE_BROADCAST`: red preferida cuando el perfil permita ambas.
- `FARMACIAS_MESHCORE_CHANNEL`: índice real del canal MeshCore `FARMACIA`.
- `FARMACIAS_MESHTASTIC_CHANNEL`: índice real del canal Meshtastic `FARMACIA`.

Con `RADIO_PROFILE=meshcore_only` y transporte `auto`, la difusión se realiza por MeshCore.
Si el perfil se cambia a `meshcore_only` pero queda guardado explícitamente
`FARMACIAS_BROADCAST_TRANSPORT=meshtastic`, la aplicación utiliza MeshCore como
respaldo seguro y avisa por `stderr`; así no intenta publicar mediante una
interfaz Meshtastic que el broker tiene deshabilitada.
Si `RADIO_PROFILE` no está declarado en el `.env` de esta aplicación, no se
supone ningún perfil. Si el broker rechaza Meshtastic con
`meshtastic_disabled_by_radio_profile`, el fragmento se reintenta por el canal
`FARMACIAS_MESHCORE_CHANNEL` y los siguientes fragmentos continúan por MeshCore.

### API local

```env
FARMACIAS_API_HOST=0.0.0.0
FARMACIAS_API_PORT=8788
```

- `127.0.0.1` limita el acceso al propio host.
- `0.0.0.0` permite que el contenedor del broker acceda a la API mediante la puerta de enlace Docker.

En una instalación donde el broker se ejecuta en Docker y la aplicación en el host, debe usarse normalmente:

```env
FARMACIAS_API_HOST=0.0.0.0
```

### Límites de los mensajes

```env
FARMACIAS_MESHCORE_MAX_BYTES=170
FARMACIAS_MESHTASTIC_MAX_BYTES=170
FARMACIAS_INTER_MESSAGE_DELAY_SECONDS=8
```

Los mensajes se dividen respetando bytes UTF-8 y se numeran cuando es necesario. La pausa entre fragmentos evita ráfagas de transmisión.

## Configuración necesaria en el broker

El `.env` de MeshNet-Bot debe indicar dónde está la API independiente:

```env
FARMACIAS_COMMAND_ENABLED=true
FARMACIAS_SERVICE_URL=http://172.17.0.1:8788/query
FARMACIAS_SERVICE_TIMEOUT_SECONDS=3

FARMACIAS_MESHCORE_CHANNEL=1
FARMACIAS_MESHTASTIC_CHANNEL=3

FARMACIAS_MAX_REQUESTS_PER_HOUR=5
FARMACIAS_RATE_LIMIT_WINDOW_SECONDS=3600
FARMACIAS_RATE_LIMIT_SAVE_SECONDS=60
FARMACIAS_DUPLICATE_WINDOW_SECONDS=20
FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE=6
FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS=1
```

La IP `172.17.0.1` es un ejemplo. Debe utilizarse una dirección del host accesible desde el contenedor. Para obtener la gateway real del contenedor:

`FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE` limita las consultas filtradas. El
comando exacto `farma` es la excepción: devuelve todas las guardias presentes
en `current.json`, espaciando sus partes con
`FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS`.

```bash
docker exec meshnet-broker python3 -c '
import socket
for line in open("/proc/net/route"):
    fields = line.split()
    if len(fields) >= 3 and fields[1] == "00000000":
        print(socket.inet_ntoa(bytes.fromhex(fields[2])[::-1]))
        break
'
```

No es necesario integrar la aplicación en Docker ni modificar su código para relacionarla con el broker.

# Comandos CLI

La sintaxis general es:

```bash
python3 farmacias_guardia.py <comando> [opciones]
```

Ayuda general:

```bash
python3 farmacias_guardia.py --help
```

## `fetch`

Descarga los datos actuales desde la fuente configurada, los normaliza, elimina duplicados y actualiza el fichero local vigente.

```bash
python3 farmacias_guardia.py fetch
```

Ejemplo de salida:

```json
{"ok": true, "records": 7, "hash": "..."}
```

Qué realiza:

1. Consulta la fuente remota.
2. Convierte los registros al formato interno.
3. Ordena y normaliza los datos.
4. Calcula un hash estable del contenido.
5. Guarda el resultado en el directorio `data`.

Qué no realiza:

- No envía mensajes al broker.
- No publica en la malla.
- No inicia la API.

Uso recomendado:

```bash
python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py status
```

## `preview`

Muestra exactamente cómo quedarían los mensajes de difusión para la red y el canal seleccionados por el `.env`.

```bash
python3 farmacias_guardia.py preview
```

Ejemplo:

```text
--- 154 bytes ---
FARMACIAS GUARDIA 24/07 [1/2]
...

--- 132 bytes ---
FARMACIAS GUARDIA 24/07 [2/2]
...
Destino: meshcore canal 1
```

Qué comprueba:

- Red de difusión resuelta.
- Canal configurado.
- Fragmentación por bytes.
- Numeración de las partes.
- Contenido exacto que se enviaría.

Qué no realiza:

- No descarga datos nuevos.
- No transmite ningún mensaje.

Por ello, antes de usar `preview` debe existir un listado local generado por `fetch`.

Flujo seguro:

```bash
python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py preview
```

## `send`

Descarga el listado actual y lo publica mediante el broker en la red y canal configurados.

```bash
python3 farmacias_guardia.py send
```

Secuencia interna:

1. Descarga la fuente.
2. Actualiza el listado local.
3. Resuelve el destino según el perfil.
4. Genera los fragmentos.
5. Envía cada fragmento al puerto de control del broker.
6. Guarda el hash y el resultado del último envío.

Para MeshCore utiliza:

```text
MESHCORE_SEND
```

Para Meshtastic utiliza:

```text
SEND_TEXT
```

Ejemplo de salida resumida:

```json
{
  "sent": true,
  "network": "meshcore",
  "channel": 1,
  "messages": 2,
  "results": [...]
}
```

Un resultado `broker_accepted` confirma que el broker aceptó y encoló los mensajes. No constituye una confirmación de recepción RF por todos los nodos.

### `send --force`

```bash
python3 farmacias_guardia.py send --force
```

Fuerza el envío aunque el contenido coincida con el último listado registrado.

En la implementación actual, `send` ya realiza una difusión al ejecutarse manualmente. La opción `--force` es especialmente útil para dejar explícito que se desea repetir el contenido y para compatibilidad con controles de cambio presentes o futuros.

Usos habituales:

- Prueba inicial de instalación.
- Reenvío tras una incidencia de radio.
- Verificación del canal configurado.
- Publicación manual extraordinaria.

## `check`

Descarga nuevamente la fuente y compara el hash anterior con el nuevo.

```bash
python3 farmacias_guardia.py check
```

Salida sin cambios:

```json
{"changed": false, "new_pharmacies": 0}
```

Salida con cambios:

```json
{"changed": true, "new_pharmacies": 2}
```

Qué realiza:

- Descarga datos nuevos.
- Actualiza el fichero local.
- Indica si el contenido cambió.

Qué no realiza sin opciones:

- No publica el listado.

### `check --send`

```bash
python3 farmacias_guardia.py check --send
```

Publica solamente las farmacias cuya identidad no existía en la copia local
anterior. Los cambios de datos o las bajas actualizan la copia usada por las
consultas manuales, pero no generan una difusión.

Ejemplo:

```json
{
  "changed": true,
  "new_pharmacies": 2,
  "send": {
    "sent": true,
    "network": "meshcore",
    "channel": 1,
    "messages": 2
  }
}
```

Los mensajes utilizan la cabecera `NUEVAS FARMACIAS DE GUARDIA` y no repiten
las farmacias que ya se habían publicado.

Si no hay cambios:

```json
{"changed": false, "new_pharmacies": 0}
```

No se genera tráfico de radio.

## `status`

Muestra el contenido vigente y el estado del último envío.

```bash
python3 farmacias_guardia.py status
```

La salida contiene:

- `current`: listado normalizado vigente, fecha de actualización y hash.
- `state`: información del último envío aceptado por el broker.

Campos habituales de `state`:

```json
{
  "last_sent_hash": "...",
  "last_sent_at": "2026-07-24T08:30:00+02:00",
  "last_network": "meshcore",
  "last_channel": 1,
  "last_messages": 2,
  "last_status": "broker_accepted"
}
```

Este comando no accede a la fuente ni transmite mensajes.

## `doctor`

Ejecuta comprobaciones básicas de configuración y conectividad.

```bash
python3 farmacias_guardia.py doctor
```

Comprueba:

- Directorio de datos.
- Existencia de un listado local.
- Perfil configurado.
- Red y canal de difusión resueltos.
- Comunicación con el puerto de control del broker.
- Acceso y número de registros obtenidos desde la fuente.

Ejemplo:

```json
{
  "data_dir": "/home/meshnet/MeshNet-Bot/tools/farmacias_guardia/data",
  "current_exists": true,
  "profile": "meshcore_only",
  "target": ["meshcore", 1],
  "source_records": 7
}
```

### Nota sobre `meshcore_only`

La versión actual de `doctor` consulta el comando general `BROKER_STATUS`. En un perfil `meshcore_only`, el broker puede responder:

```text
iface manager not ready
```

Esto puede ser un falso negativo relacionado con la interfaz Meshtastic desactivada y no demuestra que MeshCore esté desconectado.

Para comprobar el estado real de MeshCore, consulta `MESHCORE_STATUS` directamente en el puerto de control:

```bash
python3 - <<'PY'
import json
import socket

with socket.create_connection(("127.0.0.1", 8766), timeout=5) as sock:
    sock.sendall((json.dumps({"cmd": "MESHCORE_STATUS", "params": {}}) + "\n").encode())
    print(sock.makefile("r", encoding="utf-8").readline())
PY
```

Los campos esperados son:

```json
{
  "enabled": true,
  "available": true,
  "connected": true
}
```

## `serve`

Inicia el servidor HTTP utilizado por el broker para resolver los comandos `farma`.

```bash
python3 farmacias_guardia.py serve
```

Salida esperada:

```text
API farmacias escuchando en http://0.0.0.0:8788
```

El proceso permanece en primer plano hasta pulsar `Ctrl+C`. Para funcionamiento 24x7 debe ejecutarse mediante `systemd`.

### Endpoint `/health`

```bash
curl -s http://127.0.0.1:8788/health | python3 -m json.tool
```

Ejemplo:

```json
{
  "ok": true,
  "updated_at": "2026-07-24T08:30:00+02:00"
}
```

`ok` será verdadero cuando exista un listado local vigente.

### Endpoint `/query`

```bash
curl -s \
  -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "farma",
    "network": "meshcore",
    "source_id": "prueba",
    "channel": 1,
    "is_direct": true
  }' \
  http://127.0.0.1:8788/query | python3 -m json.tool
```

Ejemplo de respuesta:

```json
{
  "recognized": true,
  "messages": [
    "GUARDIA 24/07 [1/1]\nDELICIAS\nAv Madrid 185 · 976332929"
  ]
}
```

La API solamente genera el contenido de respuesta. El broker conserva la responsabilidad de:

- detectar el comando recibido por radio;
- aplicar el límite de cinco peticiones por hora;
- identificar al contacto origen;
- enviar la respuesta por DM en la misma red.

# Comandos disponibles por radio

El broker envía a `/query` los comandos recibidos por DM o en el canal `FARMACIA`.

```text
farma
```

Devuelve todas las farmacias de guardia disponibles.

```text
farma ayuda
```

Devuelve las localidades disponibles.

```text
farma zaragoza
```

Devuelve las áreas o barrios disponibles para Zaragoza.

```text
farma zaragoza <barrio>
```

Devuelve las farmacias del barrio solicitado.

```text
farma <localidad>
```

Devuelve las farmacias de la localidad cuando la fuente configurada dispone de esos registros.

La fuente incluida actualmente puede contener únicamente Zaragoza. La aparición de otras localidades depende de los datos devueltos por `FARMACIAS_SOURCE_URL` o del adaptador de fuente utilizado.

# Flujo operativo recomendado

## Primera instalación

```bash
cd /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
cp .env.example .env
nano .env

python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py preview
python3 farmacias_guardia.py doctor
python3 farmacias_guardia.py serve
```

En otra terminal:

```bash
curl -s http://127.0.0.1:8788/health | python3 -m json.tool
```

Después, desde el contenedor del broker:

```bash
docker exec -i meshnet-broker python3 - <<'PY'
import json
import urllib.request

payload = json.dumps({
    "text": "farma",
    "network": "meshcore",
    "source_id": "prueba-docker",
    "channel": 1,
    "is_direct": True,
}).encode()

request = urllib.request.Request(
    "http://172.17.0.1:8788/query",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=5) as response:
    print(response.read().decode())
PY
```

## Publicación manual segura

```bash
python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py preview
python3 farmacias_guardia.py send --force
python3 farmacias_guardia.py status
```

## Operación automática

- API permanente: `serve` mediante `meshnet-farmacias-api.service`.
- Envío completo diario: `send` todos los días a las 08:30 mediante
  `meshnet-farmacias-daily.timer`.
- Las consultas manuales `farma` siguen atendidas permanentemente por la API y
  no dependen del temporizador de difusión.
- Cada tres horas, `check --send` actualiza la copia local y, si encuentra
  incorporaciones, difunde exclusivamente las nuevas farmacias.

### Por qué antes los envíos posteriores podían incluir más farmacias

El envío de las 08:30 ejecuta `send`, que descarga en ese momento una
instantánea nueva de la fuente antes de publicarla. El antiguo temporizador de
cambios volvía a descargar la fuente cada 30 minutos; si el proveedor añadía
registros después de las 08:30, cambiaba el hash y se difundía de nuevo el
listado completo, ya con esas farmacias adicionales. Por tanto, no se añadían
datos dentro de MeshNet: eran actualizaciones posteriores de la fuente.

Ahora `meshnet-farmacias-check.timer` realiza la comprobación cada tres horas.
Las actualizaciones mantienen vigente la consulta manual, pero solamente las
altas generan el aviso `NUEVAS FARMACIAS DE GUARDIA`; nunca se repite el
listado completo de las 08:30.

# Instalación de systemd

```bash
cd /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now meshnet-farmacias-api.service
sudo systemctl enable meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
sudo systemctl restart meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
```

Si se actualiza una instalación que ya utilizaba la comprobación periódica,
hay que reemplazar sus unidades antiguas y reiniciar el temporizador. Es
importante verificar que el temporizador indica `OnUnitActiveSec=3h`:

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
sudo systemctl restart meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
systemctl cat meshnet-farmacias-check.service
```

Se usa `restart` deliberadamente: `enable --now` no reinicia una unidad que ya
estuviera activa antes de copiar una versión nueva. Después de actualizar, ambos
temporizadores deben mostrar una fecha en las columnas `NEXT` y `LEFT`:

```bash
systemctl is-active meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
systemctl list-timers --all meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
```

Una salida con `NEXT` igual a `-`, como la de un temporizador detenido, se
resuelve cargando y reiniciando las unidades:

```bash
sudo systemctl daemon-reload
sudo systemctl restart meshnet-farmacias-daily.timer meshnet-farmacias-check.timer
```

El temporizador diario no utiliza `Persistent=true`. De esta forma, si se
arranca o actualiza el servicio después de las 08:30, no recupera el envío
perdido a mediodía: programa directamente las 08:30 del día siguiente.

Comprobar servicios:

```bash
systemctl status meshnet-farmacias-api.service
systemctl list-timers --all | grep farmacias
```

Logs de la API:

```bash
journalctl -u meshnet-farmacias-api.service -f
```

Logs del envío diario:

```bash
journalctl -u meshnet-farmacias-daily.service -n 100 --no-pager
```

# Comprobación de recepción desde MeshCore

Un envío realizado por el broker muestra:

```text
[meshcore] enqueue -> chan_idx=1 ...
[meshcore-embedded TX] ...
```

Eso es un **TX hacia MeshCore**, no una consulta recibida.

Una consulta real procedente de otro nodo debe mostrar:

```text
[meshcore-embedded RX] ... text='farma'
```

Después, la API debe registrar:

```text
[farmacias-api] "POST /query HTTP/1.1" 200 -
```

Y el broker debe encolar la respuesta directa:

```text
[meshcore] enqueue -> contact=<prefijo-origen> ...
```

# Datos y estado

La aplicación crea su directorio de datos automáticamente. Los ficheros principales son:

```text
data/
├── current.json
└── state.json
```

- `current.json`: listado normalizado vigente y hash.
- `state.json`: último hash enviado, fecha, red, canal y número de mensajes.

No deben editarse manualmente durante la operación normal.

# Resolución de problemas

## `broker sin respuesta`

Comprueba:

```bash
ss -lnt | grep 8766
```

Y revisa:

```env
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766
```

## `iface manager not ready`

En `meshcore_only`, consulta `MESHCORE_STATUS`. La interfaz Meshtastic puede estar desactivada correctamente.

## La API responde en el host pero no desde Docker

Comprueba que escucha en todas las interfaces:

```env
FARMACIAS_API_HOST=0.0.0.0
```

Y prueba desde el contenedor usando la gateway Docker correcta.

## `canal FARMACIAS no configurado`

Configura un índice válido:

```env
FARMACIAS_MESHCORE_CHANNEL=1
```

El valor `-1` significa canal no configurado.

## `farma` no genera respuesta

Comprueba, en este orden:

1. El broker registra `[meshcore-embedded RX]` o el RX Meshtastic equivalente.
2. El canal recibido coincide con `FARMACIAS_MESHCORE_CHANNEL` o `FARMACIAS_MESHTASTIC_CHANNEL`.
3. `FARMACIAS_COMMAND_ENABLED=true` está visible dentro del contenedor.
4. El contenedor alcanza `FARMACIAS_SERVICE_URL`.
5. La API registra un `POST /query` con código `200`.
6. El evento RX contiene un identificador de contacto válido para contestar por DM.

## No aparecen Utebo u otras localidades

Ejecuta:

```bash
python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py status
```

La lista de localidades depende de los registros reales devueltos por la fuente. Si solo existen registros de Zaragoza, la API solo ofrecerá Zaragoza.

# Responsabilidades de cada componente

## Aplicación `farmacias_guardia`

- Descarga y normaliza datos.
- Mantiene el último listado válido.
- Comprueba cada tres horas si existen nuevas incorporaciones.
- Formatea mensajes por bytes.
- Expone `/health` y `/query`.
- Solicita al broker el listado completo diario de las 08:30 y avisos que
  contienen exclusivamente las nuevas incorporaciones detectadas.

## Broker

- Recibe los mensajes de radio.
- Detecta `farma` antes de puentes y otros servicios.
- Limita a cinco consultas por hora y contacto.
- Evita duplicados.
- Consulta la API independiente.
- Responde por DM mediante la misma red de origen.

## Bot

No participa en esta funcionalidad y puede permanecer detenido o no estar instalado.

## Sectores y barrios de Zaragoza

La fuente oficial del Ayuntamiento muestra las farmacias de guardia agrupadas
por sectores como **Delicias**, **Gran Vía**, **Centro**, **Romareda**,
**Las Fuentes** o **Avda. Cataluña-Barrio La Jota**. La aplicación obtiene ese
dato del bloque de guardia del registro.

El parser admite dos representaciones de la fuente:

```json
{
  "guardia": {
    "horario": "Abiertas de 9:15 h. a 9:15 h. del día siguiente",
    "sector": "Sector Delicias",
    "turno": "T-17"
  }
}
```

y el formato combinado:

```text
Abiertas de 9:15 h. a 9:15 h. del día siguiente. Sector Delicias. Turno: T-17
```

Las notas descriptivas posteriores al sector se eliminan cuando empiezan por
marcadores como `-Esquina`, `-Frente` o `-Junto`. Así, por ejemplo:

```text
Sector Delicias-Esquina C/ Biarritz-Urb. La Bombarda
```

se guarda como:

```text
Delicias
```

Los nombres compuestos oficiales que no son una indicación descriptiva se
conservan, por ejemplo:

```text
Avda. Cataluña-Barrio La Jota
```

Después de instalar esta versión es obligatorio regenerar `current.json`:

```bash
cd /home/meshnet/MeshNet-Bot/farmacias_guardia
python3 farmacias_guardia.py fetch
```

Comprobación rápida de los sectores guardados:

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("data/current.json")
payload = json.loads(path.read_text(encoding="utf-8"))
areas = sorted({
    str(row.get("area") or "").strip()
    for row in payload.get("pharmacies", [])
    if str(row.get("locality") or "").strip().casefold() == "zaragoza"
})
print("\n".join(f"- {area}" for area in areas if area))
PY
```

Consultas disponibles desde la malla:

```text
farma
farma ayuda
farma zaragoza
farma zaragoza delicias
farma zaragoza centro
farma zaragoza gran via
```

`farma zaragoza` enumera primero los sectores disponibles. Las búsquedas de
sector ignoran mayúsculas y tildes y admiten una coincidencia parcial cuando es
única.



## Arquitectura independiente definitiva

La aplicación reside íntegramente en:

```text
/home/meshnet/MeshNet-Bot/tools/farmacias_guardia
```

No se instala `farmacias_commands.py` dentro del broker y no es necesario
reconstruir su imagen Docker. El proceso `serve` realiza dos trabajos en hilos
separados:

1. Expone la API local `/health` y `/query` en el puerto 8788.
2. Se conecta como cliente al flujo JSONL del broker en `127.0.0.1:8765`.

Cuando recibe un evento MeshCore con `farma`:

- acepta mensajes directos y el canal configurado en
  `FARMACIAS_MESHCORE_CHANNEL`;
- obtiene `meshcore_pubkey_prefix` del emisor;
- genera la respuesta con los datos locales;
- responde por DM mediante el comando público `MESHCORE_SEND` del puerto de
  control `127.0.0.1:8766`.

Toda la configuración pertenece al `.env` de esta aplicación. No hay que añadir
variables `FARMACIAS_*` al `.env` del broker.

### Variables del listener

```env
BROKER_EVENT_HOST=127.0.0.1
BROKER_EVENT_PORT=8765
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766
FARMACIAS_COMMAND_LISTENER_ENABLED=1
FARMACIAS_LISTENER_RECONNECT_SECONDS=5
FARMACIAS_EVENT_DEDUP_SECONDS=30
```

### Logs esperados

Al iniciar el servicio:

```text
[farmacias-listener] conectando a 127.0.0.1:8765
[farmacias-listener] conectado a 127.0.0.1:8765
API farmacias escuchando en http://0.0.0.0:8788
```

Al atender una consulta:

```text
[farmacias-listener] atendido source=<prefijo> kind=contact channel=None command='farma' parts=1
```

Para observarlo:

```bash
journalctl -u meshnet-farmacias-api.service -f
```
