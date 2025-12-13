# Mesh Triple Bridge – Enlace externo A↔B↔C para Meshtastic

Sistema de puente externo para Meshtastic que permite enlazar hasta **tres nodos** de forma estable:

- **Nodo A** – Nodo principal del sistema (normalmente el que usa el broker/bot).
- **Nodo B** – Nodo remoto nº1.
- **Nodo C** – Nodo remoto nº2.

El objetivo es crear una “estrella lógica” con el nodo A como centro, donde B y C quedan totalmente integrados, sin modificar el broker ni depender de Docker.

---

## 1. Arquitectura general

Esquema típico cuando el broker/bot está en Docker (o en otro proceso) conectado al nodo A:

```text
             +---------------------------+
             |      BROKER / BOT         |
             |   (Docker o proceso local)|
             |           │               |
             |       Conexión TCP        |
             +------------│--------------+
                          │ A_HOST:A_PORT
                          ▼
                    +-----------+
                    |   Nodo A  |
                    +-----------+
                      ▲       ▲
         A2B_CH_MAP   │       │   A2C_CH_MAP
         B2A_CH_MAP   │       │   C2A_CH_MAP
                      │       │
                +-----------+ +-----------+
                |  Nodo B   | |  Nodo C   |
                +-----------+ +-----------+

                 (Triple Bridge Externo)
```

El script `mesh_triple_bridge.py` mantiene conexiones TCP simultáneas con A, B y C, y reenvía mensajes siguiendo los mapas de canal definidos.

---

## 2. Casos de uso habituales

### 2.1. Broker en Docker solo con A, y puente externo A↔B↔C

Escenario recomendado:

- El contenedor Docker (broker, bot, APRS, web, etc.) se conecta solo a A.
- El bridge embebido del broker está desactivado.
- El script `mesh_triple_bridge.py` (fuera de Docker) crea:
  - A ↔ B
  - A ↔ C
- Efecto:
  - Cualquier mensaje que toque A se propaga adecuadamente a B y C.
  - Broker y bot siguen viendo una única malla: la del nodo A.

Ejemplo de configuración en `.env` del broker:

```env
MESH_HOST=192.168.1.126
MESH_PORT=4403
BRIDGE_ENABLED=0
```

Y el triple bridge se configura independientemente con su propio `.env` (ver más abajo).

### 2.2. Red existente A↔B embebida y añadir solo C con el triple bridge

Escenario alternativo:

- El broker sigue teniendo activo su bridge embebido A↔B.
- Solo se quiere añadir un nuevo nodo C enlazado con A.
- El `mesh_triple_bridge.py` se utiliza únicamente para A↔C.

En este caso:

- En el `.env` del triple bridge:
  - `A2B_CH_MAP` y `B2A_CH_MAP` se dejan vacíos.
  - Solo se definen `A2C_CH_MAP` y `C2A_CH_MAP`.
- El broker sigue gestionando A↔B por dentro.
- El triple bridge gestiona únicamente A↔C.

Ejemplo:

```env
A2B_CH_MAP=
B2A_CH_MAP=
A2C_CH_MAP=0:0
C2A_CH_MAP=0:0
```

### 2.3. Red sin broker, solo triple bridge

También es posible:

- No usar broker ni bot.
- Ejecutar únicamente `mesh_triple_bridge.py` en una Raspberry o PC.
- Así se enlazan tres mallas Meshtastic (A, B, C) entre sí, sin más software.

---

## 3. Archivo principal: `mesh_triple_bridge.py`

Este script:

- Abre conexiones TCP a los tres nodos usando `meshtastic.tcp_interface` o `tcpinterface_persistent`.
- Se suscribe al bus de eventos `pubsub` de Meshtastic.
- Analiza cada paquete recibido:
  - Tipo de aplicación (texto, posición, telemetría).
  - Canal lógico.
  - Nodo emisor (`fromId`).
- Decide si se reenvía:
  - Desde A hacia B.
  - Desde A hacia C.
  - Desde B hacia A.
  - Desde C hacia A.
- Aplica:
  - Deduplicación basada en hash y TTL.
  - Rate–limit por sentido.
  - Anti-bucle local (no reenvía mensajes generados por el propio destino).
  - Etiquetas opcionales en el texto para trazar desde qué puente ha pasado.

---

## 4. Requisitos

Se necesita Python 3.9 o superior.

Instalación de dependencias mínimas:

```bash
pip install meshtastic pubsub
```

Si se utiliza una implementación de conexión persistente como `tcpinterface_persistent.TCPInterface`, el archivo correspondiente debe estar accesible en el mismo directorio o en el `PYTHONPATH`.

---

## 5. Configuración: archivo `.env`

Se recomienda crear un `.env` específico para el triple bridge, por ejemplo:

`/home/pi/mesh-triple-bridge/.env-triple-bridge` en Raspberry  
`C:\MeshTripleBridge\.env-triple-bridge` en Windows

Ejemplo completo:

```env
# --- NODOS ---
A_HOST=192.168.1.xxx
A_PORT=4403

B_HOST=192.168.1.xxx
B_PORT=4403

C_HOST=192.168.1.xxx
C_PORT=4403

# --- MAPAS DE CANALES ---
# Formato: origen:destino,origen:destino,...
A2B_CH_MAP=0:0
B2A_CH_MAP=0:0

A2C_CH_MAP=0:0
C2A_CH_MAP=0:0

# --- TIPOS DE MENSAJE A REENVIAR ---
FORWARD_TEXT=1
FORWARD_POSITION=0

# --- CONTROL ---
RATE_LIMIT_PER_SIDE=8
DEDUP_TTL=45
REQUIRE_ACK=0

# --- ETIQUETAS PARA LOGS / TEXTO ---
TAG_BRIDGE=[BRIDGE]
# Opcionales y más específicos:
# TAG_BRIDGE_A2B=[B]
# TAG_BRIDGE_B2A=[A]
# TAG_BRIDGE_A2C=[C]
# TAG_BRIDGE_C2A=[A]
```

### 5.1. Ejemplo 1: Todos en canal 0

Supuesto:

- A, B y C utilizan el mismo canal 0 para mensajes de texto.

Configuración:

```env
A2B_CH_MAP=0:0
B2A_CH_MAP=0:0
A2C_CH_MAP=0:0
C2A_CH_MAP=0:0
```

Efecto:

- Cualquier mensaje de texto en canal 0 de A se replica en canal 0 de B y de C.
- Cualquier mensaje de texto que llegue por B canal 0 se inyecta en A canal 0.
- Cualquier mensaje de texto que llegue por C canal 0 se inyecta en A canal 0.

### 5.2. Ejemplo 2: Canales diferentes por nodo

Supuesto:

- Nodo A usa canal 0 para tráfico interno.
- Nodo B debe usar canal 1.
- Nodo C debe usar canal 2.

Configuración:

```env
A2B_CH_MAP=0:1
B2A_CH_MAP=1:0

A2C_CH_MAP=0:2
C2A_CH_MAP=2:0
```

Efecto:

- A canal 0 → B canal 1, C canal 2.
- B canal 1 → A canal 0 → C canal 2.
- C canal 2 → A canal 0 → B canal 1.

### 5.3. Ejemplo 3: Varios canales mapeados

Supuesto:

- Quieres compartir dos canales:
  - Canal 0 de A con canal 0 de B y C.
  - Canal 1 de A con canal 3 de B y canal 4 de C.

Configuración:

```env
A2B_CH_MAP=0:0,1:3
B2A_CH_MAP=0:0,3:1

A2C_CH_MAP=0:0,1:4
C2A_CH_MAP=0:0,4:1
```

---

## 6. Ejecución en Raspberry Pi (sin Docker)

### 6.1. Preparación inicial

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
mkdir -p ~/mesh-triple-bridge
cd ~/mesh-triple-bridge
```

Copiar en este directorio:

- `mesh_triple_bridge.py`
- `.env-triple-bridge`

Crear entorno virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install meshtastic pubsub
```

### 6.2. Lanzar manualmente

```bash
cd ~/mesh-triple-bridge
source .venv/bin/activate
export $(grep -v '^#' .env-triple-bridge | xargs)
python mesh_triple_bridge.py
```

Verás en consola:

```text
[triple-bridge] Conectando A: 192.168.1.126:4403
[triple-bridge] Conectando B: 192.168.1.140:4403
[triple-bridge] Conectando C: 192.168.1.150:4403
[triple-bridge] local_id_A=!a1b2c3d4
[triple-bridge] local_id_B=!92ab0c11
[triple-bridge] local_id_C=!03aa99ff
```

Al enviar un mensaje desde A canal 0:

```text
[triple-bridge] A2B A->B ch 0->0 txt(25B) OK
[triple-bridge] A2C A->C ch 0->0 txt(25B) OK
```

Al enviar desde B:

```text
[triple-bridge] B2A B->A ch 0->0 txt(18B) OK
```

Al enviar desde C:

```text
[triple-bridge] C2A C->A ch 0->0 txt(18B) OK
```

### 6.3. Servicio systemd en Raspberry Pi

Crear el archivo de servicio:

`/etc/systemd/system/mesh-triple-bridge.service`

```ini
[Unit]
Description=Mesh Triple Bridge (A<->B<->C)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/mesh-triple-bridge
EnvironmentFile=/home/pi/mesh-triple-bridge/.env-triple-bridge
ExecStart=/home/pi/mesh-triple-bridge/.venv/bin/python /home/pi/mesh-triple-bridge/mesh_triple_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Activar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-triple-bridge.service
```

Ver logs:

```bash
journalctl -u mesh-triple-bridge.service -f
```

---

## 7. Ejecución en Windows (sin Docker)

### 7.1. Preparar directorio

Crear carpeta, por ejemplo:

`C:\MeshTripleBridge`

Copiar:

- `mesh_triple_bridge.py`
- `.env-triple-bridge`

### 7.2. Crear entorno virtual

Abrir PowerShell:

```powershell
cd C:\MeshTripleBridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install meshtastic pubsub
```

### 7.3. Cargar variables del `.env` (sesión actual)

```powershell
Get-Content .\.env-triple-bridge | ForEach-Object {
  if (-not $_.StartsWith("#") -and $_.Trim() -ne "") {
    $k,$v = $_.Split("=",2)
    [System.Environment]::SetEnvironmentVariable($k, $v, "Process")
  }
}
```

### 7.4. Ejecutar el script

```powershell
python .\mesh_triple_bridge.py
```

Verás en la consola de PowerShell mensajes similares a los descritos en la sección de Raspberry.

---

## 8. Lógica de reenvío detallada

### 8.1. Desde A hacia B y C

Cuando un paquete llega por la interfaz de A:

- Se comprueba el tipo:
  - Texto (TEXT_MESSAGE_APP), si `FORWARD_TEXT=1`.
  - Posición/telemetría, si `FORWARD_POSITION=1`.
- Se mira el canal `ch` del paquete.
- Si `ch` está en `A2B_CH_MAP`:
  - Se calcula `out_ch = A2B_CH_MAP[ch]`.
  - Se reenvía a B con `channelIndex=out_ch`.
- Si `ch` está en `A2C_CH_MAP`:
  - Se calcula `out_ch = A2C_CH_MAP[ch]`.
  - Se reenvía a C con ese canal.

### 8.2. Desde B hacia A

Cuando un paquete llega por B:

- Se mira el canal `ch`.
- Si `ch` está en `B2A_CH_MAP`:
  - Se calcula `out_ch = B2A_CH_MAP[ch]`.
  - Se reenvía a A por ese canal.

Desde el punto de vista del broker/bot:

- El mensaje originado en B aparece como un mensaje Mesh normal recibido por A.

### 8.3. Desde C hacia A

Simétrico a B:

- Canal `ch` recibido en C.
- Si `ch` está en `C2A_CH_MAP`, se reenvía a A usando el canal correspondiente.

### 8.4. Comunicación B ↔ C a través de A

Gracias a la combinación:

- B → A (B2A)
- A → C (A2C)
- C → A (C2A)
- A → B (A2B)

El efecto es que B y C pueden “verse” mutuamente aunque no estén conectados directamente, sino a través de A.

---

## 9. Deduplicación y protección contra bucles

El script incluye un mecanismo de seguridad:

- Cada mensaje se identifica por un hash que combina:
  - Dirección (A2B, B2A, A2C, C2A).
  - Emisor (`fromId`).
  - Canal.
  - Contenido (texto normalizado o JSON ordenado).
- Se guarda el hash con un tiempo de vida (`DEDUP_TTL`, por defecto 45 s).
- Si el mismo hash vuelve a aparecer dentro de ese intervalo, se descarta.
- Además se aplica rate–limit por sentido (`RATE_LIMIT_PER_SIDE` mensajes/minuto).

Ejemplo de efecto práctico:

- Si un mensaje ya ha sido reenviado de A a B, un rebote idéntico no vuelve a salir hacia B.
- Evita cascadas del tipo B→A→B→A...

---

## 10. Ejemplos prácticos de pruebas

### 10.1. Prueba básica de A hacia B y C

1. Lanzar el triple bridge y confirmar conexiones.
2. Desde el nodo A, enviar un mensaje en canal 0, por ejemplo:
   - Texto: `Hola desde A`
3. Comprobar:
   - Consola bridge:
     - `A2B A->B ch 0->0 txt(...) OK`
     - `A2C A->C ch 0->0 txt(...) OK`
   - Pantallas o clientes de B y C:
     - Ambos deben recibir el texto.

### 10.2. Prueba desde B hacia A y C

1. Desde B, enviar: `Mensaje desde B` en canal 0.
2. Comprobar:
   - Consola bridge:
     - `B2A B->A ch 0->0 txt(...) OK`
   - Nodo A:
     - Recibe el mensaje de B (el broker/bot lo verá como recibido en la malla).
   - Si el mapa `A2C_CH_MAP` está definido, el siguiente paso será:
     - `A2C A->C ch 0->0 txt(...) OK`
   - Nodo C:
     - También ve el mensaje originado en B.

### 10.3. Prueba desde C hacia A y B

Simétrica:

1. Desde C, enviar un texto por canal 0.
2. Ver en consola:
   - `C2A C->A ch 0->0 txt(...) OK`
   - Posteriormente, `A2B A->B ch 0->0 txt(...) OK` si `A2B_CH_MAP` lo permite.
3. B y A deben ver el mensaje.

---

## 11. Resolución de problemas habituales

### 11.1. No conecta con algún nodo

Síntomas:

- Consola muestra errores al conectar, o se queda colgado.

Pasos:

1. Verificar IP y puerto:
   - `A_HOST`, `B_HOST`, `C_HOST`.
   - `A_PORT`, `B_PORT`, `C_PORT`.
2. Comprobar conectividad desde la máquina donde corre el script:
   - `ping IP_NODO`
   - `nc -vz IP_NODO 4403` (Linux) o herramientas equivalentes en Windows.
3. Verificar que el nodo Meshtastic tiene activado el servidor TCP.

### 11.2. No se ven reenvíos en consola

- Revisar si el tipo de mensaje está permitido:
  - `FORWARD_TEXT=1` para textos.
  - `FORWARD_POSITION=1` para posiciones.
- Confirmar que los canales están en los mapas:
  - El canal que usas debe estar presente en `A2B_CH_MAP`, `B2A_CH_MAP`, etc.
- Revisar el rate–limit:
  - Si se envían muchos mensajes seguidos, puede bloquearse temporalmente el reenvío.

### 11.3. Sospecha de bucles o tráfico excesivo

- Aumentar el `DEDUP_TTL` para que un mismo contenido quede “congelado” más tiempo:
  - Por ejemplo: `DEDUP_TTL=90`.
- Reducir `RATE_LIMIT_PER_SIDE`:
  - Por ejemplo: `RATE_LIMIT_PER_SIDE=4`.
- Verificar que no hay otros procesos adicionales haciendo de puente con la misma malla.

---

## 12. Resumen

`mesh_triple_bridge.py` permite:

- Enlazar tres nodos Meshtastic A, B y C, con A como centro.
- Mantener el broker y el bot aislados de la complejidad de los puentes.
- Escalar la red añadiendo nuevos nodos remotos sin reescribir el broker.
- Controlar con precisión qué canales y qué tipos de mensajes se replican.

Con una configuración cuidadosa de los mapas de canales y las variables de control, se consigue una malla robusta, ampliable y preparada para escenarios de uso intensivo o de emergencia.
