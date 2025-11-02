# 🌐 Meshtastic Broker + APRS Gateway + Telegram Bot (Docker)

Este proyecto proporciona un **stack completo** basado en Docker con tres servicios principales:

- 🔌 **Broker** → Conecta al nodo Meshtastic y expone una API JSONL.  
- 📡 **APRS Gateway** → Pasarela bidireccional entre Meshtastic y APRS (vía KISS TCP).  
- 🤖 **Telegram Bot** → Control remoto y consulta del estado de la red Meshtastic desde Telegram.  

---

### 🌐 Guía BROKER

> 🔄 Documentación detallada del sistema del BROKER  
> incluyendo configuración, ejemplos, variables de entorno y modos de operación.

📘 **[Abrir guía completa → BROKER_README.md](./docs/BROKER_README.md)**

---

### 🌐 Guía configuración del BOT

> 🔄 Documentación detallada del sistema del BOT  
> incluyendo configuración, ejemplos, variables de entorno y modos de operación.

📘 **[Abrir guía completa → BOT_README.md](./docs/BOT_README.md)**

---


### 🌐 Guía APRS Gateway

> 🔄 Documentación detallada del sistema de pasarela entre Meshtastic y APRS,  
> incluyendo configuración, ejemplos, variables de entorno y modos de operación.

📘 **[Abrir guía completa → APRS_GATEWAY.md](./docs/APRS_GATEWAY.md)**

---


👉 No se expone el código fuente. Todo se distribuye mediante **imágenes Docker** publicadas en **GitHub Container Registry (GHCR)**.

## 🧾 Historial de versiones

### 🆕 **v6.1.2** — _“Corrección de grabación offline y compatibilidad”_ (Noviembre 2025)

#### ✨ Novedades principales
- **Corrección del registro de mensajes offline**
  - Se restaura la grabación correcta de mensajes en `broker_offline_log.jsonl` cuando se detiene la escucha con `/parar_escucha`.
  - Los mensajes recibidos durante la pausa se reenvían automáticamente al reanudar la escucha con `/escuchar`.
  - Recupera el comportamiento estable de la versión **6.0**, garantizando compatibilidad total con el bot.

- **Mejoras en `append_offline_log()`**
  - Acepta ambos formatos de entrada:
    - Formato **plano** (v6.0): `{"portnum": "TEXT_MESSAGE_APP", "text": "..."}`
    - Formato **anidado** (v6.1): `{"packet": {"decoded": {...}}}`
  - Mantiene los nombres de campos anteriores (`rx_time`, `channel`, `portnum`, `from`, `to`, `text`, etc.)
    e incluye los nuevos (`type`, `lat`, `lon`, `battery`, etc.) usados por el panel web.
  - Soporta también tramas `POSITION_APP`, `TELEMETRY_APP` y `NODEINFO_APP`.

- **Sin cambios rompientes**
  - Los comandos `/parar_escucha` y `/escuchar` vuelven a funcionar igual que en la versión 6.0.
  - El panel web y las integraciones existentes siguen funcionando sin modificaciones.

#### 🧰 Cambios técnicos
- **Actualizado:** `Meshtastic_Broker.py`
  - Se reescribió `append_offline_log()` para fusionar compatibilidad entre versiones antiguas y nuevas.
  - Se añadieron lecturas de campos en nivel superior (`portnum`, `text`, `rx_rssi`, `rx_snr`, `channel`).
  - Se mantiene la rotación del archivo JSONL (`broker_offline_log.jsonl`, copia `.1`).

- **Sin cambios:**  
  `Telegram_Bot_Broker.py`, `docker-compose.yml`, `.env`

#### ✅ Resultado
- Los mensajes recibidos mientras la escucha está detenida vuelven a grabarse correctamente.  
- Al reanudar la escucha, el bot reenvía los mensajes pendientes.  
- El panel web continúa leyendo el archivo JSONL sin necesidad de cambios.

---


##  🟢 v6.1.1 (Octubre 2025)

> [Ver CHANGELOG completo →](./docs/CHANGELOG_v6.1.1.md)

Principales mejoras:
- Mayor estabilidad del broker TCP.
- Nuevo sistema de notificaciones persistentes en el bot.
- Integración ampliada APRS bidireccional.
- Resiliencia avanzada (CircuitBreaker + Watchdog).
- Bridge A↔B optimizado entre presets distintos.

- **Bridge embebido más robusto (A→B)**:
  - **Detección de peer caído** (lado B) y **supresión de reenvíos** durante un **backoff configurable**.
  - **Marcado de caída** solo si falla un envío A→B; **limpieza automática** al primer éxito posterior.
  - **Estado visible en `status()`**: `peer_offline_until`, `peer_offline_remaining`, `peer_down_backoff_sec`, `is_peer_suppressed`.
- **Limpieza de imports**: eliminado `PoolTCPIF` no usado en `bridge_in_broker.py`.
- **Mejoras de logging**: trazas explícitas `SKIP (B offline, Ns restantes)` y mensajes de transición `B OFFLINE → ...` / `B volvió ONLINE → ...`.

### Variables nuevas / modificadas
| Variable | Desde | Descripción |
|---------|------|-------------|
| `BRIDGE_PEER_DOWN_BACKOFF` | v6.1.1 | Segundos de “gracia” tras detectar que **B** está caído (por defecto `60`). Durante este tiempo no se reintentan envíos A→B. |

**Ejemplo en `.env`:**
```env
# --- Bridge embebido ---
BRIDGE_PEER_DOWN_BACKOFF=60
```

### 🟢 v6.1 (Octubre 2025)
- Añadido bridge embebido y externo.
- Mejoras APRS (eco, troceo, APRS‑IS).
- Comandos `/bloquear`, `/reconectar`, `/tareas`, `/diario`.
- Cooldown y guards TCP integrados.
- Persistencia de nodos y backlog extendida.
- Ficheros `.env` ampliados con nuevas variables.

### 🟣 v6.0 (Septiembre 2025)
- Integración estable broker + bot + APRS.
- Sistema de tareas persistentes.
- Notificaciones y logs mejorados.
- Docker Compose optimizado.

---

## 🚀 Requisitos

- **Docker** y **Docker Compose v2** (o `docker compose` integrado).
- [Docker](https://docs.docker.com/get-docker/)  
- [Docker Compose](https://docs.docker.com/compose/install/)  

- Un **nodo Meshtastic** accesible por TCP (normalmente en `IP_DEL_NODO:4403`).
- (Opcional) Un **TNC KISS por TCP** (ej. Direwolf o Soundmodem) en el host: `host.docker.internal:8100` en Windows/macOS o `127.0.0.1:8100` en Linux.
- (Opcional) Credenciales de **APRS-IS** (indicativo con SSID y *passcode*) para subir posiciones etiquetadas.
- Un **bot de Telegram** (Token) y, opcionalmente, lista de administradores.


## 📦 Instalación

1. Clonar este repositorio:

```bash
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot

Nota: Actualizar a una nueva version:
 docker compose pull
 docker compose up -d

.git
cd MeshNet-Bot

Nota: Actualizar a una nueva version:
 docker compose pull
 docker compose up -d


```
2. Copiar el archivo de variables de entorno y editarlo con tus datos:

```bash
cp .env-example .env
# Abre .env y rellena al menos:
# - MESHTASTIC_HOST=...
# - TELEGRAM_TOKEN=...
# - ADMIN_IDS=...
# Recomendado en Docker: DISABLE_BOT_TCP=1
# (Opcional APRS/Bridge: KISS_HOST, KISS_PORT, BRIDGE_ENABLED, B_HOST, etc.)

```
3. Descargar las imágenes de GHCR

```bash
docker compose pull

```
4.- Levantar servicios
```bash
Levantar todo:
 docker compose up -d

levantar por partes:
 docker compose up -d broker
 docker compose up -d bot

# (Opcional APRS) Sólo radioaficionados con indicativo.
 docker compose up -d aprs

```
4. Ver logs
```bash
docker compose logs -f broker
docker compose logs -f bot
docker compose logs -f aprs

 Consejo: Si vas a usar **Direwolf**/**Soundmodem** en el host, arráncalo primero y verifica que el puerto TCP (p.ej. 8100) está escuchando.
```

5. Actualizar a una nueva versiön
```bash
git pull              # (si quieres actualizar el código/compose)
docker compose pull   # baja nuevas imágenes desde GHCR
docker compose up -d  # recrea con la nueva versión

 Consejo: Si vas a usar **Direwolf**/**Soundmodem** en el host, arráncalo primero y verifica que el puerto TCP (p.ej. 8100) está escuchando.
```

## ⚙️ Variables de entorno (`.env`)

Crea un archivo `.env` en la raíz (puedes partir de `.env-example.txt`). Mínimo, ajusta estos campos:

| Clave | Descripción | Ejemplo |
|---|---|---|
| `MESHTASTIC_HOST` | IP/host del nodo Meshtastic (TCPInterface, normalmente 4403) | `192.168.1.201` |
| `BROKER_PORT` | Puerto **JSONL** del broker hacia clientes (bot/APRS) | `8765` |
| `BACKLOG_PORT` | Puerto **backlog/ctrl** del broker (UDP/TCP) | `8766` |
| `TELEGRAM_TOKEN` | Token del bot de Telegram | `123456:ABC...` |
| `ADMIN_IDS` | Lista de IDs (coma/; separada) con rol administrador | `1111,2222` |
| `KISS_HOST` | Host del TNC KISS TCP | `host.docker.internal` (Windows/macOS) / `127.0.0.1` (Linux) |
| `KISS_PORT` | Puerto del TNC KISS TCP | `8100` |
| `MESHTASTIC_CH` | Canal lógico por defecto para inyección desde APRS si no hay etiqueta | `0` |
| `BOT_START_DELAY` | Segundos que el bot espera al iniciar (permitir enlazar el nodo) | `90` |

**Parámetros APRS opcionales (si subes a APRS‑IS):**

| Clave | Descripción | Ejemplo |
|---|---|---|
| `APRSIS_USER` | Indicativo-SSID con el que subir a APRS‑IS | `EB2XXX-10` |
| `APRSIS_PASSCODE` | *Passcode* asociado a tu indicativo | `12345` |
| `APRSIS_HOST` | Servidor APRS‑IS | `rotate.aprs2.net` |
| `APRSIS_PORT` | Puerto APRS‑IS | `14580` |
| `APRSIS_FILTER` | Filtro APRS-IS opcional | `m/50` |

**Ajustes KISS (10 ms/unidad):** `KISS_TXDELAY=30` (300 ms), `KISS_PERSIST=200`, `KISS_SLOTTIME=10`, `KISS_TXTAIL=3`.

**Control y red del broker (avanzado):**

- `BROKER_HOST` / `BROKER_CTRL_HOST`: cómo se conectan bot/APRS al broker dentro de Docker. Por defecto, el compose los resuelve por nombre de servicio.
- `DISABLE_BOT_TCP=1`: evita doble sesión TCP del **bot** al nodo cuando ya existe la del **broker**.
- En la pasarela APRS se usa `network_mode: "service:broker"` para **compartir la red** del broker y enlazarlo por `127.0.0.1`.

> **Windows/macOS:** usa `host.docker.internal` para que el contenedor alcance el TNC KISS del host.
>
> **Linux:** usa `127.0.0.1` solo si compartes *network namespace* con el broker; si no, mapea el puerto del host (`-p 8100:8100`).

### FUNCIONES PRINCIPALES DEL BOT

### Mensaje diario automático por horas separados por comas
```text
/diario <HH:MM[,HH:MM,...]> [mesh|aprs|ambos] [grupo <id>]
            <destino[:canal] | canal N | CALL|broadcast> [aprs <CALL|broadcast>:] <texto…>

    Ejemplos:
      /diario 09:00 mesh canal 2 Parte diario Mesh
      /diario 08:00,12:30 ambos grupo fiestas2025 canal 2 aprs EA1ABC: Programa de fiestas
      /diario 18:45 aprs EA1ABC: Aviso para APRS
```
👉 Creará una tarea **diaria** a las 12:00 (hora local). Revisa `/tareas` para ver su ID y estado. Para detenerla: `/cancelar_tarea <id>`.

### Envío múltiple por minutos separados por comas
```text
 /en <minutos|m1,m2,...> <destino[:canal] | canal N> <texto…>
    Ejemplos:
      /en 15 canal 0 Buenos días a todos
      /en 5 !b03df4cc:1 Aviso rápido
      /en 5,10,25 canal 0 Mensaje      ← múltiples envíos programados
```

### Envío directo 
```text
  /enviar canal <n> <texto>
    /enviar <número|!id|alias> <texto>
    - NO refresca nodos ni llama a API; usa sólo nodos.txt (cargar_aliases_desde_nodes).
    - Envío priorizando la cola del BROKER (dispara bridge A→B) con fallback al pool y adapter resiliente.
    - Broadcast (node_id=None) sin ACK; unicast sin ACK aquí (para evitar duplicados).
    - Añade feedback local: '✅ Nodo local confirmó transmisión' si ok y hay packet_id.
```

### Envío directo con ACK
```text
  /enviar_ack [reintentos=N espera=S backoff=X] <dest|broadcast[:canal] | canal N> <texto…>
      - Unicast (!id/alias/índice): intenta usar broker-queue con ACK; si no está disponible, usa pool con waitForAck y fallback de reintentos.
      - Broadcast (explícito o 'canal N'): no existe ACK de aplicación → broker-queue primero para disparar bridge A→B.
    """
```

### Programar mensajes 
```text
/programar <YYYY-MM-DD HH:MM> <destino[:canal] | canal N> <texto...> [ack]
    Ejemplos:
      /programar 2025-09-02 09:30 canal 0 broadcast Buenos días a todos
      /programar 2025-09-02 21:45 !b03df4cc:1 Aviso crítico ack
    ZH: Europe/Madrid (por defecto). Guarda en bot_data/scheduled_tasks.jsonl.
    
```

### Envío de mensajes por APRS
```text
Formatos aceptados (inmediato):
      • /aprs canal N <texto>
      • /aprs N <texto>
      • /aprs <CALL|broadcast>: <texto> [canal N]
    Formatos nuevos (programado; múltiple con comas):
      • /aprs en M canal N <texto>         (M = 5  o  5,10,25)
      • /aprs en M N <texto>               (atajo: N equivale a 'canal N')
    Troceo APRS inmediato: si el texto excede APRS_MAX_LEN (p.e. 67), se trocea.
    
```

### Activar el GATE APRS -> MESH (tráfico recibido en APRS se reenciará a la malla)
```text
/aprs_on
    Activa el gate APRS→Mesh (tráfico recibido en APRS se reenviará a la malla).

/aprs_off
    Desactiva el gate APRS→Mesh.
    
```

### Programar mensaje para ENVIAR MAÑANA
```text
/mañana <HH:MM> <destino[:canal] | canal N> <texto…>
    Ejemplos:
      /mañana 09:30 canal 0 Buenos días
      /mañana 21:45 !b03df4cc:1 Aviso crítico
    Programa un mensaje para mañana a la hora indicada.
    
```
### Ver las TAREAS PROGAMADAS y CANCELAR TAREAS
```text
/tareas [pending|done|failed|canceled]
    Lista tareas desde bot_data/scheduled_tasks.jsonl

/cancelar_tarea
    Cancelar tarea por ID mostrado en TAREAS PROGRAMADAS
    
```

### Escuchar canal o canales en directo
```text
 Suscribe este chat a los mensajes TEXT_MESSAGE_APP del broker.
    Uso: /escuchar [N|all]
      - N   → escuchar solo ese canal lógico
      - all → escuchar todos los canales

    Cambios:
    - Evita escuchas duplicadas por chat.
    - Lanza una task asyncio propia que conecta al broker y reenvía mensajes.
    - Guarda estado y task en context.chat_data para poder parar luego.
    
```
### Parar Escuchar sobre canal o canales en directo
```text
Detiene la escucha activa de este chat.
    - Cancela la task de escucha si existe.
    - Cierra el writer TCP si está abierto.
    - Limpia el flag context.chat_data["listen_state"].
    - Informa del canal que estaba en escucha (o 'todos los canales').
    
```

### Traceroute y Traceroute Status
```text
/traceroute <!id|alias>  [timeout_s]
      - Prefiere ejecutar el traceroute vía broker (BacklogServer) y leer los TRACEROUTE_APP del backlog.
      - Si el broker no puede lanzarlo, fallback CLI con: PAUSAR → ejecutar CLI → REANUDAR.

/traceroute_status [N]
    /traceroute_status <!id|alias>
      - Sin args: muestra el último registro.
      - Con N: muestra los últimos N (máx 10).
      - Con !id|alias: muestra el último para ese destino.
    
```

### Telemetria
```text
/telemetria [!id|alias] [mins|max_n] [timeout]
      - Sin destino: listado rápido de métricas "en vivo" (pool persistente), ordenado por recencia.
        * [max_n] (opcional) limita filas. [timeout] (opcional) espera pool.
      - Con destino (!id o alias): métricas "en vivo" + HISTÓRICO desde el broker (FETCH_TELEMETRY).
        * [mins] (opcional) ventana en minutos para el histórico (por defecto 30 min).
        * [timeout] (opcional) espera pool.
      Campos habituales si existen: SNR, RSSI, batería/voltaje, temperatura, airmon, etc.
    
```

### Ver nodos recibidos 
```text
/ver_nodos [max_n] [timeout]
      - Lee nodos del pool persistente, sin abrir nuevas conexiones al 4403.
      - Orden por recencia (más recientes primero).
      - Muestra alias, !id, SNR y 'visto hace'.
    
```

### Ver nodos vecinos 
```text
/vecinos [max_n] [hops_max]
    - Sin args: muestra como /ver_nodos pero aplicable a 'vecinos' (sin filtro de hops).
    - 1er arg numérico: max_n
    - 2º arg numérico: hops_max (mantiene solo hops <= hops_max)
    
```
### Ver las programaciones DIARIAS realizadas para enviar  mensajes 
```text
/mis_diarios [estado] [grupo <group_id>]
    Lista las tareas que tienen meta.repeat == 'daily'.
    Estados: pending|done|failed|canceled (por defecto: pending)
    Filtro opcional por grupo: daily_group_id
    
```
### PARAR un GRUPO DIARIO realizado por NOMBRE DE GRUPO 
```text
/parar_diario_grupo <group_id>
    Cancela todas las tareas diarias asociadas a ese grupo.
    
```
### PARAR un ENVIO DIARIO realizado por ID 
```text
/parar_diario <task_id>
    Alias de cancelar para tareas diarias (pero sirve para cualquier task ID).
    
```

### Ver el ESTADO LORA del nodo 
```text
/lora status
    /lora ignore_incoming on|off
    /lora ignore_mqtt on|off
    /lora set ignore_incoming=on ignore_mqtt=off
    
```

### Ver las POSICIONES DE NODOS y POSICIONES EN HEADMAP
```text
/position <N>[min] | /position <|id|alias>[min][N]
    Ver últimas posiciones de nodos recibidos en mapa headmap

/position_mapa <kml|gpx> [N] [min]
    Ver últimas posiciones de nodos recibidos en mapa headmap
    
```

### Ver la COBERTURA de los nodos recibidos
```text
/cobertura [!id|alias] [Xh] [entorno]
      - Genera un mapa de cobertura a partir del BacklogServer (sin abrir sockets al nodo).
      - HTML: Heatmap + Círculos (si Folium). KML: polígonos circulares + pines.
      - 'entorno' ∈ {urbano, suburbano, abierto}. Por defecto: urbano.
      - Ejemplos:
        /cobertura
        /cobertura 12h
        /cobertura !9eeb1328 48h suburbano
        /cobertura Quasimodo abierto
    
```

### Ver los CANALES configurados en el nodo
```text
/canales — Muestra lista de canales (número + nombre/PSK si existe).
    Intenta reutilizar la interfaz del pool; si no está lista, fuerza ensure_connected
    y recurre a las rutas alternativas del pool (session/run_with_interface/acquire/get).
    
```

### RECONECTAR EL NODO
```text
/reconectar [seg]
    Fuerza reconexión del broker 
    
```

### Activar/Desctivaciones de avisos de tareas
```text
/notificaciones [on|off|estado]  → Activa/Desactiva o muestra el estado
    Alias: /notify, /notifs
    Solo administradores (ADMIN_IDS).
    
```
### Bloquear/Desbloquear nodos por su ID's
```text
/bloquear <id1,id2,...>     → añade IDs
    /bloquear lista             → lista IDs actuales
    (solo admin)

/desbloquear <id1,id2,...>  (solo admin)
    
```
### Estadística
```text
/estadistica 
    Uso del bot
```

### Ayuda
```text
/ayuda 
    Ayuda completa de comandos y parámetros
```

### Mostrar menú principal
```text
/start 
    Muestra el menú principal

/menu
    Abre el menú principal
```


## 🔗 Bridge A↔B (Embebido y externo)
👉  Permite enviar y recibir mensajes de uno nodo a otro y viceversa con diferentes preset

### Embebido (en el broker)
Activa en `.env`:
```bash
BRIDGE_ENABLED=1
A_HOST= ip del primer nodo
B_HOST= ip del segundo nodo
A2B_CH_MAP=0:0,1:1,2:2  <- Indica los canales imagen entre nodos (por defecto correspondenrán, aunque sean diferntes preset)
B2A_CH_MAP=0:0,1:1,2:2  <- Indica los canales imagen entre nodos (por defecto correspondenrán aunque sean diferntes prest)
RATE_LIMIT_PER_SIDE=8
DEDUP_TTL=45
TAG_BRIDGE=[BRIDGE]
```

### Externo
```bash
python mesh_preset_bridge.py --a 'ip del primer nodo' --b 'ip del segundo nodo'
```

Ambos bridges:
- Filtran duplicados (`DEDUP_TTL`)
- Limitan tráfico (`RATE_LIMIT_PER_SIDE`)
- Mantienen logs detallados

---

## 🧩 Servicios y puertos

- **broker**
- Imagen: `ghcr.io/jmmpcc/meshtastic-broker:latest`  
- Función: conecta al nodo Meshtastic y expone la API JSONL.  
- Puertos:
  - `8765` → Broker JSONL
  - `8766` → Backlog server (control interno)
  - Expone JSONL en `:8765` (por defecto) y **backlog/ctrl** en `:8766`.
  - Lee del nodo Meshtastic por TCP (`MESHTASTIC_HOST:4403`).
  - Persiste posiciones y tareas en `./bot_data` (volumen mapeado).

- **bot**
- Imagen: `ghcr.io/jmmpcc/meshtastic-bot:latest`  
- Función: control remoto vía comandos de Telegram.  
- Necesita el token del bot (`TELEGRAM_TOKEN`) y los IDs de administradores (`ADMIN_IDS`).  
  - Habla con el broker (`BROKER_HOST:8765`) y con backlog/ctrl (`:8766`).
  - Comandos principales: `/start`, `/menu`, `/ver_nodos`, `/vecinos`, `/traceroute`, `/telemetria`, `/enviar`, `/enviar_ack`, `/programar`, `/en`, `/manana`, `/tareas`, `/position`, `/position_mapa`, `/cobertura`, `/aprs`, `/aprs_on`, `/aprs_off`, `/estado`, `/reconectar`.
  - Usa `BOT_START_DELAY` para dar tiempo a que el broker enlace con el nodo.

- **aprs** SOLO DISPONIBLE PARA RADIOAFICIONADOS CON INDICATIVO
- Imagen: 
- Función: puente bidireccional entre Meshtastic y APRS (vía KISS TCP). 
  - **KISS TCP** hacia tu TNC: `KISS_HOST:KISS_PORT`.
  - **Control UDP** (desde el bot) en `127.0.0.1:9464` (compartiendo red con broker).
  - **Broker JSONL** en `127.0.0.1:8765` (compartiendo red con broker).
  - Sube a **APRS‑IS** si `APRSIS_USER` y `APRSIS_PASSCODE` están definidos.
  - **Reinyecta a malla SOLO** tramas que lleven `[CHx]` o `[CANAL x]` en el comentario.


## 🗂 Estructura de volúmenes y datos

- `./bot_data/positions.jsonl` y `positions_last.json` — últimas posiciones.
- `./bot_data/scheduled_tasks.jsonl` — planificador de mensajes.
- `./bot_data/maps/` — salidas de cobertura (HTML/KML) si generas mapas desde el bot.

> Puedes montar `bot_data` como volumen para persistir datos entre reinicios.


## 📦 Ejemplos de `docker compose`

Los servicios están definidos para que:

- `bot` y `aprs` **dependan** de `broker`.
- `aprs` use `network_mode: "service:broker"` (misma pila de red); así puede hablar con broker por `127.0.0.1`.
- Variables del `.env` prevalezcan sobre valores del YAML.

> Si no deseas APRS, puedes levantar solo `broker` y `bot`.


## 🔐 Seguridad / buenas prácticas

- El *token* de Telegram y el *passcode* de APRS‑IS **no deben** enviarse a git; guárdalos solo en `.env`.
- Usa **IDs de admin** reales para limitar comandos avanzados.
- Mapea puertos de broker solo dentro de tu red local a no ser que necesites acceso externo.


## 🧪 Pruebas rápidas

1) **Bot operativo**

- En Telegram: `/estado` → debe listar latencia y servicios.
- `/ver_nodos` → muestra nodos; `/vecinos` → directos; `/traceroute !id`.

2) **APRS**

- Con TNC activo, desde el bot: `/aprs 0 Hola APRS` ⇒ deberías ver la trama en el TNC.
- Para uplink APRS‑IS: define `APRSIS_USER` y `APRSIS_PASSCODE`; solo suben **posiciones** con `[CHx]`.

3) **Programación**

- `/en 5 canal 0 Recordatorio` ⇒ mensaje en 5 minutos por canal 0.
- `/tareas` para revisar estado.


## 🛠 Solución de problemas

- **El bot no “responde” inmediatamente**: respeta `BOT_START_DELAY` para dar tiempo a que el broker enlace con el nodo.
- **El APRS no transmite**: verifica `KISS_HOST:KISS_PORT`, que el TNC acepte KISS por TCP y que el contenedor pueda llegar (Windows/macOS → `host.docker.internal`).
- **No quiero reinyectar todo APRS a la malla**: la pasarela **solo** reinyecta si hay etiqueta `[CHx]` en el comentario (`[CANAL x]` también válido).
- **Duplicados**: el sistema hace *de‑dup* básico en APRS y gestiona ACKs por aplicación para minimizar repeticiones.
- **Heartbeat del SDK**: el broker incluye *guards* para proteger `sendHeartbeat` y evitar olores a *loopback*.


## 📥 Actualización

Para actualizar a la última versión publicada en GHCR:

```bash
docker compose pull
docker compose up broker
docker compose up bot
```

## 📜 Comandos del bot: guía completa

> Todos los comandos funcionan en chats privados con el bot y en grupos donde esté presente. Los ejemplos muestran el **mensaje que envías a Telegram** y un **resumen de lo que hace**.

> Notas generales:
> - Si el comando acepta `!id` o alias, el alias debe existir en el fichero de nodos (o haber sido visto recientemente por el broker).
> - Cuando procede, el bot **pausa** momentáneamente la sesión del broker para ejecutar CLI y luego **reanuda** (evita duplicar conexiones al 4403).
> - La mayoría de listados aceptan límites (`max_n`) y `timeout` para esperar datos del pool.

### 🧭 `/menu` y `/start`
Muestra el menú contextual oficial (Telegram **SetMyCommands**) según tu rol (admin/usuario) y un resumen rápido del sistema.
- **Ejemplo:**
  - Tú: `/start`
  - Bot: «Bienvenido… usa /menu para ver opciones». 

### 🆘 `/ayuda`
Ayuda corta con enlaces y recordatorio de los comandos más usados.

### 🛰️ `/estado`
Resumen del estado del sistema: latencia de respuesta del nodo, estado del broker, bot y APRS.
- **Ejemplo:**
  - Tú: `/estado`
  - Bot: «Broker OK (JSONL :8765, CTRL :8766) • Nodo enlazado • APRS: KISS conectado…»

### 📡 `/ver_nodos [max_n] [timeout]`
Lee los **últimos nodos** del **pool persistente** (no abre sesión nueva). Orden por recencia; muestra alias, `!id`, SNR y “visto hace”.
- **Ejemplos:**
  - `/ver_nodos` → top recientes.
  - `/ver_nodos 30 4` → hasta 30 nodos, esperando hasta 4 s al pool.

### 🤝 `/vecinos [max_n] [hops_max]`
Lista **vecinos** vistos (recientes) con su número de **saltos (hops)**, SNR y recencia. Usa broker/pool; no abre TCP nuevo.
- **Ejemplos:**
  - `/vecinos` → directos por defecto (hops 0) o configuración actual.
  - `/vecinos 20 2` → hasta 20 nodos con **hops ≤ 2**.
  - Alias/SNR y “visto hace” aparecen en salida.

### 🛰️🍞 `/traceroute <!id|alias> [timeout]`
Ejecuta **traceroute** hacia un nodo. El bot **pausa** el broker, lanza CLI `meshtastic --traceroute`, parsea los saltos y **reanuda** el broker.
- **Ejemplos:**
  - `/traceroute !06c756f0` → muestra cadena de saltos.
  - `/traceroute Zgz_Romareda 35` → con timeout 35 s.

### 📶 `/telemetria [!id|alias] [mins|max_n] [timeout]`
- **Sin destino**: listado rápido de **métricas en vivo** del pool (orden por recencia). `max_n` limita filas.
- **Con destino** (`!id`/alias): mezcla **en vivo + histórico** (FETCH_TELEMETRY en broker) en una ventana de `mins` (por defecto 30).
- **Campos** comunes: SNR, RSSI, batería/voltaje, temperatura, airmon, etc.
- **Ejemplos:**
  - `/telemetria` → top métricas recientes.
  - `/telemetria !06c756f0 20 4` → histórico 20 min, timeout 4 s.

### ✉️ `/enviar canal <n> <texto>` y `/enviar <número|!id|alias> <texto>`
Envío rápido por **canal** (broadcast) o **unicast** por `!id/alias`.
- Usa **nodos.txt** / pool (sin refrescar por API) para evitar múltiples conexiones.
- Reintento resiliente 1 vez si hay reconexión de pool.
- Broadcast: **sin ACK**; Unicast: sin ACK (evita duplicados). El adapter añade feedback local si hay `packet_id`.
- **Ejemplos:**
  - `/enviar canal 0 Hola red` → broadcast por canal 0.
  - `/enviar !ea0a8638 Prueba directa` → unicast por `!id`.
  - `/enviar Zgz_Romareda Mensaje` → unicast por alias.

### ✅ `/enviar_ack <número|!id|alias> <texto>`
Como `/enviar` unicast pero solicitando **ACK** de aplicación. El bot reporta confirmación si llega.

### ⏱️ `/programar`, `/en <min> canal <n> <texto>`, `/manana <hora> canal <n> <texto>`
Planificador de envíos diferidos y tareas.
- `/en 5 canal 0 Recordatorio` → en 5 minutos.
- `/manana 09:30 canal 0 Buenos días` → mañana a las 09:30.
- `/programar` → flujo guiado.
- `/tareas` → lista tareas con estados (`pending`, `sent`, etc.).
- `/cancelar_tarea <uuid>` → cancela.

### 👂 `/escuchar` y `/parar_escucha`
Pone al nodo en **modo escucha** un tiempo/condiciones definidas, y reporta nodos entrantes por consola/Telegram. Útil para descubrir vecinos.

### 🌐 `/canales`
Muestra/gestiona canal lógico por defecto y ayudas para **[CHx]**.

### 📍 `/position` y `/position_mapa`
- `/position` → posición actual/conocida, última hora y `!id`.
- `/position_mapa` → genera/enlaza mapa HTML/KML en `./bot_data/maps/`.

### 🗺️ `/cobertura [opciones]`
Genera **mapas de cobertura** (HTML/KML) a partir de posiciones/vistas conocidas. Archivos quedan en `./bot_data/maps/`.

### 🔌 `/reconectar`
Ordena al broker **reconectar** con el nodo (fuerza limpieza de cooldown si aplica).

### 📊 `/estadistica` *(solo admin)*
Muestra estadísticas de uso del bot por usuarios/fechas.

### 🪪 `/lora`
Información resumida del enlace LoRa y parámetros relevantes.

### 📡 APRS: `/aprs`, `/aprs_on`, `/aprs_off`, `/aprs_status`
**Puente APRS ⇄ Mesh** con etiqueta obligatoria para inyección a la malla.
- **Formatos admitidos** en `/aprs`:
  - `/aprs canal N <texto>` → broadcast a **canal N** y salida por APRS KISS.
  - `/aprs N <texto>` → atajo del anterior.
  - `/aprs <CALL|broadcast>: <texto> [canal N]` → compat dirigido o broadcast.
- **Troceo**: si el payload supera `APRS_MAX_LEN` (≈67), se divide en varias tramas.
- **Reinyección a malla**: **solo** si el comentario contiene `[CHx]` o `[CANAL x]`.
- **APRS‑IS**: si defines `APRSIS_USER`+`APRSIS_PASSCODE`, se suben **posiciones** etiquetadas.
- **Ejemplos:**
  - `/aprs canal 0 [CH0] Saludo` → emite por KISS y etiqueta para malla.
  - `/aprs EB2EAS-11: Mensaje a estación` → dirigido.
  - `/aprs_status` → estado de KISS/APRS‑IS.
  - `/aprs_on` / `/aprs_off` → habilita/inhabilita uplink a APRS‑IS.


### 🔒 Permisos y roles
- **Usuarios**: acceso a consultas estándar y envíos por canal.
- **Admins**: comandos de gestión (p.ej. `/estadistica`, `/reconectar`, cancelación de tareas) y opciones avanzadas del menú.

### Mensaje diario automático
```text
/diario 12:00 canal 2 Avisos del mediodía
```
👉 Creará una tarea **diaria** a las 12:00 (hora local). Revisa `/tareas` para ver su ID y estado. Para detenerla: `/cancelar_tarea <id>`.

### Envío múltiple por minutos separados por comas
```text
/en 5,10,25 canal 0 Recordatorio periódico
```
👉 Envía el mismo mensaje a los 5, 10 y 25 minutos.

---

### ✅ Buenas prácticas
- Evita spam de `/traceroute`: usa timeouts razonables (20–35 s) y recuerda que pausa/reanuda la sesión.
- Para **APRS**, configura bien KISS (`host.docker.internal:8100` en Windows/macOS) y etiqueta `[CHx]` para reinyectar a la malla.
- Define `BOT_START_DELAY` (p.ej. 90 s) para que el bot espere a que el broker enlace con el nodo al arrancar.



## 📝 Notas

- El código fuente **no está incluido** en este repo.  
- Todas las imágenes se publican automáticamente en **GitHub Container Registry (GHCR)** desde un repositorio privado. 

- IMPORTANTE: La pasarela de APRS solemtente estará diponible para usuarios RADIOAFICIONADOS CON    INDICATIVO. Ponerse en contacto con el autor: EB2EAS E-Mail: eb2eas@gmail.com para verificación y dar acceso a la imagen de la pasarela APRS.

- El uplink APRS‑IS está desactivado.

- Puedes inspeccionar y descargar las imágenes en:  
  👉 https://github.com/jmmpcc?tab=packages&repo_name=the-boss-docker_PUBLIC  


## 📄 Licencia

Este proyecto está disponible bajo licencia **MIT**. Repo  EB2EAS


## 🤖 Guía de comandos del Bot (v6.0)

> Todos los comandos se ejecutan desde Telegram, ya sea en chat privado con el bot o en grupos donde esté presente.

### 🧭 Comandos generales

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/menu` | Muestra el menú contextual oficial de Telegram con las opciones según tu rol (usuario/admin). | `/menu` |
| `/start` | Inicia la conversación con el bot y muestra un mensaje de bienvenida. | `/start` |
| `/ayuda` | Muestra una ayuda básica con los comandos disponibles. | `/ayuda` |
| `/estado` | Muestra el estado actual del sistema: broker, APRS, nodo y latencia. | `/estado` |
| `/reconectar` | Ordena al broker reconectar con el nodo Meshtastic. | `/reconectar` |


### 🌐 Nodos y red Mesh

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/ver_nodos [max_n] [timeout]` | Lista los últimos nodos escuchados por el broker. | `/ver_nodos 20 5` |
| `/vecinos [max_n] [hops_max]` | Lista vecinos detectados con sus hops y RSSI/SNR. | `/vecinos 30 2` |
| `/traceroute <!id|alias>` | Ejecuta un traceroute hasta un nodo. | `/traceroute !06c756f0` |
| `/telemetria [!id|alias] [minutos]` | Muestra métricas del nodo o red (batería, SNR, voltaje, temperatura, etc.). | `/telemetria !06c756f0 30` |

### ✉️ Envíos y mensajes

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/enviar canal <n> <texto>` | Envía un mensaje broadcast por canal N. | `/enviar canal 0 Hola red` |
| `/enviar <!id|alias> <texto>` | Envía un mensaje directo (unicast). | `/enviar Zgz_Romareda Mensaje` |
| `/enviar_ack <!id|alias> <texto>` | Envía mensaje unicast con confirmación ACK. | `/enviar_ack !06c756f0 Test` |


### 🕒 Programación y tareas

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/en <min> canal <n> <texto>` | Programa un mensaje para enviarse tras X minutos. | `/en 10 canal 0 Recordatorio` |
| `/manana <hora> canal <n> <texto>` | Programa mensaje a una hora concreta del día siguiente. | `/manana 09:30 canal 0 Buenos días` |
| `/programar` | Asistente paso a paso para crear una tarea. | `/programar` |
| `/tareas` | Lista tareas programadas pendientes, completadas o canceladas. | `/tareas` |
| `/cancelar_tarea <id>` | Cancela una tarea programada. | `/cancelar_tarea 1234abcd` |

> 💡 **Novedad v6.0:** Ahora puedes programar múltiples minutos separados por comas.
> Ejemplo: `/en 5,10,25 canal 0 Recordatorio` enviará el mensaje en 5, 10 y 25 minutos.
| `/diario <HH:MM> canal <n> <texto>` | Programa un envío **diario** a la hora local (Europe/Madrid). | `/diario 09:00 canal 2 Buenos días` |


### 📡 APRS

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/aprs canal <n> <texto>` | Envía mensaje APRS broadcast por canal N. | `/aprs canal 0 [CH0] Hola red` |
| `/aprs <CALL>: <texto>` | Envía mensaje dirigido a un indicativo APRS. | `/aprs EB2EAS-11: Saludos` |
| `/aprs_on` | Activa el envío de posiciones a APRS-IS. | `/aprs_on` |
| `/aprs_off` | Desactiva el envío de posiciones a APRS-IS. | `/aprs_off` |
| `/aprs_status` | Muestra estado de la pasarela APRS (KISS y APRS-IS). | `/aprs_status` |

> Solo los mensajes que contienen la etiqueta `[CHx]` o `[CANAL x]` se reinyectan desde APRS a la red Mesh.


### 📍 Posiciones y cobertura

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/position` | Muestra tu última posición conocida o la actual del nodo. | `/position` |
| `/position_mapa` | Genera un mapa HTML/KML con las posiciones conocidas. | `/position_mapa` |
| `/cobertura` | Genera mapa de cobertura a partir de posiciones y SNR. | `/cobertura` |


### 👂 Escucha activa

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/escuchar` | Activa modo escucha (el bot reporta nuevos nodos detectados). | `/escuchar` |
| `/parar_escucha` | Detiene el modo escucha. | `/parar_escucha` |



### 📊 Administrador (solo para `ADMIN_IDS`)

| Comando | Descripción | Ejemplo |
|----------|--------------|---------|
| `/estadistica` | Muestra estadísticas de uso del bot por usuario y fecha. | `/estadistica` |
| `/lora` | Muestra parámetros técnicos LoRa del nodo. | `/lora` |



## 🧾 Ejemplos rápidos

### Envío diferido y reintento resiliente
```text
/en 15 canal 2 Recordatorio de evento
```
👉 Envía un mensaje al canal 2 dentro de 15 minutos, incluso si el broker se reconecta entre tanto.

### Mensaje APRS con inyección a la malla
```text
/aprs canal 0 [CH0] Hola desde APRS
```
👉 Se emite por APRS KISS y se reinyecta a la red Mesh por el canal 0.

### Traceroute con pausa automática
```text
/traceroute !06c756f0
```
👉 El bot pausa el broker, ejecuta `meshtastic --traceroute`, y lo reanuda al terminar.

### Telemetría detallada de un nodo
```text
/telemetria !ea0a8638 60
```
👉 Muestra datos de batería, temperatura, SNR y voltaje de la última hora.

### Escucha temporal de vecinos
```text
/escuchar
# ... tras unos minutos ...
/parar_escucha
```
👉 Activa y detiene la escucha de nodos cercanos, mostrando su SNR y hops.

### Mensaje diario automático
```text
/diario 12:00 canal 2 Avisos del mediodía
```
👉 Creará una tarea **diaria** a las 12:00 (hora local). Revisa `/tareas` para ver su ID y estado. Para detenerla: `/cancelar_tarea <id>`.

### Envío múltiple por minutos separados por comas
```text
/en 5,10,25 canal 0 Recordatorio periódico
```
👉 Envía el mismo mensaje a los 5, 10 y 25 minutos.

---

