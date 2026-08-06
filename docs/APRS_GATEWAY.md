# APRS y APRS-IS en MeshNet-Bot — v7.0.38

Guía operativa completa del contenedor `meshnet-aprs`, servicio Compose `aprs`.

El componente ejecuta `source/meshtastic_to_aprs.py` mediante `docker/entrypoint_aprs.sh`, conecta con un TNC KISS TCP para APRS por RF, puede mantener conexión con APRS-IS y se integra con el broker para intercambiar mensajes con MeshCore o Meshtastic según el perfil de radio.

## 1. Componentes y puertos

| Elemento | Valor habitual | Función |
|---|---:|---|
| Servicio Compose | `aprs` | Nombre usado por `docker compose` |
| Contenedor | `meshnet-aprs` | Nombre visible en Docker |
| Imagen | `ghcr.io/jmmpcc/meshnet-bot-aprs:latest` | Imagen publicada en GHCR |
| Programa | `/app/source/meshtastic_to_aprs.py` | Gateway APRS |
| Control UDP | `127.0.0.1:9464` | Peticiones del bot y otros componentes |
| Broker JSONL | `127.0.0.1:8765` | Eventos y recepción del broker |
| Control broker | `127.0.0.1:8766` | Envíos hacia la malla |
| KISS TCP | `KISS_HOST:KISS_PORT` | Direwolf, Soundmodem o TNC compatible |
| APRS-IS | `APRSIS_HOST:APRSIS_PORT` | Red APRS-IS, normalmente `rotate.aprs2.net:14580` |

En `docker-compose.rpi.yml`, `aprs` utiliza `network_mode: service:broker`. Por ello comparte la pila de red del broker y puede usar `127.0.0.1` para los puertos del broker y para el control UDP.

## 2. Flujos disponibles

Los flujos son independientes y pueden combinarse:

```text
Telegram / control UDP
        │
        ▼
meshnet-aprs ──KISS TCP──> TNC/Direwolf/Soundmodem ──> APRS RF
        │
        └────────────────────────────────────────────> APRS-IS

APRS RF ──> TNC KISS ──> meshnet-aprs ──> broker ──> MeshCore/Meshtastic

APRS-IS ─────────────────> meshnet-aprs ──> broker ──> MeshCore/Meshtastic

MeshCore/Meshtastic ──> broker ──> meshnet-aprs ──> APRS-IS dirigido
```

Funciones principales:

- Envío manual desde Telegram hacia APRS RF y, cuando corresponda, APRS-IS.
- Recepción APRS por RF a través de KISS.
- iGate de tramas RF hacia APRS-IS cuando existen credenciales válidas.
- Reenvío APRS hacia MeshCore o Meshtastic.
- Mirror selectivo de mensajes Mesh hacia un destinatario APRS-IS.
- Tratamiento de emergencias y boletines APRS/APRS-IS.
- Fragmentación de textos largos y pausa entre partes.
- Deduplicación y protección frente a bucles.

## 3. Requisitos

- Indicativo y SSID válidos para APRS.
- TNC KISS TCP, Direwolf o Soundmodem accesible desde la Raspberry.
- Equipo de radio y configuración legalmente autorizados.
- Broker MeshNet operativo.
- Credenciales APRS-IS cuando se quiera usar Internet.
- Nodo MeshCore o Meshtastic configurado según `RADIO_PROFILE`.

APRS-IS no sustituye la configuración RF. Son transportes distintos que el gateway puede utilizar simultáneamente.

## 4. Configuración mínima de APRS RF

En `/home/meshnet/MeshNet-Bot/.env`:

```env
APRS_CALL=EB2XXX-11
APRS_PATH=WIDE1-1

KISS_HOST=host.docker.internal
KISS_PORT=8100

APRS_CTRL_HOST=127.0.0.1
APRS_CTRL_PORT=9464

BROKER_HOST=127.0.0.1
BROKER_PORT=8765
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766

APRS_GATE_ENABLED=1
APRS_DEBUG=0
APRS_MAX_LEN=67
APRS_RF_PART_DELAY_S=2.0
APRS_RF_BAUD=1200
```

### Elección de `KISS_HOST`

| Escenario | Valor recomendado |
|---|---|
| Direwolf/Soundmodem en la misma Raspberry, fuera de Docker | `host.docker.internal` |
| TNC KISS en otro equipo de la LAN | IP del equipo, por ejemplo `192.168.1.30` |
| TNC dentro del mismo espacio de red del broker | `127.0.0.1` solo cuando realmente escucha en esa pila de red |

No utilizar `127.0.0.1` para un TNC ejecutado en el host si el proceso no comparte la red del contenedor. En el Compose RPi incluido, `host.docker.internal` resuelve la puerta de enlace del host.

## 5. Configuración de APRS-IS

```env
APRSIS_USER=EB2XXX-11
APRSIS_PASSCODE=12345
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=m/20
```

El entrypoint solo añade los argumentos APRS-IS cuando `APRSIS_USER` y `APRSIS_PASSCODE` contienen valores. Si falta uno de ellos, el contenedor arranca sin conexión APRS-IS.

`APRSIS_FILTER=m/20` solicita tráfico situado aproximadamente dentro de 20 km. Debe adaptarse a la zona y al tráfico necesario para no descargar información irrelevante.

## 6. Despliegue completo en Raspberry Pi

### 6.1 Preparar el proyecto

```bash
cd /home/meshnet/MeshNet-Bot
cp .env_example .env
nano .env
python3 scripts/radio-profile-check
```

### 6.2 Comprobar el Compose efectivo

```bash
docker compose -f docker-compose.rpi.yml config --services
docker compose -f docker-compose.rpi.yml config | sed -n '/aprs:/,/^[^ ]/p'
```

Debe aparecer el servicio `aprs`.

### 6.3 Descargar y arrancar

```bash
docker compose -f docker-compose.rpi.yml pull broker aprs
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker compose -f docker-compose.rpi.yml ps broker aprs
```

### 6.4 Verificar el arranque

```bash
docker logs --tail 200 meshnet-aprs
```

La cabecera del entrypoint muestra valores equivalentes a:

```text
[aprs] KISS=host.docker.internal:8100 CALL=EB2XXX-11 PATH=WIDE1-1
[aprs] BROKER_CTRL=127.0.0.1:8766
```

No debe aparecer `CALL=NOCALL` en una instalación operativa.

## 7. Ejemplos de despliegue

### 7.1 Raspberry Pi con Direwolf local y MeshCore

```env
RADIO_PROFILE=meshcore_only
APRS_CALL=EB2XXX-11
APRS_PATH=WIDE1-1
KISS_HOST=host.docker.internal
KISS_PORT=8100
APRS_GATE_ENABLED=1
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
APRSIS_USER=EB2XXX-11
APRSIS_PASSCODE=12345
APRSIS_FILTER=m/30
```

Resultado:

- APRS RF entra por Direwolf/KISS.
- Los mensajes `[CH1]` se envían al `channel_idx` MeshCore resuelto.
- Las tramas válidas pueden subirse a APRS-IS.
- Los envíos manuales del bot salen por RF mediante el mismo TNC.

### 7.2 Raspberry Pi con TNC remoto en otro ordenador

```env
APRS_CALL=EB2XXX-11
KISS_HOST=192.168.1.30
KISS_PORT=8100
APRS_GATE_ENABLED=1
```

Comprobación desde la Raspberry:

```bash
nc -vz 192.168.1.30 8100
```

El firewall del equipo remoto debe permitir TCP 8100 únicamente desde la Raspberry o la LAN necesaria.

### 7.3 APRS RF sin APRS-IS

```env
APRS_CALL=EB2XXX-11
KISS_HOST=host.docker.internal
KISS_PORT=8100
APRSIS_USER=
APRSIS_PASSCODE=
```

El gateway puede trabajar con KISS y malla sin abrir sesión APRS-IS.

### 7.4 Mirror MeshCore hacia APRSDroid

```env
RADIO_PROFILE=meshcore_only
APRSIS_USER=EB2XXX-11
APRSIS_PASSCODE=12345
APRSIS_PUSH_ENABLED=1
APRSIS_PUSH_TO=EB2XXX-7
APRSIS_PUSH_CHANNELS=meshcore 1
APRSIS_PUSH_PREFIX=1
APRSIS_PUSH_MIN_GAP_S=2.0
```

También puede activarse temporalmente desde Telegram:

```text
/aprsis_push on meshcore 1
/aprsis_push off
```

## 8. Integración con perfiles de radio

### `meshcore_only`

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
```

Los mensajes APRS destinados a la malla deben resolverse hacia MeshCore. Los índices `[CHx]` se traducen mediante `MESHCORE_CHANNEL_MAP`; si no existe mapa, se utiliza el índice nativo equivalente cuando la implementación lo permite.

### Perfil Meshtastic

```env
RADIO_PROFILE=
APRS_TO_MESHCORE=0
MESHTASTIC_CH=0
```

Los mensajes APRS se entregan mediante el transporte Meshtastic del broker.

### Perfil mixto

No se debe asumir que el mismo número representa el mismo canal en ambas redes. Configure explícitamente mapas y destinos, y compruebe el resultado en los logs del broker.

## 9. Comandos funcionales desde Telegram

### Activar o desactivar el gate

```text
/aprs_on
/aprs_off
```

### Enviar a APRS

```text
/aprs canal 1 Muy buenas tardes
```

Ejemplo de respuesta esperada:

```text
APRS enviado a pasarela y malla.
Destino APRS: broadcast
Canal Mesh: 1
Chunks APRS: 1
Pasarela APRS: OK
```

### Mirror Mesh hacia APRS-IS

```text
/aprsis_push on all
/aprsis_push on meshtastic 0,1 meshcore 2
/aprsis_push off
```

Los comandos exactos disponibles dependen de la versión instalada del bot. Compruébelos con `/help` y los logs del contenedor.

## 10. Envío APRS hacia la malla

Formatos aceptados habituales:

```text
[CH1] Mensaje al canal 1
[CH 1] Mensaje al canal 1
[CANAL1] Mensaje al canal 1
```

Para MeshCore también se admiten etiquetas específicas cuando están implementadas y configuradas:

```text
[MC1] Mensaje al channel_idx 1
[MC1/ZARAGOZA] Mensaje regional
```

El prefijo de encaminamiento se elimina antes de mostrar el texto final en la malla.

### Programación diferida

```text
[CH3+10] Aviso dentro de 10 minutos
```

La programación se procesa localmente por el gateway. Antes de utilizarla en servicio real, valide el formato con una prueba controlada y compruebe el log.

## 11. Ejemplos de prueba funcional

### 11.1 Verificar conectividad KISS desde el contenedor

```bash
docker exec meshnet-aprs python3 -c '
import os, socket
host=os.getenv("KISS_HOST", "127.0.0.1")
port=int(os.getenv("KISS_PORT", "8100"))
s=socket.create_connection((host, port), timeout=5)
print(f"KISS OK {host}:{port}")
s.close()
'
```

### 11.2 Verificar resolución del host

```bash
docker exec meshnet-aprs getent hosts host.docker.internal
```

### 11.3 Enviar una petición de prueba al control UDP

```bash
docker exec -i meshnet-aprs python3 -c '
import json, socket
request={
  "mode":"aprsis_emergency_bulletin",
  "event_id":"TEST-DOC-001",
  "severity":"high",
  "status":"resolved",
  "text":"PRUEBA TECNICA finalizada."
}
s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(10)
s.sendto(json.dumps(request).encode(), ("127.0.0.1", 9464))
data,_=s.recvfrom(65535)
print(json.dumps(json.loads(data.decode()), indent=2))
s.close()
'
```

La respuesta puede indicar `sent: true` o una razón operativa como `rate_limited`. Un resultado limitado confirma que el endpoint respondió, pero no que se haya transmitido una nueva trama.

### 11.4 Observar transmisiones y deduplicación

```bash
docker logs -f meshnet-aprs 2>&1 | grep --line-buffered -E \
'\[ctrl→aprs\] TX|RF TX|APRS-IS|duplicado|DEDUP|rate_limited'
```

### 11.5 Comprobar APRS-IS

```bash
docker logs --tail 300 meshnet-aprs | grep -iE 'aprs-is|login|rotate\.aprs2\.net|passcode|filter'
```

La confirmación definitiva debe realizarse también en un cliente APRS-IS o servicio de monitorización, teniendo en cuenta retardos, filtros y políticas de la red.

## 12. Fragmentación y rutas AX.25

```env
APRS_MAX_LEN=67
APRS_RF_PART_DELAY_S=2.0
APRS_RF_BAUD=1200
APRS_PATH=WIDE1-1
APRS_BOT_PATH=
```

- `APRS_MAX_LEN`: tamaño máximo de texto antes de dividirlo.
- `APRS_RF_PART_DELAY_S`: pausa entre partes consecutivas.
- `APRS_RF_BAUD`: utilizado para estimar el tiempo de aire.
- `APRS_PATH`: ruta general del gateway.
- `APRS_BOT_PATH`: ruta específica de envíos inmediatos del bot.

Para transmisión RF local sin repetidores:

```env
APRS_BOT_PATH=none
```

No aumente rutas ni repita tramas para compensar problemas de cobertura. Primero revise TNC, audio, PTT, potencia, antena, temporización y duplicados.

## 13. Emergencias APRS y APRS-IS

La lógica de emergencias puede:

- detectar palabras clave y destinos especiales;
- clasificar eventos por posición y distancia;
- reenviar a canales Mesh dedicados;
- emitir boletines APRS-IS;
- aplicar rate limit y deduplicación;
- mantener flujos incluso cuando el gate general está desactivado, según configuración.

Variables habituales:

```env
APRS_EMERGENCY_KEYWORDS=EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA
APRS_EMERGENCY_DESTS=EMERGENCY,EMERG,SOS
HOME_LAT=41.638
HOME_LON=-0.903
APRS_EMERGENCY_MAX_KM=50
MESH_EMERGENCY_CHANNELS=1,2,4

# Boletines públicos APRS-IS de Emergencias
APRSIS_PUSH_ENABLED=1
APRSIS_EMERGENCY_BULLETIN_ENABLED=1
APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL=high
APRSIS_EMERGENCY_BULLETIN_GROUP=EMERG
```

### Grupos de boletines APRS-IS

`APRSIS_EMERGENCY_BULLETIN_GROUP` admite hasta cinco caracteres alfanuméricos.
El gateway normaliza el valor a mayúsculas y elimina espacios, guiones y otros
signos para respetar el addressee APRS de nueve caracteres.

| Configuración | Boletines generados |
|---|---|
| Variable vacía | `BLN0` ... `BLN9` |
| `EMERG` | `BLN0EMERG` ... `BLN9EMERG` |
| `AEMET` | `BLN0AEMET` ... `BLN9AEMET` |

### Catálogo reservado de MeshNet-Bot

| Fuente | Variable | Grupo recomendado | Estado v7.0.38 |
|---|---|---|---|
| Emergencias | `APRSIS_EMERGENCY_BULLETIN_GROUP` | `EMERG` | Activo cuando se autoriza la salida |
| AEMET | `APRSIS_AEMET_BULLETIN_GROUP` | `AEMET` | Reservado, sin publicación automática |
| Farmacias | `APRSIS_FARMACIAS_BULLETIN_GROUP` | `FARMA` | Reservado, sin publicación automática |
| Noticias | `APRSIS_NEWS_BULLETIN_GROUP` | `NEWS` | Reservado, sin publicación automática |
| Sistema MeshNet | `APRSIS_SYSTEM_BULLETIN_GROUP` | `MESH` | Reservado, sin publicación automática |
| Pruebas | `APRSIS_TEST_BULLETIN_GROUP` | `TEST` | Reservado, sin publicación automática |

Las variables reservadas únicamente establecen nombres. No habilitan servicios, no abren sockets y no publican boletines. Cada aplicación futura deberá disponer de su propio interruptor, filtro, deduplicación y rate limit antes de conectarse al gateway.

El grupo no necesita registro previo en MeshNet-Bot. Debe elegirse un nombre
claro y estable. Cambiar de grupo conserva el número asignado al evento activo:
por ejemplo, un evento almacenado como `BLN4` pasa a `BLN4EMERG`. El cambio de
grupo fuerza una publicación nueva y no queda bloqueado por la deduplicación o
el intervalo mínimo de la asignación anterior.

Para mantener exactamente el comportamiento anterior:

```env
APRSIS_EMERGENCY_BULLETIN_GROUP=
```

La operación KISS remota de emergencias se documenta además en [`APRS_Remote_KISS_Emergency.md`](APRS_Remote_KISS_Emergency.md).

## 14. NOGATE, RFONLY, bucles y duplicados

Las marcas `NOGATE` y `RFONLY` deben respetarse para evitar que tráfico definido como local se propague hacia Internet u otros sistemas.

El gateway mantiene cachés y claves recientes para impedir bucles entre:

- APRS RF;
- APRS-IS;
- broker;
- MeshCore;
- Meshtastic.

Si se observan varias tramas RF aparentemente iguales:

1. Compruebe cuántos `TX` aparecen en `meshnet-aprs`.
2. Compruebe el log de Direwolf/Soundmodem.
3. Determine si son transmisiones locales o repeticiones de digipeaters.
4. Revise `APRS_PATH` y `APRS_BOT_PATH`.
5. No atribuya automáticamente cada trama recibida por SDR a un envío múltiple del gateway.

## 15. Operación Docker

### Estado

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps aprs
docker inspect meshnet-aprs --format '{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}'
```

### Reinicio

```bash
docker compose -f docker-compose.rpi.yml restart aprs
```

### Parada y arranque

```bash
docker compose -f docker-compose.rpi.yml stop aprs
docker compose -f docker-compose.rpi.yml start aprs
```

### Recreación

```bash
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
```

### Actualización individual

```bash
docker compose -f docker-compose.rpi.yml pull aprs
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
```

### Logs

```bash
docker logs --tail 300 meshnet-aprs
docker logs -f meshnet-aprs
```

## 16. Recuperación tras reinstalación

Antes de reinstalar:

```bash
cd /home/meshnet/MeshNet-Bot
cp -a .env "$HOME/meshnet-aprs.env.backup"
cp -a bot_data "$HOME/meshnet-bot_data.backup"
```

Después de restaurar el repositorio:

```bash
cp "$HOME/meshnet-aprs.env.backup" /home/meshnet/MeshNet-Bot/.env
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml pull broker aprs
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker logs --tail 200 meshnet-aprs
```

Compruebe de nuevo la conectividad KISS y APRS-IS. Restaurar el `.env` no garantiza que Direwolf, Soundmodem, audio o firewall sigan configurados en el host remoto.

## 17. Diagnóstico por síntomas

### `Connection refused` hacia KISS

```bash
nc -vz "$KISS_HOST" "$KISS_PORT"
sudo ss -ltnp | grep ':8100'
docker logs --tail 200 meshnet-aprs
```

Causas habituales:

- TNC no arrancado.
- Dirección incorrecta.
- Servicio ligado solo a `127.0.0.1` en otro equipo.
- Firewall.
- Puerto distinto.

### No conecta a APRS-IS

Revise:

- `APRSIS_USER` con SSID correcto.
- Passcode correspondiente al indicativo base.
- DNS y salida TCP 14580.
- Host y filtro.
- Reloj del sistema.

```bash
docker exec meshnet-aprs getent hosts rotate.aprs2.net
docker exec meshnet-aprs python3 -c 'import socket; socket.create_connection(("rotate.aprs2.net",14580),5).close(); print("TCP APRS-IS OK")'
```

### APRS llega por RF pero no entra en la malla

Revise:

```env
APRS_GATE_ENABLED=1
APRS_ALLOWED_SOURCES=
APRS_TO_MESHCORE=
```

Después:

```bash
docker logs --tail 300 meshnet-aprs
docker logs --tail 300 meshnet-broker
```

Confirme también el perfil y el mapa de canales.

### El bot responde pero no hay RF

- Compruebe el `TX` en `meshnet-aprs`.
- Compruebe que el TNC recibió la trama.
- Revise PTT, audio, VOX, puerto, cableado y radio.
- Verifique que la ruta no se ha establecido en `none` por error.

### Solo se transmite la primera parte

Aumente de forma moderada:

```env
APRS_RF_PART_DELAY_S=3.0
```

Recree el contenedor y repita una prueba controlada.

### `rate_limited`

No es un fallo de conexión. El gateway ha rechazado temporalmente una repetición o un boletín por protección antiabuso. Respete `retry_after` y no fuerce reintentos rápidos.

## 18. Lista de validación final

```bash
cd /home/meshnet/MeshNet-Bot

docker compose -f docker-compose.rpi.yml config --services | grep -Fx aprs
docker compose -f docker-compose.rpi.yml ps broker aprs
docker logs --tail 100 meshnet-aprs
docker exec meshnet-aprs getent hosts host.docker.internal
```

Validar además:

- Indicativo distinto de `NOCALL`.
- KISS accesible.
- Broker operativo.
- Perfil de radio correcto.
- Mapa de canales comprobado.
- APRS-IS autenticado cuando esté habilitado.
- Una prueba RF controlada.
- Una prueba APRS-IS controlada.
- Ausencia de duplicados locales en los logs.
- Rutas AX.25 conformes con la política de la red local.

## 19. Documentación relacionada

- [`OPERATIONS.md`](OPERATIONS.md)
- [`RADIO_PROFILES.md`](RADIO_PROFILES.md)
- [`APRS_Remote_KISS_Emergency.md`](APRS_Remote_KISS_Emergency.md)
- [`BOT_README.md`](BOT_README.md)
- [`BROKER_README.md`](BROKER_README.md)
- [`../docker/README.MD/README.md`](../docker/README.MD/README.md)

Los documentos `APRS_GATEWAY_FULL_v6.2.md` y `APRS_GATEWAY_old.md` son históricos. Esta guía es la referencia operativa vigente.