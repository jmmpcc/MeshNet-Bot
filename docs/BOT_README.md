# Bot Telegram de MeshNet-Bot — guía v7.0.35

El bot proporciona control y supervisión mediante Telegram. No abre interfaces de radio directamente: envía solicitudes al broker, consulta estados, programa mensajes y presenta los resultados al operador.

## Dependencias

- contenedor `meshnet-broker` operativo;
- token de Telegram y usuarios autorizados;
- servicios APRS, BBS o correo activos cuando se utilicen sus comandos;
- perfil de radio coherente con el broker.

## Arranque

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d bot
docker logs -f meshnet-bot
```

## Configuración básica

```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_ALLOWED_USERS=<ids_autorizados>
BROKER_HOST=meshnet-broker
BROKER_PORT=8765
RADIO_PROFILE=meshcore_only
```

Los nombres exactos de variables deben tomarse de `.env_example` de la versión instalada.

## Funciones

- envío por MeshCore, Meshtastic, APRS o combinaciones permitidas;
- programación puntual y diaria;
- consulta de nodos, canales, telemetría, trazas y cobertura;
- escucha de canales;
- control APRS y emergencias;
- integración correo-malla y BBS;
- auditorías y administración.

La ayuda interna del bot es la referencia para la sintaxis exacta de comandos disponible en la versión ejecutada.

## Envío y perfil

En `meshcore_only`, usar comandos y destinos MeshCore. El bot no debe interpretar la ausencia de un manejador Meshtastic como un fallo general cuando el perfil lo deshabilita de forma deliberada.

## Reinicio

```bash
docker compose -f docker-compose.rpi.yml restart bot
docker logs --tail 200 meshnet-bot
```

## Diagnóstico

### El bot no responde

```bash
docker compose -f docker-compose.rpi.yml ps
docker logs --since 15m meshnet-bot
docker logs --since 15m meshnet-broker
```

Comprobar token, autorización, DNS y comunicación con el broker.

### El bot responde pero no transmite

Revisar el resultado devuelto por el broker, el perfil de radio y el estado del nodo. Una cola aceptada puede demorarse por cooldown o disponibilidad RF.

### Error APRS

```bash
docker logs --since 15m meshnet-aprs
docker logs --since 15m meshnet-broker
```

### Programaciones perdidas

Comprobar los volúmenes y rutas de datos montados. No utilizar `docker compose down -v` durante mantenimiento ordinario.

## Seguridad

- limitar usuarios y chats autorizados;
- no mostrar tokens en capturas o logs compartidos;
- aplicar límites de frecuencia y deduplicación;
- verificar contenido y destino antes de activar difusión APRS o canales públicos.

## Actualización

```bash
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull bot
docker compose -f docker-compose.rpi.yml up -d bot
```

## Documentación relacionada

- [`BROKER_README.md`](BROKER_README.md)
- [`RADIO_PROFILES.md`](RADIO_PROFILES.md)
- [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md)
- [`APRS_GATEWAY.md`](APRS_GATEWAY.md)
- [`OPERATIONS.md`](OPERATIONS.md)
