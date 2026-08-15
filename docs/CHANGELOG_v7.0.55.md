# MeshNet-Bot v7.0.55 — Pasarela interna entre canales

Fecha: 2026-08-15

## Estado

**BORRADOR EN DESARROLLO — NO FUSIONAR TODAVÍA.**

La primera iteración del PR implementó la pasarela únicamente para Meshtastic. La funcionalidad se amplía antes de fusionar para cubrir MeshCore y todos los perfiles de radio canónicos del proyecto.

## Requisito funcional definitivo

`Channel Gateway` debe permitir pasarelas entre canales del mismo motor de radio activo, sin asumir que el nodo principal sea Meshtastic.

Perfiles que deben cubrirse:

- `meshcore_only`: nodo A MeshCore. La pasarela opera entre canales MeshCore.
- `meshtastic_a_meshcore_embedded_b`: nodo A Meshtastic y nodo B MeshCore embebido. La pasarela puede operar sobre cualquiera de los dos motores existentes.
- `meshcore_a_meshtastic_embedded_b`: nodo A MeshCore y nodo B Meshtastic embebido. La pasarela respeta esta distribución y utiliza el motor correspondiente.
- `legacy`: se conserva el comportamiento actual sin forzar ningún transporte nuevo.

El perfil se resuelve mediante `source/radio_profile.py`, que es la autoridad común del proyecto.

## Modelo de reglas

Las reglas continúan siendo direccionales. Una regla bidireccional son dos reglas independientes.

Ejemplos conceptuales:

```text
Meshtastic CH0 -> Meshtastic CH2
MeshCore    CH0 -> MeshCore    CH2
```

En perfiles combinados las reglas deben identificar también el motor al que pertenecen para evitar ambigüedad entre índices de canal de Meshtastic y MeshCore.

## Integración segura

- La funcionalidad pertenece al broker.
- No se abre una segunda conexión Meshtastic.
- No se abre una segunda conexión MeshCore.
- Meshtastic reutiliza `meshtastic.receive` y la `SENDQ` del broker.
- MeshCore reutiliza el motor embebido ya existente: recepción `CHANNEL_MSG_RECV` y transmisión `send_chan_msg(channel_idx, text)`.
- Una regla incompatible con el perfil activo se conserva pero queda inactiva; nunca debe provocar una conexión adicional.
- Se mantienen deduplicación, anti-eco y rate-limit diferenciados por transporte/canal.
- Las TX internas no deben disparar involuntariamente las pasarelas externas A/B/C.

## Control desde Telegram

Se mantiene el objetivo de control en caliente mediante:

```text
/channel_gateway
/channel_gateway list
/channel_gateway on
/channel_gateway off
/channel_gateway add ...
/channel_gateway del ...
/channel_gateway clear
```

La sintaxis final de `add/del` se ampliará para poder seleccionar explícitamente `meshtastic` o `meshcore` cuando el perfil combinado tenga ambos motores activos.

## Persistencia

El estado se conserva en:

```text
${BOT_DATA_DIR}/channel_gateway.json
```

La persistencia debe incluir transporte, canal origen, canal destino y estado de cada regla.

## Validación obligatoria antes de fusionar

La fase no se considerará terminada hasta validar como mínimo:

1. Meshtastic CHx -> CHy unidireccional.
2. Meshtastic CHx <-> CHy sin ping-pong.
3. MeshCore CHx -> CHy unidireccional.
4. MeshCore CHx <-> CHy sin ping-pong.
5. `meshcore_only` sin intentar inicializar Meshtastic.
6. `meshtastic_a_meshcore_embedded_b` con ambos motores y reglas separadas.
7. `meshcore_a_meshtastic_embedded_b` con ambos motores y reglas separadas.
8. Gateway OFF = comportamiento idéntico al anterior.
9. Persistencia y restauración tras reinicio.
10. Comandos administrativos del bot y consulta de estado.
