# MeshNet-Bot - Scope MeshCore por transmisión

## 1. Objetivo

Esta funcionalidad permite elegir el **flood scope de MeshCore en cada transmisión**, de forma independiente del canal (`channel_idx`) por el que se envía el mensaje.

El canal y el scope pasan a tener responsabilidades separadas:

- **Canal**: determina por qué canal MeshCore se transmite el texto.
- **Scope**: determina el ámbito regional de propagación de ese TX.

No se modifica la configuración permanente del canal ni se mantiene un mapa fijo `canal -> scope`.

## 2. Motivo del cambio

Las pruebas RF realizadas mostraron este comportamiento:

1. Un mensaje enviado desde 3UTB mediante MeshNet-Bot salía sin scope y era repetido por repetidores que no pertenecían a la región deseada.
2. Un mensaje enviado con scope desde otro nodo era repetido únicamente por el repetidor regional.
3. El mismo 3UTB, conectado directamente a la aplicación MeshCore y sin MeshNet-Bot, enviaba con scope correctamente.

Esto aisló el problema en la ruta TX de MeshNet-Bot: `send_chan_msg()` no incorpora el scope como argumento. La API actual de `meshcore_py` utiliza primero `set_flood_scope(scope)` y después `send_chan_msg(...)`.

## 3. Sintaxis del bot

La sintaxis histórica de `/enviar_mc` continúa funcionando sin cambios.

### Envío normal, sin scope explícito

```text
/enviar_mc ch5 Hola
```

```text
/enviar_mc canal 5 Hola
```

```text
/enviar_mc ambos ch5 aprs broadcast Aviso
```

### Envío con scope regional explícito

```text
/enviar_mc ch5 --scope #zaragoza Hola
```

También se admite el nombre sin `#`:

```text
/enviar_mc ch5 --scope zaragoza Hola
```

MeshNet-Bot lo normaliza internamente a:

```text
#zaragoza
```

El modificador puede escribirse como:

```text
--scope #zaragoza
```

O:

```text
--scope=#zaragoza
```

### Scope con transporte MeshCore + APRS

```text
/enviar_mc ambos ch5 --scope #zaragoza aprs broadcast Aviso doble
```

El scope se aplica únicamente al TX MeshCore. La parte APRS mantiene exactamente su funcionamiento actual.

## 4. Valores especiales de scope

### Usar el scope por defecto del nodo

```text
/enviar_mc ch5 --scope 0 Hola
```

También:

```text
/enviar_mc ch5 --scope default Hola
```

`0` indica a MeshCore que utilice el default scope configurado en el nodo Companion.

### Forzar un mensaje sin scope

```text
/enviar_mc ch5 --scope * Hola
```

Esto genera tráfico unscoped/global. Debe utilizarse de forma consciente porque repetidores que acepten tráfico global pueden retransmitirlo.

## 5. Confirmación visible en Telegram

Cuando el TX utiliza un scope explícito, la confirmación muestra el valor aplicado:

```text
Envío MeshCore
Transporte: MESH
Malla MeshCore -> Canal (channel_idx): 5
Scope TX: #zaragoza
Resultado MeshCore: OK
```

Si no se indica `--scope`, la confirmación mantiene el formato histórico y no añade una línea artificial.

## 6. Funcionamiento interno

La secuencia lógica es:

```text
Telegram
   |
   | /enviar_mc ch5 --scope #zaragoza Hola
   v
MeshNet-Bot
   |
   | MESHCORE_SEND
   | channel_idx = 5
   | text = Hola
   | scope = #zaragoza
   v
Broker MeshNet
   |
   | guarda scope dentro del item de cola del TX
   v
Cola MeshCore
   |
   | conserva scope + texto + partes + retries
   v
meshcore_py
   |
   | set_flood_scope("#zaragoza")
   | send_chan_msg(5, "Hola")
   v
Radio MeshCore
```

El scope forma parte del **item lógico de transmisión**, no del canal.

## 7. Mensajes largos y fragmentación

Los mensajes largos siguen utilizando la fragmentación ya existente de MeshNet-Bot.

El scope se conserva junto al item de cola y se selecciona antes de procesar sus partes. Cada llamada efectiva a `send_chan_msg()` aplica el scope correspondiente antes de transmitir.

Por tanto:

```text
TX scope #zaragoza
  parte 1/3 -> #zaragoza
  parte 2/3 -> #zaragoza
  parte 3/3 -> #zaragoza
```

No se ha sustituido la lógica actual de fragmentación.

## 8. Reintentos y reconexiones

El scope se guarda dentro del destino del item de cola:

```text
{
  kind: chan,
  channel_idx: 5,
  scope: #zaragoza
}
```

Si la conexión MeshCore cae y el TX pasa al spool de reintentos, el scope permanece asociado al mensaje.

Cuando el item se recupera después de una reconexión, vuelve a utilizar el mismo scope.

## 9. Protección frente a fugas unscoped

Si un TX solicita un scope y `set_flood_scope()` devuelve un error, **esa parte no se transmite**.

La política es fail-closed:

```text
set_flood_scope(#zaragoza) -> ERROR
send_chan_msg(...)         -> NO se ejecuta
```

Esto evita que un mensaje que debía permanecer dentro de una región pueda salir accidentalmente como tráfico global.

## 10. Restauración después de un TX scoped

Un scope explícito es un override temporal del Companion.

Después de un TX con scope, el siguiente TX de canal que no tenga `--scope` restaura primero:

```text
set_flood_scope("0")
```

Así vuelve al default scope del nodo y un envío regional no contamina mensajes posteriores.

Antes de utilizar por primera vez un scope explícito, los TX históricos sin `--scope` no se modifican.

## 11. RX: limitación actual

En TX conocemos exactamente el scope solicitado y aplicado.

En RX, la versión actual de `meshcore_py` no expone de forma fiable el scope real dentro del evento `CHANNEL_MSG_RECV`.

Por ese motivo MeshNet-Bot **no muestra un scope inferido o inventado en RX**.

Se mantienen sin cambios los datos RX ya disponibles:

- canal MeshCore;
- emisor/alias;
- RSSI;
- SNR;
- repetidores;
- ruta/traza;
- enlace al mapa cuando está configurado.

Cuando `meshcore_py` exponga el scope RX de forma oficial, podrá añadirse como dato real del paquete.

## 12. Compatibilidad

La funcionalidad está diseñada para no alterar los flujos existentes.

No se modifica el comportamiento de:

- `/enviar_mc` cuando no incluye `--scope`;
- `/enviar_mc_dm` y `/dm_mc`;
- APRS;
- cola TX existente;
- fragmentación;
- reintentos;
- spool durante reconexiones;
- recepción MeshCore;
- trazas y resolución de repetidores;
- autorespuestas;
- BBS;
- Farmacias;
- Emergencias;
- ZaragozaNoticias;
- Channel Gateway.

Los clientes antiguos que envíen `MESHCORE_SEND` sin el campo `scope` continúan siendo válidos.

## 13. Ayuda del bot

El menú `/` actualiza la descripción de `/enviar_mc` para indicar que existe scope opcional por TX.

`/ayuda` añade un bloque específico con:

```text
/enviar_mc ch5 --scope #zaragoza Hola
/enviar_mc ambos ch5 --scope #zaragoza aprs broadcast Aviso
/enviar_mc ch5 --scope 0 Hola
/enviar_mc ch5 --scope * Hola
```

Además, ejecutar `/enviar_mc` sin parámetros conserva su ayuda histórica y añade el bloque de `--scope`.

No se elimina ni sustituye la ayuda histórica.

## 14. Prueba RF recomendada

### Prueba A - scope regional

```text
/enviar_mc ch5 --scope #zaragoza PRUEBA SCOPE REGIONAL
```

Comprobar:

1. Telegram muestra `Scope TX: #zaragoza`.
2. El repetidor perteneciente a esa región retransmite el mensaje.
3. Un repetidor que no pertenece a esa región no aparece en la traza.

### Prueba B - mismo canal, otro scope

```text
/enviar_mc ch5 --scope #otra-region PRUEBA OTRA REGION
```

El `channel_idx` es exactamente el mismo; cambia únicamente el alcance regional.

### Prueba C - default del nodo

```text
/enviar_mc ch5 --scope 0 PRUEBA DEFAULT
```

Debe utilizar el default scope del Companion.

### Prueba D - unscoped explícito

```text
/enviar_mc ch5 --scope * PRUEBA UNSCOPED
```

Es esperable que puedan retransmitirlo repetidores que admitan tráfico global/unscoped.

### Prueba E - regresión

```text
/enviar_mc ch5 PRUEBA SIN MODIFICADOR
```

Debe conservar la sintaxis y el funcionamiento histórico.

## 15. Diagnóstico en logs

En un TX scoped debe aparecer el scope en el encolado diagnóstico, por ejemplo:

```text
[meshcore] enqueue -> chan_idx=5 ... scope=#zaragoza
```

La cola continúa mostrando su `tx_id`, partes y reintentos habituales.

## 16. Seguridad operativa

- No usar `--scope *` salvo que se quiera explícitamente tráfico unscoped.
- Utilizar el nombre exacto de la región MeshCore.
- Preferir `#region` para hacer explícito que se trata de un flood scope.
- Verificar primero mediante un mensaje corto antes de pruebas de tráfico intensivo.
- Si `set_flood_scope()` falla, no forzar manualmente el envío sin investigar la causa.

## 17. Resumen

La nueva semántica es:

```text
channel_idx = por dónde sale
scope       = hasta dónde se propaga
```

Ejemplo final:

```text
/enviar_mc ch5 --scope #zaragoza Hola
```

El canal 5 no queda asociado permanentemente a `#zaragoza`. Un segundo mensaje puede utilizar el mismo canal con otro scope:

```text
/enviar_mc ch5 --scope #aragon Hola Aragón
```

O volver al scope por defecto:

```text
/enviar_mc ch5 --scope 0 Hola default
```
