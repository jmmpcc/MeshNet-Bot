# Operación, actualización y recuperación — MeshNet-Bot

Esta guía define el procedimiento operativo actual para Raspberry Pi/Linux.

## 1. Rutas oficiales

```text
Proyecto:       /home/meshnet/MeshNet-Bot
Configuración:  /home/meshnet/MeshNet-Bot/.env
Datos:          /home/meshnet/MeshNet-Bot/bot_data
ControlPanel:   /home/meshnet/MeshNet-Bot/tools/ControlPanel
Farmacias:      /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
Emergencias:    /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
```

## 2. Inventario Docker

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
```

| Servicio | Perfil | Función |
|---|---|---|
| `broker` | base | Núcleo MeshCore/Meshtastic |
| `bot` | base | Telegram |
| `aprs` | base | APRS RF/APRS-IS |
| `email-to-mesh` | base/opcional | Correo ↔ malla |
| `bridgehub-bc` | base/opcional | Bridge externo |
| `farmacias` | `farmacias` | API Farmacias |
| `farmacias-check` | `farmacias` | Comprobación periódica |
| `farmacias-daily` | `farmacias` | Envío diario |
| `emergencias` | `emergencias` | API Emergencias |
| `emergencias-check` | `emergencias` | Actualización periódica |

## 3. Tres conceptos que no deben confundirse

### `COMPOSE_PROFILES`

Hace disponibles perfiles opcionales:

```env
COMPOSE_PROFILES=farmacias,emergencias
```

No implica autostart.

### `MESHNET_DOCKER_SERVICES`

Selecciona servicios para el actualizador:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily,emergencias,emergencias-check
```

Si está vacío/no definido, se preservan sólo los servicios Compose que ya estaban `running`.

### `scripts/update_meshnet.sh`

Es el procedimiento oficial para actualizar y reconciliar el despliegue sin arrancar todo indiscriminadamente.

## 4. Arranque secuencial

Variables:

```env
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

Secuencia:

1. `broker`;
2. espera de estabilización;
3. verificación `running`;
4. servicios restantes uno a uno;
5. verificación individual;
6. aborto si falla un servicio.

El script usa `--no-deps` y valida dependencias conocidas, por lo que Compose no debe iniciar un servicio no seleccionado de forma implícita.

## 5. Actualización oficial

Antes:

```bash
cd /home/meshnet/MeshNet-Bot
git status
cp .env "$HOME/meshnet.env.backup"
cp -a bot_data "$HOME/meshnet.bot_data.backup"
```

Actualizar:

```bash
git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

Al terminar:

```bash
docker compose -f docker-compose.rpi.yml ps
```

No use como procedimiento normal:

```text
docker compose up -d
docker compose up -d --remove-orphans
```

## 6. Activar servicios nuevos

Edite primero `.env`.

Ejemplo para habilitar disponibilidad de Farmacias y Emergencias:

```env
COMPOSE_PROFILES=farmacias,emergencias
```

Ejemplo de selección:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily
```

Ejecute después:

```bash
./scripts/update_meshnet.sh
```

## 7. Arranque manual puntual

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias up -d --no-deps farmacias
docker compose -f docker-compose.rpi.yml --profile emergencias up -d --no-deps emergencias
```

Para schedulers, arranque primero API/dependencias.

## 8. Parada

```bash
docker compose -f docker-compose.rpi.yml stop farmacias-daily
docker compose -f docker-compose.rpi.yml stop emergencias-check
```

Si existe `MESHNET_DOCKER_SERVICES`, elimine también el servicio de esa lista para que la siguiente actualización no lo reactive.

## 9. Migración desde Farmacias/Emergencias systemd

Desactivar unidades legacy antes de activar Docker:

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

Deben quedar `inactive`.

El actualizador copia datos legacy a `bot_data/farmacias_guardia` y `bot_data/emergencias_guardia` sólo cuando el destino está vacío.

## 10. Salud

```bash
curl -fsS http://127.0.0.1:8788/health && echo
curl -fsS http://127.0.0.1:8789/health && echo
curl -fsS http://127.0.0.1:8790/health && echo
```

Puertos:

- Farmacias: 8788
- Emergencias: 8789
- ControlPanel: 8790

## 11. ControlPanel

```bash
systemctl status meshnet-control-panel.service --no-pager
journalctl -u meshnet-control-panel.service -n 100 --no-pager
```

ControlPanel inspecciona `COMPOSE_PROFILES`. Si detecta `farmacias`/`emergencias`, usa las acciones Docker de los manifests; si no, conserva compatibilidad systemd legacy.

## 12. Diagnóstico del broker

```bash
docker logs --tail 200 meshnet-broker
```

Si `bot` o `aprs` no conectan a `127.0.0.1:8765`, diagnostique primero el broker.

La dependencia MeshCore se mantiene acotada a:

```text
meshcore>=2.2.28,<2.3.0
```

El build GHCR incluye un smoke test de importación para evitar publicar una librería MeshCore incompatible con Python 3.11.

## 13. Logs

```bash
docker logs -f meshnet-broker
docker logs -f meshnet-bot
docker logs -f meshnet-aprs
docker logs -f meshnet-farmacias
docker logs -f meshnet-emergencias
```

## 14. Recuperación

Si una actualización falla:

1. no borre `bot_data`;
2. revise `docker compose ... ps`;
3. revise el primer servicio que falló en la secuencia;
4. restaure `.env` sólo si la configuración fue modificada;
5. vuelva a ejecutar `./scripts/update_meshnet.sh` cuando la causa esté corregida.

Evite `docker compose down -v`: puede eliminar almacenamiento gestionado por Docker.

## 15. Seguridad

- No versionar `.env`.
- No mantener timers systemd y schedulers Docker equivalentes simultáneamente.
- No publicar APIs a Internet sin firewall/autenticación apropiados.
- Guardar copia de `.env` y `bot_data` antes de cambios importantes.

## 16. Verificación de actualización GHCR de Emergencias

Cuando el cambio afecte a `tools/emergencias_guardia/`, GitHub Actions debe
publicar `ghcr.io/jmmpcc/meshnet-bot-emergencias:latest`. El `git pull` solo
actualiza el código del host; **no actualiza la imagen del contenedor**.

1. Revise [Build & Push Docker images to GHCR](https://github.com/jmmpcc/MeshNet-Bot/actions/workflows/build-ghcr.yml) y confirme que el build de Emergencias del `main` recién fusionado ha terminado correctamente.
2. Descargue solo `emergencias-check`: `docker compose -f docker-compose.rpi.yml --profile emergencias pull emergencias-check`.
3. Antes de reiniciar, compruebe dentro de la imagen descargada que existen `parse_check_output` y el override `BROKER_CTRL_HOST`, siguiendo el comando del [README principal](../README.md#emergencias--aprs-is-instalación-portable-y-verificación).
4. Si es correcta, recree únicamente el servicio `emergencias-check` con `--no-deps --force-recreate`. No se requiere detener broker, bot, aprs ni farmacias.
5. Compruebe `BROKER EFECTIVO=broker`, `APRS CONTROL=broker`, la salud de la API y nuevas operaciones en la auditoría. Los registros históricos se conservan.

El `emergencias-check` utiliza red Compose propia; `aprs` y `bot` comparten
la red del broker. No cambiar `APRS_CTRL_HOST` globalmente en `.env`.
Si hay cambios locales, no forzar `git pull`: preservar y revisar cada fichero
antes de sincronizar. No publicar controles UDP APRS en la LAN.
