# CHANGELOG v7.0.44 — 2026-08-10

## Objetivo

Alinear `/aprsis_push` del bot Telegram con la sintaxis real que ya soporta el gateway APRS-IS para Meshtastic, MeshCore y perfiles mixtos, sin modificar el parser ni el transporte APRS existente.

## Bot Telegram

- `/aprsis_push` sin argumentos muestra ahora una ayuda completa y operativa.
- Se documenta la sintaxis Meshtastic legacy: `on all` y `on 0,1,2`.
- Se documenta la selección explícita de Meshtastic: `on meshtastic 0,1`.
- Se documenta la selección explícita de MeshCore: `on meshcore 1,2`.
- Se documenta la combinación de ambos transportes: `on meshtastic 0,1 meshcore 2,3`.
- Se muestran los alias admitidos por el gateway: `mesh`/`malla` para Meshtastic y `mc` para MeshCore.
- Se aclara el rango de canales `0..15`.
- Se advierte que `RADIO_PROFILE=meshcore_only` requiere el prefijo explícito `meshcore` o `mc`; la sintaxis sin prefijo pertenece al modo Meshtastic legacy.
- Se corrige la descripción obsoleta y con errores tipográficos del menú Telegram `SetMyCommands`.
- Se amplía la sección APRS de `/ayuda` con ejemplos Meshtastic, MeshCore y mixtos.

## Estado APRS-IS push

Se implementa `/aprsis_push status` en el bot usando el contrato de consulta de solo lectura que ya existía en `source/meshtastic_to_aprs.py`.

El estado muestra, cuando el gateway los devuelve:

- ON/OFF;
- destino APRS-IS;
- expresión de canales;
- configuración separada por transporte;
- prefijo;
- intervalo mínimo (`min_gap_s`).

## Compatibilidad

- No se modifica `source/meshtastic_to_aprs.py`.
- No se modifica `_parse_push_channel_config()`.
- Los payloads históricos de `/aprsis_push on` y `/aprsis_push off` conservan los mismos campos y significado.
- No se modifica APRS RF, APRS-IS, KISS, deduplicación, `MIN_INTERVAL` ni el dispatcher de emergencias.
- No se modifica MeshCore ni Meshtastic fuera de la selección de canales ya soportada por el gateway.

## Validación

- `python -m py_compile source/Telegram_Bot_Broker.py` correcto.
- `git diff --check` correcto.
- `tests/test_aprsis_push_bot_help.py` verifica ayuda, `status`, compatibilidad de payloads y menú Telegram sin importar dependencias externas del bot.
