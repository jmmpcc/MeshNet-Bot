# Índice de documentación de MeshNet-Bot v7.0.36

Este directorio contiene la documentación vigente del núcleo. Los README situados dentro de `tools/` son la referencia principal de cada aplicación independiente.

## Documentación vigente

| Área | Documento | Alcance |
|---|---|---|
| Instalación, recuperación y mantenimiento | [`OPERATIONS.md`](OPERATIONS.md) | Docker, systemd, reinicio, reinstalación y diagnóstico |
| Perfiles MeshCore/Meshtastic | [`RADIO_PROFILES.md`](RADIO_PROFILES.md) | Perfiles, transportes y mapas de canales |
| Broker | [`BROKER_README.md`](BROKER_README.md) | Interfaces, colas y control JSONL |
| Bot Telegram | [`BOT_README.md`](BOT_README.md) | Comandos, programación y administración |
| APRS y APRS-IS | [`APRS_GATEWAY.md`](APRS_GATEWAY.md) | Contenedor, KISS RF, iGate, APRS-IS, mirror Mesh, despliegues, pruebas y recuperación |
| KISS remoto y emergencias | [`APRS_Remote_KISS_Emergency.md`](APRS_Remote_KISS_Emergency.md) | TNC remoto y flujo especializado de emergencia |
| Correo ↔ malla | [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md) | Contenedor `email-to-mesh`, IMAP, SMTP y contactos |
| Auditorías | [`AUDITORIAS.md`](AUDITORIAS.md) | Informes y análisis de red |
| Instalación Raspberry Pi | [`Manual_Instalacion_MeshNet_RaspberryPi.md`](Manual_Instalacion_MeshNet_RaspberryPi.md) | Preparación base de la Raspberry |
| Historial | [`Historial_Versiones.md`](Historial_Versiones.md) y `CHANGELOG_v*.md` | Evolución del proyecto |

## Componentes Docker documentados

| Servicio Compose | Contenedor | Documento principal |
|---|---|---|
| `broker` | `meshnet-broker` | [`BROKER_README.md`](BROKER_README.md) |
| `bot` | `meshnet-bot` | [`BOT_README.md`](BOT_README.md) |
| `aprs` | `meshnet-aprs` | [`APRS_GATEWAY.md`](APRS_GATEWAY.md) |
| `email-to-mesh` | `meshnet-email-to-mesh` | [`EMAIL_TO_MESH.md`](EMAIL_TO_MESH.md) |
| `bridgehub-bc` | `meshnet-bridge-bc` | [`../bridge-bc/Mesh_Triple_Bridge_README.md`](../bridge-bc/Mesh_Triple_Bridge_README.md) |

### APRS

El componente APRS no es una función secundaria del bot. Es un contenedor propio que:

- se conecta a KISS TCP para transmitir y recibir APRS por RF;
- puede actuar como iGate hacia APRS-IS;
- recibe peticiones por UDP en el puerto 9464;
- comparte la red del broker;
- reenvía tráfico hacia MeshCore o Meshtastic según `RADIO_PROFILE`;
- admite mirror selectivo Mesh → APRS-IS;
- gestiona emergencias, boletines, fragmentación y deduplicación.

La instalación y los ejemplos funcionales están en [`APRS_GATEWAY.md`](APRS_GATEWAY.md).

### Email-to-mesh

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

1. Código, archivos `docker-compose*.yml`, entrypoints y unidades systemd de la versión instalada.
2. README específico del componente o aplicación.
3. `OPERATIONS.md` y README principal.
4. Documentos históricos.