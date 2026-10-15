# MeshNet-Bot “The Boss” — guía actual de instalación y operación

MeshNet-Bot integra **MeshCore**, **Meshtastic**, **APRS RF/APRS-IS**, Telegram, correo electrónico, BBS, ControlPanel y aplicaciones opcionales como Farmacias y Emergencias.

> Esta README describe la operación actual de `main`. Los documentos con versión antigua o sufijo `_old` se conservan como histórico y no deben prevalecer sobre esta guía, `docs/OPERATIONS.md` o el README específico de cada componente.

## Arquitectura actual

| Servicio / componente | Ejecución | Función |
|---|---|---|
| `broker` / `meshnet-broker` | Docker | Núcleo de comunicaciones MeshCore/Meshtastic, colas, control y Channel Gateway |
| `bot` / `meshnet-bot` | Docker | Telegram, escucha, administración, tareas y consultas |
| `aprs` / `meshnet-aprs` | Docker | KISS, APRS RF, APRS-IS, iGate y pasarela Mesh/APRS |
| `email-to-mesh` | Docker opcional | IMAP/SMTP ↔ malla |
| `bridgehub-bc` | Docker opcional | Puentes externos |
| `farmacias` | Docker opcional, perfil `farmacias` | API de Farmacias de Guardia |
| `farmacias-check` | Docker opcional, perfil `farmacias` | Comprobación periódica |
| `farmacias-daily` | Docker opcional, perfil `farmacias` | Envío diario |
| `emergencias` | Docker opcional, perfil `emergencias` | API y estado de Emergencias |
| `emergencias-check` | Docker opcional, perfil `emergencias` | Actualización periódica de fuentes |
| ControlPanel | systemd en host | Panel web; detecta automáticamente si Farmacias/Emergencias operan por Docker |
| Voice RF Gateway | systemd en host | Síntesis/flujo de voz según configuración |

Las antiguas unidades systemd de Farmacias/Emergencias se mantienen únicamente para compatibilidad con instalaciones legacy. **No deben permanecer activas a la vez que sus equivalentes Docker.**

## Documentación

| Documento | Uso |
|---|---|
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Operación diaria, actualización, selección y arranque Docker |
| [docs/OPTIONAL_APPS_DOCKER.md](docs/OPTIONAL_APPS_DOCKER.md) | Farmacias/Emergencias en Docker y migración desde systemd |
| [docs/RADIO_PROFILES.md](docs/RADIO_PROFILES.md) | `meshcore_only`, Meshtastic y perfiles mixtos |
| [docs/BROKER_README.md](docs/BROKER_README.md) | Broker |
| [docs/BOT_README.md](docs/BOT_README.md) | Bot Telegram |
| [docs/APRS_GATEWAY.md](docs/APRS_GATEWAY.md) | APRS |
| [docs/EMAIL_TO_MESH.md](docs/EMAIL_TO_MESH.md) | Correo ↔ malla |
| [tools/ControlPanel/README.md](tools/ControlPanel/README.md) | ControlPanel |
| [tools/farmacias_guardia/README.md](tools/farmacias_guardia/README.md) | Farmacias |
| [tools/emergencias_guardia/README.md](tools/emergencias_guardia/README.md) | Emergencias |
| [docs/README.md](docs/README.md) | Índice documental completo |

## Instalación base

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip netcat-openbsd
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Cierre sesión y vuelva a entrar para aplicar el grupo Docker.

```bash
cd /home/meshnet
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd /home/meshnet/MeshNet-Bot
cp .env_example .env
nano .env
python3 scripts/radio-profile-check
```

## Configuración Docker: concepto clave

MeshNet-Bot separa **disponibilidad** de **selección de arranque**.

### 1. `COMPOSE_PROFILES`: qué aplicaciones opcionales están disponibles

Ejemplo:

```env
COMPOSE_PROFILES=farmacias,emergencias
```

Esto hace visibles los perfiles, pero **no significa que deban arrancarse automáticamente**.

### 2. `MESHNET_DOCKER_SERVICES`: qué servicios debe administrar el actualizador

Ejemplo de núcleo + Farmacias + Emergencias:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily,emergencias,emergencias-check
```

Ejemplo de núcleo mínimo:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs
```

Si `MESHNET_DOCKER_SERVICES` está vacío o no existe, `scripts/update_meshnet.sh` detecta los servicios Compose que ya estaban `running` y actualiza/recrea únicamente esos mismos.

## Arranque secuencial seguro

El actualizador no levanta todos los contenedores a la vez. Cuando `broker` está seleccionado:

1. arranca `broker`;
2. espera el periodo de estabilización;
3. comprueba que continúa `running`;
4. arranca los restantes uno por uno;
5. comprueba cada servicio antes de continuar;
6. aborta la secuencia si alguno no queda operativo.

Variables:

```env
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

El arranque se realiza con `--no-deps` para impedir que Compose inicie servicios no seleccionados de forma implícita.

## Primera puesta en marcha

Defina primero los servicios que desea. Ejemplo:

```env
COMPOSE_PROFILES=farmacias,emergencias
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily,emergencias,emergencias-check
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

Después:

```bash
cd /home/meshnet/MeshNet-Bot
./scripts/update_meshnet.sh
```

Estado:

```bash
docker compose -f docker-compose.rpi.yml ps
```

## Arrancar un servicio manualmente

Para activaciones puntuales puede arrancarse explícitamente un servicio. No use un `up -d` global.

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias up -d --no-deps farmacias
docker compose -f docker-compose.rpi.yml --profile emergencias up -d --no-deps emergencias
```

Para servicios dependientes, asegúrese primero de que sus dependencias ya están activas.

## Detener un servicio

```bash
docker compose -f docker-compose.rpi.yml stop emergencias-check
docker compose -f docker-compose.rpi.yml stop farmacias-daily
```

Al quedar apagado, una actualización futura no lo volverá a iniciar si `MESHNET_DOCKER_SERVICES` está vacío. Si existe una lista explícita, elimínelo también de esa lista.

## Actualización oficial

Éste es el procedimiento recomendado:

```bash
cd /home/meshnet/MeshNet-Bot
git status
cp .env "$HOME/meshnet.env.backup"
cp -a bot_data "$HOME/meshnet.bot_data.backup"

git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

**No usar como procedimiento normal:**

```text
docker compose up -d
docker compose up -d --remove-orphans
```

Ambos pueden alterar la selección operativa de servicios. El actualizador coordinado existe precisamente para conservarla.

## Farmacias y Emergencias en Docker

URLs internas del broker:

```env
FARMACIAS_SERVICE_URL=http://farmacias:8788/query
EMERGENCIAS_SERVICE_URL=http://emergencias:8789/query
```

Publicación LAN, si se desea:

```env
FARMACIAS_API_BIND_HOST=0.0.0.0
FARMACIAS_API_PORT_HOST=8788
EMERGENCIAS_API_BIND_HOST=0.0.0.0
EMERGENCIAS_API_PORT_HOST=8789
```

Comprobación local:

```bash
curl -fsS http://127.0.0.1:8788/health && echo
curl -fsS http://127.0.0.1:8789/health && echo
```

Los datos persistentes están en:

```text
bot_data/farmacias_guardia
bot_data/emergencias_guardia
```

El actualizador migra datos legacy sólo cuando el destino está vacío; no sobrescribe datos Docker existentes.

## ControlPanel

ControlPanel sigue siendo un servicio del host:

```bash
systemctl status meshnet-control-panel.service --no-pager
curl -fsS http://127.0.0.1:8790/health && echo
```

Cuando `COMPOSE_PROFILES` contiene `farmacias` y/o `emergencias`, el panel usa las acciones Docker correspondientes. En instalaciones legacy sin esos perfiles conserva las acciones systemd.

Acceso LAN habitual:

```text
http://IP_DE_LA_RASPBERRY:8790
```

## Migración desde systemd legacy

Antes de activar Farmacias/Emergencias Docker:

```bash
sudo systemctl disable --now \
  meshnet-farmacias-api.service \
  meshnet-farmacias-check.timer \
  meshnet-farmacias-daily.timer \
  meshnet-emergencias-api.service \
  meshnet-emergencias-check.timer
```

Comprobar:

```bash
systemctl is-active meshnet-farmacias-api.service
systemctl is-active meshnet-farmacias-check.timer
systemctl is-active meshnet-farmacias-daily.timer
systemctl is-active meshnet-emergencias-api.service
systemctl is-active meshnet-emergencias-check.timer
```

Todos deben quedar `inactive` cuando se utilizan sus equivalentes Docker.

## MeshCore y compatibilidad Python

Las imágenes principales continúan sobre Python 3.11. La dependencia MeshCore está limitada a la familia validada:

```text
meshcore>=2.2.28,<2.3.0
```

El Dockerfile ejecuta un smoke test de importación durante el build. Una versión futura incompatible debe hacer fallar CI/GHCR antes de llegar a producción.

## BBS con `meshcore_only`

BBS es opcional y compatible con el perfil MeshCore-only:

```env
RADIO_PROFILE=meshcore_only
BBS_ENABLED=1
```

La activación de BBS no obliga a habilitar Meshtastic.

## Diagnóstico rápido

```bash
docker compose -f docker-compose.rpi.yml ps
docker logs --tail 100 meshnet-broker
docker logs --tail 100 meshnet-bot
docker logs --tail 100 meshnet-aprs
docker logs --tail 100 meshnet-farmacias
docker logs --tail 100 meshnet-emergencias
journalctl -u meshnet-control-panel.service -n 100 --no-pager
```

Si `bot` o `aprs` muestran errores de conexión a `127.0.0.1:8765`, revise primero `meshnet-broker`: ambos dependen de él.

## Seguridad

- Nunca subir `.env`, tokens, contraseñas, claves API o passcodes.
- Limitar los puertos LAN mediante firewall cuando corresponda.
- No mantener simultáneamente schedulers Docker y timers systemd equivalentes.
- No utilizar `docker compose down -v` salvo que se pretenda eliminar datos.
- Respetar normativa de radioaficionado, APRS y las políticas de las fuentes externas.

## Pruebas

```bash
python3 -m compileall -q source shared tools
python3 -m pytest -q
```

## Licencia

Consulte [LICENSE](LICENSE).
