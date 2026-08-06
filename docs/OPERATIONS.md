# Operación, recuperación y reinstalación — MeshNet-Bot v7.0.35

## 1. Rutas oficiales

```text
Proyecto:       /home/meshnet/MeshNet-Bot
Configuración:  /home/meshnet/MeshNet-Bot/.env
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

Contenedores habituales: `meshnet-broker`, `meshnet-bot`, `meshnet-aprs` y puentes opcionales.

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
journalctl -u meshnet-farmacias-api.service -n 100 --no-pager
journalctl -u meshnet-emergencias-api.service -n 100 --no-pager
journalctl -u meshnet-control-panel.service -n 100 --no-pager
```

Puertos locales habituales:

```bash
sudo ss -ltnup | grep -E ':(8766|8788|8789|8790|9464)\b'
```

## 5. Recuperar después de `docker compose down`

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

No es necesario reinstalar. `down` elimina contenedores y red, no el código ni los datos montados. `down -v` sí puede borrar volúmenes.

## 6. Actualización del repositorio

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

## 7. Reinstalar unidades systemd

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

## 8. Reinstalación completa sin perder configuración

Guardar primero:

```bash
mkdir -p "$HOME/meshnet-backup"
cp -a /home/meshnet/MeshNet-Bot/.env "$HOME/meshnet-backup/root.env"
cp -a /home/meshnet/MeshNet-Bot/tools/farmacias_guardia/.env "$HOME/meshnet-backup/farmacias.env" 2>/dev/null || true
cp -a /home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env "$HOME/meshnet-backup/emergencias.env" 2>/dev/null || true
cp -a /home/meshnet/MeshNet-Bot/tools/ControlPanel/.env "$HOME/meshnet-backup/controlpanel.env" 2>/dev/null || true
```

Conservar también `bot_data/` y los directorios `data/` de las aplicaciones cuando se necesite mantener estados, deduplicación o histórico.

Reinstalar:

```bash
cd /home/meshnet
mv MeshNet-Bot "MeshNet-Bot.old.$(date +%Y%m%d-%H%M%S)"
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
cp "$HOME/meshnet-backup/root.env" .env
```

No copiar indiscriminadamente todo el árbol antiguo sobre el nuevo. Restaurar únicamente configuración y datos identificados.

## 9. Validación de aplicaciones

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

## 10. Temporizadores

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

## 11. Criterio de recuperación

1. Verificar configuración y perfil.
2. Confirmar procesos y puertos.
3. Probar la API local desde el host.
4. Probarla desde el contenedor broker cuando exista integración HTTP.
5. Revisar logs antes de reinstalar.
6. Reinstalar solo el componente afectado.
