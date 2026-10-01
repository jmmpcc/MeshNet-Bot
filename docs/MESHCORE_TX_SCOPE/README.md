# MeshNet-Bot - Scope MeshCore por transmisión

## 1. Objetivo

Esta funcionalidad permite elegir el **flood scope de MeshCore en cada transmisión**, de forma independiente del canal (`channel_idx`) por el que se envía el mensaje.

- **Canal**: determina por qué canal MeshCore se transmite el texto.
- **Scope**: determina el ámbito regional de propagación de ese TX.

No se modifica la configuración permanente del canal ni existe un mapa fijo `canal -> scope`.

## 2. Sintaxis recomendada

Para evitar problemas de autocorrección con guiones en Telegram, la sintaxis recomendada es:

```text
/enviar_mc canal 5 scope#utebo Prueba envio a Utebo
```

También:

```text
/enviar_mc ch5 scope#utebo Hola
/enviar_mc 5 scope#utebo Hola
/enviar_mc [ch5] scope#utebo Hola
```

El token `scope#utebo` se normaliza internamente a:

```text
#utebo
```

El scope puede colocarse en cualquier posición de los argumentos, siempre que no rompa la sintaxis histórica del destino. Por ejemplo:

```text
/enviar_mc ambos ch5 scope#zaragoza aprs broadcast Aviso doble
```

## 3. Compatibilidad con la sintaxis anterior

Las formas anteriores siguen aceptándose:

```text
/enviar_mc ch5 --scope #utebo Hola
/enviar_mc ch5 --scope=#utebo Hola
```

No obstante, la forma recomendada para uso diario es `scope#region` porque no depende de que Telegram, el teclado o el sistema operativo conserven dos guiones ASCII consecutivos.

No se permite mezclar en el mismo TX `scope#region` y `--scope`.

## 4. Envío sin scope explícito

La sintaxis histórica continúa funcionando sin cambios:

```text
/enviar_mc ch5 Hola
/enviar_mc canal 5 Hola
/enviar_mc ambos ch5 aprs broadcast Aviso
```

Si no se indica ningún modificador de scope, se delega en el flujo histórico.

## 5. Valores especiales

### Default scope del nodo

```text
/enviar_mc canal 5 scope#0 Hola
```

`scope#0` equivale a `set_flood_scope("0")` y utiliza el default scope configurado en el Companion.

### TX explícitamente unscoped

```text
/enviar_mc canal 5 scope#* Hola
```

Esto fuerza tráfico sin scope. Debe utilizarse de forma consciente porque repetidores que acepten tráfico global/unscoped pueden retransmitirlo.

## 6. Confirmación visible en Telegram

Cuando el TX utiliza un scope explícito, la confirmación muestra el valor aplicado:

```text
Envío MeshCore
Transporte: MESH
Malla MeshCore -> Canal (channel_idx): 5
Scope TX: #utebo
Resultado MeshCore: OK
```

Si no se indica scope, la confirmación mantiene el formato histórico.

## 7. Funcionamiento interno

Ejemplo:

```text
/enviar_mc canal 5 scope#utebo Hola
```

Flujo:

```text
Telegram
   |
   | scope#utebo
   v
MeshNet-Bot
   |
   | MESHCORE_SEND
   | channel_idx = 5
   | text = Hola
   | scope = #utebo
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
   | set_flood_scope("#utebo")
   | send_chan_msg(5, "Hola")
   v
Radio MeshCore
```

El scope pertenece al **TX**, no al canal.

## 8. Fragmentación

Los mensajes largos siguen utilizando la fragmentación existente. El scope se conserva junto al item de cola durante todas sus partes:

```text
TX scope #utebo
  parte 1/3 -> #utebo
  parte 2/3 -> #utebo
  parte 3/3 -> #utebo
```

No se sustituye la lógica actual de fragmentación.

## 9. Reintentos y reconexiones

El scope se guarda dentro del destino del item de cola:

```text
{
  kind: chan,
  channel_idx: 5,
  scope: #utebo
}
```

Si la conexión cae y el TX pasa al spool, el scope permanece asociado al mensaje y se reutiliza tras la reconexión.

## 10. Protección fail-closed

Si `set_flood_scope()` devuelve un error, esa parte no se transmite:

```text
set_flood_scope(#utebo) -> ERROR
send_chan_msg(...)      -> NO se ejecuta
```

Así un mensaje regional no puede salir accidentalmente como tráfico global.

## 11. Restauración después de un TX scoped

Después de un TX scoped, el siguiente TX de canal sin scope explícito restaura primero:

```text
set_flood_scope("0")
```

Esto evita que el scope anterior contamine mensajes posteriores.

## 12. RX

En TX conocemos el scope solicitado y aplicado.

En RX, la versión actual de `meshcore_py` no expone de forma fiable el scope real en `CHANNEL_MSG_RECV`. Por ese motivo MeshNet-Bot **no inventa ni infiere un scope recibido**.

Se mantienen los datos RX existentes: canal, alias, RSSI, SNR, repetidores, traza y mapa cuando esté configurado.

## 13. Ayuda del bot

El menú `/` muestra que `/enviar_mc` admite `scope#region` opcional por TX.

`/ayuda` y `/enviar_mc` sin parámetros incluyen ejemplos como:

```text
/enviar_mc canal 5 scope#utebo Hola
/enviar_mc ch5 scope#zaragoza Hola
/enviar_mc ambos ch5 scope#zaragoza aprs broadcast Aviso
/enviar_mc ch5 scope#0 Hola
/enviar_mc ch5 scope#* Hola
```

La ayuda histórica no se elimina; sólo se añade este bloque.

## 14. Compatibilidad protegida

No se modifica el comportamiento de:

- `/enviar_mc` sin scope;
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

Los clientes antiguos que envían `MESHCORE_SEND` sin `scope` siguen siendo válidos.

## 15. Pruebas RF recomendadas

### A. Scope Utebo

```text
/enviar_mc canal 5 scope#utebo PRUEBA SCOPE UTEBO
```

Comprobar:

1. Telegram muestra `Scope TX: #utebo`.
2. El repetidor de Utebo retransmite.
3. Un repetidor fuera de esa región no aparece en la traza.

### B. Mismo canal, otro scope

```text
/enviar_mc canal 5 scope#zaragoza PRUEBA OTRO SCOPE
```

El canal sigue siendo `5`; cambia sólo el alcance regional.

### C. Default

```text
/enviar_mc canal 5 scope#0 PRUEBA DEFAULT
```

### D. Unscoped explícito

```text
/enviar_mc canal 5 scope#* PRUEBA UNSCOPED
```

### E. Regresión histórica

```text
/enviar_mc canal 5 PRUEBA SIN SCOPE
```

Debe conservar el comportamiento histórico.

## 16. Diagnóstico

En un TX scoped debe aparecer en el log del broker algo equivalente a:

```text
[meshcore] enqueue -> chan_idx=5 ... scope=#utebo
```

Telegram debe mostrar:

```text
Scope TX: #utebo
```

Si falta esa línea, el modificador no ha sido reconocido y no debe darse por validado el scope.

## 17. Resumen

La semántica final es:

```text
channel_idx = por dónde sale
scope       = hasta dónde se propaga
```

Sintaxis recomendada:

```text
/enviar_mc canal 5 scope#utebo Hola
```

El canal 5 no queda asociado permanentemente a `#utebo`. El siguiente mensaje puede usar otro scope o ninguno.
