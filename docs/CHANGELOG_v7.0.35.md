# MeshNet-Bot v7.0.35

## Fase 2D · APRS RF automático para emergencias

- El Emergency Dispatcher puede solicitar APRS RF para eventos `high` y `critical`.
- Se reutiliza el gateway UDP APRS existente; no se abre KISS ni otra sesión de radio.
- Nuevo interruptor seguro `EMERGENCIAS_APRS_RF_ENABLED=0`.
- Se reutilizan `APRS_CTRL_HOST`, `APRS_CTRL_PORT`, `APRS_EMERG_DEST`, `APRS_BOT_PATH`, `APRS_PATH`, `APRS_MAX_LEN` y `APPS_APRS_MAX_CHUNKS`.
- APRS RF, APRS-IS y Voice RF devuelven resultados independientes.
- Los fallos secundarios no revierten ni repiten un envío Mesh correcto.
- Se mantiene `RF_MANAGER_ENABLED=0`; el transmisor continúa bajo el gateway APRS actual.
