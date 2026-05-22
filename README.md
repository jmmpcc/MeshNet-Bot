# 🌐 Meshtastic Broker + APRS Gateway + Telegram Bot (Docker)

## Atribución obligatoria

Este proyecto ha sido desarrollado por **José Miguel Molina (EB2EAS)**.

Cualquier uso, redistribución, modificación, integración o creación de
derivados deberá incluir una referencia clara y visible al autor original
y un enlace al repositorio principal.

Ejemplo recomendado:
“Basado en el proyecto desarrollado por José Miguel Molina (EB2EAS)”.

## Condición para forks y proyectos derivados

Cualquier fork o proyecto derivado que emplee una parte sustancial de este
código deberá incluir en su README un apartado visible reconociendo al autor
original: “Proyecto basado en el trabajo de José Miguel Molina (EB2EAS)”.

Esta condición forma parte de la licencia MIT utilizada en este repositorio.


Este proyecto proporciona un **stack completo** basado en Docker con tres servicios principales:

- 🔌 **Broker** → Conecta al nodo Meshtastic y expone una API JSONL.  
- 📡 **APRS Gateway** → Pasarela bidireccional entre Meshtastic y APRS (vía KISS TCP).  
- 🤖 **Telegram Bot** → Control remoto y consulta del estado de la red Meshtastic desde Telegram.  

---

# MeshNet — Changelog Consolidado

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

Programa un mensaje diario hacia un canal MeshCore.

Sintaxis principal:

```text
/diario_mc <HH:MM[,HH:MM,...]> <chX|canal X> <texto>
```

Ejemplos:

```text
/diario_mc 09:00 ch2 Parte diario MeshCore
/diario_mc 09:00 canal 2 Parte diario MeshCore
/diario_mc 09:00,21:00 grupo avisos_mc canal 2 Parte diario MeshCore
```

La tarea queda identificada internamente con metadatos equivalentes a:

```python
meta = {
    "via": "/diario_mc",
    "repeat": "daily",
    "daily_time": "09:00",
    "transport": "meshcore",
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
> se incluye nuevas funciones de comunicación de EMERGENCIAS: APRS -> MESH <- APRS  
> incluyendo configuración, ejemplos, variables de entorno y modos de operación.

📘 **[Abrir guía completa → APRS_GATEWAY.md](./docs/APRS_GATEWAY.md)**

---

### 🛰️ Auditorías MeshNet

> Este documento describe las dos auditorías integradas en MeshNet:

- **Auditoría de Red (`auditoria_red`)**
- **Auditoría Integral (`auditoria_integral`)**

Ambas funciones analizan la información del backlog, nodos escuchados, métricas SNR/RSSI, distancias y rutas para generar un informe claro del estado real de la malla.

📘 **[Abrir guía completa → AUDITORIAS.md](./docs/AUDITORIAS.md)**

---

### 🌐 Guía Operación APRS + Mesh vía KISS Remoto

> 🔄 Guía oficial de despliegue en emergencias MeshNet The Boss

📘 **[Abrir guía completa → APRS_Remote_KISS_Emergency_Deployment.md](./docs/APRS_Remote_KISS_Emergency.md)**

---

### 🌐 Historial de versiones publicadas

> 🔄 Listado Historial de versiones publicadas

📘 **[Abrir Historial de versiones → Historial_Versiones.md](./docs/Historial_Versiones.md)**

---
# 🖥️ Instalación en Windows (Docker Desktop)


# ✔ Formas de ejecutar el proyecto en Windows

Existen **dos modos diferentes** de arrancar el sistema. Ambos funcionan correctamente, pero sirven para distintos casos.

---

# 🅰 Opción A — Construir localmente (modo recomendado para desarrollo)

Esta opción usa tu ordenador para construir las imágenes Docker con los Dockerfile del proyecto.

```powershell
docker compose up -d
```

### Ventajas:
- Perfecto si vas a modificar código Python o Dockerfiles.  
- Permite reconstruir rápidamente mientras desarrollas.  
- No dependes de internet para reconstrucciones posteriores.

### Inconvenientes:
- Construye las imágenes en tu PC.  
- No garantiza usar exactamente la misma imagen que en Raspberry.

---

# 🅱 Opción B — Usar imágenes oficiales precompiladas desde GHCR (modo “sin compilación”)

Aquí Windows **no construye nada**.  
Descarga directamente las imágenes multi-arch ya generadas por GitHub Actions:

```powershell
docker compose -f docker-compose.yml up -d
```

### Ventajas:
- Mucho más rápido.  
- Usa exactamente las mismas imágenes que Raspberry Pi.  
- No compila nada en tu ordenador.

### Inconvenientes:
- No recomendado si vas a modificar el código.  
- Depende de que el repositorio GHCR esté actualizado.

---

# ¿Qué opción elegir?

| Situación | Opción recomendada |
|----------|--------------------|
| Quieres modificar código o desarrollar | **Opción A (build local)** |
| Quieres instalar y usar sin complicaciones | **Opción B (GHCR)** |
| Notas que tu PC va justo de recursos | **Opción B (GHCR)** |
| Quieres que Windows use la misma imagen que Raspberry | **Opción B (GHCR)** |

---

# 🍓 Instalación en Raspberry Pi

Compatible con Raspberry Pi **2B**, **3**, **4**, **5**.  
La arquitectura correcta se selecciona automáticamente (arm/v7 o arm64).

---

### 🌐 Manual instalación MeshNet The Boss desde 0 en Rasp 2B/3/4/5

> 🔄 MANUAL de despliegue desde 0, en Raspberry PI

📘 **[Abrir MANUAL → Manual_Instalacion_MeshNet_RaspberryPi.md](./docs/Manual_Instalacion_MeshNet_RaspberryPi.md)**

---

## 1. Instalar Docker + Docker Compose Plugin
```bash
curl -sSL https://get.docker.com | sh
sudo apt install -y docker-compose-plugin
```

## 2. Clonar el repositorio y editar archivo .env de configuración (Ver paso 1º)
```bash
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
```

## 3. Descargar imágenes multi-arch desde GHCR
```bash
docker compose -f docker-compose.rpi.yml pull
```

## 4. Arrancar el sistema
```bash
  1.- Arrancar todos los contenedores
    
      docker compose -f docker-compose.rpi.yml up -d

  2.- Arrancar cada contenedor de manera individual

    Broker:
      docker compose -f docker-compose.rpi.yml up -d broker
    
    Bot:
      docker compose -f docker-compose.rpi.yml up -d bot
    
    Aprs:
      docker compose -f docker-compose.rpi.yml up -d aprs
    
    Bridgehub-bc:
      docker compose -f docker-compose.rpi.yml up -d bridgehub-bc

```

## 5. Comandos relacionados
```bash
  1.- Ver estado de todos los contenedores
    
      docker ps

  2.- Ver logs de un servicio concreto

      docker logs -f meshnet-broker
      docker logs -f meshnet-bot
      docker logs -f aprs-gateway
      docker logs -f meshtastic-bridge

  3.- Ver logs de TODOS los contenedores al mismo tiempo
  
      docker compose -f docker-compose.rpi.yml logs -f
  
  4.- Ver logs de cada UNO de los contenedores

      docker compose -f docker-compose.rpi.yml logs -f broker
      docker compose -f docker-compose.rpi.yml logs -f bot
      docker compose -f docker-compose.rpi.yml logs -f aprs
      docker compose -f docker-compose.rpi.yml logs -f bridgehub-bc

```
## 6. Si hicimos 'docker compose down'

```bash
  Arrancar solo uno:

    docker compose -f docker-compose.rpi.yml up -d broker

  Arrancar todos:

    docker compose -f docker-compose.rpi.yml up -d


```

---

# 🧩 Ficheros del proyecto

- **docker-compose.yml** → Uso general en Windows.  
- **docker-compose.rpi.yml** → Override para Raspberry Pi.  
- **Dockerfile / Dockerfile.aprs / Dockerfile.bridge** → Construcción por servicio.  
- **bot_data/** → Datos persistentes del bot.  
- **.github/workflows/** → Compilación multi-arch automática.

---

# 🔄 Actualización del proyecto

## Windows
```powershell
git pull
docker compose up -d --build
```

## Raspberry Pi
```bash
git pull
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
```

---

# 🧪 Logs

## Broker
```bash
docker compose -f docker-compose.rpi.yml logs -f broker
```

### Diagnóstico rápido: `[Errno 113] No route to host` hacia `MESHTASTIC_HOST:4403`

Si en el broker aparece algo como:

- `Conectando a Meshtastic en 192.168.1.127:4403…`
- `Fallo al crear interface (tcp): [Errno 113] No route to host`

el problema **no suele ser del bot**, sino de **ruta/red IP entre la Raspberry (host Docker) y el nodo Meshtastic**.

Comprobaciones recomendadas en la Raspberry (host):

```bash
# 1) Ver IP/ruta del host
ip route

# 2) Ver si la IP del nodo responde
ping -c 4 192.168.1.127

# 3) Verificar puerto Meshtastic TCP (debe estar abierto)
nc -vz 192.168.1.127 4403

# 4) Probar desde el namespace de red del broker
CID=$(docker compose -f docker-compose.rpi.yml ps -q broker)
docker exec -it "$CID" sh -lc 'ip route; nc -vz 192.168.1.127 4403'
```

Causas típicas:

- `MESHTASTIC_HOST` incorrecto o IP cambiada por DHCP.
- Nodo apagado/reiniciando o sin servicio TCP Meshtastic en `4403`.
- Raspberry en otra VLAN/subred sin ruta hacia `192.168.1.127`.
- Aislamiento Wi‑Fi/AP o reglas firewall que bloquean tráfico lateral LAN.

Acciones:

1. Fijar IP estática/reserva DHCP al nodo y actualizar `MESHTASTIC_HOST` en `.env`.
2. Confirmar en la app/CLI de Meshtastic que el nodo tiene habilitado el acceso TCP.
3. Reiniciar servicios tras cambios:
   ```bash
   docker compose -f docker-compose.rpi.yml down
   docker compose -f docker-compose.rpi.yml up -d
   ```
4. Si usas USB directo en la Raspberry, cambia a `MESH_TRANSPORT=usb` y define `MESH_USB_PORT=/dev/ttyACM0` para evitar dependencia de la red IP.

## Bot
```bash
docker compose -f docker-compose.rpi.yml logs -f bot
```

## APRS
```bash
docker compose -f docker-compose.rpi.yml logs -f aprs
```

# Parar los servicios
```bash
Parar todos los servicios:
   docker compose -f docker-compose.rpi.yml down

Parar servicio Broker:
   docker compose -f docker-compose.rpi.yml stop broker

Parar servicio bot:
   docker compose -f docker-compose.rpi.yml stop bot

Parar servicio aprs
   docker compose -f docker-compose.rpi.yml stop aprs


```

# 🐳 Cómo funcionan las imágenes multi-arch

GitHub Actions compila automáticamente para:

- `linux/amd64` (PC / Windows)  
- `linux/arm/v7` (Raspberry Pi 2B / 3)  
- `linux/arm64` (Raspberry Pi 4 / 5)  

y publica en GHCR:

```
ghcr.io/<usuario>/meshnet-bot-broker:latest
ghcr.io/<usuario>/meshnet-bot-bot:latest
ghcr.io/<usuario>/meshnet-bot-aprs:latest
ghcr.io/<usuario>/meshnet-bot-bridge:latest
```

Docker descarga la variante correcta según tu hardware.

---

# 🛠 Detener el sistema
```bash
  docker compose down
```

Con volúmenes:
```bash
  docker compose down -v
```

 Consejo: Si vas a usar **Direwolf**/**Soundmodem** en el host, arráncalo primero y verifica que el puerto TCP (p.ej. 8100) está escuchando.

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
# =========================
# Pasarela entre dos nodos
# =========================
# Nodo A (normalmente el que ya usas en MESHTASTIC_HOST)
A_HOST=${MESHTASTIC_HOST}
A_PORT=4403

# Nodo B (el segundo nodo, con preset distinto)
B_HOST=192.......
B_PORT=4403

# Mapeo de canales (A→B y B→A).
# Si ambos nodos usan los mismos canales, deja "espejo" 0:0,1:1,2:2
A2B_CH_MAP=0:0,1:1,2:2,3:3,4:4,5:5
B2A_CH_MAP=0:0,1:1,2:2,3:3,4:4,5:5

# Qué reenviar
FORWARD_TEXT=1
# 1 si quieres cruzar posiciones/telemetría (como resumen de texto)
FORWARD_POSITION=0   
# 1 si quieres pedir ACK en envíos (solo tiene sentido en unicast)
REQUIRE_ACK=0         
# Anti-ruido # máx. mensajes/minuto por sentido
RATE_LIMIT_PER_SIDE=8 
# segundos de ventana antidupe
DEDUP_TTL=45          
# segundos (defecto: 60)
BRIDGE_PEER_DOWN_BACKOFF=75   

# Marcado opcional (vacío para no marcar)
TAG_BRIDGE=[BRIDGE]
# Etiquetas específicas por dirección (opcionales)
TAG_BRIDGE_A2B=[BRIDGE A→B]
TAG_BRIDGE_B2A=[BRIDGE B→A]

# =========================
# Bridge embebido en el broker (opcional)
# =========================
 # 1=activar dentro del broker, 0=desactivar
BRIDGE_ENABLED=1

# Retardo antes de emitir hacia el nodo B cuando el bridge está embebido (A->B)
# 0 = sin retardo
BRIDGE_B_TX_DELAY_MS=2000

# Nodo B (ya lo tienes definido)
BRIDGE_B_HOST=192......
BRIDGE_B_PORT=4403

# Mapeo de canales (usa los mismos si quieres espejo)
BRIDGE_A2B_CH_MAP=${A2B_CH_MAP}
BRIDGE_B2A_CH_MAP=${B2A_CH_MAP}

# Qué reenviar
BRIDGE_FORWARD_TEXT=${FORWARD_TEXT}
BRIDGE_FORWARD_POSITION=${FORWARD_POSITION}
BRIDGE_REQUIRE_ACK=${REQUIRE_ACK}

# Anti-ruido
BRIDGE_RATE_LIMIT_PER_SIDE=${RATE_LIMIT_PER_SIDE}
BRIDGE_DEDUP_TTL=${DEDUP_TTL}

# Etiquetas
TAG_BRIDGE=${TAG_BRIDGE}
TAG_BRIDGE_A2B=${TAG_BRIDGE_A2B}
TAG_BRIDGE_B2A=${TAG_BRIDGE_B2A}
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

