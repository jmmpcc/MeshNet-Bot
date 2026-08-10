# MeshNet-Bot “The Boss” — v7.0.47

MeshNet-Bot es una plataforma de comunicaciones para radioaficionados que integra **MeshCore**, **Meshtastic**, **APRS RF**, **APRS-IS**, Telegram, correo electrónico, BBS, panel web y aplicaciones auxiliares independientes.

La arquitectura separa los componentes críticos: el broker administra interfaces y colas; el bot ofrece control por Telegram; el contenedor APRS gestiona KISS, RF, APRS-IS, iGate, mirror y emergencias; `email-to-mesh` conecta IMAP/SMTP con la malla; y las aplicaciones independientes consultan y publican información sin introducir dependencias dentro del núcleo.

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/README.md`](docs/README.md) | Índice documental y clasificación de documentos vigentes o históricos. |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Instalación, actualización, reinicio, recuperación y reinstalación. |
| [`docs/RADIO_PROFILES.md`](docs/RADIO_PROFILES.md) | Perfiles `meshcore_only`, Meshtastic y configuraciones mixtas. |
| [`docs/BROKER_README.md`](docs/BROKER_README.md) | Arquitectura y operación del broker. |
| [`docs/BOT_README.md`](docs/BOT_README.md) | Configuración y funciones del bot Telegram. |
| [`docs/APRS_GATEWAY.md`](docs/APRS_GATEWAY.md) | Contenedor APRS: KISS RF, APRS-IS, iGate, mirror, despliegues, pruebas y recuperación. |
| [`docs/APRS_Remote_KISS_Emergency.md`](docs/APRS_Remote_KISS_Emergency.md) | KISS remoto y flujo especializado de emergencias. |
| [`docs/EMAIL_TO_MESH.md`](docs/EMAIL_TO_MESH.md) | Contenedor `email-to-mesh`: IMAP, SMTP, contactos, operación y diagnóstico. |
| [`tools/ControlPanel/README.md`](tools/ControlPanel/README.md) | Panel de control independiente. |
| [`tools/farmacias_guardia/README.md`](tools/farmacias_guardia/README.md) | Aplicación de farmacias de guardia. |
| [`tools/emergencias_guardia/README.md`](tools/emergencias_guardia/README.md) | Aplicación de emergencias. |
| [`tools/voice_rf_gateway/README.md`](tools/voice_rf_gateway/README.md) | Síntesis de voz para emergencias; fase sin transmisión RF. |

## Componentes principales

| Componente | Ejecución | Función |
|---|---|---|
| `meshnet-broker` | Docker | Interfaces MeshCore/Meshtastic, colas, control JSONL, comandos de malla y puentes. |
| `meshnet-bot` | Docker | Telegram, programación, consultas, escucha y administración. |
| `meshnet-aprs` | Docker | KISS TCP, APRS RF, APRS-IS, iGate, pasarela Mesh/APRS, mirror y boletines. |
| `meshnet-email-to-mesh` | Docker | Lectura IMAP, correo hacia la malla, SMTP desde malla/Telegram/CLI y contactos persistentes. |
| `meshnet-bridge-bc` | Docker opcional | Puentes externos adicionales. |
| ControlPanel | systemd, host | Administración web de aplicaciones y servicios. |
| Farmacias | systemd, host | Consulta, API local y publicación programada. |
| Emergencias | systemd, host | Agregación de fuentes, API local y avisos incrementales. |
| Voice RF Gateway | systemd, host | Preparación y síntesis WAV; sin PTT ni emisión RF en v7.0.36. |

Los nombres de servicio Compose son `broker`, `bot`, `aprs`, `email-to-mesh` y, opcionalmente, `bridgehub-bc`.

## Requisitos

- Raspberry Pi OS o Linux Debian compatible; Docker Desktop para pruebas en Windows.
- Docker Engine y complemento `docker compose`.
- Python 3.10 o posterior para aplicaciones independientes.
- Nodo MeshCore o Meshtastic accesible según `RADIO_PROFILE`.
- Para APRS RF: TNC KISS TCP, Direwolf o Soundmodem accesible y equipo de radio correctamente configurado.
- Para APRS-IS: indicativo, SSID y passcode válidos.
- Para correo: cuenta IMAP y, cuando se use malla hacia correo, cuenta SMTP o contraseña de aplicación.
- Credenciales únicamente en archivos `.env` locales, nunca en Git.

## Instalación base en Raspberry Pi

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip netcat-openbsd
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Cerrar sesión y volver a entrar para aplicar el grupo Docker. Después:

```bash
cd /home/meshnet
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd /home/meshnet/MeshNet-Bot
cp .env_example .env
nano .env
python3 scripts/radio-profile-check
```

Arranque recomendado:

```bash
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
docker compose -f docker-compose.rpi.yml ps
```

Comprobación:

```bash
docker logs --tail 100 meshnet-broker
docker logs --tail 100 meshnet-bot
docker logs --tail 200 meshnet-aprs
docker logs --tail 100 meshnet-email-to-mesh
```

## Perfiles de radio

El valor de `RADIO_PROFILE` determina qué interfaces pueden utilizarse. No deben configurarse destinos Meshtastic en una instalación `meshcore_only`.

```env
RADIO_PROFILE=meshcore_only
```

Los perfiles mixtos deben ajustarse siguiendo [`docs/RADIO_PROFILES.md`](docs/RADIO_PROFILES.md). El broker es la autoridad: una aplicación auxiliar no debe forzar una interfaz deshabilitada por el perfil.

## Contenedor APRS y APRS-IS

El servicio `aprs` está incluido en `docker-compose.rpi.yml`, usa la imagen `ghcr.io/jmmpcc/meshnet-bot-aprs:latest`, ejecuta `/app/source/meshtastic_to_aprs.py` y comparte la red del broker.

### Configuración mínima APRS RF

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

### Añadir APRS-IS

```env
APRSIS_USER=EB2XXX-11
APRSIS_PASSCODE=12345
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=m/20
```

El entrypoint activa APRS-IS únicamente cuando existen usuario y passcode.

### Arranque y comprobación

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d broker aprs
docker compose -f docker-compose.rpi.yml ps broker aprs
docker logs --tail 200 meshnet-aprs
```

### Probar KISS desde el contenedor

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

### Ejemplo MeshCore + APRS

```env
RADIO_PROFILE=meshcore_only
APRS_TO_MESHCORE=1
MESHCORE_CHANNEL_MAP=0:0,1:1,2:2
APRS_CALL=EB2XXX-11
KISS_HOST=host.docker.internal
KISS_PORT=8100
APRS_GATE_ENABLED=1
```

Un mensaje APRS con `[CH1]` se resolverá hacia el canal MeshCore configurado. La guía completa, incluidos mirror Mesh → APRS-IS, emergencias, boletines y pruebas UDP, está en [`docs/APRS_GATEWAY.md`](docs/APRS_GATEWAY.md).

Los boletines de Emergencias pueden publicarse en un grupo APRS configurable. Con `APRSIS_EMERGENCY_BULLETIN_GROUP=EMERG`, las líneas serán `BLN0EMERG` a `BLN9EMERG`; dejando la variable vacía se conserva el formato histórico `BLN0` a `BLN9`.

En v7.0.42 la configuración recomendada deja `APRSIS_EMERGENCY_BULLETIN_GROUP=` vacío para que los avisos se publiquen como boletines estándar `BLN0..BLN9`. El soporte de grupos permanece intacto: si el operador asigna un valor, por ejemplo `EMERG`, el mismo código genera `BLN0EMERG..BLN9EMERG`.

Los estados terminales `resolved`, `cancelled`, `expired` y `closed` omiten exclusivamente `APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC`; la deduplicación continúa activa. APRS RF y APRS-IS usan además un resumen específico de hasta 67 caracteres (`EMERGENCIAS_APRS_TEXT_MAX_CHARS`) que conserva primero estado y tipo de emergencia y después carretera/km y municipio.

El catálogo reservado del proyecto define `EMERG` para emergencias, `AEMET` para avisos meteorológicos oficiales, `FARMA` para farmacias de guardia, `NEWS` para noticias relevantes, `MESH` para avisos del sistema y `TEST` para pruebas controladas. En v7.0.38 solo `EMERG` está conectado a publicación automática; los demás grupos quedan preparados y no generan tráfico hasta que su aplicación se habilite expresamente.

### Control APRS desde aplicaciones systemd del host

En Raspberry Pi, `meshnet-aprs` comparte el namespace de red del broker. Para que aplicaciones independientes como Emergencias puedan solicitar APRS RF y boletines APRS-IS, el Compose publica el control UDP exclusivamente en el loopback del host:

```env
APRS_CTRL_HOST=127.0.0.1
APRS_CTRL_PORT=9464
APRS_CTRL_BIND=0.0.0.0
APRS_CTRL_PORT_HOST=9464
```

En `docker-compose.rpi.yml`, el servicio APRS monta el gateway local `./source/meshtastic_to_aprs.py` sobre `/app/source/meshtastic_to_aprs.py`. De este modo, la recreación usa el código instalado aunque la imagen `latest` aún no haya sido republicada.

La publicación efectiva es `127.0.0.1:9464/udp`; no queda accesible desde la LAN.

## Contenedor email-to-mesh

El servicio está incluido en `docker-compose.rpi.yml` y se inicia junto con el resto del núcleo:

```bash
cd /home/meshnet/MeshNet-Bot
docker compose -f docker-compose.rpi.yml up -d email-to-mesh
docker compose -f docker-compose.rpi.yml ps email-to-mesh
docker logs --tail 100 meshnet-email-to-mesh
```

Reinicio y recreación:

```bash
docker compose -f docker-compose.rpi.yml restart email-to-mesh
docker compose -f docker-compose.rpi.yml up -d --force-recreate email-to-mesh
```

La configuración IMAP/SMTP se lee del `.env` principal. Los contactos y el estado de deduplicación se conservan en `bot_data/`, montado como `/app/bot_data` dentro del contenedor. La guía completa está en [`docs/EMAIL_TO_MESH.md`](docs/EMAIL_TO_MESH.md).

## Actualización segura

Antes de actualizar:

```bash
cd /home/meshnet/MeshNet-Bot
git status
cp .env "$HOME/meshnet.env.backup"
cp -a bot_data "$HOME/meshnet.bot_data.backup"
```

No ejecutar `git pull` con conflictos o archivos sin resolver. Con cambios locales válidos, guardarlos en un commit o en `stash`.

```bash
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

## Operación diaria

```bash
# Estado Docker
docker compose -f docker-compose.rpi.yml ps

# Reiniciar componentes concretos
docker compose -f docker-compose.rpi.yml restart broker
docker compose -f docker-compose.rpi.yml restart aprs
docker compose -f docker-compose.rpi.yml restart email-to-mesh

# Logs continuos
docker logs -f meshnet-broker
docker logs -f meshnet-aprs
docker logs -f meshnet-email-to-mesh

# Estado de aplicaciones independientes
systemctl status meshnet-control-panel.service --no-pager
systemctl status meshnet-farmacias-api.service --no-pager
systemctl status meshnet-emergencias-api.service --no-pager
systemctl list-timers 'meshnet-*'
```

`docker compose down` elimina contenedores y red, pero no los datos persistentes del proyecto. Para reconstruir:

```bash
docker compose -f docker-compose.rpi.yml up -d
```

No usar `down -v` salvo que se pretenda eliminar volúmenes.

## Aplicaciones independientes

Rutas operativas oficiales:

```text
/home/meshnet/MeshNet-Bot/tools/ControlPanel
/home/meshnet/MeshNet-Bot/tools/farmacias_guardia
/home/meshnet/MeshNet-Bot/tools/emergencias_guardia
/home/meshnet/MeshNet-Bot/tools/voice_rf_gateway
```

Cada aplicación mantiene su configuración local y sus servicios systemd. `aprs` y `email-to-mesh` no pertenecen a este grupo: son contenedores del núcleo Docker y utilizan el `.env` principal.

## Seguridad

- No subir `.env`, `.web_env`, contraseñas, tokens, claves API ni datos operativos.
- No publicar el puerto de control del broker fuera del host sin filtrado de red.
- Restringir `EMAIL_ALLOWED_SENDERS`.
- Limitar KISS TCP al host o LAN necesarios; no exponerlo a Internet.
- Respetar `NOGATE` y `RFONLY`.
- Usar indicativos, rutas AX.25, potencia y frecuencias legalmente válidos.
- No generar pruebas repetitivas sobre APRS RF o APRS-IS.
- Las API auxiliares enlazadas a `0.0.0.0` deben quedar limitadas por firewall.

## Pruebas

```bash
cd /home/meshnet/MeshNet-Bot
python3 -m compileall -q source shared tools
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Licencia y atribución

Consulte [`LICENSE`](LICENSE). Los forks y trabajos derivados deben conservar la atribución requerida por el proyecto.

## Variables compartidas de Emergencias · v7.0.40

El servicio `meshnet-emergencias-check.service` carga primero `/home/meshnet/MeshNet-Bot/.env` y después `tools/emergencias_guardia/.env`. Esto permite que el dispatcher reciba `APPS_APRS_ENABLED`, `EMERGENCIAS_APRS_ENABLED`, `EMERGENCIAS_APRS_RF_ENABLED`, `APRS_CTRL_HOST` y `APRS_CTRL_PORT`. El archivo local de Emergencias conserva prioridad para sobrescrituras específicas. Tras instalar la unidad debe ejecutarse `sudo systemctl daemon-reload`.


## APRS RF y APRS-IS de Emergencias · v7.0.42

El dispatcher deja de estimar localmente la fragmentación APRS. APRS RF y APRS-IS generan primero un resumen específico de hasta 67 caracteres (`EMERGENCIAS_APRS_TEXT_MAX_CHARS=67`) que conserva estado y tipo de emergencia al principio. Para RF, `aprs_preview` usa exactamente el mismo saneamiento y troceado que el envío real y no transmite ni altera la deduplicación. Emergencias mantiene además el máximo de 3 tramas (`EMERGENCIAS_APRS_RF_MAX_CHUNKS=3`) como barrera de seguridad. El mensaje Meshtastic/MeshCore no se modifica.

## Fuentes de Emergencias · v7.0.43

`emergencias_guardia` conserva DGT DATEX II, Ayuntamiento de Zaragoza, IGN y NASA FIRMS e incorpora dos orígenes oficiales adicionales, desactivados por defecto:

- **AEMET CAP 1.2** para avisos meteorológicos adversos. El conector utiliza AEMET OpenData, resuelve la URL temporal de datos y normaliza fenómeno, severidad, vigencia y área al modelo `Event` existente. Requiere `AEMET_API_KEY` cuando se activa esta modalidad.
- **CHE / SAIH Ebro** para comunicaciones hidrológicas oficiales. El conector filtra el canal de comunicaciones de la CHE y conserva únicamente contenidos operativos relativos a crecidas, avenidas, cauces, barrancos e inundaciones.

Las nuevas fuentes aparecen en **ControlPanel → Emergencias → Fuentes y cobertura**. RAN/Protección Civil y 112 Aragón quedan pendientes de una interfaz pública estructurada y estable; no se incorporan mediante scraping HTML.
