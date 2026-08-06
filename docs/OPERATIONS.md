# Operación, recuperación y reinstalación — MeshNet-Bot v7.0.36

## 1. Rutas oficiales

```text
Proyecto:       /home/meshnet/MeshNet-Bot
Configuración:  /home/meshnet/MeshNet-Bot/.env
Datos Docker:   /home/meshnet/MeshNet-Bot/bot_data
Farmacias:      /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
Emergencias:    /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
ControlPanel:   /home/meshnet/MeshNet-Bot/tools/ControlPanel
Voice RF:       /home/meshnet/MeshNet-Bot/tools/voice_rf_gateway
```

No utilizar rutas antiguas fuera del árbol `MeshNet-Bot`.

## 2. Inventario de servicios

### Docker

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
```

| Servicio Compose | Contenedor | Función |
|---|---|---|
| `broker` | `meshnet-broker` | MeshCore/Meshtastic, colas y control |
| `bot` | `meshnet-bot` | Telegram y programación |
| `aprs` | `meshnet-aprs` | KISS RF, APRS-IS, iGate y pasarela Mesh/APRS |
| `email-to-mesh` | `meshnet-email-to-mesh` | IMAP, SMTP y contactos |
| `bridgehub-bc` | `meshnet-bridge-bc` | Puente opcional |

`bot`, `aprs` y `email-to-mesh` comparten la red del servicio `broker` en `docker-compose.rpi.yml`.

### systemd

```bash
systemctl status meshnet-control-panel.service --no-pager
systemctl status meshnet-farmacias-api.service --no-pager
systemctl status meshnet-emergencias-api.service --no-pager
systemctl status meshnet-voice-rf.service --no-pager
systemctl list-timers --all 'meshnet-*'
```

## 3. Reinicio normal

### Todo Docker

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml restart
```

### Componente concreto

```bash
docker compose -f docker-compose.rpi.yml restart broker
docker compose -f docker-compose.rpi.yml restart bot
docker compose -f docker-compose.rpi.yml restart aprs
docker compose -f docker-compose.rpi.yml restart email-to-mesh
```

### Aplicaciones independientes

```bash
sudo systemctl restart meshnet-control-panel.service
sudo systemctl restart meshnet-farmacias-api.service
sudo systemctl restart meshnet-emergencias-api.service
sudo systemctl restart meshnet-voice-rf.service
```

Los servicios `*-check.service` y `*-daily.service` son normalmente `oneshot`.

## 4. Diagnóstico mínimo

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
docker logs --tail 200 meshnet-broker
docker logs --tail 100 meshnet-bot
docker logs --tail 300 meshnet-aprs
docker logs --tail 200 meshnet-email-to-mesh
journalctl -u meshnet-farmacias-api.service -n 100 --no-pager
journalctl -u meshnet-emergencias-api.service -n 100 --no-pager
journalctl -u meshnet-control-panel.service -n 100 --no-pager
```

Puertos locales habituales:

```bash
sudo ss -ltnup | grep -E ':(8765|8766|8788|8789|8790|8791|9464)\b'
```

El puerto KISS, normalmente TCP 8100, puede estar en la Raspberry o en otro equipo.

## 5. Recuperar después de `docker compose down`

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

`down` elimina contenedores y red, no el código ni los directorios montados. `down -v` puede borrar volúmenes y no debe utilizarse durante mantenimiento normal.

## 6. Operación APRS y APRS-IS

### 6.1 Configuración mínima RF

```env
APRS_CALL=EB2XXX-11
APRS_PATH=WIDE1-1
KISS_HOST=host.docker.internal
KISS_PORT=8100
APRS_CTRL_HOST=127.0.0.1
APRS_CTRL_PORT=9464
APRS_GATE_ENABLED=1
APRS_MAX_LEN=67
APRS_RF_PART_DELAY_S=2.0
```

### 6.2 Añadir APRS-IS

```env
APRSIS_USER=EB2XXX-11
APRSIS_PASSCODE=12345
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=m/20
```

El entrypoint solo activa APRS-IS cuando existen usuario y passcode.

### 6.3 Arrancar APRS y su dependencia

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker compose -f docker-compose.rpi.yml ps broker aprs
docker logs --tail 200 meshnet-aprs
```

### 6.4 Comprobar KISS desde el contenedor

```bash
docker exec meshnet-aprs python3 -c '
import os,socket
h=os.getenv("KISS_HOST","127.0.0.1")
p=int(os.getenv("KISS_PORT","8100"))
s=socket.create_connection((h,p),5)
print(f"KISS OK {h}:{p}")
s.close()
'
```

### 6.5 Ejemplo KISS remoto

```env
KISS_HOST=192.168.1.30
KISS_PORT=8100
```

Prueba desde el host:

```bash
nc -vz 192.168.1.30 8100
```

### 6.6 Ejemplo MeshCore

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
```

Mensaje APRS:

```text
[CH1] Prueba de enlace
```

Flujo esperado:

```text
APRS RF/APRS-IS -> meshnet-aprs -> broker -> MeshCore channel_idx resuelto
```

### 6.7 Prueba del control UDP

```bash
docker exec -i meshnet-aprs python3 -c '
import json,socket
req={"mode":"aprsis_emergency_bulletin","event_id":"TEST-OPS-001","severity":"high","status":"resolved","text":"PRUEBA TECNICA finalizada."}
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.settimeout(10)
s.sendto(json.dumps(req).encode(),("127.0.0.1",9464))
data,_=s.recvfrom(65535)
print(json.dumps(json.loads(data.decode()),indent=2))
s.close()
'
```

`rate_limited` confirma que el endpoint respondió, aunque no haya transmitido una trama nueva.

### 6.8 Reinicio, recreación y actualización

```bash
docker compose -f docker-compose.rpi.yml restart aprs
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
docker compose -f docker-compose.rpi.yml pull aprs
docker compose -f docker-compose.rpi.yml up -d --force-recreate aprs
```

### 6.9 Diagnóstico APRS

```bash
docker logs -f meshnet-aprs 2>&1 | grep --line-buffered -E \
'KISS|APRS-IS|TX|RF TX|duplicado|DEDUP|rate_limited|error'
```

Si el bot responde pero no hay RF, revise en este orden:

1. `TX` del contenedor APRS.
2. Recepción de la trama en Direwolf/Soundmodem.
3. PTT y audio.
4. Radio, frecuencia y antena.
5. Ruta `APRS_PATH`/`APRS_BOT_PATH`.

La guía completa está en [`APRS_GATEWAY.md`](APRS_GATEWAY.md).

## 7. Operación de email-to-mesh

### Arranque y estado

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d email-to-mesh
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker logs --tail 200 meshnet-email-to-mesh
```

### Reinicio y recreación

```bash
docker compose -f docker-compose.rpi.yml restart email-to-mesh
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

### Archivos persistentes

```text
bot_data/email_contacts.json
bot_data/email_to_mesh_state.json
```

### Comprobar contactos

```bash
chmod +x scripts/email-to-mesh
scripts/email-to-mesh mail_contactos
```

La referencia completa está en [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md).

## 8. Actualización del repositorio

```bash
cd /home/meshnet/MeshNet-Bot
git status
```

Si está limpio:

```bash
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

Si existen cambios locales, guardarlos en un commit o `stash`; nunca sobrescribirlos a ciegas.

## 9. Reinstalar unidades systemd

### Farmacias

```bash
cd /home/meshnet/MeshNet-Bot
sudo install -m 0644 tools/farmacias_guardia/systemd/meshnet-farmacias-*.service /etc/systemd/system/
sudo install -m 0644 tools/farmacias_guardia/systemd/meshnet-farmacias-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-farmacias-api.service
sudo systemctl enable --now meshnet-farmacias-check.timer meshnet-farmacias-daily.timer
```

### Emergencias

```bash
sudo install -m 0644 tools/emergencias_guardia/systemd/meshnet-emergencias-*.service /etc/systemd/system/
sudo install -m 0644 tools/emergencias_guardia/systemd/meshnet-emergencias-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-emergencias-api.service
sudo systemctl enable --now meshnet-emergencias-check.timer
```

### ControlPanel

```bash
cd /home/meshnet/MeshNet-Bot/tools/ControlPanel
sudo ./install_control_panel_service.sh
```

### Voice RF Gateway

```bash
cd /home/meshnet/MeshNet-Bot
sudo install -m 0644 systemd/meshnet-voice-rf.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-voice-rf.service
```

No se instalan unidades systemd para `aprs` ni `email-to-mesh`; se administran con Docker Compose.

## 10. Reinstalación completa sin perder configuración

Guardar primero:

```bash
mkdir -p "$HOME/meshnet-backup"
cp -a /home/meshnet/MeshNet-Bot/.env "$HOME/meshnet-backup/root.env"
cp -a /home/meshnet/MeshNet-Bot/bot_data "$HOME/meshnet-backup/bot_data"
cp -a /home/meshnet/MeshNet-Bot/tools/farmacias_guardia/.env "$HOME/meshnet-backup/farmacias.env" 2>/dev/null || true
cp -a /home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env "$HOME/meshnet-backup/emergencias.env" 2>/dev/null || true
cp -a /home/meshnet/MeshNet-Bot/tools/ControlPanel/.env "$HOME/meshnet-backup/controlpanel.env" 2>/dev/null || true
```

Conservar también la configuración del TNC, Direwolf o Soundmodem cuando esté fuera del repositorio.

Reinstalar:

```bash
cd /home/meshnet
mv MeshNet-Bot "MeshNet-Bot.old.$(date +%Y%m%d-%H%M%S)"
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
cp "$HOME/meshnet-backup/root.env" .env
cp -a "$HOME/meshnet-backup/bot_data/." bot_data/
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
```

## 11. Validación de componentes

### APRS

```bash
docker compose -f docker-compose.rpi.yml ps aprs
docker logs --tail 200 meshnet-aprs
```

### Email-to-mesh

```bash
docker compose -f docker-compose.rpi.yml ps email-to-mesh
scripts/email-to-mesh mail_contactos
```

### Farmacias

```bash
cd /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
python3 farmacias_guardia.py doctor
curl -fsS http://127.0.0.1:8788/health
```

### Emergencias

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
python3 emergencias_guardia.py doctor
curl -fsS http://127.0.0.1:8789/health
```

### ControlPanel

```bash
curl -fsS http://127.0.0.1:8790/health
```

### Voice RF Gateway

```bash
python3 /home/meshnet/MeshNet-Bot/tools/voice_rf_gateway/voice_rf_gateway.py doctor
curl -fsS http://127.0.0.1:8791/health
```

## 12. Temporizadores

```bash
systemctl list-timers --all 'meshnet-*'
systemctl cat meshnet-farmacias-daily.timer
systemctl cat meshnet-emergencias-check.timer
```

Ejecutar manualmente:

```bash
sudo systemctl start meshnet-farmacias-check.service
sudo systemctl start meshnet-farmacias-daily.service
sudo systemctl start meshnet-emergencias-check.service
```

## 13. Criterio de recuperación

1. Verificar `.env` y `RADIO_PROFILE`.
2. Confirmar contenedores y unidades systemd.
3. Probar puertos desde el proceso real que los utiliza.
4. Para APRS, separar fallo de gateway, KISS, TNC, PTT, audio y RF.
5. Para APRS-IS, comprobar credenciales, DNS y TCP 14580.
6. Revisar logs antes de reinstalar.
7. Conservar `.env`, `bot_data/` y configuración externa del TNC.
8. Reinstalar solo el componente afectado.