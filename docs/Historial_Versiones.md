## 🧾 Historial de versiones

## v7.0.21 — Validación preventiva de perfiles de radio

- Añadido `scripts/radio-profile-check` para validar `.env` sin abrir conexiones de radio.
- Añadidas pruebas unitarias de aliases, capacidades, overrides y requisitos de los tres perfiles.
- Añadida la guía `docs/RADIO_PROFILES.md`.
- No se altera el comportamiento operativo de broker, bot, APRS, correo ni bridges.

# Changelog — MeshNet v6.2.3

Fecha: 2026-01-24
Versión: **v6.2.3**

---

## 🆕 Cambios principales

### 1. Control estricto de mensajes privados (DM) hacia el nodo “home”
Se introduce el uso explícito de la variable de entorno:

```env
HOME_NODE_ID=!xxxxxxxx
```

El broker ahora **solo considera órdenes especiales** aquellas que llegan como **mensaje privado (DM) dirigido lógicamente a este nodo**, independientemente de los saltos realizados en la malla.

- No se activan órdenes por:
  - mensajes de canal
  - broadcast (`^all`)
  - DMs dirigidos a otros nodos
- Se elimina cualquier ambigüedad en el tratamiento de mensajes privados.

---

### 2. Nuevo flujo: DM `/aprs canal N …` → doble acción controlada

Se implementa un comportamiento nuevo, sin romper nada existente:

1. **Entrada**: mensaje privado (DM) al nodo `HOME_NODE_ID` con el formato  
   ```
   /aprs canal N DESTINO: Texto
   ```

2. **Acciones automáticas del broker**:
   - **APRS**  
     El mensaje se envía por APRS:
       - por **RF (KISS)**  
       - y por **APRS-IS** si hay credenciales activas  
   - **Mesh**  
     El broker **reinyecta únicamente el texto limpio** en el **canal N** de la malla.

3. **Privacidad garantizada**:
   - El comando `/aprs …` **nunca aparece en canales públicos**
   - En la malla solo se ve el texto final

---

### 3. Reinyección segura y sin bucles
La reinyección al canal Mesh:
- elimina el prefijo `/aprs`
- elimina destino y metadatos
- evita bucles y re-disparo del gateway APRS
- no interfiere con el tráfico normal de la malla

---

### 4. Mejora del parser `/aprs`
El gateway APRS ahora acepta:

- `/aprs DESTINO: texto`
- `/aprs canal N DESTINO: texto`
- `/aprs ch N DESTINO: texto`

El identificador `canal N`:
- **se ignora para APRS** (APRS no tiene canales)
- **se usa únicamente** para decidir la reinyección en Mesh

Compatibilidad total con versiones anteriores.

---

## ✅ Ejemplos de uso

### Ejemplo 1 — Envío silencioso a APRS + publicación en canal Mesh

**Mensaje privado (DM) enviado al nodo HOME:**
```
/aprs canal 2 EB2EAS-7: Hola buenas tardes
```

**Resultado:**
- Mesh:
  - En **canal 2** aparece:
    ```
    Hola buenas tardes
    ```
- APRS:
  - Mensaje enviado a `EB2EAS-7` por RF
  - También por APRS-IS si está habilitado
- Privacidad:
  - El comando `/aprs …` no se ve en ningún canal

---

### Ejemplo 2 — Envío silencioso solo a APRS (sin reinyección)

**Mensaje privado (DM):**
```
/aprs EB2EAS-7: Recibido OK
```

**Resultado:**
- Mesh:
  - No aparece nada en canales
- APRS:
  - Mensaje enviado a `EB2EAS-7`
- Comportamiento idéntico a versiones anteriores

---

### Ejemplo 3 — Mensaje de canal (sin reinyección especial)

**Mensaje enviado en canal 4:**
```
/aprs EB2EAS-7: Prueba desde canal
```

**Resultado:**
- Mesh:
  - Visible en canal 4
- APRS:
  - Mensaje enviado
- No hay lógica DM→canal (no es privado)

---

## 🔒 Reglas de seguridad y diseño

- Solo los DMs dirigidos a `HOME_NODE_ID` pueden activar reinyección.
- No se interpreta texto como control de ruteo Mesh.
- No se altera el comportamiento de nodos terceros.
- No se modifica el flujo APRS existente.

---

## ⚙️ Requisitos de configuración

```env
HOME_NODE_ID=!9ef0c2cc
```

Opcional:
- Si `HOME_NODE_ID` no está definido, el broker vuelve al comportamiento previo
  (DM genérico, sin estricta validación).

---

## 🧩 Archivos afectados

- `Meshtastic_Broker.py`
  - Nueva lógica DM estricta
  - Reinyección controlada Mesh
- `meshtastic_to_aprs.py`
  - Parser `/aprs` extendido
  - Compatibilidad con `canal/ch`

---

## ✔️ Compatibilidad

- Totalmente compatible con v6.2.2
- Sin cambios en comandos existentes
- Sin impacto en nodos que no usen `/aprs`

---

**MeshNet v6.2.3**  
Control fino, privacidad real y pasarela Mesh ↔ APRS más potente sin ruido en la red.


## [v6.2.2] - 2026-01-14 Enero

> Enfoque de esta versión: estabilidad 24/7 (reconexión segura), unificación del “control plane” vía BacklogServer (JSONL/TCP), y enriquecimiento de datos (nodos/telemetría/cobertura) usando el broker como fuente de verdad.

### Added
- 
- **APRS gateway (meshtastic_to_aprs.py)**
  - Modo **APRS-IS push** configurable (reenvío dirigido) con logging de estado y parámetros de canal/prefijo/gap mínimo (`aprsis_push`). fileciteturn1file3
- **API adapter (meshtastic_api_adapter.py)**
  - Capa **“API-first”** (TCPInterface) con fallback a CLI, y soporte explícito de **TCPInterfacePool**. fileciteturn1file12
  - Resolución robusta de host/puerto de control: prioridad `BROKER_CTRL_*`, luego `BROKER_HOST/BACKLOG_PORT`, luego `BROKER_PORT+1`. fileciteturn1file12
- **Cobertura (coverage_backlog.py)**
  - Normalización robusta de IDs (formato `!xxxxxxxx`) y composición de identificador humano `alias (id)` para mapas/cobertura. fileciteturn1file8
- 
### Changed
- **Broker**
  - `append_offline_log()` ahora es **retrocompatible**: acepta paquete “plano v6” o `{packet:{...}}`, y guarda campos aplanados para panel (TEXT/POS/TELEM/NODEINFO). fileciteturn1file10
  - Reestructuración de reconexión del pool con:
    - anti-reentradas,
    - lock interproceso por `host:port`,
    - resolución robusta de host/puerto runtime (evita bloqueos y “dobles sesiones”). fileciteturn1file17
- **Bot**
  - Comandos que consultan/enlazan con el broker pasan a **usar control TCP/JSONL** (se evita UDP “flaky” y problemas DNS). fileciteturn1file13
  - `/telemetria` mejora el parseo de argumentos: sin destino -> listado; con destino -> añade histórico del broker con ventana configurable (por defecto 30 min). fileciteturn1file5
  - `/reconectar` (admin) ahora espera `connected=True` con timeout configurable y reporta `node_host:node_port` reales. fileciteturn1file13
- **APRS gateway**
  - Sanitización a ASCII APRS para `dest/text` antes de TX (menos rechazos en APRS-IS). fileciteturn1file3
  - Dedupe de mensajes para evitar doble envío (bot + gateway). fileciteturn1file3

### Fixed
- Reducción de “flapping” y falsos positivos de desconexión:
  - cooldown + bloqueo TX durante reconexión en el broker. fileciteturn1file1
  - serialización de reconexiones y locks por transporte (evita dos procesos luchando por el mismo TCP). fileciteturn1file17
- Errores típicos de await (mezcla sync/async) mitigados con helper `maybe_await()`. fileciteturn1file13
## [6.2.0-1] - 2026-01-04 Enero 

### Añadido
- **Pasarela externa “Triple Bridge” reforzada para operación 24/7**:
  - Cola de transmisión (TX spool) con reintentos y planificación por “due time” (evita pérdida de mensajes cuando un peer cae y vuelve).
  - Reconexión automática de peers TCP (B/C) con ventana de supresión/offline y reintentos progresivos.
  - Watchdog opcional por *stale RX* (`TRIPLE_B_STALE_SEC`, `TRIPLE_C_STALE_SEC`) para detectar conexiones “zombis” y forzar reconexión.
  - Soporte de selección de peers mediante `BRIDGE_PEERS` (`B`, `C` o `B,C`) para operar con 1 o 2 nodos sin duplicar despliegues.
  - Modo `HUB_MODE=broker` consolidado: A se alimenta por backlog del broker (sin TCP a A) y se inyecta hacia A vía `SEND_TEXT`. fileciteturn32file0

- **Parámetros de resiliencia configurables por entorno** (sin tocar lógica base):
  - `TCP_TIMEOUT_S`, `TRIPLE_WATCHDOG_TICK`, `BROKER_POLL_SEC`, `BROKER_FETCH_LIMIT`, `BROKER_TIMEOUT_S`.
  - Etiquetas de origen por dirección (`TAG_BRIDGE_A2B`, `TAG_BRIDGE_B2A`, `TAG_BRIDGE_A2C`, `TAG_BRIDGE_C2A`) y tag base (`TAG_BRIDGE`).

- **Mejora de observabilidad**:
  - Logs explícitos de estado de peers (ONLINE/OFFLINE/DEFER/OK) para diagnosticar caídas intermitentes de WiFi/CPU sin “silencios” en consola.

### Cambiado
- **Estrategia de entrega en presencia de caída de peer**:
  - A→(B/C): en lugar de fallar y perder el envío si el peer está `None`/offline, se difiere y reintenta automáticamente hasta reconectar, manteniendo el rate-limit por lado.
- **Control de eco**:
  - Detección y uso de `local_id_*` (myInfo) para evitar reinyectar mensajes que provienen del propio nodo destino cuando hay caminos de retorno.

### Corregido
- **Cortes de conexión persistente a nodos TCP**:
  - Mitigación del patrón `NoneType has no attribute sendText` en reenvíos, al garantizar que nunca se intente enviar cuando la interfaz no está lista: se reprograma en cola.
- **Convivencia broker ↔ pasarela**:
  - En `HUB_MODE=broker`, la pasarela ya no necesita ni intenta abrir TCP a A (evita colisiones/competiciones por el mismo interfaz).
- **Estabilidad en entornos 24/7**:
  - Reconexión “suave” y reintentos escalonados, reduciendo bloqueos por timeouts prolongados.

### Operación / Despliegue
- **Keepalive TCP por contenedor (recomendado)**:
  - Para minimizar sesiones TCP colgadas en redes WiFi, aplicar `sysctls` en `docker-compose.yml` del servicio que mantiene conexiones a nodos:
    - `net.ipv4.tcp_keepalive_time`
    - `net.ipv4.tcp_keepalive_intvl`
    - `net.ipv4.tcp_keepalive_probes`
  - Verificación: `docker inspect <container> --format '{{json .HostConfig.Sysctls}}'`.

### Notas de compatibilidad
- No se rompen comandos existentes: se mantiene el comportamiento por defecto (`BRIDGE_PEERS=B,C`, `HUB_MODE=tcp`).
- En `BRIDGE_PEERS=C` o `BRIDGE_PEERS=B`, evitar dejar el host del peer no usado como cadena vacía; el script valida peers activos y aborta si no hay ninguno.

### v6.1.3 — Estable (Diciembre 2025)

## 🧠 Broker
- Reconexión persistente robusta.
- Cooldown seguro con pausa suave.
- Watchdog + CircuitBreaker integrados.
- Cola SendQueue con coalescing.
- BacklogServer mejorado con control remoto.
- Manejo refinado de sockets y reconexión limpia.

---

# 🗺️ Auditorías incluidas en v6.1.3

## ✔️ Auditoría de Red (`auditoria_red`)
Nueva auditoría orientada a evaluar **salud y calidad actual de la malla**.

Incluye:
- SNR mínimo/máximo/promedio por nodo.
- Clasificación de calidad por colores.
- Distancia a HOME (Haversine).
- Provincia/Ciudad con reverse-geocoder offline.
- Última vez escuchado.
- Vecinos detectados.
- Rutas y hops reales.
- Ranking por calidad.
- Detección de nodos sin posición o sin métricas.

Salida:
- Informe estructurado en Telegram.
- Datos combinados del backlog y nodes.txt.

---

## ✔️ Auditoría Integral (`auditoria_integral`)
Auditoría avanzada que evalúa:

- Cobertura total de la red.
- Mapas KML/GPX generados automáticamente.
- Heatmap de posiciones desde backlog.
- Análisis temporal (24h / 72h / 7 días).
- Estadísticas por nodo:
  - mensajes enviados/recibidos
  - distancias máximas alcanzadas
  - saltos medios
  - SNR promedio
- Detección de agujeros de cobertura.
- Rutas poco eficientes.
- Ranking de cobertura y centralidad.

Salida:
- KML de cobertura.
- KML 24h.
- Histograma básico de calidad.
- Resumen detallado por nodo.

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

## 1. COMÚN a ambos sistemas (Windows y Raspberry):
     Clonar el repositorio y Editar archivo de configuración .env
```powershell
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot

IMPORTANTE: Revisar configuración de docker-compose.yml (Windows) o de docker-compose.rpi.yml para adaptarlo, según indicaciones
            en el propio fichero. Por ejemplo: Que el contenedor APRS esté en otro dispositivo diferente de donde se encuentra el
            BROKER y el BOT.

```

```bash
 Editar .env y rellena al menos, estas variables:
    # === Telegram ===
    #TELEGRAM_TOKEN=
    #ADMIN_IDS=

    # === Nodos Meshtastic ===

    # Nodo principal
    #MESHTASTIC_HOST=
    #Nodo secundario - SI EXISTE -
    #MESH_NODE_HOST=

    #DEBUG_KM=1
    #HOME_NODE_ID= el Id del Nodo Principal

    # Mapa Live (opcional)
    #HOME_LAT=
    #HOME_LON=
    
    # Raspberry,, siempre never
    #BOT_PAUSE_MODE=never

    # Windows: auto o always
    #BOT_PAUSE_MODE= always

    # === APRS-IS (iGate hacia Internet) ===
    #APRSIS_USER=       # tu indicativo-SSID de iGate
    #APRSIS_PASSCODE=   # passcode APRS-IS de de indicativo

    # 1 Pasarela ACTIVA (APRS <-> MESH) 0 Pasarela NO ACTIVA
    # APRS_GATE_ENABLED=1
  
    #sólo se permiten mensajes de estos indicativos, separados por comas
    #APRS_ALLOWED_SOURCES=

    # === APRS ===
    # Indicativo de la pasarela
    #APRS_CALL=
    
    # Activa o desactiva el eco del mensaje enviaso de APRS a MESH
    #APRS_ECHO_HOME_ENABLED=1

    # Nodo B (el segundo nodo, con preset distinto)
    #B_HOST=
    
    # =========================
    # Bridge embebido en el broker (opcional)
    # =========================
    # 1=activar dentro del broker, 0=desactivar
    #BRIDGE_ENABLED=1

    #     Recomendado en Docker: DISABLE_BOT_TCP=1
  2.- Revisar:
# (Opcional APRS/Bridge: KISS_HOST, KISS_PORT, BRIDGE_ENABLED, B_HOST, etc.)

```
---

# APRS con KISS-TCP en un Equipo Remoto
  
  ## Configuración Oficial — MeshNet The Boss

Este documento explica cómo usar Direwolf o Soundmodem en un ordenador distinto
del que ejecuta el Broker + Bot + APRS Gateway (contenedor `meshtastic-aprs`).

```
# 1. Qué se modifica en la configuración

Solo se cambia **una cosa** en el `.env` de la máquina donde corre Docker:

KISS_HOST=IP_DEL_PC_REMOTO
KISS_PORT=8100

KISS_HOST=192.168.1.100
KISS_PORT=8100

```
  ## Configuración del PC donde corre Soundmodem/Direwolf — MeshNet The Boss


  1. Soundmodem
```
      En Settings → KISS Server:

      . Enable KISS over TCP
      . Address: 0.0.0.0
      . Port: 8100
```

  2. Direwolf

    Comando típico:

      direwolf -t 0 -p -r 48000 -D 1

      Y en direwolf.conf:

        KISSHOST 0.0.0.0
        KISSPORT 8100


Esto permite que la Raspberry/PC principal se conecte sin firewall local.

  3. Probar conectividad entre APRS y el PC remoto

```
    En la máquina donde corre Docker:

      telnet IP_DEL_PC_REMOTO 8100

    Si sale:

      Connected

    la comunicación está bien.
```

  4. Reiniciar APRS para aplicar cambios:


       docker restart meshtastic-aprs

    o si usas compose:

      docker compose up -d aprs

  5. Qué debe aparecer en los logs si todo está bien

```
    En docker logs -f meshtastic-aprs:

    [aprs] KISS=192.168.1.30:8100 CALL=EB2XXX-11 PATH=WIDE1-1,WIDE2-1
    [aprs] Conectado a KISS TCP remoto
```
6. Ventajas de esta arquitectura
   
```

  APRS Gateway permanece junto al broker → máxima estabilidad.
  No se usa APRS remoto → se elimina toda complejidad JSONL/UDP entre hosts.
  Solo se expone KISS TCP hacia la red local.
  Ideal para emergencias (RPi alimentada por batería).
  Soundmodem/Direwolf pueden correr en varios PCs sin modificar la arquitectura.
   
```
7. Resumen rápido
    
  ```
  Componente	          Dónde corre	      Ajuste necesario

  Broker                Raspberry/PC	      Sin cambios
  Bot	                  Raspberry/PC	      Sin cambios
  APRS Gateway	      Raspberry/PC	      KISS_HOST=IP_DEL_PC
  Soundmodem/Direwolf	  PC remoto	          Escucha en 0.0.0.0:8100

```
