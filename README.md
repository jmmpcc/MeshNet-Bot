# MeshNet-Bot “The Boss” — v7.0.43

MeshNet-Bot es una plataforma de comunicaciones para radioaficionados que integra **MeshCore**, **Meshtastic**, **APRS RF**, **APRS-IS**, Telegram, correo electrónico, BBS, panel web y aplicaciones auxiliares independientes.

La arquitectura separa los componentes críticos: el broker administra interfaces y colas; el bot ofrece control por Telegram; el contenedor APRS gestiona KISS, RF, APRS-IS, iGate, mirror y emergencias; `email-to-mesh` conecta IMAP/SMTP con la malla; y las aplicaciones independientes consultan y publican información sin introducir dependencias dentro del núcleo.

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/README.md`](docs/README.md) | Índice documental y clasificación de documentos vigentes o históricos. |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Instalación, actualización, reinicio, recuperación y reinstalación. |
| [`docs/RADIO_PROFILES.md`](docs/RADIO_PROFILES.md) | Perfiles `meshcore_only`, Meshtastic y configuraciones mixtas. |
| [`docs/BROKER_README.md`](docs/BROKER_README.md) | Arquitectura y operación del broker. |
| [`docs/BOT_README.md`](docs/BOT_README.md) | Configuración y funciones del bot Telegram. |
| [`docs/APRS_GATEWAY.md`](docs/APRS_GATEWAY.md) | Contenedor APRS: KISS RF, APRS-IS, iGate, mirror, despliegues, pruebas y recuperación. |
| [`docs/APRS_Remote_KISS_Emergency.md`](docs/APRS_Remote_KISS_Emergency.md) | KISS remoto y flujo especializado de emergencias. |
| [`docs/EMAIL_TO_MESH.md`](docs/EMAIL_TO_MESH.md) | Contenedor `email-to-mesh`: IMAP, SMTP, contactos, operación y diagnóstico. |
| [`tools/ControlPanel/README.md`](tools/ControlPanel/README.md) | Panel de control independiente. |
| [`tools/farmacias_guardia/README.md`](tools/farmacias_guardia/README.md) | Aplicación de farmacias de guardia. |
| [`tools/emergencias_guardia/README.md`](tools/emergencias_guardia/README.md) | Aplicación de emergencias. |
| [`tools/voice_rf_gateway/README.md`](tools/voice_rf_gateway/README.md) | Síntesis de voz para emergencias; fase sin transmisión RF. |

## Componentes principales

| Componente | Ejecución | Función |
|---|---|---|
| `meshnet-broker` | Docker | Interfaces MeshCore/Meshtastic, colas, control JSONL, comandos de malla y puentes. |
| `meshnet-bot` | Docker | Telegram, programación, consultas, escucha y administración. |
| `meshnet-aprs` | Docker | KISS TCP, APRS RF, APRS-IS, iGate, pasarela Mesh/APRS, mirror y boletines. |
| `meshnet-email-to-mesh` | Docker | Lectura IMAP, correo hacia la malla, SMTP desde malla/Telegram/CLI y contactos persistentes. |
| `meshnet-bridge-bc` | Docker opcional | Puentes externos adicionales. |
| ControlPanel | systemd, host | Administración web de aplicaciones y servicios. |
| Farmacias | systemd, host | Consulta, API local y publicación programada. |
| Emergencias | systemd, host | Agregación de fuentes, API local y avisos incrementales. |
| Voice RF Gateway | systemd, host | Preparación y síntesis WAV; sin PTT ni emisión RF en v7.0.36. |

Los nombres de servicio Compose son `broker`, `bot`, `aprs`, `email-to-mesh` y, opcionalmente, `bridgehub-bc`.

## Emergencias v7.0.43

La aplicación independiente `emergencias_guardia` mantiene las fuentes existentes (DGT DATEX II, Ayuntamiento de Zaragoza, IGN y NASA FIRMS) e incorpora dos nuevas fuentes oficiales:

- **AEMET CAP 1.2** para avisos meteorológicos adversos, normalizados al modelo `Event` y clasificados en inundación/lluvia, tormenta, nieve, viento fuerte o temperatura extrema.
- **CHE / SAIH Ebro** mediante el RSS oficial de comunicaciones, filtrando únicamente avisos hidrológicos sobre crecidas, cauces, barrancos e inundaciones.

RAN Protección Civil y 112 Aragón se mantienen documentadas como fuentes candidatas, pero no se integran por scraping: se incorporarán solo cuando exista un endpoint público estructurado y estable.

La configuración de fuentes continúa centralizada en `tools/emergencias_guardia/data/config.json` y el ControlPanel refleja las nuevas fuentes dentro de **Fuentes y cobertura**.
