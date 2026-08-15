# Pasarela interna entre canales

## Objetivo

`Channel Gateway` permite que el nodo Meshtastic gestionado por el broker reenvíe mensajes recibidos en un canal hacia otro canal del mismo nodo. La función se ejecuta dentro del proceso del broker, no abre una segunda conexión Meshtastic y permanece desactivada hasta que se configure y active.

## Arquitectura

El broker se inicia mediante `source/Meshtastic_Broker_ChannelGateway.py`. Este launcher instala `source/channel_gateway.py` y después ejecuta el `Meshtastic_Broker.py` existente sin modificarlo. El gateway se suscribe a `meshtastic.receive` y reutiliza preferentemente la `SENDQ` que ya posee el broker. Las transmisiones generadas por el gateway llevan `origin=channel_gateway`, metadatos de canal origen/destino y `no_bridge=True` por defecto para no alimentar de forma involuntaria la pasarela externa entre nodos.

El bot se inicia mediante `source/Telegram_Bot_ChannelGateway.py`, que conserva `Telegram_Bot_Broker.py` intacto y añade los comandos `/channel_gateway` y `/pasarela_canales`.

## Direcciones

Una regla siempre representa una dirección concreta:

```text
0 -> 2
```

Para funcionamiento bidireccional se almacenan dos reglas:

```text
0 -> 2
2 -> 0
```

El gateway admite múltiples reglas y múltiples destinos desde un mismo canal.

## Comandos del bot

Consulta del estado:

```text
/channel_gateway
/channel_gateway list
```

Activar o desactivar:

```text
/channel_gateway on
/channel_gateway off
```

Añadir una regla unidireccional:

```text
/channel_gateway add 0 2
```

Añadir ambas direcciones:

```text
/channel_gateway add 0 2 both
```

Eliminar una dirección:

```text
/channel_gateway del 0 2
```

Eliminar ambas direcciones:

```text
/channel_gateway del 0 2 both
```

Eliminar todas las reglas:

```text
/channel_gateway clear
```

La consulta de estado es de solo lectura. Las operaciones que modifican estado o reglas requieren que el usuario figure en `ADMIN_IDS`.

## Persistencia

El estado se guarda de forma atómica en:

```text
${BOT_DATA_DIR}/channel_gateway.json
```

Por ello, activar/desactivar o modificar reglas desde Telegram sobrevive a un reinicio del broker.

Las variables de entorno siguientes solo actúan como configuración inicial cuando todavía no existe el JSON persistente:

```env
CHANNEL_GATEWAY_ENABLED=0
CHANNEL_GATEWAY_MAP=
```

Ejemplo de arranque con una regla bidireccional ya preparada:

```env
CHANNEL_GATEWAY_ENABLED=1
CHANNEL_GATEWAY_MAP=0:2,2:0
```

## Variables opcionales

```env
# Socket de control interno broker -> bot.
CHANNEL_GATEWAY_CTRL_BIND=0.0.0.0
CHANNEL_GATEWAY_CTRL_PORT=8767
CHANNEL_GATEWAY_CTRL_HOST=broker

# Token compartido opcional para el socket de control.
CHANNEL_GATEWAY_CTRL_TOKEN=

# TTL de deduplicación y anti-eco en segundos.
CHANNEL_GATEWAY_DEDUP_TTL=12
CHANNEL_GATEWAY_TX_ECHO_TTL=12

# Máximo de reenvíos por minuto y por regla. 0 = sin límite.
CHANNEL_GATEWAY_RATE_LIMIT=30

# Por seguridad los mensajes directos no atraviesan el gateway.
CHANNEL_GATEWAY_FORWARD_DIRECT=0

# Por defecto una TX del gateway no se refleja además por el bridge A/B/C.
CHANNEL_GATEWAY_ALLOW_EXTERNAL_BRIDGE=0

# Ruta alternativa del estado persistente.
CHANNEL_GATEWAY_STATE_FILE=
```

`CHANNEL_GATEWAY_CTRL_PORT` usa por defecto `BROKER_CTRL_PORT + 1`; con la configuración habitual del proyecto será `8767`. En Docker no es necesario publicar este puerto al host si bot y broker comparten la misma red interna.

## Protecciones

El gateway incluye:

- filtro exclusivo `TEXT_MESSAGE_APP`;
- exclusión de mensajes directos por defecto para evitar fuga de conversaciones privadas entre canales;
- deduplicación de RX;
- huella de transmisiones recientes para impedir ping-pong con reglas bidireccionales;
- detección del ID del nodo local cuando está disponible;
- rate-limit independiente por regla;
- persistencia atómica;
- aislamiento respecto al bridge externo mediante `no_bridge=True`;
- estadísticas de reenviados, ecos bloqueados, duplicados, rate-limit y errores.

## Prueba funcional recomendada

1. Comprobar estado inicial:

```text
/channel_gateway
```

2. Añadir una única dirección:

```text
/channel_gateway add 0 2
/channel_gateway on
```

3. Desde otro nodo, enviar `PRUEBA-GW-01` por CH0. Debe recibirse una sola copia adicional por CH2 y ninguna por CH0 generada por el gateway.

4. Activar bidireccionalidad:

```text
/channel_gateway add 2 0
```

5. Enviar `PRUEBA-GW-02` por CH2. Debe aparecer una sola copia adicional en CH0. No debe producirse secuencia CH0 -> CH2 -> CH0.

6. Consultar estadísticas:

```text
/channel_gateway
```

7. Desactivar:

```text
/channel_gateway off
```

Con el gateway desactivado, el comportamiento del broker y del bot es el anterior y las reglas permanecen guardadas para una futura activación.
