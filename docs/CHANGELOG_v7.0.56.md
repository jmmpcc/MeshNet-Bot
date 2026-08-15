# MeshNet-Bot v7.0.56 — Channel Gateway multi-radio operativo

Fecha: 2026-08-15

## Motivo

La v7.0.55 introdujo la interfaz y el control del Channel Gateway, pero el backend fusionado seguía ejecutando únicamente la ruta Meshtastic. La v7.0.56 completa el requisito original sin revertir ninguna función ya operativa.

## Cambios

- Las reglas pasan de `(origen, destino)` a `(transporte, origen, destino)`.
- `RADIO_PROFILE` determina qué motores pueden ejecutar reglas.
- Meshtastic continúa reutilizando `meshtastic.receive` y `SENDQ`/interfaz existente.
- MeshCore reutiliza exclusivamente `MESHCORE_ENGINE` ya levantado por el broker.
- El receptor MeshCore se enlaza a la sesión `_meshcore` existente y escucha `CHANNEL_MSG_RECV`.
- La transmisión MeshCore usa `MESHCORE_ENGINE.enqueue_send_channel()`; no se crea una segunda conexión.
- Deduplicación, anti-eco y rate-limit quedan separados por transporte.
- El estado informa de reglas activas/inactivas para el perfil actual.

## Perfiles

### meshcore_only

Solo admite reglas `meshcore` y no instala la suscripción Meshtastic del Channel Gateway.

### meshtastic_a_meshcore_embedded_b

Admite reglas independientes para `meshtastic` y `meshcore`.

### meshcore_a_meshtastic_embedded_b

Admite reglas independientes para `meshcore` y `meshtastic`, respetando la distribución A/B definida por `radio_profile.py`.

### legacy

No se adivina una topología. Las reglas ambiguas no se activan.

## Compatibilidad con v7.0.55

El fichero `${BOT_DATA_DIR}/channel_gateway.json` se migra de forma conservadora:

- si el perfil activo tiene un único transporte, las reglas antiguas sin `transport` se asignan automáticamente a ese transporte;
- si el perfil es combinado, la regla antigua se conserva con transporte vacío e inactiva para evitar aplicarla a la radio equivocada;
- las reglas nuevas se persisten con `version: 2` y campo `transport`.

## Sintaxis

En perfiles de un único transporte:

```text
/channel_gateway add 0 2
/channel_gateway add meshcore 0 2
```

En perfiles combinados:

```text
/channel_gateway add meshtastic 0 2
/channel_gateway add meshcore 0 2
/channel_gateway add meshcore 0 2 both
```

El bot ya valida la sintaxis contra `RADIO_PROFILE` antes de llamar al broker.

## Protecciones

- Gateway global ON/OFF persistente.
- No se crean conexiones Meshtastic o MeshCore adicionales.
- Anti-loop independiente por transporte/canal/texto.
- Deduplicación RX independiente por transporte.
- Rate-limit por regla `(transport, source, destination)`.
- DM Meshtastic excluidos por defecto.
- `no_bridge=True` en TX Meshtastic internas salvo habilitación explícita.
- Reglas incompatibles con el perfil se conservan pero permanecen inactivas.

## Pruebas añadidas/actualizadas

`tests/test_channel_gateway.py` cubre:

1. parser de reglas con transporte;
2. Meshtastic CHx→CHy;
3. anti-eco Meshtastic bidireccional;
4. MeshCore CHx→CHy reutilizando `MESHCORE_ENGINE`;
5. anti-eco MeshCore bidireccional;
6. rechazo de Meshtastic en `meshcore_only`;
7. coexistencia de reglas MeshCore/Meshtastic en perfil combinado;
8. migración segura del estado v7.0.55;
9. exclusión de DM Meshtastic;
10. RPC con transporte y persistencia.

Se conservan además las pruebas de ayuda contextual por perfil introducidas en v7.0.55.
