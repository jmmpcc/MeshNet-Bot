# Aplicaciones opcionales Docker: Farmacias y Emergencias

MeshNet-Bot puede ejecutar Farmacias de Guardia y Emergencias como aplicaciones
Docker opcionales e independientes del núcleo.

## Objetivo

Las instalaciones que no utilizan estas funciones no descargan ni arrancan sus
contenedores. Las que sí las utilizan reciben las actualizaciones mediante GHCR,
igual que el resto de componentes Docker.

## Perfiles disponibles

En el `.env` raíz:

```env
# Sin aplicaciones opcionales
COMPOSE_PROFILES=

# Solo Farmacias
COMPOSE_PROFILES=farmacias

# Solo Emergencias
COMPOSE_PROFILES=emergencias

# Ambas
COMPOSE_PROFILES=farmacias,emergencias
```

Los comandos de consulta permanecen desactivados por defecto:

```env
FARMACIAS_COMMAND_ENABLED=0
EMERGENCIAS_COMMAND_ENABLED=0
```

Para habilitar una aplicación hay que activar su perfil y su comando:

```env
COMPOSE_PROFILES=farmacias
FARMACIAS_COMMAND_ENABLED=1
```

o:

```env
COMPOSE_PROFILES=emergencias
EMERGENCIAS_COMMAND_ENABLED=1
```

## Contenedores de Farmacias

El perfil `farmacias` utiliza una sola imagen:

```text
ghcr.io/jmmpcc/meshnet-bot-farmacias:latest
```

y crea tres servicios:

- `meshnet-farmacias`: API local de consultas.
- `meshnet-farmacias-check`: comprobación cada tres horas.
- `meshnet-farmacias-daily`: envío diario a las 08:30 Europe/Madrid.

Los datos persistentes se guardan en:

```text
bot_data/farmacias_guardia/
```

## Contenedores de Emergencias

El perfil `emergencias` utiliza:

```text
ghcr.io/jmmpcc/meshnet-bot-emergencias:latest
```

y crea:

- `meshnet-emergencias`: API local.
- `meshnet-emergencias-check`: comprobación cada dos minutos.

Los datos persistentes se guardan en:

```text
bot_data/emergencias_guardia/
```

## Red

Los servicios opcionales usan la red Docker Compose normal. El broker los
resuelve por nombre de servicio:

```env
FARMACIAS_SERVICE_URL=http://farmacias:8788/query
EMERGENCIAS_SERVICE_URL=http://emergencias:8789/query
```

Las APIs se publican además hacia el host. Por defecto quedan accesibles desde
la LAN de la Raspberry:

```env
FARMACIAS_API_BIND_HOST=0.0.0.0
FARMACIAS_API_PORT_HOST=8788
EMERGENCIAS_API_BIND_HOST=0.0.0.0
EMERGENCIAS_API_PORT_HOST=8789
```

Así pueden comprobarse desde otro equipo de la LAN mediante
`http://IP_RASPBERRY:8788/health` y `http://IP_RASPBERRY:8789/health`.
Para limitar las APIs al propio host, use `127.0.0.1` como *_API_BIND_HOST.

Una instalación histórica basada en systemd puede mantener temporalmente:

```env
FARMACIAS_SERVICE_URL=http://host.docker.internal:8788/query
EMERGENCIAS_SERVICE_URL=http://host.docker.internal:8789/query
```

## Actualización

Con `COMPOSE_PROFILES` definido en `.env`, el procedimiento normal actualiza
únicamente los perfiles activos:

```bash
cd /home/meshnet/MeshNet-Bot
./scripts/update_meshnet.sh
```

También puede hacerse manualmente:

```bash
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

## Migración desde systemd

No deben coexistir los schedulers Docker y systemd de la misma aplicación,
porque podrían duplicar comprobaciones o envíos automáticos.

### Migrar Farmacias

1. Configure:

```env
COMPOSE_PROFILES=farmacias
FARMACIAS_COMMAND_ENABLED=1
FARMACIAS_SERVICE_URL=http://farmacias:8788/query
```

2. Detenga y deshabilite exclusivamente las unidades legacy:

```bash
sudo systemctl disable --now \
  meshnet-farmacias-api.service \
  meshnet-farmacias-check.timer \
  meshnet-farmacias-daily.timer
```

3. Arranque el perfil:

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias pull
docker compose -f docker-compose.rpi.yml --profile farmacias up -d
```

### Migrar Emergencias

1. Configure:

```env
COMPOSE_PROFILES=emergencias
EMERGENCIAS_COMMAND_ENABLED=1
EMERGENCIAS_SERVICE_URL=http://emergencias:8789/query
```

2. Detenga y deshabilite las unidades legacy:

```bash
sudo systemctl disable --now \
  meshnet-emergencias-api.service \
  meshnet-emergencias-check.timer
```

3. Arranque:

```bash
docker compose -f docker-compose.rpi.yml --profile emergencias pull
docker compose -f docker-compose.rpi.yml --profile emergencias up -d
```

El actualizador coordinado detecta perfiles activos y no reinicia las versiones
systemd equivalentes. Si detecta una unidad legacy todavía activa, muestra un
aviso y conserva su estado para no deshabilitar nada sin autorización explícita.


## Selección explícita de contenedores durante actualizaciones

`COMPOSE_PROFILES` sólo hace disponibles los perfiles opcionales. No implica
que `scripts/update_meshnet.sh` deba arrancarlos.

El actualizador preserva el estado Docker:

- si `MESHNET_DOCKER_SERVICES` está vacío o no existe, detecta los servicios
  Compose que ya estaban en ejecución y sólo actualiza/recrea esos mismos;
- si `MESHNET_DOCKER_SERVICES` contiene una lista CSV, actúa únicamente sobre
  esos servicios y sus dependencias obligatorias de Compose;
- nunca ejecuta `docker compose up -d` sin una lista de servicios;
- no utiliza `--remove-orphans` durante este flujo.

Ejemplo para mantener únicamente el núcleo:

```env
COMPOSE_PROFILES=farmacias,emergencias
MESHNET_DOCKER_SERVICES=broker,bot,aprs
```

Ejemplo para añadir Farmacias explícitamente:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily
```

La presencia de `emergencias` en `COMPOSE_PROFILES` no la iniciará mientras
no esté ya activa o se incluya explícitamente en `MESHNET_DOCKER_SERVICES`.
