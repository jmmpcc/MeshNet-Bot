# Despliegue APRS con KISS remoto para emergencias — v7.0.36

Guía especializada para operar MeshNet-Bot con el TNC KISS TCP, Direwolf o Soundmodem instalado en otro equipo de la red local.

La referencia general de APRS, APRS-IS, perfiles, comandos y diagnóstico está en [`APRS_GATEWAY.md`](APRS_GATEWAY.md).

## 1. Objetivo

Separar físicamente el puesto de radio APRS del equipo central:

```text
PC remoto / puesto de radio
  ├── Direwolf o Soundmodem
  ├── interfaz de audio/PTT
  ├── transceptor APRS
  └── servidor KISS TCP :8100
             │ LAN
             ▼
Raspberry Pi / centro de coordinación
  ├── meshnet-broker
  ├── meshnet-aprs
  ├── meshnet-bot, opcional
  └── nodo MeshCore o Meshtastic
```

Este diseño permite mantener APRS RF y la malla sin Internet siempre que exista conectividad LAN entre ambos equipos.

## 2. Requisitos

### Equipo central

- Proyecto en `/home/meshnet/MeshNet-Bot`.
- Docker Engine y `docker compose`.
- Contenedores `meshnet-broker` y `meshnet-aprs`.
- Nodo MeshCore o Meshtastic operativo.

### Equipo remoto

- Direwolf, Soundmodem o TNC compatible con KISS TCP.
- Audio, PTT y radio probados localmente.
- Puerto TCP accesible desde la Raspberry.
- Dirección IP estable o reserva DHCP.

## 3. Configuración del equipo central

En `/home/meshnet/MeshNet-Bot/.env`:

```env
APRS_CALL=EB2XXX-11
APRS_PATH=WIDE1-1

KISS_HOST=192.168.1.30
KISS_PORT=8100

APRS_CTRL_HOST=127.0.0.1
APRS_CTRL_PORT=9464
BROKER_HOST=127.0.0.1
BROKER_PORT=8765
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766

APRS_GATE_ENABLED=1
```

Solo `KISS_HOST` y `KISS_PORT` apuntan al equipo remoto. Los puertos del broker y del control APRS permanecen en `127.0.0.1` porque los contenedores comparten la red del servicio `broker`.

## 4. Configuración del equipo remoto

### Soundmodem

Active el servidor KISS TCP y configure:

```text
Bind: 0.0.0.0
Port: 8100
```

Reinicie Soundmodem después de guardar.

### Direwolf

La directiva exacta depende de la versión. Debe quedar un servidor KISS TCP escuchando en el puerto 8100 y accesible desde la LAN. Verifique el resultado con:

```bash
ss -ltnp | grep ':8100'
```

No exponga el puerto KISS a Internet. Limítelo mediante firewall a la IP de la Raspberry.

Ejemplo con UFW en el equipo remoto:

```bash
sudo ufw allow from 192.168.1.69 to any port 8100 proto tcp
```

Sustituya la IP por la dirección real del equipo central.

## 5. Comprobación previa

Desde la Raspberry:

```bash
ping -c 3 192.168.1.30
nc -vz 192.168.1.30 8100
```

Prueba desde el mismo contenedor APRS:

```bash
docker exec meshnet-aprs python3 -c '
import os,socket
h=os.getenv("KISS_HOST")
p=int(os.getenv("KISS_PORT","8100"))
s=socket.create_connection((h,p),5)
print(f"KISS remoto OK {h}:{p}")
s.close()
'
```

Esta segunda prueba es la decisiva porque reproduce la conectividad desde el proceso real.

## 6. Arranque

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker compose -f docker-compose.rpi.yml ps broker aprs
docker logs --tail 200 meshnet-aprs
```

El nombre correcto del contenedor es:

```text
meshnet-aprs
```

No utilizar nombres históricos como `meshtastic-aprs`.

La cabecera debe mostrar valores equivalentes a:

```text
[aprs] KISS=192.168.1.30:8100 CALL=EB2XXX-11 PATH=WIDE1-1
[aprs] BROKER_CTRL=127.0.0.1:8766
```

## 7. Ejemplo con MeshCore

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
```

Flujo:

```text
APRS RF
  -> radio y TNC remoto
  -> KISS TCP por LAN
  -> meshnet-aprs
  -> broker
  -> MeshCore
```

Ejemplo de mensaje APRS:

```text
[CH1] Puesto avanzado operativo
```

El prefijo se utiliza para resolver el canal y se elimina antes de presentar el mensaje final en la malla.

## 8. Ejemplo de envío desde Telegram

```text
/aprs canal 1 Prueba desde centro de coordinación
```

Flujo:

```text
Telegram
  -> meshnet-bot
  -> UDP 127.0.0.1:9464
  -> meshnet-aprs
  -> KISS TCP 192.168.1.30:8100
  -> TNC remoto
  -> APRS RF
```

## 9. Operación sin Internet

Sin Internet continúan disponibles:

- APRS RF mediante KISS remoto.
- APRS RF hacia MeshCore/Meshtastic.
- Mesh hacia APRS RF cuando se ordene localmente.
- Broker y aplicaciones locales.

No estarán disponibles:

- Telegram.
- APRS-IS.
- Servicios externos de Internet.

La caída de APRS-IS no debe impedir el funcionamiento KISS RF.

## 10. Prueba operativa controlada

1. Verifique `nc -vz` al puerto KISS.
2. Arranque `broker` y `aprs`.
3. Abra logs:

```bash
docker logs -f meshnet-aprs
```

4. Envíe una única trama APRS de prueba.
5. Confirme recepción en Direwolf/Soundmodem.
6. Confirme salida por RF mediante un receptor independiente.
7. Confirme recepción en la malla.
8. Repita en sentido Mesh → APRS RF.

No genere ráfagas repetitivas durante la validación.

## 11. Recuperación

### Reiniciar solo APRS

```bash
docker compose -f docker-compose.rpi.yml restart aprs
docker logs --tail 200 meshnet-aprs
```

### Recrear el contenedor

```bash
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
```

### Recuperar después de `docker compose down`

```bash
docker compose -f docker-compose.rpi.yml up -d broker aprs
```

### Reiniciar el TNC remoto

Después de reiniciar Direwolf o Soundmodem, compruebe de nuevo:

```bash
nc -vz 192.168.1.30 8100
docker compose -f docker-compose.rpi.yml restart aprs
```

## 12. Diagnóstico

### `Connection refused`

- El servidor KISS no está iniciado.
- El puerto configurado no coincide.
- El servicio escucha solo en `127.0.0.1`.
- El firewall bloquea la Raspberry.

### `No route to host`

- IP incorrecta.
- Equipo remoto apagado.
- Segmento o VLAN sin ruta.
- Wi-Fi desconectado.

### KISS conecta, pero no hay RF

- Revise PTT, audio y radio en el equipo remoto.
- Confirme que Direwolf/Soundmodem recibe la trama.
- Verifique frecuencia, squelch, ganancia y cableado.
- Compruebe con un receptor independiente.

### APRS RF funciona, pero no llega a la malla

```bash
docker logs --tail 300 meshnet-aprs
docker logs --tail 300 meshnet-broker
```

Revise `APRS_GATE_ENABLED`, `RADIO_PROFILE`, `APRS_TO_MESHCORE` y mapas de canales.

### Varias tramas visibles en SDR

Determine primero si son:

- múltiples `TX` locales;
- repeticiones de digipeaters;
- ecos recibidos por distintos caminos.

Compare los logs de `meshnet-aprs` y del TNC antes de modificar el código.

## 13. Lista de despliegue rápido

Equipo central:

```env
KISS_HOST=192.168.1.30
KISS_PORT=8100
APRS_CALL=EB2XXX-11
APRS_GATE_ENABLED=1
```

Comandos:

```bash
cd /home/meshnet/MeshNet-Bot
nc -vz 192.168.1.30 8100
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker logs --tail 200 meshnet-aprs
```

Equipo remoto:

```text
KISS TCP: 0.0.0.0:8100
Firewall: permitir solo la Raspberry
Radio, audio y PTT: verificados
```

## 14. Seguridad operativa

- No exponer KISS TCP a Internet.
- Utilizar una LAN o VPN controlada.
- Restringir el firewall por IP.
- No usar rutas AX.25 innecesariamente amplias.
- Respetar `NOGATE` y `RFONLY`.
- Mantener indicativos, frecuencias y potencia dentro de la autorización aplicable.
- Documentar qué estación transmite realmente por RF.