# MeshNet-Bot “The Boss” — v7.0.35

MeshNet-Bot es una plataforma de comunicaciones para radioaficionados que integra **MeshCore**, **Meshtastic**, **APRS**, Telegram, correo electrónico, BBS, panel web y aplicaciones auxiliares independientes.

La arquitectura mantiene separados los componentes críticos: el broker administra interfaces y colas; el bot ofrece control por Telegram; la pasarela APRS gestiona KISS/APRS-IS; y las aplicaciones independientes consultan y publican información sin introducir dependencias dentro del núcleo.

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/README.md`](docs/README.md) | Índice documental y clasificación de documentos vigentes o históricos. |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Instalación, actualización, reinicio, recuperación y reinstalación. |
| [`docs/RADIO_PROFILES.md`](docs/RADIO_PROFILES.md) | Perfiles `meshcore_only`, Meshtastic y configuraciones mixtas. |
| [`docs/BROKER_README.md`](docs/BROKER_README.md) | Arquitectura y operación del broker. |
| [`docs/BOT_README.md`](docs/BOT_README.md) | Configuración y funciones del bot Telegram. |
| [`docs/APRS_GATEWAY.md`](docs/APRS_GATEWAY.md) | Pasarela APRS RF/APRS-IS. |
| [`docs/EMAIL_TO_MESH.md`](docs/EMAIL_TO_MESH.md) | Correo hacia MeshCore/Meshtastic. |
| [`tools/ControlPanel/README.md`](tools/ControlPanel/README.md) | Panel de control independiente. |
| [`tools/farmacias_guardia/README.md`](tools/farmacias_guardia/README.md) | Aplicación de farmacias de guardia. |
| [`tools/emergencias_guardia/README.md`](tools/emergencias_guardia/README.md) | Aplicación de emergencias. |
| [`tools/voice_rf_gateway/README.md`](tools/voice_rf_gateway/README.md) | Síntesis de voz para emergencias; fase sin transmisión RF. |

## Componentes principales

| Componente | Ejecución | Función |
|---|---|---|
| `meshnet-broker` | Docker | Interfaces MeshCore/Meshtastic, colas, control JSONL, comandos de malla y puentes. |
| `meshnet-bot` | Docker | Telegram, programación, consultas, escucha y administración. |
| `meshnet-aprs` | Docker | APRS RF por KISS, APRS-IS, pasarela Mesh/APRS y boletines. |
| `meshnet-bridge-bc` | Docker opcional | Puentes externos adicionales. |
| ControlPanel | systemd, host | Administración web de aplicaciones y servicios. |
| Farmacias | systemd, host | Consulta, API local y publicación programada. |
| Emergencias | systemd, host | Agregación de fuentes, API local y avisos incrementales. |
| Voice RF Gateway | systemd, host | Preparación y síntesis WAV; sin PTT ni emisión RF en v7.0.35. |

## Requisitos

- Raspberry Pi OS o Linux Debian compatible; Docker Desktop para pruebas en Windows.
- Docker Engine y complemento `docker compose`.
- Python 3.10 o posterior para aplicaciones independientes.
- Nodo MeshCore o Meshtastic accesible según `RADIO_PROFILE`.
- Credenciales y claves únicamente en archivos `.env` locales, nunca en Git.

## Instalación base en Raspberry Pi

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip
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
docker logs --tail 100 meshnet-aprs
```

## Perfiles de radio

El valor de `RADIO_PROFILE` determina qué interfaces pueden utilizarse. No deben configurarse destinos Meshtastic en una instalación `meshcore_only`.

```env
RADIO_PROFILE=meshcore_only
```

Los perfiles mixtos deben ajustarse siguiendo [`docs/RADIO_PROFILES.md`](docs/RADIO_PROFILES.md). El broker es la autoridad: una aplicación auxiliar no debe forzar una interfaz deshabilitada por el perfil.

## Actualización segura

Antes de actualizar:

```bash
cd /home/meshnet/MeshNet-Bot
git status
cp .env "$HOME/meshnet.env.backup"
```

No ejecutar `git pull` con conflictos o archivos sin resolver. Con cambios locales válidos, guardarlos en un commit o en `stash`.

```bash
git pull --ff-only
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d --remove-orphans
```

Las aplicaciones del host pueden requerir volver a copiar sus unidades systemd; véase [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Operación diaria

```bash
# Estado Docker
docker compose -f docker-compose.rpi.yml ps

# Reiniciar solo un componente
docker compose -f docker-compose.rpi.yml restart broker

# Logs continuos
docker logs -f meshnet-broker

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

Cada aplicación mantiene su configuración local y sus servicios systemd. Sus README contienen instalación desde cero, reinstalación, comprobaciones y diagnóstico.

## Seguridad

- No subir `.env`, `.web_env`, contraseñas, tokens, claves API ni datos operativos.
- No publicar el puerto de control del broker fuera del host sin filtrado de red.
- Las API auxiliares enlazadas a `0.0.0.0` deben quedar limitadas por firewall a la red Docker/LAN necesaria.
- Realizar pruebas APRS con indicativos, rutas y límites de emisión legalmente válidos.

## Pruebas

```bash
cd /home/meshnet/MeshNet-Bot
python3 -m compileall -q source shared tools
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Licencia y atribución

Consulte [`LICENSE`](LICENSE). Los forks y trabajos derivados deben conservar la atribución requerida por el proyecto.
