# 🧩 Broker JSONL — Núcleo de comunicación entre Meshtastic y el Bot

## ⚙️ Descripción general

El **Broker JSONL** actúa como puente principal entre la red **Meshtastic** y los módulos externos,  
como el **bot de Telegram**, el **gateway APRS** o las **pasarelas de presets**.

> Su objetivo es mantener una conexión TCP persistente con el nodo Meshtastic,  
> distribuir los mensajes recibidos y gestionar las tareas programadas o en cola.

---

## 🧠 Funciones principales

| Función | Descripción |
|----------|-------------|
| **TCPInterface Persistente** | Mantiene la conexión continua con el nodo Meshtastic a través de `meshtastic.tcp_interface`. |
| **JSONL Server (puerto 8765)** | Proporciona una API local para que otros servicios (bot, APRS, bridge) se comuniquen con el nodo sin abrir nuevas conexiones TCP. |
| **BacklogServer (puerto 8766)** | Módulo adicional para control y consultas, permitiendo comandos remotos como `BROKER_STATUS`, `SEND_TEXT`, o `FORCE_RECONNECT`. |
| **Gestión de tareas programadas** | Ejecuta las tareas almacenadas en `scheduled_tasks.jsonl`, generadas por el bot con comandos como `/diario` o `/enviar en`. |
| **Anti-duplicados y cooldowns** | Implementa mecanismos de protección frente a envíos repetidos y reconexiones simultáneas. |

---

## 🔌 Comunicación entre módulos

```
🧠 Telegram Bot  ⇄  ⚙️ Broker JSONL  ⇄  📡 Nodo Meshtastic
                               ↕
                             APRS Gateway (opcional)
```

El broker centraliza todas las transmisiones de texto, telemetría y control,
sirviendo de **punto único de entrada/salida** para la red Mesh.

---

## 🔄 Flujo interno

1. **Recepción TCP desde Meshtastic** → Se decodifica el mensaje.  
2. **Enrutamiento interno** → Determina si el mensaje es `TEXT_MESSAGE_APP`, `TELEMETRY_APP`, etc.  
3. **Registro** → Guarda eventos en formato JSONL para auditoría y trazabilidad.  
4. **Publicación local** → Notifica al bot, APRS u otros clientes vía socket o JSONL.  
5. **Ejecución de tareas programadas** → Envía mensajes automáticos o recordatorios.

---

## ⚙️ Comandos de control (BacklogServer)

| Comando | Descripción |
|----------|-------------|
| `BROKER_STATUS` | Devuelve estado actual (`running`, `paused`, `cooldown_remaining`, `connected`, etc.). |
| `BROKER_PAUSE` / `BROKER_RESUME` | Suspende o reanuda la comunicación TCP con el nodo Meshtastic. |
| `SEND_TEXT` | Solicita el envío de un mensaje a la malla Mesh. |
| `FORCE_RECONNECT` | Cierra la conexión actual y fuerza reconexión limpia. |
| `FETCH_BACKLOG` | Devuelve los últimos mensajes recibidos (historial reciente). |

---

## ⚙️ Variables de entorno relevantes

| Variable | Descripción |
|-----------|-------------|
| `BROKER_HOST` | Dirección del broker (por defecto `0.0.0.0`). |
| `BROKER_PORT` | Puerto principal JSONL (por defecto `8765`). |
| `BROKER_CTRL_PORT` | Puerto de control BacklogServer (`BROKER_PORT + 1`). |
| `MESHTASTIC_HOST` | IP o hostname del nodo Meshtastic. |
| `MESHTASTIC_TIMEOUT` | Tiempo máximo de espera para respuestas TCP. |
| `ACK_MAX_ATTEMPTS` | Intentos máximos de envío con confirmación. |
| `ACK_WAIT_SEC` | Tiempo de espera entre reintentos. |
| `ACK_BACKOFF` | Factor multiplicador de backoff exponencial. |

---

## 🧩 Archivos implicados

| Archivo | Rol |
|----------|-----|
| `Meshtastic_Broker.py` | Núcleo principal del broker (TCP + JSONL + Backlog). |
| `broker_task.py` | Ejecuta y mantiene las tareas programadas. |
| `bridge_in_broker.py` | **Pasarela interna** entre brokers/presets. **Desde v6.1.1**: incorpora supresión A→B cuando B está caído (*peer-down backoff*), y expone el estado en `status()`. |
| `.env` | Define los parámetros de conexión, puertos y tiempos de espera. |

---

## 🆕 Novedades **v6.1.1**

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

---

## 🔎 Ejemplos de log (bridge embebido)

```
[2025-10-27 08:00:01] [bridge] A2B ch 0->0 txt OK
[2025-10-27 09:00:00] [bridge] A2B ch 2->2 SKIP (B offline, 41s restantes)
[2025-10-27 09:00:00] [bridge] B OFFLINE → suprime A2B 60s (BrokenPipeError: [Errno 32] Broken pipe)
[2025-10-27 09:01:05] [bridge] B volvió ONLINE → reanudo A2B
[2025-10-27 09:01:05] [bridge] A2B ch 2->2 txt OK
```

> Notas:
> - `SKIP (B offline, Ns restantes)` indica que no se reenviará hacia B hasta que termine el backoff.
> - Al primer **éxito** tras la caída, se limpia el estado y vuelven los reenvíos normales.

---

## 🧯 Troubleshooting (bridge A→B)

- **Veo reintentos continuos cuando B está fuera**  
  → Desde v6.1.1 se evita automáticamente. Asegúrate de tener `BRIDGE_PEER_DOWN_BACKOFF` definido (o usa el valor por defecto `60`).
- **Quiero saber si el bridge está suprimido**  
  → Consulta `status()` del bridge: revisa `peer_state.is_peer_suppressed` y `peer_offline_remaining`.
- **Quiero logs más compactos**  
  → Se emite una sola notificación al marcar *peer-down*; durante el backoff solo verás `SKIP …`.

---

## 📋 Ejemplo de log (broker)

```
[2025-10-19 23:27:43] 🛡️ Guards anti-heartbeat activos (sendHeartbeat protegido).
[2025-10-19 23:27:43] ℹ️ Broker: TCPInterface enlazado al pool persistente.
[2025-10-19 23:27:43] 🔌 Broker JSONL escuchando en ('0.0.0.0', 8765)
```

---

## 🛰️ Resumen

| Característica | Descripción |
|----------------|-------------|
| **Tipo** | Módulo de comunicación central |
| **Conexión principal** | TCP persistente a Meshtastic |
| **Puertos locales** | 8765 (JSONL), 8766 (control) |
| **Soporte APRS** | Bidireccional mediante `meshtastic_to_aprs.py` |
| **Compatibilidad** | Bot/Bridge v6.1+; Bridge con *peer-down backoff* desde **v6.1.1** |

---

📍 **Autor:** [jmmpcc / MeshNet "The Boss"](https://github.com/jmmpcc)  
📦 **Versión:** **v6.1.1** — *sin soporte USB (modo TCP/IP)*  
🛰️ **Módulo:** `Meshtastic_Broker.py`
