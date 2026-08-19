# MeshCore Observability — Fase 1: auditoría y contrato de eventos

## Objetivo

La Fase 1 inventaría la información MeshCore que el Broker ya obtiene y define un
contrato de datos estable para fases posteriores. No modifica RX, TX, colas,
reintentos, BBS, APRS, Telegram, puentes, perfiles de radio ni conexiones.

El nuevo módulo `source/meshcore_observability.py` no está importado todavía por
`Meshtastic_Broker.py`; por tanto esta fase es funcionalmente neutra.

## Fuente de verdad actual

La autoridad MeshCore sigue siendo `MeshCoreEmbeddedBridge` dentro de
`source/Meshtastic_Broker.py`. El motor ya soporta Serial, TCP y BLE, supervisor
24/7, reconexión, cola TX, retry spool y deduplicación anti-eco.

### Datos de sesión disponibles

- transporte activo: `serial`, `tcp` o `ble`;
- estado conectado/desconectado;
- último éxito y último error;
- mapas de canales y contactos;
- cola/reintentos TX.

### Contactos

El Broker ya mantiene `_mc_contacts_cache` y `_mc_path_prefix_cache`.
`_meshcore_remember_contact()` normaliza y recuerda:

- `public_key`;
- prefijos de clave;
- nombre/alias anunciado;
- latitud/longitud cuando existe;
- índices de prefijos usados para resolver hashes de rutas.

`list_contacts()` obtiene además, según soporte de la librería:

- `last_seen` / `last_advert`;
- `adv_type`;
- tipo normalizado: unknown, companion, repeater, room o sensor;
- `can_repeat` cuando puede afirmarse;
- `flags`, `feat1`, `feat2`, `lastmod`;
- `out_path` y geometría de ruta dirigida;
- `dm_key` seguro basado en prefijo de `public_key`.

### Canales

`list_channels()` consulta `get_channel(channel_idx)` y combina el resultado con
`MESHCORE_CHANNEL_MAP`. Actualmente dispone de:

- `channel_idx`;
- nombre del canal;
- rol lógico/mapeo;
- origen de la información;
- `channel_hash`.

No se necesita persistir `channel_secret` para observabilidad.

### Mensajes RX

`_on_msg()` recibe `CONTACT_MSG_RECV` y `CHANNEL_MSG_RECV` y ya diferencia:

- DM/contacto frente a mensaje de canal;
- `channel_idx`;
- texto;
- `pubkey_prefix`;
- identificadores `id`, `message_id` o `timestamp` cuando existen;
- alias/nombre resuelto;
- canal lógico/tag;
- posición del emisor si el contacto está en caché.

El callback también consume determinados comandos internos —BBS, respuestas
automáticas, farmacias, emergencias y correo— antes de continuar por el flujo
normal. La observabilidad futura deberá capturar el evento antes de esos
`return`, pero sin cambiar su semántica.

### Rutas y repetidores

`_meshcore_enrich_path_info()` ya genera `meshcore_repeaters` utilizando los
hashes de ruta y la caché de contactos. Por cada salto puede existir:

- hash/prefijo;
- nombre resuelto;
- indicador `resolved`;
- indicador `ambiguous`;
- SNR por salto;
- latitud/longitud del repetidor conocido.

Esta información debe reutilizarse. No se creará un segundo resolutor de rutas.

### Descubrimiento y trace

El Broker ya dispone de lógica de trace/path discovery que devuelve:

- contacto objetivo;
- `public_key`/prefix;
- `path_hex`;
- longitud de ruta;
- ancho de hash;
- SNR por salto;
- hops resueltos;
- payload original del trace.

La futura topología deberá consumir estos resultados en lugar de repetir el
proceso de descubrimiento.

## Componentes existentes que NO se reutilizan como modelo de eventos

### `shared/delivery_audit.py`

Es un journal best-effort de entregas de aplicaciones. Registra resultados como
`sent`, `failed`, `duplicate` o `rate_limited`. Su unidad de información es una
entrega lógica/física, no un paquete RF recibido. Se mantiene independiente.

### `source/auditoria_red.py`

Analiza principalmente datos de red Meshtastic almacenados en JSONL y
`nodos.txt`. No debe convertirse en la fuente de verdad de MeshCore. En fases
posteriores podrá consumir la nueva API si interesa ofrecer informes comunes.

## Contrato Fase 1

`MeshCoreEvent` define los campos estables siguientes:

- `schema_version`;
- `timestamp_utc`;
- `event_type`;
- `direction`;
- `transport`;
- `message_kind`;
- `packet_id`;
- `sender_prefix`;
- `sender_public_key`;
- `sender_alias`;
- `channel_idx`;
- `channel_tag`;
- `text`;
- `source_lat` / `source_lon`;
- `path_hex`;
- `path_hops`;
- `payload`;
- `metadata`.

`MeshCoreRepeaterHop` representa los saltos ya enriquecidos por el Broker.

`build_meshcore_message_event()` transforma un payload ya recibido/enriquecido
en el contrato común. No consulta la radio, no resuelve contactos y no escribe
en disco.

## Reglas para la Fase 2

1. El Packet Archive será best-effort: cualquier error de SQLite se absorberá.
2. El archivo no participará en decisiones RX/TX.
3. La inserción deberá realizarse antes de los retornos provocados por comandos
   internos para conservar una visión fiel de la actividad RF.
4. Se reutilizará `_meshcore_enrich_path_info()`; no se duplicará la resolución
   de rutas.
5. La persistencia estará desactivable mediante configuración.
6. No se persistirán secretos de canal ni credenciales.
7. El Broker seguirá siendo la autoridad y las futuras API serán únicamente una
   vista de observabilidad.
8. Las migraciones de esquema conservarán `schema_version` para mantener
   compatibilidad con registros anteriores.

## Validación Fase 1

Las pruebas `tests/test_meshcore_observability.py` verifican:

- reutilización de campos ya enriquecidos por el Broker;
- representación de repetidores y SNR;
- serialización JSON de bytes y estructuras heterogéneas;
- degradación segura ante valores inesperados;
- ausencia de mutación del payload original.

La fase queda deliberadamente sin integración runtime. El siguiente cambio de
comportamiento corresponderá exclusivamente a la Fase 2: Packet Archive.
