# Comandos Docker operativos

Referencia rápida. Para el procedimiento completo consulte [../../docs/OPERATIONS.md](../../docs/OPERATIONS.md).

## Actualizar

```bash
cd /home/meshnet/MeshNet-Bot
git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

## Estado

```bash
docker compose -f docker-compose.rpi.yml ps
```

## Logs

```bash
docker logs -f meshnet-broker
docker logs -f meshnet-bot
docker logs -f meshnet-aprs
docker logs -f meshnet-farmacias
docker logs -f meshnet-emergencias
```

## Arrancar un servicio explícito

```bash
docker compose -f docker-compose.rpi.yml up -d --no-deps broker
```

Con perfil:

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias up -d --no-deps farmacias
```

## Detener

```bash
docker compose -f docker-compose.rpi.yml stop <servicio>
```

## Reiniciar

```bash
docker compose -f docker-compose.rpi.yml restart <servicio>
```

## Recrear un servicio explícito

```bash
docker compose -f docker-compose.rpi.yml up -d --no-deps --force-recreate <servicio>
```

## No recomendado como operación normal

```text
docker compose up -d
docker compose up -d --remove-orphans
docker compose down -v
```

La selección se controla mediante `MESHNET_DOCKER_SERVICES` y el actualizador coordinado.
