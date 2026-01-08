# 🧩 Broker JSONL — Guía completa de configuración y operación

## 1. Visión general

El **Broker JSONL** es el núcleo operativo del proyecto MeshNet. Centraliza todas las comunicaciones entre:

- Nodo(s) **Meshtastic** (TCP / USB / BLE indirecto)
- **Bot de Telegram**
- **Gate APRS** (APRS-IS y/o RF)
- **Bridges** (A↔B↔C, presets, MeshCore, etc.)

Actúa como **único punto de entrada/salida**, evitando conexiones TCP duplicadas al nodo, gestionando colas, ACKs, tareas programadas, reconexiones seguras y funcionamiento continuo 24/7.

```
Telegram Bot  ⇄  Broker JSONL  ⇄  Nodo Meshtastic
                          ↕
                      APRS / Bridges
```

---

## 2. Funciones críticas

- Conexión **TCP persistente** al nodo Meshtastic
- Servidor **JSONL** para clientes locales
- **BacklogServer** de control y consulta
- Gestión de **colas SENDQ** y reintentos
- Gestión de **ACKs** (unicast y opcional broadcast)
- **Cooldowns** y anti-duplicados
- Pausa/reanudación segura para uso de CLI
- Ejecución de **tareas programadas** (diarias, diferidas)
- Almacenamiento de eventos (mensajes, telemetría, posiciones)
- Soporte **APRS emergencia** y fallback RF
- Operación estable en **Raspberry Pi** y **Windows (Docker)**

---

## 3. Puertos y servicios

| Servicio | Puerto | Descripción |
|--------|--------|------------|
| JSONL Server | 8765 | API local para bot, APRS, bridges |
| BacklogServer | 8766 | Control, estado, backlog, comandos |

> Por defecto `BROKER_CTRL_PORT = BROKER_PORT + 1`

---

## 4. Flujo interno de mensajes

1. RX desde Meshtastic (TCPInterface)
2. Normalización y decodificación
3. Clasificación por `portnum` (TEXT, TELEMETRY, POSITION, etc.)
4. Registro en JSONL
5. Enrutado a:
   - Bot Telegram
   - Gate APRS
   - Bridges
6. Gestión de ACK / reintentos si procede
7. Publicación a clientes JSONL

---

## 5. Comandos del BacklogServer

| Comando | Función |
|------|--------|
| `BROKER_STATUS` | Estado global, conexión, cooldown |
| `BROKER_PAUSE` | Pausa RX/TX al nodo |
| `BROKER_RESUME` | Reanuda operación |
| `FORCE_RECONNECT` | Reconexión limpia TCP |
| `SEND_TEXT` | Envío de mensaje (cola) |
| `FETCH_BACKLOG` | Recuperar mensajes recientes |
| `RUN_TRACEROUTE` | Lanzar traceroute desde broker |
| `RUN_CLI` | Ejecutar CLI controlado |

---

## 6. Variables de entorno (.env)

### 6.1 Variables básicas del broker

| Variable | Descripción | Ejemplo |
|--------|-------------|--------|
| `BROKER_HOST` | IP de escucha local | `0.0.0.0` |
| `BROKER_PORT` | Puerto JSONL | `8765` |
| `BROKER_CTRL_HOST` | Host control | `127.0.0.1` |
| `BROKER_CTRL_PORT` | Puerto control | `8766` |
| `BROKER_VERBOSE` | Logs humanos | `1` |

---

### 6.2 Conexión al nodo Meshtastic

| Variable | Descripción |
|--------|-------------|
| `MESHTASTIC_HOST` | IP o hostname del nodo |
| `MESHTASTIC_PORT` | Puerto TCP (4403) |
| `MESHTASTIC_TIMEOUT` | Timeout TCP |
| `MESHTASTIC_RECONNECT_SEC` | Espera entre reconexiones |

> En USB/BLE estas variables no se usan directamente, pero deben existir.

---

### 6.3 Colas, ACKs y reintentos

| Variable | Descripción |
|--------|-------------|
| `ACK_MAX_ATTEMPTS` | Reintentos máximos |
| `ACK_WAIT_SEC` | Espera inicial |
| `ACK_BACKOFF` | Backoff exponencial |
| `BROKER_ALLOW_BROADCAST_ACK` | ACK en broadcast (0/1) |
| `SENDQ_MAX` | Tamaño máximo de cola |

---

### 6.4 Cooldowns y protección

| Variable | Descripción |
|--------|-------------|
| `BROKER_COOLDOWN_SEC` | Cooldown tras error |
| `BROKER_DUP_WINDOW_SEC` | Ventana anti-duplicado |
| `BROKER_MAX_ERRORS` | Errores antes de pausa |

---

### 6.5 Pausa segura (CLI / Windows / Raspberry)

| Variable | Descripción |
|--------|-------------|
| `BOT_PAUSE_MODE` | `never` / `auto` / `always` |

- **Raspberry**: `never`
- **Windows/Docker**: `auto` o `always`

---

### 6.6 APRS y emergencias

| Variable | Descripción |
|--------|-------------|
| `APRS_ENABLED` | Activar gateway |
| `APRS_IS_HOST` | Servidor APRS-IS |
| `APRS_CALLSIGN` | Indicativo |
| `APRS_EMERGENCY_KEYWORDS` | Palabras clave |
| `APRS_EMERGENCY_DESTS` | Destinos emergencia |
| `MESH_EMERGENCY_CHANNELS` | Canales Mesh |
| `APRS_MAX_KM` | Radio máximo |

---

### 6.7 Logs y almacenamiento

| Variable | Descripción |
|--------|-------------|
| `BROKER_DATA_DIR` | Directorio base |
| `LOG_FILE` | Log principal |
| `POSITIONS_LOG` | JSONL posiciones |
| `POSITIONS_KEEP_DAYS` | Retención |

---

## 7. Diferencias Raspberry vs Windows

### Raspberry Pi

- `BOT_PAUSE_MODE=never`
- Sin CLI pesado
- TCP estable 24/7
- Ideal para producción

### Windows / Docker

- `BOT_PAUSE_MODE=auto`
- CLI disponible
- Docker Desktop
- Ideal para pruebas

---

## 8. Escenarios soportados

- Broker único + nodo local
- Broker + triple bridge (A/B/C)
- Broker + APRS RF sin internet
- Broker con nodo USB
- Broker con nodo remoto TCP
- Operación autónoma con energía solar

---

## 9. Sistema de emergencias (Mesh ⇄ APRS ⇄ Telegram)

Este proyecto incorpora un **circuito de emergencias** pensado para funcionar incluso en escenarios degradados:

- **Entrada por Mesh** (Telegram → Broker → Mesh) y salida por APRS (RF y/o APRS-IS).
- **Entrada por APRS** (RF / APRS-IS → Gateway) y reenvío a **Mesh** (Broker) con marcado y ruteo por canal.
- **Notificación inmediata a Telegram** (a chats admin) cuando una trama APRS se clasifica como emergencia.

### 9.1 Qué se considera “emergencia”

La clasificación se basa en dos heurísticas configurables por `.env`:

1) **Destino APRS** (campo `DEST`) en lista de emergencia.
2) **Palabras clave** encontradas en el texto (mensaje / comentario / info).

Variables:

- `APRS_EMERGENCY_KEYWORDS` → lista separada por comas. Ej.: `EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA`
- `APRS_EMERGENCY_DESTS` → lista separada por comas. Ej.: `EMERGENCY,EMERG,SOS`

Opcionales:

- `APRS_EMERGENCY_MAX_KM` → radio máximo (km) para considerar “local”. Si es 0, se desactiva el filtro.
- `HOME_LAT` / `HOME_LON` → necesarios para el cálculo de distancia.

### 9.2 Canales Mesh de emergencia

Puedes decidir a qué canales Mesh reenviar una emergencia:

- `MESH_EMERGENCY_CHANNELS` vacío → SOLO reenvía al canal indicado por el propio mensaje si viene marcado con `[CH x]`.
- `MESH_EMERGENCY_CHANNELS=0,3,5` → reenvía la emergencia a esos canales (multicanal), además de la lógica de canal si aplica.

### 9.3 Control de entrada APRS (seguridad mínima)

Para evitar que cualquier estación APRS controle tu malla:

- `APRS_ALLOWED_SOURCES` → lista blanca de indicativos permitidos.
  - Si está vacío, no filtra.
  - Si se define, solo aceptará tramas de esos indicativos.

### 9.4 Notificación a Telegram de emergencias APRS

Cuando se detecta emergencia, el gateway puede notificar por Telegram:

- `TELEGRAM_TOKEN` → token del bot.
- `TELEGRAM_EMERG_CHAT_IDS` → IDs de chat destino (si vacío, se usa `ADMIN_IDS`).

Se recomienda separar:

- `ADMIN_IDS` → administración del bot.
- `TELEGRAM_EMERG_CHAT_IDS` → donde se publican emergencias (grupos/canales/privado).

### 9.5 Variables del Gateway APRS implicadas

Estas variables están relacionadas con el flujo de emergencias y el enlace APRS:

- `APRS_GATE_ENABLED` (1/0) → habilita el gate APRS→Mesh.
- `APRS_DEBUG` (1/0) → depuración de tramas.
- `APRS_DEDUP_TTL` → antirebote para evitar doble TX.
- `APRS_CTRL_HOST` / `APRS_CTRL_PORT` → UDP local de control (bot → APRS).
- `KISS_HOST` / `KISS_PORT` → conexión a soundmodem/direwolf (KISS TCP).
- `APRS_CALL` → indicativo del gateway.
- `APRS_GATEWAY_PREFIX` → prefijo identificativo en mensajes.
- `APRS_PATH` → ruta RF (WIDE1-1,WIDE2-1...).
- `APRS_MSG_MAX` / `APRS_STATUS_MAX` → longitud máxima por trama.

APRS-IS (opcional):

- `APRSIS_USER`, `APRSIS_PASSCODE`, `APRSIS_HOST`, `APRSIS_PORT`, `APRSIS_FILTER`

### 9.6 Ejemplos operativos

#### Ejemplo A — Emergencia por palabra clave (APRS → Mesh + Telegram)

`.env`:

- `APRS_EMERGENCY_KEYWORDS=EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA`
- `APRS_EMERGENCY_DESTS=EMERGENCY,EMERG,SOS`
- `APRS_GATE_ENABLED=1`
- `MESH_EMERGENCY_CHANNELS=0,3`
- `HOME_LAT=41.638390`
- `HOME_LON=-0.903839`
- `APRS_EMERGENCY_MAX_KM=25`

Trama recibida (idea):

- Texto contiene: “MAYDAY, necesito ayuda en …”

Resultado:

- Clasificación: `reason=keyword`
- Se calcula distancia a HOME (si hay lat/lon), se marca `is_local` si aplica.
- Se reenvía a Mesh en canales 0 y 3.
- Se notifica a Telegram a `TELEGRAM_EMERG_CHAT_IDS`.

#### Ejemplo B — Emergencia por DEST (APRS DEST=EMERGENCY)

`.env`:

- `APRS_EMERGENCY_DESTS=EMERGENCY,EMERG,SOS`

Trama recibida:

- `DEST=EMERGENCY` aunque el texto no tenga palabras clave.

Resultado:

- Clasificación: `reason=dest=EMERGENCY`
- Reenvío a Mesh según `MESH_EMERGENCY_CHANNELS`.

#### Ejemplo C — Bot Telegram lanza /aprs y sale por RF (con troceo)

Uso típico:

- `/aprs canal 0 EMERGENCIA: corte eléctrico. Nodo solar activo. Punto de reunión: ...`

Comportamiento:

- El bot envía por UDP al gateway APRS.
- El gateway convierte a ASCII seguro y **trocea** si supera `APRS_MSG_MAX`.
- TX por KISS (soundmodem/direwolf) y opcionalmente uplink APRS-IS si está configurado.

#### Ejemplo D — Gate APRS→Mesh desactivado (modo silencioso)

`.env`:

- `APRS_GATE_ENABLED=0`

Comportamiento:

- Las tramas APRS se reciben, pero **no** se reinyectan a Mesh.
- La salida Mesh→APRS sigue funcionando si se usa `/aprs`.

### 9.7 Checklist rápido de emergencia (pruebas)

1) Verifica que el broker está vivo:

```
echo '{"cmd":"BROKER_STATUS"}' | nc 127.0.0.1 8766
```

2) Verifica que el gate APRS está activo:

- `APRS_GATE_ENABLED=1`
- `APRS_DEBUG=1` temporalmente para ver tramas

3) Verifica que hay ruta KISS:

- `KISS_HOST` accesible
- `KISS_PORT=8100` abierto

4) Verifica notificación Telegram:

- `TELEGRAM_TOKEN` correcto
- `TELEGRAM_EMERG_CHAT_IDS` o `ADMIN_IDS` configurados

---

## 10. Buenas prácticas

- Un solo broker por nodo
- No abrir TCP desde bot
- Usar siempre BacklogServer
- Monitorizar `BROKER_STATUS`
- Logs rotados

---

## 10. Estado del sistema

Comando rápido:

```
echo '{"cmd":"BROKER_STATUS"}' | nc 127.0.0.1 8766
```

---

## 11. Archivos implicados

| Archivo | Rol |
|------|----|
| `Meshtastic_Broker.py` | Núcleo |
| `broker_task.py` | Tareas |
| `bridge_in_broker.py` | Bridges |
| `meshtastic_to_aprs.py` | APRS |
| `.env` | Configuración |

---

## 12. Conclusión

El Broker JSONL es el **corazón operativo** del proyecto. Su correcta configuración garantiza estabilidad, resiliencia y capacidad de operación en escenarios normales y de emergencia.

