# 🤖 Bot de Telegram — Interfaz de control para MeshNet "The Boss"

## ⚙️ Descripción general

El **bot de Telegram** es la interfaz principal de usuario del sistema.  
Permite gestionar, supervisar y enviar mensajes a la red Meshtastic, así como controlar tareas programadas, el gateway APRS y el estado del broker.

> Está construido sobre `python-telegram-bot v20.7` con soporte para comandos asíncronos y menús contextuales oficiales de Telegram.

---

## 📱 Comandos principales

| Comando | Descripción |
|----------|-------------|
| `/start` | Muestra el menú principal y registra al usuario. |
| `/estado` | Consulta el estado actual del broker y del nodo conectado. |
| `/vecinos` | Lista de nodos visibles (con RSSI, SNR y saltos). |
| `/traceroute` | Realiza un traceroute a un nodo o alias. |
| `/telemetria` | Solicita datos de telemetría de un nodo. |
| `/enviar` | Envía un mensaje de texto a un nodo o canal (con soporte ACK y envío múltiple). |
| `/aprs` | Envía mensajes a la red APRS o al gateway APRS local. |
| `/diario` | Programa mensajes diarios automáticos (ej. avisos, recordatorios). |
| `/tareas` | Lista todas las tareas programadas, pendientes o ejecutadas. |
| `/escuchar` | Activa el modo escucha para recibir mensajes en tiempo real. |
| `/parar_escucha` | Detiene la escucha activa del chat. |
| `/broker_resume` | Reanuda la conexión del broker tras una pausa. |
| `/force_reconnect` | Fuerza reconexión inmediata con el nodo. |

---

## 📡 Flujo de comunicación

```
📲 Usuario (Telegram)
        │
        ▼
🤖 Bot (Telegram_Bot_Broker.py)
        │
        ▼
⚙️ Broker JSONL → 📡 Nodo Meshtastic
        │
        └── 🌐 APRS Gateway (opcional)
```

---

## 🧩 Integración con el Broker

El bot **no abre conexiones TCP directas** con el nodo Meshtastic.  
En su lugar, utiliza el **Broker JSONL** como intermediario:

| Operación | Encargado |
|------------|------------|
| Envío de mensajes | Broker (`SEND_TEXT`) |
| Estado del nodo | Broker (`BROKER_STATUS`) |
| Traceroute | API o CLI (`meshtastic --traceroute`) |
| Tareas programadas | `broker_task.py` |
| Telemetría | `meshtastic_api_adapter.py` |

---

## ⚙️ Variables de entorno relevantes

| Variable | Descripción |
|-----------|-------------|
| `TELEGRAM_TOKEN` | Token del bot de Telegram. |
| `ADMIN_IDS` | IDs de los administradores (separados por coma). |
| `BROKER_HOST` / `BROKER_PORT` | Dirección y puerto del broker JSONL. |
| `SEND_LISTEN_SEC` | Tiempo de escucha tras cada envío. |
| `TRACEROUTE_TIMEOUT` | Timeout máximo de los traceroutes. |
| `ACK_MAX_ATTEMPTS` | Intentos máximos de envío con ACK. |
| `ACK_WAIT_SEC` | Tiempo de espera entre intentos. |
| `NODES_FORCE_API_ONLY` | Si es `1`, fuerza lectura solo desde la API (sin nodos.txt). |
| `DISABLE_BOT_TCP` | Si es `1`, el bot no abre sockets TCP directos (modo broker-only). |

---

## 📂 Archivos implicados

| Archivo | Rol |
|----------|-----|
| `Telegram_Bot_Broker.py` | Código principal del bot, comandos y handlers. |
| `broker_task.py` | Módulo de ejecución y persistencia de tareas programadas. |
| `meshtastic_api_adapter.py` | Comunicación con la API de Meshtastic y TCPInterface. |
| `positions_store.py` | Gestión de posiciones y generación de archivos KML/GPX. |
| `.env` | Configuración de tokens, IDs y parámetros de conexión. |

---

## 🧭 Menú contextual de Telegram

El bot usa el menú oficial de Telegram (`SetMyCommands`) con comandos visibles según el rol del usuario:

| Rol | Comandos visibles |
|------|------------------|
| **Administrador** | `/start`, `/estado`, `/vecinos`, `/traceroute`, `/diario`, `/tareas`, `/reconectar`, `/ayuda` |
| **Usuario** | `/start`, `/estado`, `/vecinos`, `/traceroute`, `/ayuda` |

---

## 🧠 Ejemplo de uso

```
/enviar canal 2 Buenas noches red Mesh 🌙
/aprs EB2EAS-11: Hola desde Zaragoza!
/diario 08:00 canal 0 Boletín diario automático
```

---

## 🛡️ Sistema de protección

- **Anti-cooldown:** evita envíos durante reconexiones del broker.  
- **Anti-duplicados:** bloquea mensajes repetidos en ventana corta.  
- **Modo seguro:** pausa y reanuda el broker cuando se ejecutan operaciones exclusivas (CLI).

---

## 🛰️ Resumen

| Característica | Descripción |
|----------------|-------------|
| **Framework** | python-telegram-bot v20.7 |
| **Interfaz** | Menú Telegram contextual + comandos slash |
| **Integración** | Broker JSONL y APRS Gateway |
| **Soporte USB** | Desactivado en v6.1 (modo TCP/IP) |
| **Compatibilidad** | Total con la versión 6.1 del sistema |

---

📍 **Autor:** [jmmpcc / MeshNet "The Boss"](https://github.com/jmmpcc)  
📦 **Versión:** v6.1 — *sin soporte USB (modo TCP/IP)*  
🛰️ **Módulo:** `Telegram_Bot_Broker.py`
