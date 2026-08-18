# Conexión MeshCore: TCP y Serial

Este documento describe únicamente el transporte entre MeshNet-Bot y un nodo
MeshCore Companion. No cambia `RADIO_PROFILE`, los mapas de canales, la BBS ni
las pasarelas existentes.

## Variables que mandan

La selección del transporte MeshCore se realiza con `MESHCORE_MODE`.

- `MESHCORE_MODE=tcp`: conexión al Companion MeshCore por IP.
- `MESHCORE_MODE=serial`: conexión directa a un Serial Companion mediante un
  dispositivo Linux como `/dev/ttyUSB0` o `/dev/ttyACM0`.

`MESH_TRANSPORT` pertenece al transporte Meshtastic histórico y no sustituye
`MESHCORE_MODE`. En perfiles que incluyen ambas radios pueden existir las dos
variables simultáneamente.

## Modo TCP

Configuración mínima:

```dotenv
MESHCORE_ENABLE=1
MESHCORE_MODE=tcp
MESHCORE_TCP_HOST=192.168.1.21
MESHCORE_TCP_PORT=5000
```

En este modo `MESHCORE_SERIAL_PORT` y `MESHCORE_SERIAL_BAUD` no intervienen.
Pueden quedar comentadas o conservar valores anteriores sin que el cliente TCP
las utilice.

El firmware del nodo debe ofrecer una interfaz Companion accesible mediante el
servidor TCP configurado en el propio dispositivo o en el transporte utilizado
por ese nodo.

## Modo Serial

Configuración mínima:

```dotenv
MESHCORE_ENABLE=1
MESHCORE_MODE=serial
MESHCORE_SERIAL_PORT=/dev/ttyUSB0
MESHCORE_SERIAL_BAUD=115200
```

El puerto debe obtenerse del host donde se ejecuta Docker:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

No debe suponerse que siempre será `/dev/ttyACM0`. Adaptadores USB-UART suelen
aparecer como `/dev/ttyUSB0`; dispositivos con USB CDC pueden aparecer como
`/dev/ttyACM0`.

`docker-compose.rpi.yml` mantiene el broker con acceso privilegiado a los
dispositivos del host y no fija un nombre concreto de TTY. La selección se hace
exclusivamente mediante `MESHCORE_SERIAL_PORT`.

### Requisito de firmware

Abrir correctamente `/dev/ttyUSB0` o `/dev/ttyACM0` no demuestra que el nodo sea
un Serial Companion. El firmware debe estar compilado/flasheado como MeshCore
Serial Companion y responder al `APP_START` que envía `meshcore_py`.

Una secuencia como esta:

```text
Serial Connection started
Connected successfully: /dev/ttyUSB0
Sending appstart command
No response from meshcore node, disconnecting
Are you sure your node is a serial companion ?
```

significa que el transporte serie físico funciona pero el firmware no completa
el protocolo Companion.

## Perfiles de radio

El transporte MeshCore es independiente del perfil. Los mismos bloques TCP o
Serial pueden utilizarse cuando el perfil habilita MeshCore:

```dotenv
RADIO_PROFILE=meshcore_only
```

```dotenv
RADIO_PROFILE=meshtastic_a_meshcore_embedded_b
```

```dotenv
RADIO_PROFILE=meshcore_a_meshtastic_embedded_b
```

El perfil decide qué radios existen y cuál es la salida predeterminada. La
variable `MESHCORE_MODE` decide únicamente cómo se alcanza el Companion
MeshCore.

## Cambio de TCP a Serial

Partiendo de una configuración TCP que funciona:

```dotenv
MESHCORE_MODE=tcp
MESHCORE_TCP_HOST=192.168.1.21
MESHCORE_TCP_PORT=5000
```

cambiar a:

```dotenv
MESHCORE_MODE=serial
MESHCORE_SERIAL_PORT=/dev/ttyUSB0
MESHCORE_SERIAL_BAUD=115200
```

No es necesario eliminar `MESHCORE_TCP_HOST` ni `MESHCORE_TCP_PORT`; quedan
inactivos mientras `MESHCORE_MODE=serial`.

## Cambio de Serial a TCP

Partiendo de:

```dotenv
MESHCORE_MODE=serial
MESHCORE_SERIAL_PORT=/dev/ttyUSB0
MESHCORE_SERIAL_BAUD=115200
```

cambiar a:

```dotenv
MESHCORE_MODE=tcp
MESHCORE_TCP_HOST=192.168.1.21
MESHCORE_TCP_PORT=5000
```

Los valores serial pueden conservarse para un cambio posterior; no se utilizan
en modo TCP.

## Comprobaciones operativas

Variables efectivas del broker:

```bash
docker exec meshnet-broker env | grep -E 'MESHCORE_MODE|MESHCORE_TCP|MESHCORE_SERIAL'
```

Dispositivos serie visibles en el host:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Dispositivos visibles dentro del broker:

```bash
docker exec meshnet-broker sh -c 'ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true'
```

En TCP no es necesario que exista ningún dispositivo TTY. En Serial deben
coincidir `MESHCORE_SERIAL_PORT`, el dispositivo visible en el host y el visible
en el contenedor.

## Configuración recomendada para un `.env` reutilizable

Se pueden conservar ambas configuraciones y cambiar solo `MESHCORE_MODE`:

```dotenv
MESHCORE_ENABLE=1

# Selección activa: tcp | serial
MESHCORE_MODE=tcp

# Parámetros TCP
MESHCORE_TCP_HOST=192.168.1.21
MESHCORE_TCP_PORT=5000

# Parámetros Serial
MESHCORE_SERIAL_PORT=/dev/ttyUSB0
MESHCORE_SERIAL_BAUD=115200
```

El código selecciona exclusivamente el bloque correspondiente al valor de
`MESHCORE_MODE`. Esto evita tener que reescribir el `.env` al alternar entre un
nodo de red y un nodo conectado físicamente al host.
