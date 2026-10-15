# MeshNet-Bot — Manual operativo de servicios

## 1. Principio de operación

MeshNet-Bot no arranca automáticamente todos los servicios descritos en Compose. Cada instalación decide cuáles utiliza.

- `COMPOSE_PROFILES`: aplicaciones opcionales disponibles.
- `MESHNET_DOCKER_SERVICES`: servicios seleccionados para actualización/arranque.
- sin `MESHNET_DOCKER_SERVICES`: se conserva el conjunto que ya estaba activo.

## 2. Estado general

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
```

## 3. Actualización normal

```bash
git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

El script:

1. valida Git;
2. migra datos legacy cuando procede y sólo si el destino está vacío;
3. determina servicios seleccionados;
4. hace pull sólo de esos servicios;
5. valida dependencias;
6. arranca broker primero;
7. espera `BROKER_STARTUP_GRACE_SECONDS`;
8. valida broker;
9. arranca el resto uno a uno;
10. detiene la secuencia ante el primer fallo;
11. reinicia servicios host sólo si ya estaban activos y su código cambió.

## 4. Configuración recomendada

```env
COMPOSE_PROFILES=farmacias,emergencias
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily,emergencias,emergencias-check
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

La lista debe adaptarse a cada instalación.

## 5. Arranque manual controlado

```bash
docker compose -f docker-compose.rpi.yml up -d --no-deps broker
```

Espere y verifique el broker antes de dependientes:

```bash
docker compose -f docker-compose.rpi.yml ps broker
docker logs --tail 100 meshnet-broker
```

Después:

```bash
docker compose -f docker-compose.rpi.yml up -d --no-deps bot
docker compose -f docker-compose.rpi.yml up -d --no-deps aprs
```

Para perfiles:

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias up -d --no-deps farmacias
docker compose -f docker-compose.rpi.yml --profile emergencias up -d --no-deps emergencias
```

## 6. Parada

```bash
docker compose -f docker-compose.rpi.yml stop <servicio>
```

Si existe una lista explícita en `MESHNET_DOCKER_SERVICES`, retire de ella cualquier servicio que deba permanecer apagado.

## 7. Reinicio puntual

```bash
docker compose -f docker-compose.rpi.yml restart broker
```

Para una recreación controlada:

```bash
docker compose -f docker-compose.rpi.yml up -d --no-deps --force-recreate broker
```

## 8. Farmacias

```bash
curl -fsS http://127.0.0.1:8788/health && echo
docker logs --tail 100 meshnet-farmacias
```

## 9. Emergencias

```bash
curl -fsS http://127.0.0.1:8789/health && echo
docker logs --tail 100 meshnet-emergencias
```

## 10. ControlPanel

```bash
systemctl status meshnet-control-panel.service --no-pager
curl -fsS http://127.0.0.1:8790/health && echo
```

ControlPanel cambia automáticamente a operaciones Docker para Farmacias/Emergencias cuando sus perfiles están presentes en `COMPOSE_PROFILES`.

## 11. Servicios systemd legacy

Si Farmacias/Emergencias funcionan por Docker, estas unidades deben permanecer inactivas:

```text
meshnet-farmacias-api.service
meshnet-farmacias-check.timer
meshnet-farmacias-daily.timer
meshnet-emergencias-api.service
meshnet-emergencias-check.timer
```

## 12. Diagnóstico

```bash
docker logs --tail 200 meshnet-broker
docker logs --tail 100 meshnet-bot
docker logs --tail 100 meshnet-aprs
```

Bot y APRS dependen del broker. Diagnostique siempre el broker primero si ambos presentan fallos de conexión.

## 13. Comandos que no deben usarse como rutina

Evite:

```text
docker compose up -d
docker compose up -d --remove-orphans
```

Use selección explícita o `scripts/update_meshnet.sh`.
