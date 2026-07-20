# 🤖 Guía completa del BOT de Telegram — MeshNet "The Boss" (v7.0.15)

## 1. Visión general
El BOT de Telegram es la **interfaz operativa principal** del sistema MeshNet. Centraliza control, supervisión y envío de mensajes hacia la red Meshtastic, gestiona tareas programadas, auditorías de red, mapas de cobertura, pasarela APRS y control del broker, sin abrir conexiones directas al nodo cuando se opera en modo broker.

Arquitectura lógica:
Usuario Telegram → BOT → Broker JSONL → Nodo(s) Meshtastic → (opcional) APRS RF / APRS-IS

El diseño prioriza operación **24/7**, tolerancia a fallos, y separación estricta de responsabilidades.

---

## 2. Modos de despliegue soportados

### 2.1 Raspberry Pi (producción)
- Sistema recomendado: Raspberry Pi OS Lite 64-bit.
- Ejecución habitual: Docker + docker-compose.
- Conexión al nodo: TCP/IP, USB o BLE (gestionado por broker).
- Uso típico: nodo principal, pasarela APRS RF, sistema de emergencia.

### 2.2 Windows (laboratorio / pruebas)
- Docker Desktop (WSL2 recomendado).
- Uso de `host.docker.internal` para KISS / APRS.
- Ideal para pruebas, desarrollo y validación antes de producción.

---

## 3. Flujo de comunicación
```
📲 Usuario Telegram
        ↓
🤖 Telegram_Bot_Broker.py
        ↓
⚙️ Broker JSONL (8766)
        ↓
📡 Nodo Meshtastic (TCP / USB / BLE)
        ↓
🌐 APRS RF / APRS-IS (opcional)
```

El BOT **no mantiene sockets persistentes** contra la radio. Todo pasa por el broker.

---

## 4. Comandos del BOT

### 4.1 Comandos generales
- `/start`, `/menu` – Menú principal.
- `/ayuda` – Ayuda completa.
- `/estado` – Estado del broker y nodo.
- `/bridge_status` – Estado de bridges A↔B / A↔C.

### 4.2 Nodos y red
- `/ver_nodos [N] [timeout]`
- `/vecinos [N] [hops]`
- `/traceroute <!id|alias> [timeout]`
- `/traceroute_status`
- `/telemetria [!id|alias] [histórico]`
- `/position`, `/position_mapa`

### 4.3 Envío de mensajes
- `/enviar [mesh|aprs|ambos] canal N texto`
- `/enviar [mesh|aprs|ambos] !id texto`
- `/enviar_mc [mesh|aprs|ambos] chX texto`
- `/enviar_mc_dm contacto texto` / `/dm_mc contacto texto`
- `/enviar_ack !id texto reintentos=3 espera=10 backoff=1.5`

Soporta:
- Broadcast / unicast.
- ACK real combinado (librería + broker ROUTING_APP).
- Troceado automático.

### 4.4 Escucha
- `/escuchar [canal|all]`
- `/parar_escucha`

### 4.5 Programación y tareas
- `/diario HH:MM [mesh|aprs|ambos] destino texto`
- `/diario_mc HH:MM [mesh|aprs|ambos] chX texto`
- `/diario_mc_dm HH:MM contacto texto`
- `/en minutos destino texto`
- `/manana HH:MM destino texto`
- `/programar YYYY-MM-DD HH:MM destino texto`
- `/tareas`, `/mis_diarios`
- `/cancelar_tarea`, `/parar_diario`

### 4.6 APRS
- `/aprs texto`
- `/aprs CALL: texto`
- `/aprs canal N texto`
- `/aprs_on`, `/aprs_off`
- `/aprs_status`, `/aprsis_push`

### 4.7 Correo ↔ malla
- `/mail_contactos`
- `/mail_add contacto correo@dominio`
- `/mail_edit contacto_o_numero nuevo@correo`
- `/mail_del contacto_o_numero`
- `/mail contacto_o_numero texto`

### 4.8 Auditoría y cobertura
- `/auditoria_red`
- `/auditoria_integral`
- `/cobertura [!id] [horas] [entorno]`

---

## 5. Variables de entorno (.env)

### 5.1 Telegram
```
TELEGRAM_TOKEN=xxxx
ADMIN_IDS=12345,67890
BOT_START_DELAY=60
```

### 5.2 Broker
```
BROKER_HOST=meshnet-broker
BROKER_PORT=8766
BROKER_CHANNEL=0
DISABLE_BOT_TCP=1
```

### 5.3 Meshtastic
```
MESHTASTIC_HOST=192.168.1.127
MESHTASTIC_PORT=4403
MESHTASTIC_TIMEOUT=10
TRACEROUTE_TIMEOUT=120
TELEMETRY_TIMEOUT=8
```

### 5.4 ACK y reintentos
```
ACK_MAX_ATTEMPTS=3
ACK_WAIT_SEC=10
ACK_BACKOFF=1.5
BROKER_ALLOW_BROADCAST_ACK=1
```

### 5.5 APRS
```
APRS_GATE_ENABLED=1
APRS_CALL=EB2EAS-11
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=m/20
APRS_MAX_LEN=67
APRS_GATE_ECHO=1
APRS_ECHO_HOME_ENABLED=1
APRS_ALLOWED_SOURCES=EB2EAS-11,EB2EAS-7
```

### 5.6 APRS RF / KISS
```
KISS_HOST=host.docker.internal   # Windows
# o IP real en Raspberry
KISS_PORT=8100
```

### 5.7 Bridges A↔B / A↔C
```
A_HOST=${MESHTASTIC_HOST}
A_PORT=4403
B_HOST=192.168.1.126
B_PORT=4403
A2B_CH_MAP=0:0,1:1,2:2,3:3
B2A_CH_MAP=0:0,1:1,2:2,3:3
FORWARD_TEXT=1
FORWARD_POSITION=0
REQUIRE_ACK=0
```

### 5.8 Posición HOME
```
HOME_LAT=41.65
HOME_LON=-0.89
HOME_NODE_ID=!abcd1234
```

### 5.9 Auditoría
```
AUD_WINDOW_H=72
AUD_HOPS_MAX=3
AUD_SNR_OK=0.0
AUD_PERSIST_RATIO_MIN=0.45
AUD_TZ=Europe/Madrid
```

### 5.10 Mapas y datos
```
BOT_DATA_DIR=/app/bot_data
BOT_MAPS_DIR=bot_data/reportes
AUD_HEATMAP_ENABLE=1
```

### 5.11 Correo ↔ malla
```
EMAIL_CONTACTS_PATH=/app/bot_data/email_contacts.json
EMAIL_SMTP_HOST=smtp.example.org
EMAIL_SMTP_PORT=587
EMAIL_SMTP_SSL=0
EMAIL_SMTP_STARTTLS=1
EMAIL_SMTP_USER=cuenta@example.org
EMAIL_SMTP_PASSWORD=token_o_contrasena_de_aplicacion
EMAIL_FROM=cuenta@example.org
EMAIL_OUT_SUBJECT_PREFIX=[Mesh]
```

---

## 6. Ejemplos prácticos

### Envío con ACK
```
/enviar_ack !2744ee88 Mensaje crítico reintentos=5 espera=15
```

### Diario APRS + Mesh
```
/diario 08:00 both canal 0 Boletín diario
```

### Auditoría integral
```
/auditoria_integral
```

### Emergencia APRS
```
/aprs EMERGENCY: Corte eléctrico zona norte
```

---

## 7. Seguridad y robustez
- Anti-duplicados.
- Cooldown durante reconexiones.
- Pausa del broker para CLI.
- Fallback API/CLI.
- Persistencia de tareas.

---

## 8. Uso recomendado
- Windows: pruebas y validación.
- Raspberry Pi: producción 24/7.
- Integración con APRS RF para emergencias reales.

---

## 9. Estado del proyecto
- Versión: v7.0.15
- Compatible broker v7.x
- Operativo 24/7

Autor: jmmpcc — MeshNet "The Boss"

---

## 10. Comandos añadidos en v7.x

### 10.1 Selección de transporte en `/enviar` y `/enviar_mc`

Los comandos de envío pueden escoger transporte de forma explícita:

```text
/enviar [mesh|aprs|ambos] <destino[:canal] | canal N> [aprs <CALL|broadcast>] <texto>
/enviar_mc [mesh|aprs|ambos] <chX|X|canal X> [aprs <CALL|broadcast>] <texto>
```

Ejemplos:

```text
/enviar mesh canal 0 Aviso solo Meshtastic
/enviar aprs EB2ABC-7: Aviso solo APRS
/enviar ambos canal 0 aprs broadcast Aviso doble Meshtastic y APRS
/enviar_mc mesh ch2 Aviso solo MeshCore
/enviar_mc aprs broadcast: Aviso APRS desde flujo MeshCore
/enviar_mc ambos ch2 aprs EB2ABC-7 Aviso doble MeshCore y APRS
```

### 10.2 Programación diaria MeshCore

```text
/diario_mc 09:00 mesh ch2 Parte diario MeshCore
/diario_mc 09:00 ambos canal 2 aprs broadcast Parte diario doble
/diario_mc 09:00 aprs EB2ABC-7: Parte diario APRS
/diario_mc 09:00,21:00 grupo avisos_mc canal 2 Parte diario MeshCore
/diario_mc_dm 09:00 6a18cb3d125b Parte diario directo
/diario_mc_dm 09:00 [MC:6a18cb3d125b] Parte diario directo
/diario_mc_dm 09:00,21:00 grupo avisos_dm 6a18cb3d125b Parte diario directo
```

Las tareas creadas por `/diario_mc` y `/diario_mc_dm` son persistentes y se gestionan con `/mis_diarios`, `/parar_diario` y `/parar_diario_grupo`.

### 10.3 Correo ↔ malla desde el bot

```text
/mail_contactos
/mail_add eb2eas eb2eas@example.org
/mail_edit eb2eas nuevo@example.org
/mail_del eb2eas
/mail eb2eas Mensaje enviado desde Telegram
```

El bot reutiliza la libreta común `EMAIL_CONTACTS_PATH` y el envío SMTP configurado con las variables `EMAIL_SMTP_*`, `EMAIL_FROM` y `EMAIL_OUT_SUBJECT_PREFIX`.

---

## 11. Historial funcional integrado desde el README principal

El bloque siguiente conserva las actualizaciones que antes estaban en la cabecera del `README.md`, ahora ubicadas en esta guía completa del bot para mantener el README principal más limpio sin perder la documentación operativa ni los ejemplos.

# MeshNet — Changelog Consolidado

# CHANGELOG — /enviar y /enviar_mc con selección de transporte Mesh/APRS

## v7.0.15-enviar-aprs-transporte

### Añadido

- `/enviar` permite elegir explícitamente el transporte igual que `/diario`:
  - `mesh` / `malla` / `meshtastic`: solo malla Meshtastic.
  - `aprs`: solo APRS.
  - `ambos` / `both`: Meshtastic + APRS.
- `/enviar_mc` permite la misma selección de transporte aplicada a MeshCore:
  - `mesh` / `malla` / `meshcore` / `mc`: solo malla MeshCore.
  - `aprs`: solo APRS.
  - `ambos` / `both`: MeshCore + APRS.
- En modo `ambos`, se puede indicar el destino APRS justo después del destino de malla con `aprs <CALL|broadcast>`:

```text
/enviar ambos canal 0 aprs EB2ABC-7 Aviso enviado a Meshtastic y APRS
/enviar_mc ambos ch2 aprs broadcast Aviso enviado a MeshCore y APRS
```

- Si no se especifica destino APRS en modo `ambos`, el destino APRS por defecto es `broadcast`.
- En modo `aprs`, el comando no envía a la malla; solo manda a la pasarela APRS por UDP usando el mismo troceo que `/aprs`.
- Las respuestas del bot ahora muestran claramente:
  - `Transporte: MESH`, `Transporte: APRS` o `Transporte: BOTH`.
  - Resultado de la malla Meshtastic o MeshCore.
  - Resultado APRS, destino APRS y número de partes APRS enviadas.

### Sintaxis rápida

```text
/enviar [mesh|aprs|ambos] <destino[:canal] | canal N> [aprs <CALL|broadcast>] <texto>
/enviar_mc [mesh|aprs|ambos] <chX|X|canal X> [aprs <CALL|broadcast>] <texto>
```

### Ejemplos y resultado esperado

| Comando | Sale por | Resultado mostrado |
|---|---|---|
| `/enviar canal 0 Hola malla` | Meshtastic | `Transporte: MESH`, destino de malla, resultado Meshtastic |
| `/enviar mesh canal 0 Hola malla` | Meshtastic | Igual que el modo clásico, pero indicando transporte |
| `/enviar aprs EB2ABC-7: Hola APRS` | APRS | `Transporte: APRS`, destino APRS y partes enviadas |
| `/enviar ambos canal 0 aprs EB2ABC-7 Hola doble` | Meshtastic + APRS | Resultado Meshtastic y resultado APRS separados |
| `/enviar_mc ch2 Hola MeshCore` | MeshCore | `Transporte: MESH`, canal MeshCore y resultado MeshCore |
| `/enviar_mc aprs broadcast: Hola APRS` | APRS | Solo APRS, sin enviar a MeshCore |
| `/enviar_mc ambos ch2 aprs EB2ABC-7 Hola doble` | MeshCore + APRS | Resultado MeshCore y resultado APRS separados |

### Conservado

- La sintaxis clásica de `/enviar canal N texto` y `/enviar <nodo|alias> texto` sigue funcionando.
- La sintaxis clásica de `/enviar_mc chX texto`, `/enviar_mc X texto` y `/enviar_mc canal X texto` sigue funcionando.
- `/diario` conserva su comportamiento y sirve como referencia para los modos `mesh`, `aprs` y `ambos`.
- `/aprs` sigue disponible como comando especializado de APRS.
- El envío APRS se trocea con `APRS_MAX_LEN` para respetar el límite de trama configurado.

---

# CHANGELOG — Programación diaria MeshCore

## v7.0.11-diario-meshcore

### Añadido

- Nuevo comando de Telegram `/diario_mc` para programar mensajes diarios hacia MeshCore por canal.
- Nuevo comando de Telegram `/diario_mc_dm` para programar mensajes diarios directos hacia un contacto MeshCore.
- Integración de `/diario_mc` como equivalente programado diario de `/enviar_mc`.
- Integración de `/diario_mc_dm` como equivalente programado diario de `/enviar_mc_dm` / `/dm_mc`.
- Soporte de varias horas en una sola orden, igual que `/diario`:

```text
/diario_mc 09:00,21:00 canal 2 Texto diario MeshCore
/diario_mc_dm 09:00,21:00 6a18cb3d125b Texto diario directo MeshCore
```

- Soporte de agrupación mediante `grupo <id>` para facilitar cancelaciones masivas:

```text
/diario_mc 09:00 grupo avisos_mc canal 2 Texto diario MeshCore
/diario_mc_dm 09:00 grupo avisos_dm 6a18cb3d125b Texto diario directo MeshCore
```

- Registro de los nuevos comandos en el menú oficial de Telegram:

```text
/diario_mc
/diario_mc_dm
```

- Nuevo transporte lógico `meshcore` dentro del scheduler persistente `broker_task.py`.
- Nueva distinción interna mediante `meta.meshcore_mode`:
  - `channel`: envío MeshCore por canal.
  - `dm`: envío MeshCore directo a contacto.
- Nuevo helper interno `_meshcore_send_via_broker_ctrl()` para enviar tareas programadas hacia MeshCore reutilizando el broker.
- Envío programado MeshCore mediante el comando ya existente del broker `MESHCORE_SEND`.
- Compatibilidad con envío MeshCore por canal usando `kind="chan"` y `channel_idx`.
- Compatibilidad con envío MeshCore directo usando `kind="contact"` y `contact_prefix`.
- Troceo automático de mensajes largos antes de enviarlos a MeshCore.
- Prefijo automático de partes para mensajes fragmentados:

```text
(1/3) texto...
(2/3) texto...
(3/3) texto...
```

- Reprogramación diaria automática conservando el comportamiento ya existente de `/diario`.
- Persistencia en JSONL mediante el scheduler actual.
- Compatibilidad con `/mis_diarios`, `/parar_diario` y `/parar_diario_grupo` sin cambios adicionales.

### Conservado

- No se modifica el comportamiento existente de `/diario`.
- No se modifica el comportamiento existente de `/enviar_mc`.
- No se modifica el comportamiento existente de `/enviar_mc_dm` ni `/dm_mc`.
- No se modifica el comportamiento existente de `/mc_contactos`.
- No se modifica `Meshtastic_Broker.py`.
- No se abre ninguna conexión MeshCore nueva desde el bot o desde el scheduler.
- No se duplica la lógica del motor MeshCore embebido.
- No se altera `MESHCORE_ENGINE`.
- No se altera `enqueue_send_channel()`.
- No se altera `enqueue_send_contact()`.
- No se modifica el sistema actual de cancelación de tareas diarias.
- No se modifica la estructura base de `ScheduledTask`.

### Detalles técnicos

#### `/diario_mc`

Programa un mensaje diario hacia un canal MeshCore, hacia APRS o hacia ambos transportes, con la misma selección `mesh|aprs|ambos` de `/diario` y `/enviar_mc`.

Sintaxis principal:

```text
/diario_mc <HH:MM[,HH:MM,...]> [mesh|aprs|ambos] [grupo <id>] <chX|canal X> [aprs <CALL|broadcast>] <texto>
/diario_mc <HH:MM[,HH:MM,...]> aprs <CALL|broadcast>: <texto>
```

Ejemplos:

```text
/diario_mc 09:00 mesh ch2 Parte diario MeshCore
/diario_mc 09:00 ambos canal 2 aprs broadcast Parte diario doble
/diario_mc 09:00 aprs EB2ABC-7: Parte diario APRS
/diario_mc 09:00,21:00 grupo avisos_mc canal 2 Parte diario MeshCore
```

La tarea queda identificada internamente con metadatos equivalentes a:

```python
meta = {
    "via": "/diario_mc",
    "repeat": "daily",
    "daily_time": "09:00",
    "transport": "meshcore",  # o "aprs" / "meshcore_aprs"
    "meshcore_mode": "channel",
    "meshcore_channel_idx": 2,
}
```

#### `/diario_mc_dm`

Programa un mensaje diario directo hacia un contacto MeshCore.

Sintaxis principal:

```text
/diario_mc_dm <HH:MM[,HH:MM,...]> <contact_prefix|[MC:prefix]> <texto>
```

Ejemplos:

```text
/diario_mc_dm 09:00 6a18cb3d125b Parte diario directo
/diario_mc_dm 09:00 [MC:6a18cb3d125b] Parte diario directo
/diario_mc_dm 09:00,21:00 grupo avisos_dm 6a18cb3d125b Parte diario directo
```

La tarea queda identificada internamente con metadatos equivalentes a:

```python
meta = {
    "via": "/diario_mc_dm",
    "repeat": "daily",
    "daily_time": "09:00",
    "transport": "meshcore",
    "meshcore_mode": "dm",
    "meshcore_contact": "6a18cb3d125b",
}
```

### Ficheros modificados

- `Telegram_Bot_Broker.py`
- `broker_task.py`

### Validación realizada

Compilación de sintaxis Python:

```bash
python3 -m py_compile Telegram_Bot_Broker_diario_mc.py broker_task_diario_mc.py
```

Resultado:

```text
OK — sin errores de compilación.
```

### Instalación recomendada

```bash
cd ~/MeshNet-Bot

cp Telegram_Bot_Broker.py Telegram_Bot_Broker.py.bak_diario_mc_$(date +%Y%m%d_%H%M%S)
cp broker_task.py broker_task.py.bak_diario_mc_$(date +%Y%m%d_%H%M%S)

cp Telegram_Bot_Broker_diario_mc.py Telegram_Bot_Broker.py
cp broker_task_diario_mc.py broker_task.py

python3 -m py_compile Telegram_Bot_Broker.py broker_task.py
```

Reinicio del bot:

```bash
docker restart meshnet-bot
```

O, si se ejecuta mediante servicio systemd:

```bash
sudo systemctl restart meshnet-bot
```

### Pruebas recomendadas

```text
/diario_mc 09:00 canal 2 Prueba diaria MeshCore canal
/diario_mc_dm 09:00 6a18cb3d125b Prueba diaria MeshCore directo
/mis_diarios
```

Comprobación de logs:

```bash
docker logs -f meshnet-bot
docker logs -f meshnet-broker | grep -i meshcore
```

### Notas operativas

- `/diario_mc` usa el canal MeshCore indicado por el usuario.
- `/diario_mc_dm` envía directamente al contacto MeshCore indicado.
- Las tareas quedan gestionadas por el mismo scheduler persistente que `/diario`.
- Si el broker MeshCore no está disponible en el momento del envío, se mantiene el sistema de reintentos/backoff existente.
- La cancelación individual y por grupo sigue usando los comandos ya existentes.


## [v7.0.2] — (12 de Abril de 2026)

### 📝 Changelog reciente (2026-04-12)

- ✅ Corrección de inconsistencia en el cálculo de **Hops reales**: se unifica la fórmula a `hop_start - hop_limit` (acotada a `>= 0`) para evitar mostrar saltos invertidos.
- ✅ Mejora en extracción de saltos y metadatos en eventos MeshCore/broker sintéticos:
  - ahora se buscan valores en `summary.*`, `payload.*`, `routing.*`, `raw.routing.*`
  - soporte para claves `snake_case` y `camelCase` (`hop_limit/hopLimit`, `hop_start/hopStart`, `relay_node/relayNode`)
- ✅ Ajuste en `/vecinos` para mantener el mismo criterio de cálculo de hops que el resto del bot.
- ℹ️ Nota: si un evento no trae `hop_start/hop_limit` en origen, el bot seguirá mostrando `—` (no se inventan saltos).

## [v7.0.2] — (9 de Abril de 2026)

### 🔄 Mejorado

- **`/cobertura` — Visualización de saltos RF (hops) en el mapa de cobertura:**
  - Nueva capa HTML **"Saltos RF (hops)"**: marcadores coloreados por número de saltos que
    atravesó cada paquete de posición antes de ser escuchado por el nodo local.
    - 🟢 Verde: recibido directamente (0 saltos)
    - 🟠 Naranja: 1 salto (un repetidor intermedio)
    - 🔴 Rojo-naranja: 2 saltos
    - 🔴 Rojo oscuro: 3 o más saltos
    - ⚫ Gris: sin datos de hops disponibles
  - Nueva capa HTML **"Nodos repetidores"**: marcadores naranjas sobre los nodos que
    actuaron como repetidores y cuya posición GPS es conocida en la misma ventana temporal.
  - Nueva capa HTML **"Rutas de salto"** (desactivada por defecto): líneas que unen cada
    origen con su repetidor para visualizar los caminos RF en la malla.
  - Leyenda de colores fija en la esquina inferior derecha del mapa HTML.
  - **KML**: pines coloreados por hop count (verde/naranja/rojo/gris) con descripción del
    salto y el ID del repetidor en cada placemark.
  - Todas las capas son activables/desactivables individualmente desde el `LayerControl`.
  - Compatibilidad total con el fallback `positions.jsonl`: si no hay datos de BacklogServer,
    el mapa se genera sin capas de hops (sin errores).

### 🧠 Cambios internos

- `coverage_backlog.py`:
  - Nuevos helpers `_get_hops()`, `_hop_color()`, `_hop_label()`, `_kml_hop_style_id()`.
  - `build_coverage_from_backlog()` construye un índice `relay_positions` (node_id → lat/lon)
    desde el propio dataset del backlog para localizar repetidores en el mapa.
  - Tuple de puntos extendido a `(lat, lon, score, label, hops, relay_nid)`.
  - Eliminación del bloque Folium duplicado en `build_coverage_from_backlog`;
    ahora delega completamente en `_build_html_heatmap_and_circles()`.

> **Nota:** Solo se conoce el *último* repetidor del salto (`relay_node` del paquete),
> no la cadena completa. Para la ruta completa de nodos intermedios se requeriría `TRACEROUTE_APP`.

---

## [v7.0.1] — (3 de Abril de 2026)

### 🔄 Mejorado

- Broker MeshCore embebido:
  - Detección explícita de errores silenciosos de TX cuando `send_msg` / `send_chan_msg`
    devuelven `EventType.ERROR` sin excepción.
  - Reintento automático de **una sola vez** del mensaje fallido, persistido para la
    siguiente sesión tras reconexión (no se pierde al recrear la cola TX interna).
  - Conservación de pendientes TX en reconexión: los mensajes ya en cola (y los
    encolados sin sesión activa) se preservan para reenvío al restablecer enlace.
  - Límite de memoria para cola diferida (`MESHCORE_RETRY_SPOOL_MAX`, default 2000)
    para evitar crecimiento indefinido en desconexiones prolongadas.
  - Logs de envío con contador de reintentos (`retry=0/1`) para diagnóstico en producción.

### 🐞 Corregido

- Estado de conexión MeshCore “zombie” tras caídas de Internet:
  - si falla TX, se marca el enlace como no saludable y se fuerza reconexión limpia del engine.
  - se reduce la pérdida del primer mensaje tras recuperación de enlace.

### 🧠 Cambios internos

- Soporte de subcomandos CLI directos (`schedule`, `tasks`, `cancel`) mediante
  despacho por `sys.argv` hacia `_cli_tasks`.

### 🛠️ Uso de los comandos internos (broker)

Los subcomandos internos son **opcionales** y de uso manual (operación/soporte).
En el funcionamiento normal 24/7 **no se lanzan automáticamente**; lo habitual es
usar solo la recuperación/reconexión automática de nodos.

Si se necesitan, se ejecutan directamente sobre el proceso del broker:

```bash
python source/Meshtastic_Broker.py schedule --when "2026-04-03 22:30" --channel 0 --dest broadcast --msg "Prueba programada" --ack 0 --max-attempts 3
python source/Meshtastic_Broker.py tasks --status pending
python source/Meshtastic_Broker.py cancel --id <TASK_ID>
```

Notas:
- `--when` usa hora local configurada del scheduler (`Europe/Madrid` por defecto en broker tasks).
- `--dest broadcast` envía al canal; para DM usa destino específico cuando aplique.
- `tasks` permite filtrar por estado: `pending`, `done`, `failed`, `canceled`.
- Si no vas a programar tareas, puedes ignorar completamente estos subcomandos.

---


## [v7.0.0] — (20 de Marzo de 2026)

### 🚀 Añadido

- Soporte completo para **multi-transporte**:
  - USB (Serial)
  - TCP (IP)
  - BLE (Bluetooth)
- Sistema de **prefetch inicial de nodos** al arranque.
- Integración total con:
  - Broker v7
  - BacklogServer (SEND / FETCH)
- Compatibilidad con **MeshCore embebido**.
- Reactivación automática de conexión al enviar mensajes.
- Sistema de espera activa hasta que el broker esté operativo.
- Mejora en la gestión de alias MeshCore:
  ```
  [MC:<CANAL>:<ALIAS>]
  ```

---

### 🔄 Mejorado

- Reconexión automática en escenarios reales:
  - caída de nodo Meshtastic
  - reinicio del broker
  - desconexión USB
- Sincronización bot ↔ broker:
  - evita comandos en frío
  - evita estados inconsistentes
- Gestión de estado interno del bot:
  - mayor coherencia tras reconexiones
- Comportamiento uniforme entre USB / TCP / BLE.
- Estabilidad general para ejecución continua (24/7).
- Tolerancia a fallos en conexiones TCP intermitentes.
- Reducción de condiciones de carrera en arranque.

---

### 🧠 Cambios internos

- Refactor de lógica de conexión para adaptarse al broker v7.
- Integración con arquitectura de backend embebido:
  - MeshCore
  - Bridge Meshtastic
- Mejora en el control de flujo de inicialización.
- Ajustes en timing de arranque y espera de servicios.
- Optimización de gestión de eventos entrantes.

---

### 🐞 Corregido

- Problemas al iniciar el bot antes de que el broker estuviera listo.
- Fallos al trabajar con nodo en modo USB.
- Estados bloqueados tras desconexiones prolongadas.
- Pérdida de mensajes en reconexiones rápidas.
- Inconsistencias al cambiar entre transportes.

---

### ⚠️ Compatibilidad

- Requiere:
  - Broker v7.0.0 o superior
- Compatible con:
  - Triple Bridge v7
  - MeshCore embebido
- No recomendado usar con versiones antiguas del broker.

---

### 📌 Notas

- Esta versión está diseñada para **producción 24/7**.
- Sustituye completamente versiones anteriores del bot.
- El comportamiento observado de reconexión automática tras envío es intencionado y forma parte del diseño resiliente.

# Changelog — MeshNet / MeshBot v6.2.6

> Estado: **Validado para producción 24/7**
> Fecha: 2026-02-10

---

## 🔒 BBS y Bridge (cambios críticos)
- **Bloqueo total de tráfico BBS en bridges**
  - Las **solicitudes BBS** (`#BBS`, `@bbs`) **no cruzan** el bridge.
  - Las **respuestas BBS** (aunque no lleven `#BBS`) se marcan y **no se replican**.
  - Implementado de forma robusta mediante flag **`no_bridge`** en broker/backlog.
- **Filtro adicional en bridge externo**
  - Variables `TRIPLE_BLOCK_BBS`, `TRIPLE_BLOCK_BBS_FORCE`.
  - Filtro por canales BBS configurables (`BBS_CHANNELS` / `BBS_CHANNEL`).

---

## 🔁 Broker (Meshtastic_Broker_v6.2.6)
- **Compatibilidad completa de firma en TX**
  - `_tasks_send_adapter` acepta `ch`, `dest`, `ack` vía `**kwargs`.
- **Espejo A→B seguro**
  - Evita `TypeError` ante cambios de firma.
  - Soporte de `payload` estructurado.
- **Persistencia 24/7**
  - Envíos fallidos se registran en `offline_log.jsonl`.
- **Normalización de rutas BBS**
  - `BBS_DB_PATH` y `BBS_KEY_PATH` aceptan **directorios** o **ficheros**.
  - Resolución automática a `bbs_data.db` / `.bbs_key`.

---

## 🤖 Bot de Telegram (Telegram_Bot_Broker_v6.2.6)
- **SQLite en modo seguro de solo lectura**
  - `mode=ro`, `cache=shared`, `busy_timeout`.
  - `query_only=ON` para evitar escrituras accidentales.
  - Compatible con WAL y alta concurrencia.
- **Acceso BBS desde el bot**
  - Lectura directa de **noticias** y **boletines**.
  - Paginación y filtros por categoría.
  - Shortlinks locales para URLs largas.
- **Comandos admin reforzados**
  - `/reconectar` con verificación real de estado del broker.
- **Mayor resiliencia de red**
  - Reintentos controlados, timeouts y logs explicativos.

---

## 🌉 Bridge externo (mesh_triple_bridge.py)
- **Prevención de loops y ecos**
  - Dedupe por hash + ventana TTL.
  - Rate-limit por dirección.
- **Gestión de tamaño de payload**
  - Límite conservador (`TRIPLE_MAX_TEXT_LEN`, default 160).
  - Troceo por palabras para evitar `Data payload too big`.
- **Modo broker estable**
  - No abre TCP a A cuando hay broker activo.
  - RX vía `FETCH_BACKLOG`, TX vía `SEND_TEXT`.

---

## 📡 APRS (meshtastic_to_aprs.py)
- **Saneo ASCII 7-bit estricto**
  - Normalización Unicode → ASCII seguro.
- **Deduplicación de tramas**
  - TTL configurable (`APRS_DEDUP_TTL`).
- **Push APRS-IS robusto**
  - Reintento automático ante `Broken pipe`.
  - IDs de mensaje `{nn}` para evitar supresión en clientes.
- **Log separado para WebAdmin**
  - `aprs_rx.jsonl` independiente de `positions.jsonl`.

---

## 🧱 Infraestructura y utilidades
- **TCPInterface persistente**
  - Pool reutilizable para evitar colisiones.
- **BacklogServer**
  - Comandos: `SEND_TEXT`, `SEND_TEXT_WAIT`, `FETCH_BACKLOG`, `BROKER_STATUS`, `FORCE_RECONNECT`.
- **Auditoría y cobertura**
  - Herramientas consolidadas sin cambios funcionales.
- **Sin regresiones**
  - No se ha modificado comportamiento previo que ya funcionaba.

---

## ✅ Dictamen
- Código **coherente**, **estable** y **apto para operación continua 24/7**.
- Riesgos conocidos mitigados (BBS↔bridge, SQLite locks, payload size).
- Cambios realizados de forma **quirúrgica**, sin romper flujos existentes.

---


## 🆕 v6.2.4-5 (30 Enero 2026)

### Arquitectura general
- Consolidación completa del modo **24/7 production** en todos los servicios (broker, APRS, bridges y BBS).
- Revisión integral de reconexión suave y manejo de excepciones `OSError` y `Connection reset by peer`.
- Limpieza de rutas de ejecución duplicadas y condiciones de carrera en hilos y timers.
- Normalización de logs con prefijos coherentes por subsistema (`[broker]`, `[bridge]`, `[aprs]`, `[bbs]`, `[web]`).

### Broker Meshtastic
- Estabilización del loop principal de lectura Meshtastic con recuperación automática sin caída del proceso.
- Protección frente a agotamiento de hilos en callbacks (`heartbeat`, `sendText`).
- Mejora del control de interfaces principales/secundarias.
- Optimización del envío `SEND_TEXT` con control de colas y timeouts.
- **Control estricto de DMs** dirigidos al nodo `HOME_NODE_ID` para órdenes especiales.
- Eliminación de ambigüedad entre DMs válidos, mensajes de canal y broadcast.

### Flujo avanzado `/aprs` (heredado y ampliado de v6.2.3)
- Soporte completo:
  - `/aprs DESTINO: texto`
  - `/aprs canal N DESTINO: texto`
  - `/aprs ch N DESTINO: texto`
- Cuando el mensaje es **DM al HOME_NODE_ID**:
  - Envío por APRS (RF + APRS‑IS si está activo).
  - Reinyección del texto limpio en el **canal Mesh N**.
- El comando nunca aparece en canales públicos.
- Prevención total de bucles Mesh ↔ APRS.

### Bridges (Mesh ↔ Mesh / Mesh ↔ MeshCore)
- Reenvío bidireccional estable A↔B y A↔B↔C.
- Correspondencia de canales Meshtastic ↔ MeshCore configurable por `.env`.
- Aislamiento de fallos por bridge.
- Logging detallado de tramas generadas.

### APRS
- Filtro explícito `src == APRSIS_USER` para evitar reinyección de tramas propias.
- Separación clara de flujos `mesh → APRS` y `APRS → mesh`.
- Estabilidad total del envío desde comandos `/aprs` por canal o DM autorizado.

### BBS Meshtastic
- Integración completa del servidor BBS.
- Formato obligatorio:
  ```
  #BBS <COMANDO> [parámetros]
  ```
- Soporte de múltiples BBS en un mismo canal.
- Respuestas siempre por DM al solicitante.
- Funcionalidades:
  - Boletines (NEWS)
  - Noticias automáticas (RSS / NOTICIAS)
- Persistencia robusta y menú ampliado.

### Automatización de noticias
- Servicio `bbs-news-ingestor` preparado para `systemd`.
- Separación entre boletines manuales y noticias automáticas.
- Clasificación por etiquetas y control de duplicados.

### Web Admin
- Corrección del stream en vivo de:
  - posiciones
  - telemetría
  - vecinos
- Contadores en tiempo real sin reinicio.
- Sincronización correcta entre histórico y live.

### Configuración y despliegue
- Revisión completa de variables `.env` (BBS, APRS, bridges, broker).
- Compatibilidad confirmada:
  - Raspberry Pi 2B / 3 / 4 / 5
  - Docker multi‑arch
- Base estable para clonación y despliegue rápido.

---
