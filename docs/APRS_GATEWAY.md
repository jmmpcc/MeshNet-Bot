# 🌐 APRS Gateway — Integración bidireccional con Meshtastic

## ⚙️ Descripción general

Cuando está **activado el modo `aprs_on`** (o `APRS_GATE_ENABLED=1` en `.env`),  
el sistema entra en **modo pasarela APRS bidireccional**, permitiendo que:

> 🔄 Los mensajes enviados desde la red **Meshtastic** se publiquen en la red **APRS**,  
> y los mensajes recibidos en **APRS (RF o APRS-IS)** se reenvíen automáticamente a **Meshtastic**.

Este modo convierte tu nodo en un **gateway completo APRS↔Mesh**, compatible con **Direwolf**, **Soundmodem** o cualquier **TNC KISS TCP**.

---

## 🧩 Comportamiento detallado

### 1️⃣ Mensajes Meshtastic → APRS (uplink)
- El bot de Telegram usa el comando `/aprs` para enviar mensajes.
- Se comunica con el servicio `meshtastic_to_aprs.py` mediante **UDP (puerto 9464)**.
- Este gateway convierte el mensaje al formato **AX.25 (KISS)** y lo transmite por radio.
- Si hay configuradas credenciales APRS-IS (`APRSIS_USER`, `APRSIS_PASSCODE`), también se sube a **aprs.fi** automáticamente.

📤 **Ejemplo de flujo:**
```
Telegram → Bot → UDP 9464 → meshtastic_to_aprs.py → Soundmodem/Direwolf → RF (APRS)
                                                   ↳ opcional: APRS-IS (aprs.fi)
```

---

### 2️⃣ Mensajes APRS → Meshtastic (downlink)
- El gateway escucha todas las tramas APRS recibidas por el puerto KISS.
- Si el mensaje contiene un marcador `[CHx]` (por ejemplo `[CH1]`),  
  el gateway lo reenvía automáticamente al **canal correspondiente** de Meshtastic.
- El reenvío se realiza hacia el **broker JSONL** (`BROKER_HOST:8765`).

📥 **Ejemplo:**
```
APRS RF → Soundmodem/Direwolf → meshtastic_to_aprs.py → TCP 8765 → Broker → Mesh Network
```

> 💡 Ejemplo de trama APRS que se reenviará al canal 1:
> ```
> EA2XXX>APRS:Hola desde APRS [CH1]
> ```

---

### 3️⃣ Prevención de bucles y duplicados

El sistema mantiene una **caché de mensajes recientes** (`_recent_aprs_keys`)  
para evitar que los mismos paquetes circulen en bucle entre la red APRS e Internet o la red Mesh.

> 🔁 TTL típico: 20 segundos  
> Evita que un mensaje reenviado vuelva a entrar al origen.

---

### 4️⃣ Mensajes especiales: `NOGATE` y `RFONLY`
Si un mensaje incluye cualquiera de estos términos:
- `NOGATE`
- `RFONLY`

Entonces el gateway **no lo reenvía a APRS-IS** ni a la red Mesh.  
Se respeta la intención original del usuario APRS (solo RF local).

---

### 5️⃣ Modo APRS-IS (Internet uplink)

Si se configuran las credenciales de usuario y passcode, el gateway se conecta a la red APRS-IS global:

```bash
APRSIS_USER=EB2XXX-10
APRSIS_PASSCODE=12345
```

Esto crea una conexión persistente a:
```
rotate.aprs2.net:14580
```

Y sube automáticamente los mensajes válidos en formato *third-party frame*, como:

```
IGATE>APRS,TCPIP*,qAR,IGATE:}SRC>DEST,PATH:payload
```

---

### 6️⃣ Registro y depuración

Activa el modo de depuración añadiendo en `.env`:

```bash
APRS_DEBUG=1
```

📜 Ejemplo de salida:
```
[aprs→IS] Enviando: EB2EAS>APRS,TCPIP*,qAR,EB2EAS:}EA2XXX>APRS:Hola mundo [CH0]
[aprs→mesh] Reenviando desde APRS a Mesh canal 0: "Hola mundo"
```

> Desactívalo con `APRS_DEBUG=0` para un funcionamiento silencioso.

---

## 🛰️ Flujo completo de comunicación

```
📱 Telegram (/aprs)
       │
       ▼
🧠 Bot (Telegram_Bot_Broker.py)
       │ UDP 9464
       ▼
🛰️ meshtastic_to_aprs.py
       │
       ├── RF → APRS (via Direwolf/Soundmodem)
       │
       └── 🌐 APRS-IS (aprs.fi)
```

**Y en sentido inverso (si `APRS_GATE_ENABLED=1`):**

```
📡 RF (APRS)
       │
       ▼
Soundmodem/Direwolf
       │
       ▼
🛰️ meshtastic_to_aprs.py
       │ TCP 8765
       ▼
Broker JSONL
       │
       ▼
🌐 Red Meshtastic
```

---

## ⚙️ Variables de entorno relacionadas (.env)

| Variable | Descripción |
|-----------|-------------|
| `APRS_GATE_ENABLED` | `1` para activar la pasarela APRS bidireccional |
| `KISS_HOST` / `KISS_PORT` | Dirección del modem KISS (Direwolf/Soundmodem) |
| `APRS_CTRL_PORT` | Puerto UDP donde el bot envía los mensajes (por defecto `9464`) |
| `BROKER_HOST` | Dirección del broker Meshtastic (por defecto `127.0.0.1`) |
| `BROKER_CTRL_PORT` | Puerto de control JSONL del broker (`8766`) |
| `APRSIS_USER` / `APRSIS_PASSCODE` | Credenciales APRS-IS para subida a aprs.fi |
| `APRS_PATH` | Ruta por defecto de retransmisión RF (`WIDE1-1,WIDE2-1`) |
| `APRS_MSG_MAX` | Longitud máxima de mensaje (por defecto `67` bytes) |
| `APRS_DEBUG` | `1` para mostrar todos los paquetes APRS procesados |

---

## 🔄 Comparativa de modos

| Estado | Dirección | Activo | Descripción |
|:-------:|:----------|:-------:|-------------|
| `APRS_GATE_ENABLED=1` | Mesh → APRS | ✅ | Envía mensajes desde Meshtastic a la red APRS |
| `APRS_GATE_ENABLED=1` | APRS → Mesh | ✅ | Reenvía mensajes recibidos por RF a la red Mesh |
| `APRS_GATE_ENABLED=0` | Mesh → APRS | ✅ | Solo envío unidireccional desde Mesh |
| `APRS_GATE_ENABLED=0` | APRS → Mesh | ❌ | No se reciben mensajes APRS |

---

## 🧠 Ejemplos de uso desde Telegram

| Comando | Acción |
|----------|--------|
| `/aprs canal 2 Hola red APRS` | Envía un mensaje al canal 2 de Meshtastic y APRS |
| `/aprs EB2EAS-11: Buenos días desde la red Mesh` | Mensaje directo a un indicativo APRS |
| `/aprs en 10 canal 0 Revisión programada` | Programa un envío dentro de 10 minutos |
| `/aprs en 5,15,30 canal 1 Estado red Mesh` | Envía mensajes programados a intervalos múltiples |
| `/aprs broadcast: Mensaje general` | Difunde el mensaje a toda la red APRS |

---

## 🗂️ Archivos implicados

| Archivo | Rol |
|----------|-----|
| `Telegram_Bot_Broker.py` | Contiene el comando `/aprs` y gestiona la lógica desde Telegram |
| `meshtastic_to_aprs.py` | Gateway APRS ↔ Mesh, convierte tramas y comunica con el broker |
| `broker_task.py` | Gestiona tareas programadas (`/aprs en ...`) |
| `.env` | Contiene las variables de configuración del gateway |

---

## 📋 Recomendaciones

- 🛠️ Usa **Direwolf** o **Soundmodem** como TNC con salida KISS TCP.
- 🔒 Evita usar APRS-IS si tu objetivo es solo cobertura local (RF-only).
- 📡 Si tienes varios gateways, mantén `APRS_GATE_ENABLED=1` **solo en uno** para evitar eco de mensajes.
- 📄 Revisa los logs con:
  ```bash
  docker compose logs -f aprs
  ```

---

## ✅ Resumen final

| Función | Descripción |
|----------|-------------|
| `/aprs` | Envía mensajes a la red APRS |
| `/aprs_on` | Activa el gateway bidireccional APRS↔Mesh |
| `/aprs_off` | Desactiva el reenvío de mensajes APRS hacia la red Mesh |
| `APRS_GATE_ENABLED=1` | Equivalente a tener `/aprs_on` activo permanentemente |

---

📍 **Autor:** [jmmpcc / MeshNet "The Boss"](https://github.com/jmmpcc)  
📦 **Versión:** v6.1 — *sin soporte USB (modo TCP/IP)*  
🛰️ **Módulo:** `meshtastic_to_aprs.py` integrado con `Telegram_Bot_Broker.py`
