# MeshNet-Bot v7.0.39 — Control UDP APRS para aplicaciones del host

## Objetivo

Permitir que aplicaciones independientes ejecutadas mediante systemd en la Raspberry, especialmente `emergencias_guardia`, alcancen el gateway APRS que comparte el namespace de red del broker.

## Cambios

- Nueva variable `APRS_CTRL_BIND`.
- Nueva variable `APRS_CTRL_PORT_HOST`.
- Publicación Compose `127.0.0.1:9464:9464/udp` en el servicio `broker`.
- El listener APRS usa `APRS_CTRL_BIND`; los clientes siguen usando `APRS_CTRL_HOST`.
- Compatibilidad histórica si no se define `APRS_CTRL_BIND`.
- Pruebas unitarias para ambas configuraciones.
- Montaje de `source/meshtastic_to_aprs.py` en el servicio APRS para ejecutar la versión local y no depender de una imagen GHCR todavía antigua.
- Cabeceras de versión del gateway y de la guía APRS actualizadas a v7.0.39.

## Seguridad

El puerto se publica solo en el loopback del host y no queda disponible en la LAN. No se modifica KISS, APRS RF, APRS-IS, MeshCore, Meshtastic ni `/aprsis_push`.
