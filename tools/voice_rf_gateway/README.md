# MeshNet-Bot Voice RF Gateway v7.0.34

Servicio independiente para preparar la futura conversión de emergencias a voz.

## Estado de esta fase

- API local `/health` y `/dispatch`.
- Piper con fallback eSpeak NG.
- Normalización de textos y URLs.
- Generación y validación de WAV.
- Modo diagnóstico y síntesis manual.
- Sin reproducción ALSA.
- Sin PTT.
- Sin acceso al transmisor.
- `VOICE_RF_TRANSMIT_ENABLED` no habilita RF en v7.0.34.

## Comandos

```bash
cd /home/meshnet/MeshNet-Bot
python3 tools/voice_rf_gateway/voice_rf_gateway.py doctor
python3 tools/voice_rf_gateway/voice_rf_gateway.py synthesize \
  --test --text "Incendio simulado en Zuera" --keep
python3 tools/voice_rf_gateway/voice_rf_gateway.py serve
```

## Servicio systemd

```bash
sudo cp systemd/meshnet-voice-rf.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-voice-rf.service
```

El servicio puede permanecer arrancado con `VOICE_RF_SERVICE_ENABLED=0`; responderá
a `/health`, pero rechazará la síntesis automática con `reason=disabled`.
