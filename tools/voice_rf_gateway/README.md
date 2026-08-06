# MeshNet-Bot Voice RF Gateway v7.0.35

Aplicación independiente que normaliza textos de emergencia y genera audio WAV mediante Piper o eSpeak NG.

## Estado funcional

En v7.0.35 están disponibles:

- API local `/health` y `/dispatch`.
- Normalización de texto, indicativos, números y URL.
- Síntesis con Piper y respaldo mediante eSpeak NG.
- Validación y conservación opcional del WAV.
- Diagnóstico CLI.

No están habilitados:

- reproducción ALSA;
- control PTT;
- acceso a transmisor;
- emisión RF.

`VOICE_RF_TRANSMIT_ENABLED` no convierte esta fase en un transmisor RF.

## Ruta oficial

```text
/home/meshnet/MeshNet-Bot/tools/voice_rf_gateway
```

## Requisitos

```bash
sudo apt update
sudo apt install -y python3 python3-venv espeak-ng
```

Piper es opcional. Cuando no está disponible, el servicio puede usar eSpeak NG según configuración.

## Configuración

Variables principales:

```env
VOICE_RF_SERVICE_ENABLED=0
VOICE_RF_TRANSMIT_ENABLED=0
VOICE_RF_HOST=127.0.0.1
VOICE_RF_PORT=8791
VOICE_RF_TTS_ENGINE=auto
VOICE_RF_OUTPUT_DIR=/tmp/meshnet-voice-rf
```

Mantener `VOICE_RF_TRANSMIT_ENABLED=0` en esta fase.

## Prueba manual

```bash
cd /home/meshnet/MeshNet-Bot
python3 tools/voice_rf_gateway/voice_rf_gateway.py doctor
python3 tools/voice_rf_gateway/voice_rf_gateway.py synthesize \
  --test \
  --text "Incendio simulado en Zuera" \
  --keep
```

La prueba debe generar un WAV válido, pero no reproducirlo ni activar PTT.

## Ejecución manual

```bash
python3 tools/voice_rf_gateway/voice_rf_gateway.py serve
```

Comprobación:

```bash
curl -fsS http://127.0.0.1:8791/health
```

## Instalación systemd

```bash
cd /home/meshnet/MeshNet-Bot
sudo install -m 0644 systemd/meshnet-voice-rf.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-voice-rf.service
sudo systemctl status meshnet-voice-rf.service --no-pager
```

## Reinicio y logs

```bash
sudo systemctl restart meshnet-voice-rf.service
journalctl -u meshnet-voice-rf.service -n 100 --no-pager
journalctl -u meshnet-voice-rf.service -f
```

## Reinstalación

```bash
sudo systemctl disable --now meshnet-voice-rf.service
sudo rm -f /etc/systemd/system/meshnet-voice-rf.service
sudo install -m 0644 /home/meshnet/MeshNet-Bot/systemd/meshnet-voice-rf.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-voice-rf.service
```

## API

### `GET /health`

Devuelve el estado del servicio, motor TTS y capacidad efectiva.

### `POST /dispatch`

Recibe un texto estructurado y solicita síntesis. Con el servicio deshabilitado responde con `reason=disabled`. En esta fase una respuesta correcta significa que se preparó audio; no confirma ninguna emisión.

## Diagnóstico

```bash
systemctl cat meshnet-voice-rf.service
sudo ss -ltnp | grep ':8791'
command -v piper || true
command -v espeak-ng
find /tmp/meshnet-voice-rf -maxdepth 1 -type f -name '*.wav' -ls
```

Errores habituales:

- `disabled`: activar únicamente `VOICE_RF_SERVICE_ENABLED`; no habilitar transmisión.
- motor no encontrado: instalar eSpeak NG o configurar correctamente Piper.
- WAV no creado: revisar permisos del directorio de salida.
- API inaccesible: comprobar host, puerto y unidad systemd.

## Seguridad

El servicio debe permanecer limitado al host o a una red controlada. No exponer `/dispatch` a Internet. La futura activación de audio/PTT requerirá enclavamientos, límites temporales, control de duplicados y validación regulatoria independiente.
