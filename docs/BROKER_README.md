# Broker MeshNet-Bot — guía de operación v7.0.35

El broker es el núcleo de comunicaciones. Mantiene las interfaces MeshCore/Meshtastic permitidas por `RADIO_PROFILE`, procesa recepción y transmisión, aplica colas, deduplicación, ACK, puentes y comandos locales, y expone el puerto de control utilizado por el bot y aplicaciones auxiliares.

## Principios operativos

- El broker es la autoridad sobre las interfaces disponibles.
- `meshcore_only` no debe intentar crear ni utilizar manejadores Meshtastic.
- Las aplicaciones independientes solicitan envíos al broker; no controlan directamente el nodo.
- Un resultado de cola aceptada no equivale a confirmación RF en todos los nodos.

## Ejecución

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d broker
docker logs -f meshnet-broker
```

## Puertos relevantes

- `8765`: interfaz JSONL bot/broker, según compose.
- `8766`: control de aplicaciones y comandos internos.
- Los puertos concretos deben verificarse con `docker compose ... config` y `.env`.

## Configuración mínima

```env
RADIO_PROFILE=meshcore_only
BROKER_HOST=meshnet-broker
BROKER_PORT=8765
BROKER_CTRL_PORT=8766
```

La configuración del nodo MeshCore o Meshtastic depende del perfil. Consulte [`RADIO_PROFILES.md`](RADIO_PROFILES.md).

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

## Reinicio

```bash
docker compose -f docker-compose.rpi.yml restart broker
docker logs --tail 200 meshnet-broker
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
docker exec meshnet-broker python3 -c 'import socket; print(socket.create_connection(("<IP_NODO>",4403),3))'
```

### `iface manager not ready`

La aplicación llegó al broker antes de que la interfaz de radio estuviera disponible. Revisar conexión del nodo, perfil y logs; no duplicar interfaces como solución.

### `meshtastic_disabled_by_radio_profile`

La petición intenta usar Meshtastic con un perfil que lo prohíbe. Corregir el destino o usar transporte `auto`/MeshCore.

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
```

## Archivos principales

- `source/Meshtastic_Broker.py`
- `source/radio_profile.py`
- `source/broker_task.py`
- `source/bridge_in_broker.py`
- `source/farmacias_commands.py`
- `source/emergencias_commands.py`
- `docker-compose.rpi.yml`
- `.env_example`
