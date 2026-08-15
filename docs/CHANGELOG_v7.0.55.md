# MeshNet-Bot v7.0.55 — Pasarela interna entre canales

Fecha: 2026-08-15

## Nueva funcionalidad

Se incorpora `Channel Gateway`, una pasarela configurable entre canales del mismo nodo Meshtastic gestionado por el broker.

Ejemplos:

```text
CH0 -> CH2
CH0 <-> CH2
CH0 -> CH1, CH2, CH3
```

La bidireccionalidad se representa internamente mediante dos reglas independientes, lo que permite activar o eliminar cada dirección sin ambigüedad.

## Integración segura en el broker

La funcionalidad se ejecuta dentro del mismo proceso del broker mediante `Meshtastic_Broker_ChannelGateway.py` y `channel_gateway.py`.

No se modifica `Meshtastic_Broker.py` ni se crea una segunda conexión al nodo. El gateway escucha los eventos `meshtastic.receive` y reutiliza preferentemente la cola `SENDQ` ya operativa del broker. Como fallback, reutiliza exclusivamente la misma interfaz recibida en el evento.

Las transmisiones generadas por el gateway usan `origin=channel_gateway` y `no_bridge=True` por defecto para evitar que una pasarela interna active involuntariamente las pasarelas externas A/B/C existentes.

## Control desde Telegram

Se añaden los comandos:

```text
/channel_gateway
/channel_gateway list
/channel_gateway on
/channel_gateway off
/channel_gateway add <origen> <destino>
/channel_gateway add <origen> <destino> both
/channel_gateway del <origen> <destino>
/channel_gateway del <origen> <destino> both
/channel_gateway clear
```

También se admite el alias:

```text
/pasarela_canales
```

La consulta de estado es de solo lectura. La activación, desactivación y modificación de reglas requieren un usuario incluido en `ADMIN_IDS`.

`Telegram_Bot_Broker.py` permanece sin modificaciones: `Telegram_Bot_ChannelGateway.py` añade los handlers y delega en el bot existente.

## Persistencia

El estado y las reglas se guardan de forma atómica en:

```text
${BOT_DATA_DIR}/channel_gateway.json
```

Los cambios realizados desde el bot sobreviven a reinicios.

Si todavía no existe estado persistente, se puede definir una configuración inicial mediante:

```env
CHANNEL_GATEWAY_ENABLED=0
CHANNEL_GATEWAY_MAP=
```

Ejemplo:

```env
CHANNEL_GATEWAY_ENABLED=1
CHANNEL_GATEWAY_MAP=0:2,2:0
```

## Protecciones

La implementación incorpora:

- procesamiento exclusivo de `TEXT_MESSAGE_APP`;
- mensajes directos excluidos por defecto para no trasladar conversaciones privadas entre canales;
- deduplicación de recepción;
- anti-eco específico para transmisiones recientes del gateway;
- detección del identificador del nodo local cuando está disponible;
- rate-limit independiente por regla;
- aislamiento respecto al bridge externo;
- persistencia atómica;
- estadísticas de tráfico, duplicados, ecos bloqueados, rate-limit y errores;
- servidor de control JSONL interno en `BROKER_CTRL_PORT + 1` (`8767` por defecto), con token compartido opcional.

## Compatibilidad

Con `Channel Gateway` desactivado no se genera ninguna retransmisión adicional y las funciones existentes del broker y del bot conservan su comportamiento.

Los entrypoints únicamente cargan las extensiones y después ejecutan el broker y el bot originales.

## Validación

Se incorporan pruebas unitarias para:

- parser de reglas;
- reenvío a través de la `SENDQ` del broker;
- prevención de ping-pong en reglas bidireccionales;
- exclusión de mensajes directos;
- persistencia de configuración;
- comandos de control runtime.

La validación local de la implementación ha completado correctamente:

```text
python -m pytest -q tests/test_channel_gateway.py
5 passed
```

También se verificó la compilación Python de los cuatro módulos nuevos y la sintaxis Bash de ambos entrypoints modificados.
