# MeshNet-Bot v7.0.54

Fecha: 2026-08-11

## MeshNet Mobile API

- Añadida una capa `mobile_api_v7054.py` sobre la API A1 existente, sin reescribir ni duplicar sus endpoints.
- La versión publicada deja de estar fijada a `v7.0.49` y se detecta automáticamente desde el changelog numérico más reciente de `docs/`.
- `MESHNET_BOT_VERSION` se conserva como override explícito.
- Nuevo endpoint protegido `GET /api/v1/capabilities` para que MeshNet-Mobile descubra las funciones disponibles antes de mostrar controles.
- La API continúa en modo `read_only`: no se habilitan todavía envío de mensajes, cambios de configuración ni control de servicios.
- Los contratos de nodos MeshCore y Meshtastic continúan sin proveedor enlazado hasta identificar una fuente existente y probada.

## Compatibilidad

- No se modifica `tools/MobileAPI/mobile_api.py` de la fase A1.
- No se modifica ControlPanel ni Web Admin.
- No se modifica MeshCore, Meshtastic, APRS/APRS-IS, dispatcher, deduplicación, Emergencias, Farmacias ni BBS.
- La unidad systemd de MobileAPI pasa a utilizar el nuevo entrypoint compatible.

## Backend reconocido por capacidades

La API informa al cliente de que el servidor actual ya incorpora:

- journal común de entregas;
- matriz APRS-IS/APRS RF por categoría;
- referencia FIRMS a población cercana;
- geolocalización ligera de nodos.

Estas capacidades informativas no habilitan por sí mismas operaciones de escritura desde Android.

## Validación

- Se amplían las pruebas de contrato de MobileAPI para cubrir autodetección de versión, override, autenticación y endpoint de capacidades.
- La validación en Raspberry debe ejecutar además las regresiones actuales de ControlPanel antes de fusionar.
