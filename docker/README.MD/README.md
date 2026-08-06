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

- `broker` → contenedor `meshnet-broker`: interfaces de radio, colas y control;
- `bot` → contenedor `meshnet-bot`: Telegram y programación;
- `aprs` → contenedor `meshnet-aprs`: KISS/APRS-IS;
- `email-to-mesh` → contenedor `meshnet-email-to-mesh`: IMAP, SMTP y contactos de correo;
- puentes: opcionales según perfil y Compose;
- web: opcional según archivo Compose.

El servicio `email-to-mesh` usa la misma imagen que el broker, pero ejecuta directamente `/app/source/email_to_mesh.py`. Comparte la red del broker mediante `network_mode: service:broker` y monta `./bot_data:/app/bot_data:rw`.

## Operación general

```bash
docker compose -f docker-compose.rpi.yml ps
docker compose -f docker-compose.rpi.yml restart broker
docker logs -f meshnet-broker
docker compose -f docker-compose.rpi.yml down
docker compose -f docker-compose.rpi.yml up -d
```

No usar `down -v` salvo que se pretenda borrar volúmenes.

## Operación de email-to-mesh

Arrancar únicamente el servicio y sus dependencias:

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d email-to-mesh
```

Estado:

```bash
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker ps --filter name=meshnet-email-to-mesh
```

Logs:

```bash
docker logs --tail 200 meshnet-email-to-mesh
docker logs -f meshnet-email-to-mesh
```

Reiniciar:

```bash
docker compose -f docker-compose.rpi.yml restart email-to-mesh
```

Recrear sin borrar datos:

```bash
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

Detener y volver a iniciar:

```bash
docker compose -f docker-compose.rpi.yml stop email-to-mesh
docker compose -f docker-compose.rpi.yml start email-to-mesh
```

Actualizar la imagen y recrear:

```bash
docker compose -f docker-compose.rpi.yml pull email-to-mesh
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

Comprobar que el servicio está definido en el Compose efectivo:

```bash
docker compose -f docker-compose.rpi.yml config --services | grep -Fx email-to-mesh
```

La configuración se obtiene del `.env` principal. Los archivos persistentes habituales son:

```text
bot_data/email_contacts.json
bot_data/email_to_mesh_state.json
```

No deben borrarse durante una reinstalación si se quieren conservar contactos, UID procesados y protección contra duplicados.

Gestión rápida de contactos:

```bash
chmod +x scripts/email-to-mesh
scripts/email-to-mesh mail_contactos
scripts/email-to-mesh mail_add prueba prueba@example.org
```

La guía funcional completa está en [`../../docs/EMAIL_TO_MESH.md`](../../docs/EMAIL_TO_MESH.md).

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
docker logs --tail 200 meshnet-email-to-mesh
```

Un contenedor `Exited` requiere revisar su log antes de recrearlo.
