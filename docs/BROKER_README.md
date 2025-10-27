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
| `bridge_in_broker.py` | Pasarela interna opcional entre brokers o presets. |
| `.env` | Define los parámetros de conexión, puertos y tiempos de espera. |

---

## 📋 Ejemplo de log

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
| **Compatibilidad** | Total con el bot y bridge v6.1 |

---

📍 **Autor:** [jmmpcc / MeshNet "The Boss"](https://github.com/jmmpcc)  
📦 **Versión:** v6.1 — *sin soporte USB (modo TCP/IP)*  
🛰️ **Módulo:** `Meshtastic_Broker.py`
