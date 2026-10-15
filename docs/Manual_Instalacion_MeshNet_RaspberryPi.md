# Instalación actual de MeshNet-Bot en Raspberry Pi

Esta guía prepara una instalación nueva sin asumir que todas las aplicaciones Docker deban quedar activas.

## 1. Requisitos

- Raspberry Pi con Debian/Raspberry Pi OS compatible
- acceso a Internet para Git/GHCR
- Docker Engine + Docker Compose v2
- usuario operativo, normalmente `meshnet`
- nodo MeshCore y/o Meshtastic accesible según el perfil

## 2. Preparación

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip netcat-openbsd
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Cierre sesión y vuelva a entrar.

## 3. Clonar

```bash
cd /home/meshnet
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd /home/meshnet/MeshNet-Bot
cp .env_example .env
nano .env
```

Nunca versionar `.env`.

## 4. Elegir el perfil de radio

Ejemplo MeshCore-only:

```env
RADIO_PROFILE=meshcore_only
MESHCORE_ENABLE=1
MESHCORE_MODE=tcp
MESHCORE_TCP_HOST=192.168.1.21
MESHCORE_TCP_PORT=5000
```

BBS puede activarse también en este perfil:

```env
BBS_ENABLED=1
```

Validar:

```bash
python3 scripts/radio-profile-check
```

## 5. Elegir aplicaciones opcionales

Para que Farmacias y Emergencias estén disponibles:

```env
COMPOSE_PROFILES=farmacias,emergencias
```

Esto **no las arranca automáticamente**.

## 6. Elegir qué servicios se administrarán

Ejemplo núcleo:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs
```

Ejemplo núcleo + Farmacias:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily
```

Ejemplo núcleo + Farmacias + Emergencias:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily,emergencias,emergencias-check
```

## 7. Configurar arranque coordinado

```env
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

El broker arrancará primero. Los demás se iniciarán uno a uno sólo después de comprobar que el anterior está operativo.

## 8. Configurar APIs opcionales

```env
FARMACIAS_COMMAND_ENABLED=1
FARMACIAS_SERVICE_URL=http://farmacias:8788/query
FARMACIAS_API_BIND_HOST=0.0.0.0
FARMACIAS_API_PORT_HOST=8788

EMERGENCIAS_COMMAND_ENABLED=1
EMERGENCIAS_SERVICE_URL=http://emergencias:8789/query
EMERGENCIAS_API_BIND_HOST=0.0.0.0
EMERGENCIAS_API_PORT_HOST=8789
```

## 9. Primera puesta en marcha

Use el actualizador coordinado:

```bash
./scripts/update_meshnet.sh
```

No use `docker compose up -d` sin especificar servicios como procedimiento de instalación.

## 10. Comprobar

```bash
docker compose -f docker-compose.rpi.yml ps
docker logs --tail 100 meshnet-broker
```

Si usa Farmacias/Emergencias:

```bash
curl -fsS http://127.0.0.1:8788/health && echo
curl -fsS http://127.0.0.1:8789/health && echo
```

## 11. ControlPanel

ControlPanel es independiente de Docker:

```bash
systemctl status meshnet-control-panel.service --no-pager
curl -fsS http://127.0.0.1:8790/health && echo
```

Si se instala siguiendo su README, queda accesible normalmente en:

```text
http://IP_RASPBERRY:8790
```

## 12. Actualizaciones futuras

```bash
cd /home/meshnet/MeshNet-Bot
git status
git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

El actualizador no debe activar aplicaciones nuevas por el mero hecho de existir en Compose.

## 13. Recomendación de copia

```bash
cp .env "$HOME/meshnet.env.backup"
cp -a bot_data "$HOME/meshnet.bot_data.backup"
```

## 14. Instalaciones antiguas con systemd de Farmacias/Emergencias

Antes de migrar a Docker lea [OPTIONAL_APPS_DOCKER.md](OPTIONAL_APPS_DOCKER.md). No mantenga simultáneamente API/timers systemd y contenedores equivalentes.
