# Docker — referencia actual de MeshNet-Bot

> La referencia operativa principal está en [../../docs/OPERATIONS.md](../../docs/OPERATIONS.md). Los ficheros de ejemplo de esta carpeta pueden conservar estructuras históricas; no deben prevalecer sobre `docker-compose.rpi.yml`, `.env_example` y la documentación vigente.

## Servicios actuales

Base:

- `broker`
- `bot`
- `aprs`
- `email-to-mesh` opcional
- `bridgehub-bc` opcional

Perfiles:

- `farmacias`: `farmacias`, `farmacias-check`, `farmacias-daily`
- `emergencias`: `emergencias`, `emergencias-check`

## No arrancar todo por defecto

No utilice como rutina:

```text
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

El despliegue actual diferencia perfiles disponibles y servicios seleccionados.

```env
COMPOSE_PROFILES=farmacias,emergencias
MESHNET_DOCKER_SERVICES=broker,bot,aprs
```

## Actualización

```bash
cd /home/meshnet/MeshNet-Bot
git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

El actualizador:

- preserva sólo los servicios ya activos si no hay selección explícita;
- o administra sólo `MESHNET_DOCKER_SERVICES`;
- inicia broker primero;
- espera 60 s por defecto;
- arranca el resto uno a uno;
- usa `--no-deps`;
- aborta ante el primer servicio que no queda `running`.

## Parámetros

```env
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

## Estado

```bash
docker compose -f docker-compose.rpi.yml ps
```

## Arranque manual controlado

```bash
docker compose -f docker-compose.rpi.yml up -d --no-deps broker
docker compose -f docker-compose.rpi.yml up -d --no-deps bot
docker compose -f docker-compose.rpi.yml up -d --no-deps aprs
```

Para perfiles:

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias up -d --no-deps farmacias
docker compose -f docker-compose.rpi.yml --profile emergencias up -d --no-deps emergencias
```

## Parada

```bash
docker compose -f docker-compose.rpi.yml stop <servicio>
```

Si `MESHNET_DOCKER_SERVICES` está definido, retire también de la lista cualquier servicio que deba permanecer apagado.

## Persistencia

`./bot_data:/app/bot_data` conserva datos operativos. No utilice `down -v` salvo que pretenda eliminar almacenamiento Docker.

## Farmacias / Emergencias

Broker consume las APIs mediante DNS de Compose:

```env
FARMACIAS_SERVICE_URL=http://farmacias:8788/query
EMERGENCIAS_SERVICE_URL=http://emergencias:8789/query
```

Salud:

```bash
curl -fsS http://127.0.0.1:8788/health && echo
curl -fsS http://127.0.0.1:8789/health && echo
```

## Logs

```bash
docker logs --tail 100 meshnet-broker
docker logs --tail 100 meshnet-bot
docker logs --tail 100 meshnet-aprs
```

Para más detalle: [../../docs/OPERATIONS.md](../../docs/OPERATIONS.md).
