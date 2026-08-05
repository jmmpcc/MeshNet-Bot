# MeshNet-Bot v7.0.31

## Salida APRS común para aplicaciones independientes — Fase 1

- Añadido `shared/app_aprs_dispatcher.py` como interfaz común hacia el puerto UDP
  de control del gateway APRS existente.
- Reutilizadas las variables `APRS_CTRL_HOST`, `APRS_CTRL_PORT`, `APRS_MAX_LEN`,
  `APRS_CTRL_ACK_TIMEOUT` y `APRS_BOT_PATH`.
- Añadida lista blanca global y límite preventivo de fragmentos mediante
  `APPS_APRS_*`.
- Integrada Farmacias con salida APRS manual opcional mediante `send --aprs`.
- Añadida autorización independiente para emisiones automáticas.
- El envío APRS se ejecuta después del envío Mesh y sus errores no revierten ni
  repiten las transmisiones Mesh aceptadas.
- Configuración desactivada de forma predeterminada para conservar exactamente
  el comportamiento anterior.
- Validación: compilación Python correcta y 29 pruebas unitarias superadas.
