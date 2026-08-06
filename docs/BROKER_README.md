# Broker MeshNet-Bot — guía de operación v7.0.36

El broker es el núcleo de comunicaciones. Mantiene las interfaces MeshCore/Meshtastic permitidas por `RADIO_PROFILE`, procesa recepción y transmisión, aplica colas, deduplicación, ACK, puentes y comandos locales, y expone los puertos utilizados por el bot, APRS y aplicaciones auxiliares.

## Principios operativos

- El broker es la autoridad sobre las interfaces disponibles.
- `meshcore_only` no debe intentar crear ni utilizar manejadores Meshtastic.
- Las aplicaciones independientes solicitan envíos al broker; no controlan directamente el nodo.
- El contenedor APRS utiliza el broker para entregar tráfico APRS a MeshCore/Meshtastic.
- Un resultado de cola aceptada no equivale a confirmación RF en todos los nodos.

## Ejecución

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d broker
docker logs -f meshnet-broker
```

## Puertos relevantes

| Puerto | Uso |
|---:|---|
| `8765` | Interfaz JSONL bot/broker y eventos consumidos por componentes internos |
| `8766` | Control de aplicaciones, envíos y comandos internos |
| `9464/UDP` | Control del contenedor APRS; escucha el proceso APRS, no el broker |

Los puertos concretos deben verificarse con `docker compose -f docker-compose.rpi.yml config` y `.env`.

## Configuración mínima

```env
RADIO_PROFILE=meshcore_only
BROKER_HOST=127.0.0.1
BROKER_PORT=8765
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766
```

En el Compose RPi, `bot`, `aprs` y `email-to-mesh` comparten la red del servicio `broker`. Por ello utilizan `127.0.0.1` para comunicarse con sus puertos.

## Integración APRS

El contenedor `meshnet-aprs`:

- recibe tramas RF mediante KISS TCP;
- puede recibir o enviar tráfico APRS-IS;
- convierte mensajes APRS en peticiones de envío al broker;
- escucha eventos Mesh para el mirror APRS-IS;
- recibe órdenes del bot por UDP 9464.

### APRS hacia MeshCore

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
```

Ejemplo:

```text
APRS: [CH1] Prueba de enlace
  -> meshnet-aprs
  -> control del broker
  -> MESHCORE_SEND channel_idx resuelto
```

### APRS hacia Meshtastic

```env
APRS_TO_MESHCORE=0
MESHTASTIC_CH=0
```

El perfil debe permitir Meshtastic. No fuerce este transporte bajo `meshcore_only`.

### Mirror Mesh hacia APRS-IS

El proceso APRS escucha eventos del broker y filtra por red/canal:

```env
APRSIS_PUSH_ENABLED=1
APRSIS_PUSH_TO=EB2XXX-7
APRSIS_PUSH_CHANNELS=meshtastic 0,1 meshcore 2
APRSIS_PUSH_PREFIX=1
APRSIS_PUSH_MIN_GAP_S=2.0
```

La deduplicación debe impedir que un mensaje originado en APRS vuelva a salir como si fuera un mensaje Mesh nuevo.

## Aplicaciones independientes

```env
FARMACIAS_COMMAND_ENABLED=true
FARMACIAS_SERVICE_URL=http://host.docker.internal:8788/query
EMERGENCIAS_COMMAND_ENABLED=true
EMERGENCIAS_SERVICE_URL=http://host.docker.internal:8789/query
```

Comprobar desde el contenedor:

```bash
docker exec meshnet-broker python3 -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:8788/health", timeout=3).read().decode())'
docker exec meshnet-broker python3 -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:8789/health", timeout=3).read().decode())'
```

## Ejemplo de despliegue del núcleo

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml pull broker bot aprs email-to-mesh
docker compose -f docker-compose.rpi.yml up -d broker bot aprs email-to-mesh
docker compose -f docker-compose.rpi.yml ps
```

Validación:

```bash
docker logs --tail 200 meshnet-broker
docker logs --tail 200 meshnet-aprs
docker logs --tail 100 meshnet-bot
docker logs --tail 100 meshnet-email-to-mesh
```

## Reinicio

```bash
docker compose -f docker-compose.rpi.yml restart broker
docker logs --tail 200 meshnet-broker
```

Al reiniciar el broker, los contenedores que comparten su red pueden sufrir una interrupción temporal. Si no recuperan la comunicación:

```bash
docker compose -f docker-compose.rpi.yml restart aprs bot email-to-mesh
```

## Diagnóstico

```bash
python3 scripts/radio-profile-check
docker compose -f docker-compose.rpi.yml ps
docker compose -f docker-compose.rpi.yml config
docker logs --since 15m meshnet-broker
```

### `No route to host`

Comprobar IP, ruta y puerto del nodo desde host y contenedor.

```bash
ping -c 3 <IP_NODO>
nc -vz <IP_NODO> 4403
docker exec meshnet-broker python3 -c 'import socket; s=socket.create_connection(("<IP_NODO>",4403),3); print("OK"); s.close()'
```

### `iface manager not ready`

La aplicación llegó al broker antes de que la interfaz de radio estuviera disponible. Revisar conexión del nodo, perfil y logs; no duplicar interfaces como solución.

### `meshtastic_disabled_by_radio_profile`

La petición intenta usar Meshtastic con un perfil que lo prohíbe. Corregir el destino o usar transporte `auto`/MeshCore.

### APRS recibe, pero el broker no publica

```bash
docker compose -f docker-compose.rpi.yml ps broker aprs
docker logs --tail 300 meshnet-aprs
docker logs --tail 300 meshnet-broker
```

Revisar `RADIO_PROFILE`, `APRS_TO_MESHCORE`, mapas de canal y rechazos de perfil.

### API auxiliar no disponible

`Connection refused` indica que el comando fue reconocido, pero la API del host no escucha o no es accesible desde Docker.

## Parada y recuperación

```bash
docker compose -f docker-compose.rpi.yml stop broker
docker compose -f docker-compose.rpi.yml start broker
```

Después de `docker compose down`:

```bash
docker compose -f docker-compose.rpi.yml up -d
```

## Pruebas

```bash
python3 -m unittest tests.test_radio_profile
python3 -m unittest tests.test_meshcore_tx_chunking
python3 -m unittest tests.test_farmacias_commands
python3 -m unittest tests.test_emergencias_commands
python3 -m unittest tests.test_aprsis_emergency_bulletin
```

## Archivos principales

- `source/Meshtastic_Broker.py`
- `source/radio_profile.py`
- `source/broker_task.py`
- `source/bridge_in_broker.py`
- `source/meshtastic_to_aprs.py`
- `source/farmacias_commands.py`
- `source/emergencias_commands.py`
- `docker-compose.rpi.yml`
- `.env_example`

## Documentación relacionada

- [`APRS_GATEWAY.md`](APRS_GATEWAY.md)
- [`RADIO_PROFILES.md`](RADIO_PROFILES.md)
- [`BOT_README.md`](BOT_README.md)
- [`OPERATIONS.md`](OPERATIONS.md)