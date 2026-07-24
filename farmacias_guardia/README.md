# Farmacias de guardia

Aplicación Python completamente independiente de MeshNet-Bot y de Docker.

No se instala dentro del proyecto MeshNet-Bot, no importa sus módulos y no lee su archivo `.env`. La relación con el broker se configura exclusivamente mediante el `.env` propio de esta aplicación.

## Ubicación

```text
/home/meshnet/farmacias_guardia/
```

## Relación con el broker

La aplicación utiliza el puerto de control del broker para publicar el listado diario:

```env
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766
BROKER_TIMEOUT_SECONDS=10
```

El perfil y los canales también se declaran en el `.env` propio:

```env
RADIO_PROFILE=meshcore_only
FARMACIAS_BROADCAST_TRANSPORT=auto
FARMACIAS_MIXED_PROFILE_BROADCAST=meshcore
FARMACIAS_MESHCORE_CHANNEL=2
FARMACIAS_MESHTASTIC_CHANNEL=3
```

La aplicación no intenta descubrir ni leer la configuración interna de MeshNet-Bot.

## Configuración del broker

El broker necesita conocer la dirección de la API de esta aplicación para resolver los comandos `farma`. Estas variables se añaden al `.env` operativo del broker:

```env
FARMACIAS_COMMAND_ENABLED=true
FARMACIAS_SERVICE_URL=http://172.17.0.1:8788/query
FARMACIAS_SERVICE_TIMEOUT_SECONDS=3
FARMACIAS_MESHCORE_CHANNEL=2
FARMACIAS_MESHTASTIC_CHANNEL=3
FARMACIAS_MAX_REQUESTS_PER_HOUR=5
FARMACIAS_RATE_LIMIT_WINDOW_SECONDS=3600
FARMACIAS_RATE_LIMIT_SAVE_SECONDS=60
FARMACIAS_DUPLICATE_WINDOW_SECONDS=20
FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE=6
```

`172.17.0.1` es habitualmente la puerta de enlace del host vista desde Docker. Debe sustituirse por la dirección real accesible desde el contenedor cuando la instalación utilice otra red Docker.

No es necesario modificar `docker-compose.yml` ni `docker-compose.rpi.yml`.

## Instalación

```bash
sudo mkdir -p /home/meshnet/farmacias_guardia
sudo cp -a farmacias_guardia/. /home/meshnet/farmacias_guardia/
sudo chown -R meshnet:meshnet /home/meshnet/farmacias_guardia
cd /home/meshnet/farmacias_guardia
cp .env.example .env
nano .env
```

## CLI

```bash
python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py preview
python3 farmacias_guardia.py send
python3 farmacias_guardia.py send --force
python3 farmacias_guardia.py check
python3 farmacias_guardia.py check --send
python3 farmacias_guardia.py status
python3 farmacias_guardia.py doctor
python3 farmacias_guardia.py serve
```

## systemd

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-farmacias-api.service
sudo systemctl enable --now meshnet-farmacias-daily.timer
sudo systemctl enable --now meshnet-farmacias-check.timer
```

## Responsabilidades

La aplicación independiente realiza la descarga, normalización, almacenamiento, detección de cambios, API de consultas y difusión diaria.

El broker únicamente detecta `farma`, aplica el límite de cinco peticiones por hora y responde por DM mediante la misma red de origen.
