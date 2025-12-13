
# Pasarela APRS ↔ MeshNet “The Boss” – Guía Completa (v6.2)

## 0. Introducción

La pasarela APRS ↔ MeshNet “The Boss” integra el mundo APRS clásico (RF y APRS‑IS) con la red Meshtastic.  
Permite comunicaciones cruzadas completas:

- APRS → Mesh (RF y APRS‑IS)
- Mesh → APRS (RF y APRS‑IS)
- Soporte de APRSdroid bidireccional
- Emergencias con prioridad, bypass y redundancia
- Geo‑fencing, notificación a Telegram y heartbeat
- Control remoto del gateway vía APRS
- Programación de mensajes desde APRS
- Integración con el bot MeshNet “The Boss”

Esta guía recoge **todo**: funciones previas y las nuevas ampliaciones orientadas a emergencias y APRS‑IS.

---

# 1. Arquitectura General

El módulo central es **meshtastic_to_aprs.py**, que se conecta a:

- TNC KISS (Direwolf / Soundmodem)
- Broker Mesh (JSONL)
- Bot de Telegram (UDP)
- APRS‑IS (lectura y escritura)

Flujos principales:

### 1.1 APRS → Mesh

Entrada por:
- RF → KISS TCP
- APRS‑IS → Cliente TCP

Funciones:

1. Leer trama en formato TNC2
2. Detectar `[CHx]` o `[CHx+M]` o `[CANAL x]`
3. Filtrar por `APRS_ALLOWED_SOURCES`
4. Respetar `NOGATE` / `RFONLY`
5. Opcionalmente procesar emergencia
6. Programar o enviar mensaje a Mesh
7. Añadir enlace a mapa si procede

### 1.2 Mesh → APRS

Entrada por:
- Comando `/aprs` desde bot o nodo Mesh
- Reenvío automático opcional desde canales definidos (`APRS_BACK_TO_APRS_CHANNELS`)

Salida:
- RF (KISS)
- APRS‑IS

Formato APRS “message” estándar:
```
SRC>APRS,PATH::DEST :texto{ID
```

---

# 2. Configuración (.env)

```ini
APRS_GATE_ENABLED=1
APRS_DEBUG=1
APRS_ALLOWED_SOURCES=EB2EAS-11,EB2EAS-7

APRS_KISS_HOST=host.docker.internal
APRS_KISS_PORT=8100

BROKER_HOST=127.0.0.1
BROKER_PORT=8765
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766

APRSIS_USER=EB2EAS-11
APRSIS_PASSCODE=XXXXX
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=m/50

MESHTASTIC_CHANNEL=1
```

Opcionales:

```ini
APRS_BACK_TO_APRS=1
APRS_BACK_TO_APRS_CALL=EB2EAS-7
APRS_BACK_TO_APRS_PATH=WIDE1-1,WIDE2-1
APRS_BACK_TO_APRS_CHANNELS=4

APRS_EMERGENCY_KEYWORDS=EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA
APRS_EMERGENCY_DESTS=EMERGENCY,EMERG,SOS
APRS_EMERGENCY_MAX_KM=50
HOME_LAT=41.638
HOME_LON=-0.902
MESH_EMERGENCY_CHANNELS=1,2
TELEGRAM_EMERG_CHAT_IDS=
```

---

# 3. APRS → Mesh

## 3.1 Mensaje directo

```
[CH4] Hola red Mesh
```

→ Mesh canal 4.

Formas válidas:
```
[CH4]
[CH 4]
[CANAL 4]
```

Prefijos eliminados antes de enviar a Mesh.

## 3.2 Programación de mensajes

```
[CH4+10] Aviso en 10 minutos
```

También modo compacto:
```
[CH42] → canal 4 + delay 2
```

## 3.3 Control remoto del gateway

Solo CH0:

```
[CH0] APRS ON
[CH0] APRS OFF
```

## 3.4 Enlaces de mapa

Si la trama APRS contiene posición:

→ Mesh recibe:
```
texto https://maps.google.com/?q=lat,lon
```

## 3.5 Prevención de loops

El sistema registra claves recientes para evitar repeticiones y loops circular APRS↔Mesh.

## 3.6 Respeto a NOGATE / RFONLY

Si la trama APRS contiene:

```
NOGATE
RFONLY
```

→ NO se envía a Mesh ni APRS‑IS.

---

# 4. APRS → Mesh vía APRS‑IS

Recepción desde APRS‑IS:

- Mensajes APRSdroid
- Mensajes de otros iGates (third‑party)
- Balizas
- Tramas dirigidas

Ejemplo real:

```
EB2EAS-7>APDR16,TCPIP*,qAC,T2UK::EB2EAS-11:[CH4] Hola prueba{1
```

El sistema:

1. Lo detecta  
2. Desenrolla third‑party si lo hubiera  
3. Extrae origen real  
4. Filtra por `APRS_ALLOWED_SOURCES`  
5. Procesa `[CH4]`  
6. Envía a Mesh

Log típico:
```
[aprs←IS→mesh] CH4 ← EB2EAS-7: Hola prueba{1 -> OK
```

---

# 5. Emergencias APRS

### 5.1 Detección

Basada en:
- Palabras clave (`EMERGENCIA`, `SOS`, etc.)
- Destinos APRS (`EMERGENCY`, `EMERG`, `SOS`)
- Tramas especiales

### 5.2 Bypass del gateway

Aunque `APRS_GATE_ENABLED=0`:

→ Emergencias **SÍ** se reenvían.

### 5.3 Geo‑fencing

Con `HOME_LAT/HOME_LON`:

- LOCAL (dentro de `APRS_EMERGENCY_MAX_KM`)
- REMOTA
- DESCONOCIDA (sin posición)

### 5.4 Reenvío redundante

```
MESH_EMERGENCY_CHANNELS=1,2
```

Emergencias locales → canal indicado + redundantes.

### 5.5 Heartbeat

Formato:

```
[EMERG APRS][LOCAL] src=EA2ABC-7 gate=OFF
texto...
maplink...
```

### 5.6 Telegram

Si configurado:
- Aviso directo al chat de emergencias o admins.

---

# 6. Mesh → APRS

## 6.1 Comando /aprs

Desde el bot o cualquier cliente Mesh:

```
/aprs EB2EAS-7: Hola desde Mesh
```

Genera en APRSdroid:

```
EB2EAS-11>APRS,WIDE1-1,WIDE2-1::EB2EAS-7 :Hola desde Mesh{1
```

## 6.2 Canal dedicado de retorno (Mesh → APRSdroid)

```
APRS_BACK_TO_APRS=1
APRS_BACK_TO_APRS_CALL=EB2EAS-7
APRS_BACK_TO_APRS_CHANNELS=4
```

→ Todo lo que ocurra en Mesh canal 4 puede reenviarse automáticamente a APRSdroid.

## 6.3 Envío manual desde Mesh

```
/aprs EB2EAS-7: [CH4] Alerta general
```

---

# 7. Ejemplos de uso

## 7.1 APRSdroid → Mesh → APRSdroid

1) Desde APRSdroid:

```
[CH4] Prueba EB2EAS-7
```

Log:
```
[aprs←IS→mesh] CH4 ← EB2EAS-7 → OK
```

Mensaje aparece en Mesh canal 4.

2) Respuesta desde Mesh:

```
/aprs EB2EAS-7: Recibido
```

APRSdroid recibe:
```
EB2EAS-11>APRS::EB2EAS-7 :Recibido{2
```

---

# 8. Variables finales (.env)

```ini
APRS_GATE_ENABLED=1
APRS_DEBUG=1
APRS_ALLOWED_SOURCES=EB2EAS-11,EB2EAS-7

APRS_KISS_HOST=host.docker.internal
APRS_KISS_PORT=8100

BROKER_HOST=127.0.0.1
BROKER_PORT=8765

APRSIS_USER=EB2EAS-11
APRSIS_PASSCODE=XXXXX
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580

APRS_EMERGENCY_KEYWORDS=EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA
APRS_EMERGENCY_DESTS=EMERGENCY,EMERG,SOS
APRS_EMERGENCY_MAX_KM=50
HOME_LAT=41.638
HOME_LON=-0.902
MESH_EMERGENCY_CHANNELS=1,2

APRS_BACK_TO_APRS=1
APRS_BACK_TO_APRS_CALL=EB2EAS-7
APRS_BACK_TO_APRS_PATH=WIDE1-1,WIDE2-1
APRS_BACK_TO_APRS_CHANNELS=4
```

---

# 9. Changelog

### v6.2
- Emergencias completas (bypass, geo‑fencing, redundancia)
- Recepción APRS‑IS → Mesh mejorada (third‑party)
- Heartbeat
- Envío Mesh → APRSdroid

### v6.1.3
- Programación APRS ([CHx+M], [CHxy])
- Control `[CH0] APRS ON/OFF`
- Limpieza automática de prefijos
- Conversión de coordenadas → Google Maps
- Prevención de loops
- Soporte APRS‑IS uplink

---

*Guía oficial de Pasarela APRS MeshNet “The Boss”.*
