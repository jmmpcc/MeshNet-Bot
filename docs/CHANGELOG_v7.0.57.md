# MeshNet-Bot v7.0.57 — Corrección de build GHCR para Channel Gateway

Fecha: 2026-08-15

## Motivo

Tras fusionar la v7.0.56 se comprobó que el workflow `Build & Push Docker images to GHCR` se ejecutaba, pero el job `build-and-push` quedaba omitido porque el detector de cambios no asociaba los nuevos ficheros de Channel Gateway con las imágenes `broker` y `bot`.

## Corrección

Se amplía `.github/workflows/build-ghcr.yml` para que:

- `source/channel_gateway.py` reconstruya la imagen `meshnet-bot-broker`.
- `docker/entrypoint_broker.sh` reconstruya la imagen `meshnet-bot-broker`.
- `source/channel_gateway_bot.py` reconstruya la imagen `meshnet-bot-bot`.
- `docker/entrypoint_bot.sh` reconstruya la imagen `meshnet-bot-bot`.

No se modifica código funcional del broker, bot, MeshCore, Meshtastic, APRS, BBS, Emergencias, Farmacias ni bridges.

## Resultado esperado

A partir de esta versión, cualquier cambio futuro del Channel Gateway provocará automáticamente el build/push GHCR del contenedor afectado.

Al fusionar este cambio, como el propio workflow ha sido modificado, la regla de seguridad ya existente reconstruirá las imágenes gestionadas por el workflow. Una vez publicados los tags `latest`/`edge`, la Raspberry debe actualizarse mediante `docker compose pull` y recreación de los servicios, no mediante build local.
