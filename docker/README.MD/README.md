# MeshNet-Bot con Docker — v7.0.36

Guía de despliegue del núcleo Docker. La operación completa se documenta en [`../../docs/OPERATIONS.md`](../../docs/OPERATIONS.md).

## Preparación

```bash
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
cp .env_example .env
nano .env
python3 scripts/radio-profile-check
```

No versionar `.env`.

## Servicios

| Servicio Compose | Contenedor | Función |
|---|---|---|
| `broker` | `meshnet-broker` | Interfaces de radio, colas y control |
| `bot` | `meshnet-bot` | Telegram y programación |
| `aprs` | `meshnet-aprs` | KISS TCP, APRS RF, APRS-IS, iGate y mirror |
| `email-to-mesh` | `meshnet-email-to-mesh` | IMAP, SMTP y contactos |
| `bridgehub-bc` | `meshnet-bridge-bc` | Puente opcional |

`bot`, `aprs` y `email-to-mesh` utilizan `network_mode: service:broker`. Comparten la pila de red del broker.

## Raspberry Pi

```bash
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

## Despliegue mínimo broker + MeshCore

```bash
docker compose -f docker-compose.rpi.yml up -d broker
docker logs --tail 200 meshnet-broker
```

## Despliegue broker + bot

```bash
docker compose -f docker-compose.rpi.yml up -d broker bot
docker compose -f docker-compose.rpi.yml ps broker bot
```

## Despliegue APRS RF con KISS local

Configuración:

```env
APRS_CALL=EB2XXX-11
APRS_PATH=WIDE1-1
KISS_HOST=host.docker.internal
KISS_PORT=8100
APRS_CTRL_HOST=127.0.0.1
APRS_CTRL_PORT=9464
APRS_GATE_ENABLED=1
```

Arranque:

```bash
docker compose -f docker-compose.rpi.yml pull broker aprs
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker compose -f docker-compose.rpi.yml ps broker aprs
docker logs --tail 200 meshnet-aprs
```

Prueba KISS:

```bash
docker exec meshnet-aprs python3 -c '
import os,socket
h=os.getenv("KISS_HOST","127.0.0.1")
p=int(os.getenv("KISS_PORT","8100"))
s=socket.create_connection((h,p),5)
print(f"KISS OK {h}:{p}")
s.close()
'
```

## Despliegue APRS con TNC remoto

```env
KISS_HOST=192.168.1.30
KISS_PORT=8100
```

Comprobación:

```bash
nc -vz 192.168.1.30 8100
docker compose -f docker-compose.rpi.yml up -d broker aprs
```

El TNC remoto debe escuchar en `0.0.0.0:8100` o en la IP de la LAN, y su firewall debe permitir únicamente la Raspberry.

## Añadir APRS-IS

```env
APRSIS_USER=EB2XXX-11
APRSIS_PASSCODE=12345
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=m/20
```

El entrypoint del contenedor añade APRS-IS solo si usuario y passcode están definidos.

Prueba TCP:

```bash
docker exec meshnet-aprs python3 -c '
import socket
s=socket.create_connection(("rotate.aprs2.net",14580),5)
print("TCP APRS-IS OK")
s.close()
'
```

## Ejemplo MeshCore + APRS

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
```

Un mensaje APRS como:

```text
[CH1] Prueba de enlace
```

se procesa mediante `meshnet-aprs` y se entrega al broker para su envío por MeshCore.

## Mirror Mesh hacia APRS-IS

```env
APRSIS_PUSH_ENABLED=1
APRSIS_PUSH_TO=EB2XXX-7
APRSIS_PUSH_CHANNELS=meshcore 1
APRSIS_PUSH_PREFIX=1
APRSIS_PUSH_MIN_GAP_S=2.0
```

Mantenerlo desactivado cuando no se necesite.

## Despliegue email-to-mesh

```bash
docker compose -f docker-compose.rpi.yml up -d email-to-mesh
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker logs --tail 200 meshnet-email-to-mesh
```

El servicio usa la imagen del broker, ejecuta `/app/source/email_to_mesh.py`, comparte la red del broker y monta `./bot_data:/app/bot_data:rw`.

## Operación general

```bash
docker compose -f docker-compose.rpi.yml ps
docker compose -f docker-compose.rpi.yml restart broker
docker compose -f docker-compose.rpi.yml restart aprs
docker compose -f docker-compose.rpi.yml restart email-to-mesh
docker compose -f docker-compose.rpi.yml down
docker compose -f docker-compose.rpi.yml up -d
```

No usar `down -v` salvo que se pretenda borrar volúmenes.

## Recreación individual

```bash
docker compose -f docker-compose.rpi.yml up -d --force-recreate broker
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

## Actualización

```bash
git status
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

Actualizar solo APRS:

```bash
docker compose -f docker-compose.rpi.yml pull aprs
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
```

## Conexión a aplicaciones del host

Farmacias y emergencias se ejecutan fuera de Docker. El broker accede mediante `host.docker.internal`:

```bash
docker exec meshnet-broker python3 -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:8788/health", timeout=3).read().decode())'
docker exec meshnet-broker python3 -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:8789/health", timeout=3).read().decode())'
```

## Diagnóstico

```bash
docker compose -f docker-compose.rpi.yml config --services
docker compose -f docker-compose.rpi.yml config
docker compose -f docker-compose.rpi.yml ps
docker inspect meshnet-broker --format '{{json .NetworkSettings.Networks}}'
docker logs --tail 200 meshnet-broker
docker logs --tail 300 meshnet-aprs
docker logs --tail 200 meshnet-email-to-mesh
```

Un contenedor `Exited` requiere revisar su log antes de recrearlo.

## Referencias

- [`../../docs/APRS_GATEWAY.md`](../../docs/APRS_GATEWAY.md)
- [`../../docs/EMAIL_TO_MESH.md`](../../docs/EMAIL_TO_MESH.md)
- [`../../docs/OPERATIONS.md`](../../docs/OPERATIONS.md)
- [`../../docs/RADIO_PROFILES.md`](../../docs/RADIO_PROFILES.md)