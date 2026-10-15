# Índice de documentación vigente — MeshNet-Bot

Este directorio separa documentación **operativa vigente** de documentos históricos. Para instalar, actualizar o administrar una instalación actual, siga primero los documentos de esta sección.

## Documentación operativa vigente

| Área | Documento |
|---|---|
| Instalación, actualización y operación | [OPERATIONS.md](OPERATIONS.md) |
| Aplicaciones opcionales Docker | [OPTIONAL_APPS_DOCKER.md](OPTIONAL_APPS_DOCKER.md) |
| Instalación Raspberry Pi | [Manual_Instalacion_MeshNet_RaspberryPi.md](Manual_Instalacion_MeshNet_RaspberryPi.md) |
| Perfiles de radio | [RADIO_PROFILES.md](RADIO_PROFILES.md) |
| Broker | [BROKER_README.md](BROKER_README.md) |
| Bot Telegram | [BOT_README.md](BOT_README.md) |
| APRS | [APRS_GATEWAY.md](APRS_GATEWAY.md) |
| Correo ↔ malla | [EMAIL_TO_MESH.md](EMAIL_TO_MESH.md) |
| Scope MeshCore | [MESHCORE_TX_SCOPE/README.md](MESHCORE_TX_SCOPE/README.md) |
| Servicios automáticos y scope | [MESHCORE_TX_SCOPE/AUTOMATIC_SERVICES.md](MESHCORE_TX_SCOPE/AUTOMATIC_SERVICES.md) |
| ControlPanel | [../tools/ControlPanel/README.md](../tools/ControlPanel/README.md) |
| Farmacias | [../tools/farmacias_guardia/README.md](../tools/farmacias_guardia/README.md) |
| Emergencias | [../tools/emergencias_guardia/README.md](../tools/emergencias_guardia/README.md) |

## Modelo operativo actual

Servicios Docker principales:

- `broker`
- `bot`
- `aprs`
- `email-to-mesh` (opcional)
- `bridgehub-bc` (opcional)

Perfiles Docker opcionales:

- `farmacias`: `farmacias`, `farmacias-check`, `farmacias-daily`
- `emergencias`: `emergencias`, `emergencias-check`

Servicios host actuales:

- `meshnet-control-panel.service`
- `meshnet-voice-rf.service` cuando se instala

Las unidades systemd de Farmacias/Emergencias se consideran **legacy** cuando esas aplicaciones funcionan por Docker.

## Regla de selección Docker

`COMPOSE_PROFILES` define qué perfiles opcionales están disponibles. No los arranca por sí mismo.

`MESHNET_DOCKER_SERVICES` define qué servicios debe gestionar explícitamente el actualizador. Si está vacío, `scripts/update_meshnet.sh` conserva el conjunto de servicios Compose que ya estaban activos.

El actualizador arranca primero `broker`, espera `BROKER_STARTUP_GRACE_SECONDS` (60 s por defecto), comprueba su estado y después inicia los demás servicios uno a uno.

## Regla de precedencia documental

Ante una contradicción:

1. código y archivos de despliegue de la rama instalada;
2. README específico del componente;
3. [OPERATIONS.md](OPERATIONS.md);
4. README principal;
5. documentos históricos.

## Documentos históricos

Los siguientes tipos de archivo se conservan como referencia de evolución y **no deben utilizarse como guía operativa actual** cuando contradigan la documentación vigente:

- `CHANGELOG_v*.md`
- `*_old.md`
- documentos con versión antigua en el título;
- manuales o copias específicas de instalaciones antiguas.

El historial no se reescribe retrospectivamente: documenta el comportamiento de la versión a la que pertenecía.
