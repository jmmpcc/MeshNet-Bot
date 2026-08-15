# Pasarela interna entre canales

## Objetivo

`Channel Gateway` permite reenviar mensajes entre canales del mismo nodo de radio gestionado por el broker. Desde v7.0.56 funciona tanto con Meshtastic como con MeshCore y siempre respeta `RADIO_PROFILE`.

La función permanece desactivable en caliente desde el bot, no crea conexiones de radio adicionales y reutiliza exclusivamente los motores ya operativos del broker.

## Arquitectura

### Meshtastic

- RX: `meshtastic.receive`.
- TX: `SENDQ` del broker; como fallback, la misma interfaz recibida por PubSub.
- No se crea otra `TCPInterface`.

### MeshCore

- RX: suscripción adicional a `CHANNEL_MSG_RECV` sobre la sesión `_meshcore` ya abierta por `MESHCORE_ENGINE`.
- TX: `MESHCORE_ENGINE.enqueue_send_channel(channel_idx, text)`.
- No se ejecuta `MeshCore.create_tcp`, `create_serial` ni `create_ble` desde Channel Gateway.

## Perfiles soportados

`source/radio_profile.py` es la autoridad.

- `meshcore_only`: solo reglas MeshCore.
- `meshtastic_a_meshcore_embedded_b`: reglas Meshtastic y MeshCore independientes.
- `meshcore_a_meshtastic_embedded_b`: reglas MeshCore y Meshtastic independientes.
- `legacy`: no se adivina ninguna topología; las reglas ambiguas quedan inactivas.

## Modelo de reglas

Una regla queda identificada por:

```text
transporte + canal_origen + canal_destino
```

Ejemplos:

```text
meshcore    CH0 -> CH2
meshtastic CH0 -> CH1
```

La bidireccionalidad se almacena como dos reglas independientes.

## Comandos

Ayuda contextual según `RADIO_PROFILE`:

```text
/channel_gateway
/channel_gateway help
```

Estado:

```text
/channel_gateway status
/channel_gateway list
```

Activación:

```text
/channel_gateway on
/channel_gateway off
```

Añadir/eliminar:

```text
/channel_gateway add [transporte] <origen> <destino> [both]
/channel_gateway del [transporte] <origen> <destino> [both]
```

En `meshcore_only` el transporte puede omitirse:

```text
/channel_gateway add 0 2
/channel_gateway add meshcore 0 2
```

En perfiles combinados es obligatorio especificarlo:

```text
/channel_gateway add meshtastic 0 2
/channel_gateway add meshcore 0 2
```

El alias `/pasarela_canales` sigue disponible.

Las operaciones de escritura requieren un usuario incluido en `ADMIN_IDS`.

## Persistencia

Estado:

```text
${BOT_DATA_DIR}/channel_gateway.json
```

Formato v7.0.56:

```json
{
  "version": 2,
  "enabled": true,
  "rules": [
    {
      "transport": "meshcore",
      "source": 0,
      "destination": 2,
      "enabled": true
    }
  ]
}
```

### Migración v7.0.55

Las reglas antiguas no incluían `transport`.

- Con un único transporte válido se migran automáticamente.
- En perfiles combinados se conservan como ambiguas e inactivas para no enviarlas por la radio equivocada.

## Variables

```env
CHANNEL_GATEWAY_ENABLED=0

# Mapas explícitos recomendados desde v7.0.56
CHANNEL_GATEWAY_MESHTASTIC_MAP=
CHANNEL_GATEWAY_MESHCORE_MAP=

# Compatibilidad: solo se aplica automáticamente si el perfil tiene un único transporte
CHANNEL_GATEWAY_MAP=

CHANNEL_GATEWAY_CTRL_BIND=0.0.0.0
CHANNEL_GATEWAY_CTRL_PORT=8767
CHANNEL_GATEWAY_CTRL_HOST=broker
CHANNEL_GATEWAY_CTRL_TOKEN=

CHANNEL_GATEWAY_DEDUP_TTL=12
CHANNEL_GATEWAY_TX_ECHO_TTL=12
CHANNEL_GATEWAY_RATE_LIMIT=30
CHANNEL_GATEWAY_FORWARD_DIRECT=0
CHANNEL_GATEWAY_ALLOW_EXTERNAL_BRIDGE=0
CHANNEL_GATEWAY_STATE_FILE=
```

## Protecciones

- Gateway global ON/OFF.
- Validación del transporte contra `RADIO_PROFILE`.
- Reglas incompatibles conservadas pero inactivas.
- Deduplicación por transporte/canal/emisor/texto.
- Anti-eco por transporte/canal/texto.
- Rate-limit por `(transport, source, destination)`.
- DM Meshtastic excluidos por defecto.
- TX internas Meshtastic marcadas `no_bridge=True` salvo configuración contraria.
- Ninguna conexión de radio adicional.

## Prueba funcional

MeshCore:

```text
/channel_gateway add meshcore 0 2 both
/channel_gateway on
```

Enviar desde otro nodo un texto por MeshCore CH0. Debe aparecer una única copia adicional en CH2 y no retornar a CH0.

Meshtastic:

```text
/channel_gateway add meshtastic 0 2 both
/channel_gateway on
```

Enviar desde otro nodo por Meshtastic CH0. Debe aparecer una única copia adicional en CH2 y no producir ping-pong.
