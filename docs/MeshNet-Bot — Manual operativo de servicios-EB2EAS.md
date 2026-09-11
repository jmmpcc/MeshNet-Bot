# MeshNet-Bot — Manual operativo de servicios

## 1. Objetivo

Este documento es la referencia operativa para administrar los servicios que forman MeshNet-Bot y sus aplicaciones auxiliares.

Su finalidad es poder determinar rápidamente:

- qué servicios existen;
- qué función realiza cada uno;
- cuáles deben estar permanentemente activos;
- cuáles son servicios ejecutados mediante `systemd timer`;
- cómo comprobar su estado;
- cómo iniciarlos;
- cómo detenerlos;
- cómo reiniciarlos;
- cómo habilitarlos al arrancar;
- cómo consultar sus registros;
- cómo diagnosticar un fallo;
- cómo recuperar el sistema después de un reinicio o actualización.

Ruta habitual del proyecto:

```bash
/home/meshnet/MeshNet-Bot
```

Usuario habitual:

```text
meshnet
```

---

# 2. Comandos systemd fundamentales

Los siguientes comandos son válidos para cualquier servicio.

## Estado

```bash
sudo systemctl status NOMBRE.service
```

Ejemplo:

```bash
sudo systemctl status meshnet-mobile-api.service
```

## Iniciar

```bash
sudo systemctl start NOMBRE.service
```

## Detener

```bash
sudo systemctl stop NOMBRE.service
```

## Reiniciar

```bash
sudo systemctl restart NOMBRE.service
```

## Recargar después de modificar un `.service` o `.timer`

```bash
sudo systemctl daemon-reload
```

Después:

```bash
sudo systemctl restart NOMBRE.service
```

## Habilitar al arrancar

```bash
sudo systemctl enable NOMBRE.service
```

Para habilitar e iniciar inmediatamente:

```bash
sudo systemctl enable --now NOMBRE.service
```

## Deshabilitar

```bash
sudo systemctl disable NOMBRE.service
```

## Logs

Últimas líneas:

```bash
journalctl -u NOMBRE.service -n 100 --no-pager
```

Seguimiento en tiempo real:

```bash
journalctl -u NOMBRE.service -f
```

Logs desde el arranque actual:

```bash
journalctl -u NOMBRE.service -b
```

---

# 3. Comprobación general del sistema

Antes de investigar individualmente cada componente:

```bash
cd /home/meshnet/MeshNet-Bot
```

## Servicios MeshNet conocidos por systemd

```bash
systemctl list-units --type=service --all | grep -Ei 'meshnet|farmacias|emergencias'
```

## Timers

```bash
systemctl list-timers --all | grep -Ei 'meshnet|farmacias|emergencias'
```

## Servicios fallidos

```bash
systemctl --failed
```

## Procesos Docker

```bash
docker ps
```

Con Docker Compose:

```bash
docker compose ps
```

---

# 4. Arquitectura de servicios

MeshNet-Bot utiliza dos mecanismos principales.

### Servicios Docker

Ejecutan los componentes centrales del bot, broker, pasarelas y otros procesos definidos por Docker Compose.

Se administran principalmente mediante:

```bash
docker compose
```

### Servicios systemd

Se utilizan para aplicaciones auxiliares e independientes:

- Control Panel;
- Mobile API;
- Emergencias;
- Farmacias;
- Voice RF;
- tareas periódicas mediante timers.

No deben confundirse ambos sistemas.

---

# 5. MeshNet-Bot — infraestructura Docker

Directorio:

```bash
cd /home/meshnet/MeshNet-Bot
```

El despliegue Raspberry utiliza principalmente:

```text
docker-compose.rpi.yml
```

## Ver contenedores

```bash
docker ps
```

o:

```bash
docker compose -f docker-compose.rpi.yml ps
```

## Arrancar

```bash
docker compose -f docker-compose.rpi.yml up -d
```

## Detener

```bash
docker compose -f docker-compose.rpi.yml stop
```

Esto detiene los contenedores sin eliminarlos.

## Reiniciar

```bash
docker compose -f docker-compose.rpi.yml restart
```

## Detener y retirar los contenedores

```bash
docker compose -f docker-compose.rpi.yml down
```

Esto no equivale a `stop`.

`stop` conserva los contenedores.

`down` elimina los contenedores creados por Compose, manteniendo las imágenes y los datos persistentes correctamente configurados.

## Reconstrucción después de cambios

```bash
docker compose -f docker-compose.rpi.yml up -d --build
```

## Logs generales

```bash
docker compose -f docker-compose.rpi.yml logs --tail=100
```

Seguimiento:

```bash
docker compose -f docker-compose.rpi.yml logs -f
```

## Reiniciar únicamente un contenedor

Primero:

```bash
docker compose -f docker-compose.rpi.yml ps
```

Después:

```bash
docker compose -f docker-compose.rpi.yml restart NOMBRE
```

---

# 6. Control Panel / Web Admin

Directorio:

```text
tools/ControlPanel/
```

Unidad:

```text
meshnet-control-panel.service
```

Fichero fuente:

```text
tools/ControlPanel/systemd/meshnet-control-panel.service
```

## Estado

```bash
sudo systemctl status meshnet-control-panel.service
```

## Iniciar

```bash
sudo systemctl start meshnet-control-panel.service
```

## Detener

```bash
sudo systemctl stop meshnet-control-panel.service
```

## Reiniciar

```bash
sudo systemctl restart meshnet-control-panel.service
```

## Habilitar automáticamente

```bash
sudo systemctl enable --now meshnet-control-panel.service
```

## Logs

```bash
journalctl -u meshnet-control-panel.service -n 100 --no-pager
```

Tiempo real:

```bash
journalctl -u meshnet-control-panel.service -f
```

## Instalación/reinstalación de la unidad

Existe:

```text
tools/ControlPanel/install_control_panel_service.sh
```

Por tanto:

```bash
cd /home/meshnet/MeshNet-Bot
chmod +x tools/ControlPanel/install_control_panel_service.sh
sudo tools/ControlPanel/install_control_panel_service.sh
```

Después comprobar:

```bash
sudo systemctl status meshnet-control-panel.service
```

---

# 7. Mobile API

Directorio:

```text
tools/MobileAPI/
```

Servicio:

```text
meshnet-mobile-api.service
```

La API proporciona la interfaz utilizada por MeshNet-Mobile.

Actualmente utiliza Uvicorn y escucha en:

```text
0.0.0.0:8791
```

La unidad ejecuta:

```text
tools.MobileAPI.mobile_api_v7054:app
```

y carga opcionalmente:

```text
/home/meshnet/MeshNet-Bot/tools/MobileAPI/.env
```

## Estado

```bash
sudo systemctl status meshnet-mobile-api.service
```

## Iniciar

```bash
sudo systemctl start meshnet-mobile-api.service
```

## Detener

```bash
sudo systemctl stop meshnet-mobile-api.service
```

## Reiniciar

```bash
sudo systemctl restart meshnet-mobile-api.service
```

## Habilitar al arrancar

```bash
sudo systemctl enable --now meshnet-mobile-api.service
```

## Logs

```bash
journalctl -u meshnet-mobile-api.service -n 100 --no-pager
```

Tiempo real:

```bash
journalctl -u meshnet-mobile-api.service -f
```

## Comprobar que escucha en 8791

```bash
ss -ltnp | grep 8791
```

## Diagnóstico

```bash
sudo systemctl status meshnet-mobile-api.service
journalctl -u meshnet-mobile-api.service -n 100 --no-pager
```

Comprobar también:

```bash
ls -la /home/meshnet/MeshNet-Bot/tools/MobileAPI/
```

y:

```bash
ls -la /home/meshnet/MeshNet-Bot/tools/MobileAPI/.env
```

---

# 8. Emergencias de Guardia

Directorio:

```text
tools/emergencias_guardia/
```

El sistema se divide en una API y una consulta periódica de fuentes.

Servicios:

```text
meshnet-emergencias-api.service
meshnet-emergencias-check.service
meshnet-emergencias-check.timer
```

---

# 9. Emergencias — API

Servicio:

```text
meshnet-emergencias-api.service
```

## Estado

```bash
sudo systemctl status meshnet-emergencias-api.service
```

## Iniciar

```bash
sudo systemctl start meshnet-emergencias-api.service
```

## Detener

```bash
sudo systemctl stop meshnet-emergencias-api.service
```

## Reiniciar

```bash
sudo systemctl restart meshnet-emergencias-api.service
```

## Habilitar

```bash
sudo systemctl enable --now meshnet-emergencias-api.service
```

## Logs

```bash
journalctl -u meshnet-emergencias-api.service -n 100 --no-pager
```

---

# 10. Emergencias — comprobación periódica

Servicio:

```text
meshnet-emergencias-check.service
```

Timer:

```text
meshnet-emergencias-check.timer
```

El timer ejecuta periódicamente la comprobación de las fuentes.

Configuración actual:

```text
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=true
RandomizedDelaySec=15
```

Por tanto, después del arranque espera aproximadamente dos minutos y posteriormente ejecuta la comprobación cada dos minutos, con un pequeño retardo aleatorio.

## Estado del timer

```bash
sudo systemctl status meshnet-emergencias-check.timer
```

## Ver próxima ejecución

```bash
systemctl list-timers --all | grep emergencias
```

## Iniciar programación automática

```bash
sudo systemctl start meshnet-emergencias-check.timer
```

## Detener programación automática

```bash
sudo systemctl stop meshnet-emergencias-check.timer
```

## Reiniciar timer

```bash
sudo systemctl restart meshnet-emergencias-check.timer
```

## Habilitar permanentemente

```bash
sudo systemctl enable --now meshnet-emergencias-check.timer
```

## Ejecutar una comprobación inmediatamente

No es necesario esperar al timer:

```bash
sudo systemctl start meshnet-emergencias-check.service
```

Después:

```bash
sudo systemctl status meshnet-emergencias-check.service
```

## Logs

```bash
journalctl -u meshnet-emergencias-check.service -n 100 --no-pager
```

Última ejecución:

```bash
journalctl -u meshnet-emergencias-check.service --since "10 minutes ago"
```

---

# 11. Farmacias de Guardia

Directorio:

```text
tools/farmacias_guardia/
```

Servicios:

```text
meshnet-farmacias-api.service
meshnet-farmacias-check.service
meshnet-farmacias-check.timer
meshnet-farmacias-daily.service
meshnet-farmacias-daily.timer
```

---

# 12. Farmacias — API

Servicio permanente:

```text
meshnet-farmacias-api.service
```

## Estado

```bash
sudo systemctl status meshnet-farmacias-api.service
```

## Iniciar

```bash
sudo systemctl start meshnet-farmacias-api.service
```

## Detener

```bash
sudo systemctl stop meshnet-farmacias-api.service
```

## Reiniciar

```bash
sudo systemctl restart meshnet-farmacias-api.service
```

## Habilitar

```bash
sudo systemctl enable --now meshnet-farmacias-api.service
```

## Logs

```bash
journalctl -u meshnet-farmacias-api.service -n 100 --no-pager
```

---

# 13. Farmacias — comprobación periódica

Timer:

```text
meshnet-farmacias-check.timer
```

Servicio ejecutado:

```text
meshnet-farmacias-check.service
```

Configuración actual:

```text
OnBootSec=10min
OnUnitActiveSec=3h
Persistent=true
RandomizedDelaySec=60
```

La primera comprobación se programa después del arranque y posteriormente aproximadamente cada tres horas.

## Estado

```bash
sudo systemctl status meshnet-farmacias-check.timer
```

## Próxima ejecución

```bash
systemctl list-timers --all | grep farmacias
```

## Habilitar

```bash
sudo systemctl enable --now meshnet-farmacias-check.timer
```

## Reiniciar

```bash
sudo systemctl restart meshnet-farmacias-check.timer
```

## Detener

```bash
sudo systemctl stop meshnet-farmacias-check.timer
```

## Forzar comprobación ahora

```bash
sudo systemctl start meshnet-farmacias-check.service
```

## Logs

```bash
journalctl -u meshnet-farmacias-check.service -n 100 --no-pager
```

---

# 14. Farmacias — envío diario

Servicio:

```text
meshnet-farmacias-daily.service
```

Timer:

```text
meshnet-farmacias-daily.timer
```

Programación actual:

```text
08:30
```

## Estado

```bash
sudo systemctl status meshnet-farmacias-daily.timer
```

## Habilitar

```bash
sudo systemctl enable --now meshnet-farmacias-daily.timer
```

## Reiniciar

```bash
sudo systemctl restart meshnet-farmacias-daily.timer
```

## Ejecutar manualmente el envío diario

```bash
sudo systemctl start meshnet-farmacias-daily.service
```

## Logs

```bash
journalctl -u meshnet-farmacias-daily.service -n 100 --no-pager
```

---

# 15. Voice RF Gateway

Directorio:

```text
tools/voice_rf_gateway/
```

Unidad:

```text
meshnet-voice-rf.service
```

Este componente gestiona la salida de voz RF/TTS cuando la funcionalidad está habilitada.

La existencia del servicio no significa que la salida de voz esté necesariamente habilitada en la configuración de Emergencias.

## Estado

```bash
sudo systemctl status meshnet-voice-rf.service
```

## Iniciar

```bash
sudo systemctl start meshnet-voice-rf.service
```

## Detener

```bash
sudo systemctl stop meshnet-voice-rf.service
```

## Reiniciar

```bash
sudo systemctl restart meshnet-voice-rf.service
```

## Logs

```bash
journalctl -u meshnet-voice-rf.service -n 100 --no-pager
```

Tiempo real:

```bash
journalctl -u meshnet-voice-rf.service -f
```

---

# 16. Email-to-Mesh

Código:

```text
source/email_to_mesh.py
```

Lanzador:

```text
scripts/email-to-mesh
```

Documentación específica:

```text
docs/EMAIL_TO_MESH.md
```

Debe distinguirse entre el proceso Email-to-Mesh y los contenedores principales.

## Localizar cómo está ejecutándose

```bash
ps aux | grep -i email_to_mesh
```

También:

```bash
docker ps | grep -i mail
```

y:

```bash
systemctl list-units --type=service --all | grep -Ei 'mail|email|mesh'
```

Si se ejecuta mediante Docker, debe administrarse mediante el servicio correspondiente definido en Docker Compose.

Logs:

```bash
docker compose -f docker-compose.rpi.yml logs --tail=100
```

---

# 17. APRS / APRS-IS

Código principal:

```text
source/meshtastic_to_aprs.py
```

Documentación:

```text
docs/APRS_GATEWAY.md
```

La pasarela APRS forma parte de la arquitectura Docker del sistema.

## Comprobar contenedores relacionados

```bash
docker compose -f docker-compose.rpi.yml ps
```

## Logs APRS

Primero obtener el nombre exacto:

```bash
docker compose -f docker-compose.rpi.yml ps
```

Después:

```bash
docker compose -f docker-compose.rpi.yml logs --tail=100 NOMBRE_APRS
```

Tiempo real:

```bash
docker compose -f docker-compose.rpi.yml logs -f NOMBRE_APRS
```

## Reiniciar únicamente APRS

```bash
docker compose -f docker-compose.rpi.yml restart NOMBRE_APRS
```

Esto es preferible a reiniciar toda la plataforma cuando el problema está limitado a APRS.

---

# 18. BBS y noticias

Componentes:

```text
source/bbs_server.py
source/news_ingestor.py
source/aprs_bbs_bridge.py
```

La base BBS forma parte de MeshNet-Bot y puede alimentarse mediante el ingestor de noticias.

Para localizar servicios instalados en la Raspberry:

```bash
systemctl list-unit-files | grep -Ei 'bbs|news|meshnet'
```

Para localizar timers:

```bash
systemctl list-timers --all | grep -Ei 'bbs|news'
```

Para procesos:

```bash
ps aux | grep -Ei 'bbs|news_ingestor'
```

Cuando exista `meshnet-news-ingestor.service`:

```bash
sudo systemctl status meshnet-news-ingestor.service
```

Logs:

```bash
journalctl -u meshnet-news-ingestor.service -n 100 --no-pager
```

Si está gobernado por timer:

```bash
systemctl list-timers --all | grep news
```

---

# 19. Comprobación rápida de todas las aplicaciones independientes

Ejecutar:

```bash
sudo systemctl status \
  meshnet-control-panel.service \
  meshnet-mobile-api.service \
  meshnet-emergencias-api.service \
  meshnet-emergencias-check.timer \
  meshnet-farmacias-api.service \
  meshnet-farmacias-check.timer \
  meshnet-farmacias-daily.timer \
  meshnet-voice-rf.service \
  --no-pager
```

Los servicios `oneshot` asociados a timers pueden aparecer como:

```text
inactive (dead)
```

sin que exista ningún problema.

Esto es normal.

Lo importante es que su correspondiente `.timer` aparezca:

```text
active (waiting)
```

---

# 20. Comprobación rápida de timers

```bash
systemctl list-timers --all | grep -E 'meshnet|farmacias|emergencias|news'
```

Permite comprobar:

- próxima ejecución;
- última ejecución;
- timer asociado;
- servicio ejecutado.

---

# 21. Reinicio controlado después de actualizar MeshNet-Bot

Después de:

```bash
git pull
```

no debe reiniciarse indiscriminadamente todo el servidor.

Primero:

```bash
cd /home/meshnet/MeshNet-Bot
git status
```

Si solamente se ha modificado código utilizado por Docker:

```bash
docker compose -f docker-compose.rpi.yml up -d --build
```

Si se ha modificado Control Panel:

```bash
sudo systemctl restart meshnet-control-panel.service
```

Si se ha modificado Mobile API:

```bash
sudo systemctl restart meshnet-mobile-api.service
```

Si se ha modificado Emergencias:

```bash
sudo systemctl restart meshnet-emergencias-api.service
sudo systemctl restart meshnet-emergencias-check.timer
```

Si se ha modificado Farmacias:

```bash
sudo systemctl restart meshnet-farmacias-api.service
sudo systemctl restart meshnet-farmacias-check.timer
sudo systemctl restart meshnet-farmacias-daily.timer
```

Si se ha modificado una unidad `.service` o `.timer`:

```bash
sudo systemctl daemon-reload
```

y después reiniciar únicamente las unidades afectadas.

---

# 22. Después de reiniciar la Raspberry

Comprobar primero:

```bash
uptime
```

Después Docker:

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
```

Después servicios:

```bash
systemctl --failed
```

Después timers:

```bash
systemctl list-timers --all | grep -E 'meshnet|farmacias|emergencias|news'
```

Después puertos:

```bash
ss -ltnup
```

---

# 23. Diagnóstico cuando algo no funciona

Secuencia recomendada:

```bash
sudo systemctl status NOMBRE.service
```

Después:

```bash
journalctl -u NOMBRE.service -n 100 --no-pager
```

Después comprobar configuración:

```bash
systemctl cat NOMBRE.service
```

Después:

```bash
systemctl show NOMBRE.service -p ActiveState -p SubState -p ExecMainStatus
```

Si se ha modificado la unidad:

```bash
sudo systemctl daemon-reload
sudo systemctl restart NOMBRE.service
```

Volver a comprobar:

```bash
sudo systemctl status NOMBRE.service
```

---

# 24. Diagnóstico Docker

Estado:

```bash
docker ps -a
```

Compose:

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml ps
```

Logs:

```bash
docker compose -f docker-compose.rpi.yml logs --tail=200
```

Contenedor concreto:

```bash
docker logs --tail=200 NOMBRE_CONTENEDOR
```

Tiempo real:

```bash
docker logs -f NOMBRE_CONTENEDOR
```

Reiniciar exclusivamente el contenedor afectado:

```bash
docker restart NOMBRE_CONTENEDOR
```

---

# 25. Saber qué programa ejecuta realmente un servicio

Comando muy importante:

```bash
systemctl cat NOMBRE.service
```

Ejemplo:

```bash
systemctl cat meshnet-mobile-api.service
```

Muestra:

- `WorkingDirectory`;
- `EnvironmentFile`;
- `ExecStart`;
- usuario;
- política de reinicio;
- dependencias.

Para conocer la definición efectiva que está utilizando la Raspberry debe consultarse siempre `systemctl cat`, no solamente el fichero existente dentro del repositorio.

---

# 26. Saber dónde está instalado un servicio

```bash
systemctl show -p FragmentPath NOMBRE.service
```

Ejemplo:

```bash
systemctl show -p FragmentPath meshnet-emergencias-api.service
```

Normalmente devolverá una ruta bajo:

```text
/etc/systemd/system/
```

---

# 27. Ver variables de entorno configuradas

No mostrar públicamente valores sensibles.

Para localizar los `EnvironmentFile` utilizados:

```bash
systemctl cat NOMBRE.service
```

Los principales ficheros de configuración pueden encontrarse en:

```text
/home/meshnet/MeshNet-Bot/.env
/home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env
/home/meshnet/MeshNet-Bot/tools/farmacias_guardia/.env
/home/meshnet/MeshNet-Bot/tools/MobileAPI/.env
```

La existencia real debe comprobarse mediante:

```bash
ls -la RUTA
```

---

# 28. Tabla operativa

| Componente | Unidad | Tipo | Operación normal |
|---|---|---|---|
| MeshNet-Bot | Docker Compose | Docker | Permanente |
| Control Panel | `meshnet-control-panel.service` | service | Permanente |
| Mobile API | `meshnet-mobile-api.service` | service | Permanente |
| Emergencias API | `meshnet-emergencias-api.service` | service | Permanente |
| Emergencias checker | `meshnet-emergencias-check.service` | oneshot | Por timer |
| Emergencias timer | `meshnet-emergencias-check.timer` | timer | Cada ~2 min |
| Farmacias API | `meshnet-farmacias-api.service` | service | Permanente |
| Farmacias checker | `meshnet-farmacias-check.service` | oneshot | Por timer |
| Farmacias timer | `meshnet-farmacias-check.timer` | timer | Cada ~3 h |
| Farmacias diario | `meshnet-farmacias-daily.service` | oneshot | Por timer |
| Farmacias diario timer | `meshnet-farmacias-daily.timer` | timer | 08:30 |
| Voice RF | `meshnet-voice-rf.service` | service | Según configuración |
| APRS/APRS-IS | Docker | contenedor | Permanente |
| Email-to-Mesh | Docker/proceso | servicio auxiliar | Permanente |
| BBS | MeshNet-Bot | integrado | Permanente |
| News ingestor | systemd/timer | tarea | Periódica |

---

# 29. Comandos de emergencia

## Reiniciar Control Panel

```bash
sudo systemctl restart meshnet-control-panel.service
```

## Reiniciar Mobile API

```bash
sudo systemctl restart meshnet-mobile-api.service
```

## Reiniciar Emergencias

```bash
sudo systemctl restart meshnet-emergencias-api.service
sudo systemctl restart meshnet-emergencias-check.timer
```

## Ejecutar Emergencias ahora

```bash
sudo systemctl start meshnet-emergencias-check.service
```

## Reiniciar Farmacias

```bash
sudo systemctl restart meshnet-farmacias-api.service
sudo systemctl restart meshnet-farmacias-check.timer
sudo systemctl restart meshnet-farmacias-daily.timer
```

## Ejecutar comprobación de Farmacias ahora

```bash
sudo systemctl start meshnet-farmacias-check.service
```

## Ejecutar envío diario de Farmacias ahora

```bash
sudo systemctl start meshnet-farmacias-daily.service
```

## Reiniciar plataforma Docker

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml restart
```

---

# 30. Comprobación completa en menos de un minuto

```bash
cd /home/meshnet/MeshNet-Bot

echo "=== SISTEMA ==="
uptime

echo
echo "=== SERVICIOS FALLIDOS ==="
systemctl --failed --no-pager

echo
echo "=== DOCKER ==="
docker compose -f docker-compose.rpi.yml ps

echo
echo "=== SERVICIOS MESHNET ==="
systemctl list-units --type=service --all | grep -Ei 'meshnet|farmacias|emergencias|news'

echo
echo "=== TIMERS ==="
systemctl list-timers --all | grep -Ei 'meshnet|farmacias|emergencias|news'

echo
echo "=== PUERTOS ==="
ss -ltnp
```

Esta secuencia debe ser la primera comprobación cuando no se conoce el estado general de MeshNet-Bot.

---

# 31. Regla operativa fundamental

No reiniciar toda la Raspberry como primera medida.

El orden correcto de diagnóstico es:

```text
1. Identificar el componente que falla.
2. Consultar su estado.
3. Consultar sus logs.
4. Comprobar su configuración.
5. Reiniciar únicamente ese servicio.
6. Comprobar nuevamente.
7. Reiniciar componentes dependientes sólo cuando sea necesario.
8. Reiniciar la Raspberry únicamente como último recurso.
```

Esto reduce interrupciones y evita alterar componentes que estaban funcionando correctamente.

---

# 32. Referencias dentro del proyecto

Documentación complementaria:

```text
README.md
docs/OPERATIONS.md
docs/APRS_GATEWAY.md
docs/EMAIL_TO_MESH.md
docs/RADIO_PROFILES.md
tools/ControlPanel/README.md
tools/MobileAPI/README.md
tools/emergencias_guardia/README.md
tools/farmacias_guardia/README.md
tools/voice_rf_gateway/README.md
```

Este documento debe mantenerse como referencia operativa central cada vez que se añada, elimine o cambie un servicio del sistema.