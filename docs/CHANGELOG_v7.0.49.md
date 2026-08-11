# MeshNet-Bot v7.0.49

Fecha: 2026-08-11

## Fase Android A1 — MeshNet Mobile API v1

Se inicia la integración con el repositorio independiente `jmmpcc/MeshNet-Mobile` sin modificar el comportamiento operativo del ControlPanel ni del Web Admin.

### Añadido

- Nueva aplicación independiente `tools/MobileAPI`.
- API REST versionada bajo `/api/v1`.
- Autenticación Bearer específica para clientes móviles.
- Modo fail-closed: si no existe `MESHNET_MOBILE_API_TOKEN`, las rutas protegidas responden 503 en lugar de quedar abiertas.
- Endpoints de lectura:
  - `/api/v1/health`
  - `/api/v1/system/overview`
  - `/api/v1/services`
  - `/api/v1/messages`
  - `/api/v1/emergencies/overview`
  - `/api/v1/nodes/meshcore`
  - `/api/v1/nodes/meshtastic`
- Unidad systemd propuesta `meshnet-mobile-api.service`.
- Pruebas de contrato y autenticación.

### Compatibilidad

- No se modifica `tools/ControlPanel/web_admin.py`.
- No se modifica ninguna ruta `/api/...` existente.
- No se modifica ningún dispatcher, lógica de MeshCore, Meshtastic, APRS, APRS-IS, Emergencias, Farmacias, BBS ni deduplicación.
- A1 es de solo lectura.
- Los endpoints de nodos mantienen `available=false` hasta enlazar en una fase posterior la fuente real ya existente, evitando datos ficticios o duplicidad de lógica.

### Siguiente fase

Validar A1 en Raspberry/PC y crear la base de `MeshNet-Mobile` con Kotlin, Jetpack Compose y cliente REST contra `/api/v1/health`.
