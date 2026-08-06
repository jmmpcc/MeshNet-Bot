# MeshNet-Bot con Docker — v7.0.35

Guía compacta para desplegar el núcleo Docker. La operación completa y las aplicaciones independientes se documentan en [`../../docs/OPERATIONS.md`](../../docs/OPERATIONS.md).

## Preparación

```bash
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
cp .env_example .env
nano .env
python3 scripts/radio-profile-check
```

No versionar `.env`.

## Raspberry Pi

```bash
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

## Desarrollo local

```bash
docker compose build
docker compose up -d
```

## Servicios

- broker: interfaces de radio, colas y control;
- bot: Telegram y programación;
- aprs: KISS/APRS-IS;
- puentes: opcionales según perfil y compose;
- web: opcional según archivo compose.

## Operación

```bash
docker compose -f docker-compose.rpi.yml ps
docker compose -f docker-compose.rpi.yml restart broker
docker logs -f meshnet-broker
docker compose -f docker-compose.rpi.yml down
docker compose -f docker-compose.rpi.yml up -d
```

No usar `down -v` salvo que se pretenda borrar volúmenes.

## Actualización

```bash
git status
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

Resolver previamente cualquier cambio o conflicto Git.

## Conexión a aplicaciones del host

Farmacias y emergencias se ejecutan fuera de Docker. El broker accede normalmente mediante `host.docker.internal`, registrado en `docker-compose.rpi.yml`.

```bash
docker exec meshnet-broker python3 -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:8788/health", timeout=3).read().decode())'
docker exec meshnet-broker python3 -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:8789/health", timeout=3).read().decode())'
```

## Diagnóstico

```bash
docker compose -f docker-compose.rpi.yml config
docker compose -f docker-compose.rpi.yml ps
docker inspect meshnet-broker --format '{{json .NetworkSettings.Networks}}'
docker logs --tail 200 meshnet-broker
```

Un contenedor `Exited` requiere revisar su log antes de recrearlo.
