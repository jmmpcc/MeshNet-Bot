# MeshNet-Bot v7.0.36 — revisión documental APRS/APRS-IS

Fecha: 6 de agosto de 2026.

## Alcance

Revisión integral de la documentación del sistema APRS y APRS-IS sin modificar código operativo, archivos Compose, entrypoints ni configuración real.

## Documentos actualizados

- `README.md`
- `docker/README.MD/README.md`
- `docs/README.md`
- `docs/OPERATIONS.md`
- `docs/APRS_GATEWAY.md`
- `docs/APRS_Remote_KISS_Emergency.md`
- `docs/BOT_README.md`
- `docs/BROKER_README.md`

## Contenido incorporado

- Servicio Compose `aprs` y contenedor `meshnet-aprs`.
- Imagen `ghcr.io/jmmpcc/meshnet-bot-aprs:latest`.
- Ejecución de `source/meshtastic_to_aprs.py` mediante `entrypoint_aprs.sh`.
- Arquitectura KISS RF, APRS-IS, broker, bot, MeshCore y Meshtastic.
- Puertos 8765, 8766, 9464/UDP, KISS TCP y APRS-IS 14580.
- Configuración de indicativo, ruta AX.25, fragmentación y demora RF.
- Despliegue con Direwolf/Soundmodem local.
- Despliegue con TNC KISS remoto.
- Despliegue APRS RF sin Internet.
- Despliegue APRS-IS e iGate.
- Perfil `meshcore_only` y `APRS_TO_MESHCORE`.
- Mirror Mesh → APRS-IS y recepción en APRSDroid.
- Comandos Telegram `/aprs_on`, `/aprs_off`, `/aprs` y `/aprsis_push`.
- Formatos `[CHx]`, `[MCx]` y programación diferida.
- Emergencias y boletines APRS-IS.
- Pruebas funcionales de KISS, DNS, TCP APRS-IS y control UDP.
- Diagnóstico de `Connection refused`, `No route to host`, ausencia de RF, fragmentación y `rate_limited`.
- Prevención de bucles, deduplicación, `NOGATE` y `RFONLY`.
- Reinicio, parada, recreación, actualización y reinstalación.
- Corrección de nombres históricos de contenedores.

## Seguridad

- No se añaden indicativos, passcodes ni credenciales reales.
- No se modifica la configuración activa del proyecto.
- KISS TCP se documenta como servicio restringido a la LAN o VPN necesaria.
- Se recuerda el cumplimiento de normativa, rutas AX.25 y límites de emisión.