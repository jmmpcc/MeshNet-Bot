# Bot Telegram de MeshNet-Bot — guía v7.0.36

El bot proporciona control y supervisión mediante Telegram. No abre interfaces de radio directamente: envía solicitudes al broker, consulta estados, programa mensajes y presenta los resultados al operador.

## Dependencias

- contenedor `meshnet-broker` operativo;
- token de Telegram y usuarios autorizados;
- contenedor `meshnet-aprs` para comandos APRS/APRS-IS;
- servicios BBS o correo activos cuando se utilicen sus comandos;
- perfil de radio coherente con el broker.

## Arranque

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d bot
docker logs -f meshnet-bot
```

## Configuración básica

```env
TELEGRAM_TOKEN=<token>
ADMIN_IDS=<ids_autorizados>
BROKER_HOST=127.0.0.1
BROKER_PORT=8765
RADIO_PROFILE=meshcore_only
```

Los nombres exactos de variables deben tomarse de `.env_example` de la versión instalada.

## Funciones

- envío por MeshCore, Meshtastic, APRS o combinaciones permitidas;
- programación puntual y diaria;
- consulta de nodos, canales, telemetría, trazas y cobertura;
- escucha de canales;
- control APRS, APRS-IS y emergencias;
- integración correo-malla y BBS;
- auditorías y administración.

La ayuda interna del bot es la referencia para la sintaxis exacta disponible en la versión ejecutada.

## APRS y APRS-IS desde Telegram

El bot envía peticiones al control UDP del contenedor APRS:

```env
APRS_CTRL_HOST=127.0.0.1
APRS_CTRL_PORT=9464
```

Como `bot`, `aprs` y `broker` comparten la red del servicio `broker` en `docker-compose.rpi.yml`, `127.0.0.1:9464` es correcto en ese despliegue.

### Activar o desactivar la pasarela

```text
/aprs_on
/aprs_off
```

### Enviar un mensaje

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

La aceptación por el gateway no garantiza que todos los receptores hayan decodificado la trama RF.

### Mirror Mesh hacia APRS-IS

```text
/aprsis_push on all
/aprsis_push on 1
/aprsis_push on meshtastic 0,1 meshcore 2
/aprsis_push off
```

El destino APRS-IS se configura con `APRSIS_PUSH_TO`. El mirror debe mantenerse desactivado salvo necesidad expresa para no publicar tráfico innecesario.

### Ejemplo de flujo funcional

```text
Telegram /aprs
  -> meshnet-bot
  -> UDP 127.0.0.1:9464
  -> meshnet-aprs
  -> KISS TCP
  -> Direwolf/Soundmodem/TNC
  -> APRS RF
```

Cuando APRS-IS está configurado, el gateway puede realizar además el flujo correspondiente hacia Internet.

## Envío y perfil

En `meshcore_only`, usar comandos y destinos MeshCore. El bot no debe interpretar la ausencia de un manejador Meshtastic como un fallo general cuando el perfil lo deshabilita de forma deliberada.

Para APRS hacia MeshCore:

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
```

## Reinicio

```bash
docker compose -f docker-compose.rpi.yml restart bot
docker logs --tail 200 meshnet-bot
```

Si solo falla APRS:

```bash
docker compose -f docker-compose.rpi.yml restart aprs
docker logs --tail 200 meshnet-aprs
```

## Diagnóstico

### El bot no responde

```bash
docker compose -f docker-compose.rpi.yml ps
docker logs --since 15m meshnet-bot
docker logs --since 15m meshnet-broker
```

Comprobar token, autorización, DNS y comunicación con el broker.

### El bot responde pero no transmite por la malla

Revisar el resultado devuelto por el broker, el perfil de radio y el estado del nodo. Una cola aceptada puede demorarse por cooldown o disponibilidad RF.

### El bot responde `Pasarela APRS: OK`, pero no hay RF

```bash
docker logs --since 15m meshnet-aprs
docker exec meshnet-aprs python3 -c '
import os,socket
h=os.getenv("KISS_HOST","127.0.0.1")
p=int(os.getenv("KISS_PORT","8100"))
s=socket.create_connection((h,p),5)
print(f"KISS OK {h}:{p}")
s.close()
'
```

Después revisar Direwolf/Soundmodem, PTT, audio, radio y antena.

### Error de control UDP APRS

```bash
docker compose -f docker-compose.rpi.yml ps bot aprs broker
docker logs --since 15m meshnet-bot
docker logs --since 15m meshnet-aprs
sudo ss -lunp | grep ':9464'
```

### Programaciones perdidas

Comprobar los volúmenes y rutas de datos montados. No utilizar `docker compose down -v` durante mantenimiento ordinario.

## Seguridad

- limitar usuarios y chats autorizados;
- no mostrar tokens en capturas o logs compartidos;
- aplicar límites de frecuencia y deduplicación;
- verificar contenido, canal, indicativo y ruta antes de activar difusión APRS;
- no utilizar `/aprsis_push on all` de forma permanente sin necesidad;
- respetar normativa y política local APRS.

## Actualización

```bash
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull bot aprs
docker compose -f docker-compose.rpi.yml up -d --force-recreate bot aprs
```

## Documentación relacionada

- [`BROKER_README.md`](BROKER_README.md)
- [`RADIO_PROFILES.md`](RADIO_PROFILES.md)
- [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md)
- [`APRS_GATEWAY.md`](APRS_GATEWAY.md)
- [`OPERATIONS.md`](OPERATIONS.md)