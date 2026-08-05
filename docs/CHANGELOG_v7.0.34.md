# MeshNet-Bot v7.0.34 — Fase 2C Voice RF segura

- Nuevo servicio independiente `tools/voice_rf_gateway`.
- API local `/health` y `/dispatch`.
- Síntesis Piper con fallback eSpeak NG.
- Normalización de texto de emergencias.
- Validación de duración WAV.
- CLI `doctor`, `serve` y `synthesize`.
- Unidad systemd `meshnet-voice-rf.service`.
- Integración opcional con `Emergency Dispatcher`.
- Sin reproducción, PTT ni transmisión RF.
- Todas las autorizaciones desactivadas por defecto.
