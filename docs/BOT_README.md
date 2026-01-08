# 🤖 Guía completa del BOT de Telegram — MeshNet "The Boss" (v6.2.1)

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
- `/enviar canal N texto`
- `/enviar !id texto`
- `/enviar_ack !id texto reintentos=3 espera=10 backoff=1.5`

Soporta:
- Broadcast / unicast.
- ACK real combinado (librería + broker ROUTING_APP).
- Troceado automático.

### 4.4 Escucha
- `/escuchar [canal|all]`
- `/parar_escucha`

### 4.5 Programación y tareas
- `/diario HH:MM destino texto`
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

### 4.7 Auditoría y cobertura
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
- Versión: v6.2.1
- Compatible broker v6.x
- Operativo 24/7

Autor: jmmpcc — MeshNet "The Boss"

