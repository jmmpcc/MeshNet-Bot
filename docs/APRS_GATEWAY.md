# APRS → MeshNet – Documentación Completa

## ⚙️ Descripción general

    Cuando está **activado el modo `aprs_on`** (o `APRS_GATE_ENABLED=1` en `.env`),  
    el sistema entra en **modo pasarela APRS bidireccional**, permitiendo que:

    > 🔄 Los mensajes enviados desde la red **Meshtastic** se publiquen en la red **APRS**,  
    > y los mensajes recibidos en **APRS (RF o APRS-IS)** se reenvíen automáticamente a **Meshtastic**.

    Este modo convierte tu nodo en un **gateway completo APRS↔Mesh**, compatible con **Direwolf**, **Soundmodem** o cualquier **TNC KISS TCP**.

    Este sistema es el broker el que recibe la trama através de RF - no interviene internet - y lo reenvía a la malla. Si internet está caído, permite enviar las tramas envias por APRS a la malla MESH.

### 1️⃣ Mensajes Meshtastic → APRS (uplink)
       - El bot de Telegram usa el comando `/aprs` para enviar mensajes.
       - Se comunica con el servicio `meshtastic_to_aprs.py` mediante **UDP (puerto 9464)**.
       - Este gateway convierte el mensaje al formato **AX.25 (KISS)** y lo transmite por radio.
       - Si hay configuradas credenciales APRS-IS (`APRSIS_USER`, `APRSIS_PASSCODE`), también se sube a **aprs. fi** automáticamente.
  
### 2️⃣ Mensajes APRS → Meshtastic (downlink)
      - El gateway escucha todas las tramas APRS recibidas por el puerto KISS.
      - Si el mensaje contiene un marcador `[CHx]` (por ejemplo `[CH1]`),  
        el gateway lo reenvía automáticamente al **canal correspondiente** de Meshtastic.
      - El reenvío se realiza hacia el **broker JSONL** (`BROKER_HOST:8765`).

📤 **Ejemplo de flujo:**
```
       Telegram → Bot → UDP 9464 → meshtastic_to_aprs.py → Soundmodem/Direwolf → RF (APRS)
                                                        ↳ opcional: APRS-IS (aprs.fi)
```

## Extensiones del Gateway APRS en MeshNet “The Boss”

Este documento reúne **todo lo implementado recientemente** en el gateway APRS, incluyendo:  
- Envío inmediato desde APRS a Meshtastic  
- Programación vía APRS  
- Comandos de control vía RF  
- Conversión de posiciones APRS a enlaces de mapa  
- Limpieza de prefijos  
- Heurísticas nuevas  
- Cambios internos  
- Ejemplos  
- Compatibilidad total  

---

# 1. Envío inmediato a la malla desde APRS

Para enviar un mensaje directamente a un canal Mesh desde APRS, usa uno de estos formatos:

```
[CH n] texto
[CHn] texto
[CH n ] texto
[CANAL n] texto
[CANALn] texto
```

**Ejemplos:**

```
[CH1] Hola a todos
[CH 4] Revisión del enlace
[CANAL7] Prueba de cobertura
```

El mensaje se envía **inmediatamente** al canal lógico `n`.

---

# 2. Envío programado desde APRS

Permite programar un envío para que ocurra dentro de `M` minutos, sin necesidad de bot ni Internet.


**Formato:**

```
[CH n+M] texto
```

- `n` → canal Mesh  
- `M` → minutos de retraso

**Ejemplos:**

```
[CH3+10] Aviso en 10 minutos
[CANAL 1+5] Recordatorio en 5 min
[CH7+30] Activación en 30 minutos
```

El gateway APRS programa el envío localmente y cuando pasan los minutos lo reenvía.

---

# 2.1 Compatibilidad con tramas APRS colapsadas

Muchos clientes APRS eliminan el signo `+` y agrupan todo en una sola cifra:

```
[CH4+2]  →  [CH42]
```

El sistema implementa una heurística:

```
Si XY > 15   → canal = X, delay = Y
```

Ejemplos:

| Entrada | Interpretación |
|--------|----------------|
| `[CH42]` | canal 4 – delay 2 |
| `[CH415]` | canal 4 – delay 15 |
| `[CH10]` | canal 10 – sin delay |
| `[CH7]` | canal 7 – sin delay |

---

# 3. Control del Gateway APRS → Mesh desde RF

Estas órdenes sólo se aceptan si el indicativo está incluido en:

```
APRS_ALLOWED_SOURCES=EA2XXX-7,EA2YYY-9
```

Comandos:

```
[CH0] APRS ON
[CH0] APRS OFF
```

- `APRS ON` → habilita toda la pasarela RF → Mesh  
- `APRS OFF` → bloquea temporalmente el reenvío

---

# 4. Conversión de posiciones APRS a enlaces de mapa

Si una trama APRS incluye posición, se genera un enlace clicable compatible con Google Maps:

**Entrada APRS:**

```
!4138.31N/00054.23W qrv R70
```

**Salida en la malla:**

```
qrv R70 https://maps.google.com/?q=41.638500,-0.903833
```

- Extrae coordenadas con `aprslib`
- Limpia el comentario
- Añade el enlace al mapa
- Si no hay comentario: solo el enlace

---

# 5. Limpieza automática del prefijo `[CH…]`

Para evitar que la malla se llene de comandos internos, el prefijo nunca aparece en el mensaje final.

Ejemplo recibido APRS:

```
[CH4+2] qrv R70-R72 sdr:...
```

Ejemplo mostrado en Mesh:

```
qrv R70-R72 sdr:... https://maps.google.com/?q=41.638000,-0.906167
```

---

# 6. Prevención de bucles y duplicados

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

# 7. Modo APRS-IS (Internet uplink)

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

# 8. Registro y depuración

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

# 9. Resumen técnico interno

Flujo completo en `task_aprs_to_meshtastic`:

1. Recepción de trama KISS  
2. Parseo AX.25  
3. Filtro por indicativo (`APRS_ALLOWED_SOURCES`)  
4. Extracción de canal + delay  
5. Limpieza del comentario  
6. Control de gateway cuando canal = 0  
7. Si delay: `_schedule_aprs_to_mesh`  
8. Si no delay: `_broker_send_text`  
9. Si es posición: conversión a enlace mapa  
10. Reenvío opcional APRS→APRS-IS  

---

# 10. Resumen rápido

```
[CH n] texto       → envío inmediato
[CH n+M] texto     → envío programado
[CH0] APRS ON      → activar gateway
[CH0] APRS OFF     → desactivar gateway
posiciones APRS    → enlace Google Maps
[CHXY]             → interpretado como CH X + delay Y si XY > 15
```

---

# 11. Formatos válidos

```
[CH4]
[CH 4]
[CH4+10]
[CH 4 + 10]
[CANAL4]
[CANAL 4+5]
[CH42]      → canal=4 delay=2 (heurística)
```

---

# 12. Variables requeridas

```
APRS_GATE_ENABLED=1
APRS_ALLOWED_SOURCES=EA2XXX-7,EA2YYY-9
MESHTASTIC_CHANNEL=0

A tener en cuenta las otras variables expuestas anteriormente.
```

`APRS_ALLOWED_SOURCES` puede estar vacío para permitir cualquier indicativo.

---

# 13. Ejemplos completos

    ### Inmediato:
    ```
    [CH1] Hola red Mesh
    ```

    ### Programado:
    ```
    [CH4+15] Aviso en 15 minutos
    ```

    ### Programado colapsado:
    ```
    [CH415] mensaje  → canal=4 delay=15
    [CH42] aviso     → canal=4 delay=2
    ```

    ### Control:
    ```
    [CH0] APRS ON
    [CH0] APRS OFF
    ```

    ### Posición:
    Entrada RF:
    ```
    !4138.31N/00054.23W qrv
    ```

    Salida Mesh:
    ```
    qrv https://maps.google.com/?q=41.638500,-0.903833
    ```

    ---

# 14. Changelog resumido

    ## v6.1.3 – Integración completa APRS→Mesh
    - Envío inmediato con `[CHn]`
    - Programación con `[CHn+M]`
    - Heurística `[CHXY] → (X,Y)`
    - Comandos `[CH0] APRS ON/OFF`
    - Conversión de posición a enlace Maps
    - Limpieza automática del prefijo
    - Mejoras en logs, parser y robustez

---

Fin del documento.
