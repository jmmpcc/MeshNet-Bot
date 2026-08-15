# MeshNet-Bot v7.0.55 — Pasarela interna de canales multi-radio

Fecha: 2026-08-15

## Alcance definitivo

La pasarela interna entre canales se diseña como funcionalidad del broker condicionada por `RADIO_PROFILE`, no como una característica exclusiva de Meshtastic.

Perfiles contemplados:

- `meshcore_only`: pasarela canal→canal dentro de MeshCore.
- `meshtastic_a_meshcore_embedded_b`: nodo A Meshtastic y nodo B MeshCore embebido; reglas independientes para ambos motores.
- `meshcore_a_meshtastic_embedded_b`: nodo A MeshCore y nodo B Meshtastic embebido; reglas independientes para ambos motores.
- `legacy`: no se fuerza ninguna topología nueva ni se adivina un transporte.

`source/radio_profile.py` es la autoridad para determinar capacidades, transporte principal, nodo A, nodo B y presencia de nodo embebido.

## Reglas multi-radio

Cada regla debe identificar explícitamente el transporte cuando el perfil tiene más de una radio disponible.

Ejemplos:

```text
meshtastic CH0 -> CH2
meshcore    CH0 -> CH1
```

Una regla incompatible con el perfil activo se conserva en persistencia pero no debe activarse ni abrir conexiones adicionales.

## Ayuda contextual del comando

El comando sin parámetros:

```text
/channel_gateway
```

muestra únicamente la sintaxis válida para el `RADIO_PROFILE` activo.

Reglas de uso:

- Si el perfil solo permite un transporte, como `meshcore_only`, el transporte puede omitirse:

```text
/channel_gateway add 0 2
/channel_gateway del 0 2
```

También se admite la forma explícita:

```text
/channel_gateway add meshcore 0 2
```

- Si el perfil es combinado, es obligatorio indicar el transporte para evitar ambigüedad:

```text
/channel_gateway add meshtastic 0 2
/channel_gateway add meshcore 0 2
```

- Un transporte no permitido por el perfil se rechaza antes de enviar ninguna orden al broker.
- En modo `legacy` o perfil no resoluble no se crean ni eliminan reglas ambiguas.
- La ayuda muestra también qué transporte corresponde al nodo A y cuál al nodo B embebido.

## Integración técnica prevista

- Meshtastic: reutiliza `meshtastic.receive` y la `SENDQ` existente del broker.
- MeshCore: reutiliza el motor embebido existente, `CHANNEL_MSG_RECV` y `send_chan_msg(channel_idx, text)`.
- No se abre una segunda conexión Meshtastic ni MeshCore.
- Deduplicación, anti-eco y rate-limit se separan por transporte y canal.
- Las TX internas no deben disparar involuntariamente los bridges externos A/B/C.

## Control desde Telegram

Sintaxis prevista:

```text
/channel_gateway
/channel_gateway help
/channel_gateway status
/channel_gateway list
/channel_gateway on
/channel_gateway off
/channel_gateway add [transporte] <origen> <destino> [both]
/channel_gateway del [transporte] <origen> <destino> [both]
/channel_gateway clear
```

Alias:

```text
/pasarela_canales
```

La consulta y la ayuda son de solo lectura. La activación, desactivación y modificación de reglas requieren un usuario incluido en `ADMIN_IDS`.

## Persistencia

El estado y las reglas se guardan de forma atómica en:

```text
${BOT_DATA_DIR}/channel_gateway.json
```

Los cambios realizados desde el bot sobreviven a reinicios.

## Validación requerida antes del merge

1. Meshtastic CHx→CHy.
2. Meshtastic CHx↔CHy sin ping-pong.
3. MeshCore CHx→CHy.
4. MeshCore CHx↔CHy sin ping-pong.
5. `meshcore_only` sin inicializar Meshtastic.
6. Ambos perfiles combinados con reglas independientes por motor.
7. Gateway OFF conserva comportamiento anterior.
8. Persistencia y restauración tras reinicio.
9. Control administrativo desde Telegram.
10. Ayuda contextual por perfil y rechazo de transportes inválidos.

La primera iteración Meshtastic del PR permanece en Draft hasta completar el soporte multi-radio y toda esta matriz de pruebas.
