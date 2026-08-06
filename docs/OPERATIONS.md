# Operación, recuperación y reinstalación — MeshNet-Bot v7.0.35

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
docker compose -f /home/meshnet/MeshNet-Bot/docker-compose.rpi.yml ps
```

Contenedores habituales:

- `meshnet-broker` — servicio Compose `broker`;
- `meshnet-bot` — servicio Compose `bot`;
- `meshnet-aprs` — servicio Compose `aprs`;
- `meshnet-email-to-mesh` — servicio Compose `email-to-mesh`;
- `meshnet-bridge-bc` — puente opcional.

`email-to-mesh` es parte del núcleo Docker. No es una unidad systemd independiente. Comparte la red del broker y conserva contactos y estado dentro de `bot_data/`.

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

Los servicios `*-check.service` y `*-daily.service` son normalmente `oneshot`; se ejecutan manualmente o por temporizador.

## 4. Diagnóstico mínimo

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
docker logs --tail 200 meshnet-broker
docker logs --tail 100 meshnet-bot
docker logs --tail 100 meshnet-aprs
docker logs --tail 200 meshnet-email-to-mesh
journalctl -u meshnet-farmacias-api.service -n 100 --no-pager
journalctl -u meshnet-emergencias-api.service -n 100 --no-pager
journalctl -u meshnet-control-panel.service -n 100 --no-pager
```

Puertos locales habituales:

```bash
sudo ss -ltnup | grep -E ':(8766|8788|8789|8790|9464)\b'
```

`email-to-mesh` no publica un puerto propio. Se comunica con el broker por la red compartida del servicio `broker`.

## 5. Recuperar después de `docker compose down`

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

No es necesario reinstalar. `down` elimina contenedores y red, no el código ni los datos montados. `down -v` sí puede borrar volúmenes.

Verificar expresamente el correo:

```bash
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker logs --tail 100 meshnet-email-to-mesh
```

## 6. Operación y recuperación de email-to-mesh

### Arranque

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d email-to-mesh
```

El servicio declara dependencia del broker, por lo que Compose iniciará el broker cuando sea necesario.

### Estado y logs

```bash
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker ps --filter name=meshnet-email-to-mesh
docker logs --tail 200 meshnet-email-to-mesh
docker logs -f meshnet-email-to-mesh
```

### Reinicio

```bash
docker compose -f docker-compose.rpi.yml restart email-to-mesh
```

### Recreación sin perder contactos ni estado

```bash
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

La recreación conserva los datos porque `./bot_data` se monta en `/app/bot_data`.

### Actualizar únicamente el contenedor

```bash
docker compose -f docker-compose.rpi.yml pull email-to-mesh
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

### Confirmar que el servicio existe en el Compose cargado

```bash
docker compose -f docker-compose.rpi.yml config --services | grep -Fx email-to-mesh
```

Si no aparece, se está usando un archivo Compose antiguo o distinto de `docker-compose.rpi.yml`.

### Archivos persistentes

```text
/home/meshnet/MeshNet-Bot/bot_data/email_contacts.json
/home/meshnet/MeshNet-Bot/bot_data/email_to_mesh_state.json
```

- `email_contacts.json`: libreta de contactos para SMTP.
- `email_to_mesh_state.json`: UIDVALIDITY, último UID y deduplicación de mensajes IMAP.

Antes de una reinstalación completa, conservar ambos archivos junto con el resto de `bot_data/`.

### Comprobar contactos

```bash
cd /home/meshnet/MeshNet-Bot
chmod +x scripts/email-to-mesh
scripts/email-to-mesh mail_contactos
```

Alternativa directa:

```bash
docker compose -f docker-compose.rpi.yml exec -T email-to-mesh \
  python /app/source/email_to_mesh.py contacts
```

### Problemas habituales

**El contenedor reinicia continuamente**

```bash
docker inspect meshnet-email-to-mesh --format '{{.State.Status}} {{.State.ExitCode}} {{.State.Error}}'
docker logs --tail 300 meshnet-email-to-mesh
```

Revisar variables IMAP/SMTP y errores de autenticación.

**No procesa correos nuevos**

Comprobar en `.env`:

```env
EMAIL_TO_MESH_ENABLED=1
EMAIL_IMAP_HOST=
EMAIL_IMAP_USER=
EMAIL_IMAP_PASSWORD=
EMAIL_ALLOWED_SENDERS=
```

Verificar que el remitente está autorizado y que el proveedor admite contraseña de aplicación.

**No envía a la malla**

```bash
docker compose -f docker-compose.rpi.yml ps broker email-to-mesh
docker logs --tail 200 meshnet-broker
docker logs --tail 200 meshnet-email-to-mesh
```

Como ambos servicios comparten la red del broker, `BROKER_CTRL_HOST=127.0.0.1` y `BROKER_CTRL_PORT=8766` son válidos en el despliegue RPi incluido.

**No envía correo SMTP**

Comprobar host, puerto, SSL/STARTTLS, usuario, contraseña y `EMAIL_FROM`. Las combinaciones habituales son 465 con SSL o 587 con STARTTLS, nunca ambas simultáneamente.

La referencia completa de variables y comandos está en [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md).

## 7. Actualización del repositorio

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

Después de actualizar unidades systemd:

```bash
sudo systemctl daemon-reload
sudo systemctl restart <unidad>
```

## 8. Reinstalar unidades systemd

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

No se instala una unidad systemd para `email-to-mesh`; se administra mediante Docker Compose.

## 9. Reinstalación completa sin perder configuración

Guardar primero:

```bash
mkdir -p "$HOME/meshnet-backup"
cp -a /home/meshnet/MeshNet-Bot/.env "$HOME/meshnet-backup/root.env"
cp -a /home/meshnet/MeshNet-Bot/bot_data "$HOME/meshnet-backup/bot_data"
cp -a /home/meshnet/MeshNet-Bot/tools/farmacias_guardia/.env "$HOME/meshnet-backup/farmacias.env" 2>/dev/null || true
cp -a /home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env "$HOME/meshnet-backup/emergencias.env" 2>/dev/null || true
cp -a /home/meshnet/MeshNet-Bot/tools/ControlPanel/.env "$HOME/meshnet-backup/controlpanel.env" 2>/dev/null || true
```

Conservar también los directorios `data/` de las aplicaciones cuando se necesite mantener estados, deduplicación o histórico.

Reinstalar:

```bash
cd /home/meshnet
mv MeshNet-Bot "MeshNet-Bot.old.$(date +%Y%m%d-%H%M%S)"
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
cp "$HOME/meshnet-backup/root.env" .env
cp -a "$HOME/meshnet-backup/bot_data/." bot_data/
```

No copiar indiscriminadamente todo el árbol antiguo sobre el nuevo. Restaurar únicamente configuración y datos identificados.

Después:

```bash
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

## 10. Validación de aplicaciones

### Email-to-mesh

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker logs --tail 100 meshnet-email-to-mesh
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
cd /home/meshnet/MeshNet-Bot
python3 tools/voice_rf_gateway/voice_rf_gateway.py doctor
curl -fsS http://127.0.0.1:8791/health
```

El puerto exacto de Voice RF debe confirmarse con su `.env` y la unidad instalada.

## 11. Temporizadores

```bash
systemctl list-timers --all 'meshnet-*'
systemctl cat meshnet-farmacias-daily.timer
systemctl cat meshnet-emergencias-check.timer
```

Ejecutar una tarea manualmente:

```bash
sudo systemctl start meshnet-farmacias-check.service
sudo systemctl start meshnet-farmacias-daily.service
sudo systemctl start meshnet-emergencias-check.service
```

Revisar su resultado:

```bash
journalctl -u meshnet-farmacias-check.service -n 100 --no-pager
journalctl -u meshnet-emergencias-check.service -n 100 --no-pager
```

## 12. Criterio de recuperación

1. Verificar configuración y perfil.
2. Confirmar procesos, contenedores y puertos.
3. Probar la API local desde el host cuando exista.
4. Probarla desde el contenedor broker cuando exista integración HTTP.
5. Revisar logs antes de reinstalar.
6. Conservar `bot_data/`, especialmente estados y contactos de `email-to-mesh`.
7. Reinstalar solo el componente afectado.
