# Índice de documentación de MeshNet-Bot v7.0.35

Este directorio contiene la documentación del núcleo. Los README situados dentro de `tools/` son la referencia principal de cada aplicación independiente.

## Documentación vigente

| Área | Documento |
|---|---|
| Instalación, recuperación y mantenimiento | [`OPERATIONS.md`](OPERATIONS.md) |
| Perfiles MeshCore/Meshtastic | [`RADIO_PROFILES.md`](RADIO_PROFILES.md) |
| Broker | [`BROKER_README.md`](BROKER_README.md) |
| Bot Telegram | [`BOT_README.md`](BOT_README.md) |
| APRS | [`APRS_GATEWAY.md`](APRS_GATEWAY.md) |
| KISS remoto para emergencias | [`APRS_Remote_KISS_Emergency.md`](APRS_Remote_KISS_Emergency.md) |
| Contenedor correo ↔ malla (`email-to-mesh`) | [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md) |
| Auditorías | [`AUDITORIAS.md`](AUDITORIAS.md) |
| Instalación Raspberry Pi | [`Manual_Instalacion_MeshNet_RaspberryPi.md`](Manual_Instalacion_MeshNet_RaspberryPi.md) |
| Historial | [`Historial_Versiones.md`](Historial_Versiones.md) y `CHANGELOG_v*.md` |

## Componentes Docker documentados

- `meshnet-broker`
- `meshnet-bot`
- `meshnet-aprs`
- `meshnet-email-to-mesh` — servicio Compose `email-to-mesh`
- `meshnet-bridge-bc`, cuando se habilita

`email-to-mesh` forma parte del núcleo Docker. No es una aplicación systemd independiente. Su configuración se obtiene del `.env` principal y sus datos persistentes se guardan en `bot_data/`.

## Aplicaciones independientes

- [`../tools/ControlPanel/README.md`](../tools/ControlPanel/README.md)
- [`../tools/farmacias_guardia/README.md`](../tools/farmacias_guardia/README.md)
- [`../tools/emergencias_guardia/README.md`](../tools/emergencias_guardia/README.md)
- [`../tools/voice_rf_gateway/README.md`](../tools/voice_rf_gateway/README.md)

## Documentos históricos

Los archivos cuyo nombre contiene una versión antigua o el sufijo `_old` se conservan como referencia histórica. No deben emplearse como guía de instalación actual cuando contradigan el README principal, `OPERATIONS.md` o el README específico del componente.

Ejemplos:

- `BROKER_README_v6.1.1.md`
- `APRS_GATEWAY_FULL_v6.2.md`
- `APRS_GATEWAY_old.md`
- `CHANGELOG_v6.1.1.md`

## Regla de precedencia

Ante una contradicción, aplicar este orden:

1. Código, archivos `docker-compose*.yml` y unidades systemd de la versión instalada.
2. README específico del componente o aplicación.
3. `OPERATIONS.md` y README principal.
4. Documentos históricos.
