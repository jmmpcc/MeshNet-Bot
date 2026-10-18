# MeshCore 2.3.17 — validación, despliegue y reversión (DRAFT)

> **NO EJECUTAR EL DESPLIEGUE EN PRODUCCIÓN** mientras el PR #214 siga en Draft.
> Este documento es un procedimiento condicionado a la aprobación de las pruebas,
> no una instrucción de actualización inmediata.

## Alcance técnico real (revisado en main)

- `docker-compose.rpi.yml`: `broker` usa `ghcr.io/jmmpcc/meshnet-bot-broker:latest`.
- `bot` y `aprs` usan `network_mode: service:broker`; recrear el broker puede
  requerir recrear ordenadamente ambos servicios para restablecer su namespace.
- `email-to-mesh` **comparte la misma imagen del broker** y su namespace de red.
- `farmacias`, `farmacias-check`, `farmacias-daily`,
  `emergencias` y `emergencias-check` tienen imágenes independientes y
  perfiles Compose distintos, pero dependen funcionalmente de `broker`.
- `.github/workflows/build-ghcr.yml` activa reconstrucción de **todas** las
  imágenes cuando cambian archivos de `requirements/`. Se debe auditar la
  publicación de imágenes aunque el cambio en código sea pequeño.
- `scripts/update_meshnet.sh` es el procedimiento oficial de arranque secuencial.
  No usar `docker compose up -d` sin lista explícita de servicios y nunca
  `docker compose down -v`.

## Resultado de los ensayos offline (18 octubre 2026)

- API y Python 3.11/3.13 compatibles con `meshcore==2.3.17`.
- Comparación A/B de TRACE: ruta dirigida mantiene el paquete de 12 bytes;
  ruta vacía produce 11 bytes en 2.3.17 frente a 10 en 2.2.31.
- `trace_contact()`: cuatro escenarios simulados pasan en ambos SDK.
- `MeshcorePacketParser.parsePacketPayload()`: anuncio incompleto (100 bytes)
  no propaga `IndexError`; anuncio válido (101 bytes) conserva sus campos.
- Test del PR: **5 passed, 10 subtests passed**.
- Selección de regresión del broker: **49 passed, 10 subtests passed**.
- La suite general NO está certificada como completamente verde: existen
  fallos anteriores en otras áreas y restricciones del Python de test.
- **No** se ha validado todavía la respuesta real `PATH_RESPONSE`/
  `TRACE_DATA`, ni SNR por repetidor en la radio.

## Preflight de solo lectura en Raspberry

Desde `/home/meshnet/MeshNet-Bot`:

```bash
set -euo pipefail
pwd
git status --short
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
for name in meshnet-broker meshnet-bot meshnet-aprs meshnet-email-to-mesh \
    meshnet-farmacias meshnet-emergencias; do
  docker inspect "$name" --format \
    '{{.Name}} image_id={{.Image}} status={{.State.Status}} restarts={{.RestartCount}}' \
    2>/dev/null || true
done
docker exec meshnet-broker python -m pip show meshcore | grep '^Version:'
docker compose -f docker-compose.rpi.yml config --services
systemctl is-enabled meshnet-docker-start.service || true
```

Confirmar los servicios realmente activos y guardar un inventario con fecha.
**No registrar ni publicar el contenido de `.env`**, que contiene secretos.

## Validación de imagen derivada de producción (18 octubre 2026, 22:35 CEST)

En la Raspberry `meshnetwifi`, sin detener los ocho contenedores activos:

- Imagen base productiva: `sha256:1ee5980797afe2325a156fda62e8250d1bdb26b25db1733fbd3b080f37c055cb`; `meshcore==2.2.31`.
- Copia local de rollback: `meshnet-backup:broker-pre-2317-20261018`. También etiquetadas las imágenes reales de Bot, APRS, Farmacias y Emergencias.
- Imagen derivada: `meshnet-broker:meshcore-2317-prod-candidate`, creada con `FROM meshnet-backup:broker-pre-2317-20261018`, instalación exclusiva de `meshcore==2.3.17`, `pip check` y smoke imports.
- Hash SHA256 de `/app/source/Meshtastic_Broker.py` coincidente en productivo y candidata: `ce725cd007e3f29c37ab86bd2007764508a19b3a02d998c450aa6eb3dc90bd4a`.
- `pip freeze` (productivo versus candidata): **única diferencia `meshcore==2.2.31` → `meshcore==2.3.17`**; sin modificaciones transitivas.

- Comparación adicional de integridad: SHA256 de todos los archivos regulares de `/app/source`, `/app/tools` y entrypoints de `/usr/local/bin` idénticos entre imagen productiva y candidata. Broker permaneció `running`, reinicios 0.
- Suite de contrato offline sobre la candidata derivada de producción: **5/5 tests OK** mediante `unittest`.
- Regresión ampliada sobre la imagen previa de pruebas: **49 passed, 10 subtests passed**.
- Broker productivo: `running`, reinicios 0, versión 2.2.31. Los ocho servicios activos mantienen su ejecución. `email-to-mesh` no figuraba entre los contenedores activos.
- Almacenamiento antes de crear la candidata: 46 GB libres en `/`.

**Alcance de la evidencia:** las cinco pruebas se han repetido en la candidata productiva. Las 49 pruebas ampliadas se ejecutaron en la imagen `meshcore-2317-pytest-full`, no todavía en la candidata nueva. Coincidencia del archivo principal y de `pip freeze` no implica por sí sola identidad de todos los archivos de la imagen. Sigue pendiente integración con la sesión MeshCore real y confirmación funcional de los servicios dependientes.

## Inventario del namespace de red y arranque (18 octubre 2026)

Comprobado en Raspberry, sin alterar contenedores:

- `meshnet-broker`: red `meshnet-bot_default`, reinicio `unless-stopped`.
- `meshnet-bot` y `meshnet-aprs`: `HostConfig.NetworkMode=container:<ID broker actual>`; reinicios `on-failure` y `unless-stopped`, respectivamente. Deben recrearse al sustituir el broker, pues el ID cambia.
- Farmacias (API/check/daily) y Emergencias (API/check): misma red Compose normal, `unless-stopped`. No recrearlas durante el experimento salvo incidencia comprobada.
- `meshnet-docker-start.service`: `enabled`, `active`; se ejecuta como unit de arranque secuencial. Evitar arranques concurrentes durante ventana de mantenimiento.
- Imagen candidata: `sha256:2df65e66ac52ee1b14d3fe0e0108b97ed3c7143779c07a2411d2d858b235004c`.
- Override temporal `/tmp/meshcore-2317-candidate.override.yml`: `docker compose ... config --quiet` correcto. Únicamente broker apunta a la candidata; el resto conserva sus imágenes.
- `email-to-mesh` no está activo; **no** incluirlo en la selección de servicios de la prueba.

**Secuencia prevista, no ejecutada**: verificar la configuración efectiva y los backups; detener Bot/APRS de forma ordenada; sustituir broker con override ya validado; recrear Bot/APRS contra el broker nuevo; comprobar capacidades y servicios dependientes; en regresión, restaurar imagen del broker original y recrear nuevamente Bot/APRS para reenganchar su namespace. Evitar `docker compose up` general, `down -v` y cualquier segunda sesión MeshCore. Preparar comando de reversión y criterios de parada antes de operar.

## Ensayo Compose dry-run (18 octubre 2026)

En Raspberry, `docker compose version` indica **v5.1.4**. Con el override temporal de la candidata:

```bash
docker compose --dry-run \
  -f docker-compose.rpi.yml \
  -f /tmp/meshcore-2317-candidate.override.yml \
  up -d --no-deps --pull never broker bot aprs
```

Resultado simulado: `meshnet-broker`, `meshnet-bot` y `meshnet-aprs` recreados e iniciados; ninguno de los cinco contenedores de Farmacias/Emergencias ni `email-to-mesh` aparece seleccionado. **Dry-run no implica que el despliegue ni sus comprobaciones funcionales se hayan ejecutado.** Preparar y verificar el override de recuperación y la ventana de mantenimiento antes de cualquier `up` real.

## Verificación del override de recuperación (18 octubre 2026)

Se creó `/tmp/meshcore-2317-rollback.override.yml` con el servicio `broker`
apuntando a `meshnet-backup:broker-pre-2317-20261018`.

- `docker compose ... config --quiet`: correcto.
- `docker image inspect`: referencia exactamente la imagen productiva
  `sha256:1ee5980797afe2325a156fda62e8250d1bdb26b25db1733fbd3b080f37c055cb`.
- `docker compose --dry-run ... up -d --no-deps --pull never broker bot aprs`:
  emite `Recreated` y `Started` para los tres servicios.
- Verificación posterior: broker `running`, cero reinicios, `meshcore==2.2.31`.

**Interpretación:** el dry-run no prueba una recuperación efectiva desde 2.3.17,
ni demuestra recreación real de los contenedores. La validación de rollback
operativo queda pendiente de una ventana de mantenimiento y verificaciones
funcionales de broker, Telegram, APRS, BBS y servicios auxiliares.

## Gates previos a la aprobación

1. Ejecutar CI y builds correspondientes sobre commit exacto del PR;
   el workflow GHCR habitual publica en `main`, no en Draft PR.
2. Identificar y conservar por *image ID* todas las imágenes actualmente
   utilizadas, no confiar exclusivamente en una etiqueta `:latest` mutable.
3. Disponer de backups verificados de `.env` y `bot_data`, con permisos y
   restauración comprobados; nunca reemplazar datos persistentes para probar SDK.
4. Comprobar `docker compose config` con los perfiles efectivos y revisar
   `MESHNET_DOCKER_SERVICES`.
5. Probar integración offline de broker, bot, BBS, email-to-mesh, Farmacias,
   Emergencias y APRS sin conexiones adicionales al dispositivo.
6. Programar ventana de mantenimiento y registrar plan de reversión;
   informar del breve corte posible del broker y dependientes.

## Despliegue controlado (solo tras aprobación)

- Seleccionar la nueva imagen **por digest o etiqueta inmutable probada**.
  Evitar actualizar a ciegas un `:latest` nuevo.
- Fijar previamente una etiqueta local de respaldo al *image ID* productivo
  y comprobar que persiste tras descargar nuevas imágenes.
- Preparar un fichero Compose de override temporal, revisado con
  `docker compose -f docker-compose.rpi.yml -f OVERRIDE config`,
  sin sobrescribir el compose ni el `.env` operativos.
- Orquestar mediante el arranque secuencial del proyecto y las dependencias
  comprobadas. No ejecutar `docker run` de un segundo broker que pueda
  abrir otra sesión MeshCore con el mismo dispositivo.
- Verificar el estado del broker y de los contenedores dependientes, consulta
  de farmacias/emergencias, Telegram, BBS, APRS y correo.
- Evaluar `PATH_RESPONSE` y `TRACE_DATA` en una única conexión; registrar
  `SNR` únicamente cuando un salto quede identificado inequívocamente.

## Reversión (solo tras preparar y verificar el plan exacto)

- Mantener la imagen antigua accesible por etiqueta local inmutable.
- Ante regresión, detener/recrear **ordenadamente** el broker y servicios que
  comparten su namespace de red; no ejecutar `docker compose down -v`.
- Seleccionar la imagen anterior mediante el override probado y verificar que
  `pip show meshcore` vuelve a `2.2.31`.
- Verificar canales, DM, BBS, APRS, email-to-mesh, Farmacias y Emergencias.
- **No** hacer `git reset --hard`, `git clean`, `git pull` forzado,
  ni modificar ficheros locales de trabajo.
- Preservar volúmenes, logs y evidencia para diagnóstico.

## Problemas ajenos al SDK

Los errores conocidos de la suite general en Farmacias, Emergencias y
AutoReply se investigan en sus contextos de ejecución correctos; no forman
parte de este cambio. El fallo de `reader.py` en 2.2.31 se ha asociado
con anuncios incompletos y cuenta con regresión sintética en 2.3.17.

**Estado: DRAFT. No fusionar ni desplegar hasta cumplir los gates.**
