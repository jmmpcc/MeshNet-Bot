# Farmacias y Emergencias como aplicaciones Docker opcionales

Farmacias y Emergencias pueden ejecutarse como perfiles Docker independientes del núcleo. El objetivo es que cada instalación active sólo las aplicaciones que necesite.

## 1. Servicios

Perfil `farmacias`:

- `farmacias`: API
- `farmacias-check`: comprobación periódica
- `farmacias-daily`: envío diario

Perfil `emergencias`:

- `emergencias`: API
- `emergencias-check`: actualización periódica

## 2. Habilitar perfiles no significa arrancarlos

```env
COMPOSE_PROFILES=farmacias,emergencias
```

Esto permite que Compose y ControlPanel conozcan esos servicios. **No obliga a iniciarlos.**

## 3. Selección explícita

Ejemplo:

```env
MESHNET_DOCKER_SERVICES=broker,bot,aprs,farmacias,farmacias-check,farmacias-daily
```

Emergencias no se iniciará porque no está seleccionada.

Si la variable está vacía, el actualizador sólo conserva y actualiza los servicios que ya estaban ejecutándose.

## 4. Dependencias

- `farmacias` requiere `broker`
- `farmacias-check` requiere `broker` + `farmacias`
- `farmacias-daily` requiere `broker` + `farmacias`
- `emergencias` requiere `broker`
- `emergencias-check` requiere `broker` + `emergencias`

El actualizador valida estas dependencias antes de iniciar.

## 5. Arranque coordinado

```env
BROKER_STARTUP_GRACE_SECONDS=60
DOCKER_SERVICE_START_TIMEOUT_SECONDS=60
```

`scripts/update_meshnet.sh`:

1. hace pull sólo de los servicios seleccionados;
2. arranca `broker`;
3. espera 60 s por defecto;
4. verifica que continúe `running`;
5. inicia cada servicio posterior uno a uno con `--no-deps`;
6. aborta si cualquiera no queda `running`.

## 6. URLs internas del broker

```env
FARMACIAS_COMMAND_ENABLED=1
FARMACIAS_SERVICE_URL=http://farmacias:8788/query

EMERGENCIAS_COMMAND_ENABLED=1
EMERGENCIAS_SERVICE_URL=http://emergencias:8789/query
```

Dentro de Docker no use `127.0.0.1` para estas APIs: broker y aplicaciones opcionales están en servicios diferentes de la red Compose.

## 7. Exposición LAN

```env
FARMACIAS_API_BIND_HOST=0.0.0.0
FARMACIAS_API_PORT_HOST=8788
EMERGENCIAS_API_BIND_HOST=0.0.0.0
EMERGENCIAS_API_PORT_HOST=8789
```

Para restringir al host:

```env
FARMACIAS_API_BIND_HOST=127.0.0.1
EMERGENCIAS_API_BIND_HOST=127.0.0.1
```

## 8. Datos persistentes

```text
bot_data/farmacias_guardia
bot_data/emergencias_guardia
```

Ambos servicios montan `bot_data`. El actualizador migra datos legacy sólo si el destino Docker está vacío y nunca elimina el origen.

## 9. Migración desde systemd legacy

Antes de arrancar Docker:

```bash
sudo systemctl disable --now \
  meshnet-farmacias-api.service \
  meshnet-farmacias-check.timer \
  meshnet-farmacias-daily.timer \
  meshnet-emergencias-api.service \
  meshnet-emergencias-check.timer
```

Opcionalmente detener oneshots que pudieran seguir ejecutándose:

```bash
sudo systemctl stop \
  meshnet-farmacias-check.service \
  meshnet-farmacias-daily.service \
  meshnet-emergencias-check.service 2>/dev/null || true
```

Comprobar puertos antes de Docker:

```bash
sudo ss -ltnp | grep -E ':(8788|8789)\b' || echo "Puertos libres"
```

## 10. Puesta en marcha recomendada

```bash
cd /home/meshnet/MeshNet-Bot
git switch main
git pull --ff-only
./scripts/update_meshnet.sh
```

No sustituya este procedimiento por un `docker compose up -d` global.

## 11. Arranque manual

API Farmacias:

```bash
docker compose -f docker-compose.rpi.yml --profile farmacias up -d --no-deps farmacias
```

API Emergencias:

```bash
docker compose -f docker-compose.rpi.yml --profile emergencias up -d --no-deps emergencias
```

Para activar schedulers manualmente, compruebe primero que broker y API correspondiente ya están `running`.

## 12. Salud

```bash
curl -fsS http://127.0.0.1:8788/health && echo
curl -fsS http://127.0.0.1:8789/health && echo
```

## 13. Logs

```bash
docker logs --tail 100 meshnet-farmacias
docker logs --tail 100 meshnet-farmacias-check
docker logs --tail 100 meshnet-farmacias-daily
docker logs --tail 100 meshnet-emergencias
docker logs --tail 100 meshnet-emergencias-check
```

## 14. ControlPanel

ControlPanel continúa como servicio systemd del host. Lee `COMPOSE_PROFILES` del entorno o del `.env` raíz:

- perfil presente → acciones Docker;
- perfil ausente → acciones systemd legacy.

Esto permite migrar instalaciones sin duplicar interfaces ni mantener dos paneles.

## 15. Rollback a systemd

1. detenga los contenedores opcionales;
2. restaure la configuración anterior si procede;
3. habilite de nuevo únicamente las unidades legacy que antes estaban activas;
4. reinicie ControlPanel.

Nunca ejecute Docker y systemd para el mismo scheduler simultáneamente.


## Conectividad de Emergencias con broker y APRS en Docker (v7.0.60+)

En el perfil Docker, `emergencias-check` tiene su propio namespace de red.
Por tanto, `127.0.0.1` dentro de ese contenedor **no** apunta al broker ni
al gateway APRS. El archivo `docker-compose.rpi.yml` configura únicamente en
ese servicio `BROKER_CTRL_HOST=broker` (TCP, puerto 8766) y
`APRS_CTRL_HOST=broker` (UDP, puerto 9464).

El comando de comprobación respeta `BROKER_CTRL_HOST` cuando está presente,
incluso cuando `bot_data/emergencias_guardia/config.json` contiene
`notifications.broker.host=127.0.0.1`. Este valor es **solo de ejecución**,
no se reescribe en el archivo persistente, y sin variable de entorno el modo
legacy/systemd conserva su configuración previa.

El gateway APRS se ejecuta con `network_mode: service:broker` y necesita
escuchar con `APRS_CTRL_BIND=0.0.0.0`; el puerto publicado del host
permanece limitado a `127.0.0.1`. No cambie el `.env` global a
`APRS_CTRL_HOST=broker`, porque bot y APRS comparten el namespace de red
del broker. No exponga UDP 9464 públicamente.

Comprobación segura de lectura (sin emisiones APRS):

```bash
docker exec -i meshnet-emergencias-check python - <<'PY'
import json, socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
    client.settimeout(5)
    client.sendto(json.dumps({"mode": "aprs_status"}).encode(), ("broker", 9464))
    response, _ = client.recvfrom(4096)
    print(json.loads(response.decode()))
PY
```

El resultado debe tener `ok: true`. Para comprobar el host efectivo
del broker sin generar ninguna transmisión:

```bash
docker exec meshnet-emergencias-check python -c 'from tools.emergencias_guardia.emergencias.config import load_config; print(load_config(create=False)["notifications"]["broker"]["host"])'
```

Un error histórico en el panel de auditoría no cambia de estado después de
corregir la conexión. La aceptación `queued: true` del broker tampoco
garantiza por sí sola una transmisión radioeléctrica definitiva.
