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

# MeshNet — Última actualización

## v7.0.15-email-to-mesh-cli — Correo ↔ malla y wrapper CLI sencillo

### Añadido

- Pasarela **correo ↔ malla** documentada desde el README mediante la guía `docs/EMAIL_TO_MESH.md`.
- Soporte de envío **malla → correo** con comandos `[mail]`, `/mail` o `mail` recibidos desde Meshtastic/MeshCore.
- Libreta persistente de contactos de correo en `EMAIL_CONTACTS_PATH`, compartida por servicio, broker y bot.
- Envío SMTP saliente configurable con `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `EMAIL_SMTP_SSL`, `EMAIL_SMTP_STARTTLS`, `EMAIL_SMTP_USER`, `EMAIL_SMTP_PASSWORD`, `EMAIL_FROM` y `EMAIL_OUT_SUBJECT_PREFIX`.
- Comandos de Telegram para gestionar contactos y enviar correos:
  - `/mail_contactos`
  - `/mail_add contacto correo@dominio`
  - `/mail_edit contacto_o_numero nuevo@correo`
  - `/mail_del contacto_o_numero`
  - `/mail contacto_o_numero texto mensaje`
- CLI de `email_to_mesh.py` para operar la libreta y enviar correo desde consola:
  - `contacts` / `list` / `ls`
  - `contact-add` / `add` / `mail_add`
  - `contact-edit` / `edit` / `mail_edit`
  - `contact-del` / `del` / `rm` / `mail_del`
  - `send` / `mail`
- Nuevo wrapper de host `scripts/email-to-mesh` para ejecutar esos comandos dentro del contenedor sin escribir el `docker compose exec` completo.

### Mejorado

- La pasarela mantiene el flujo **correo → malla** por IMAP/IMAP IDLE con remitentes autorizados y línea base segura para no procesar correos antiguos salvo que se active `EMAIL_PROCESS_EXISTING=1`.
- Se añade selección automática de red para asuntos sin prefijo mediante `EMAIL_MESH_NETWORK` / `EMAIL_DEFAULT_NETWORK` y `RADIO_PROFILE`.
- El bot incluye los comandos de correo en su menú oficial de Telegram.
- La documentación centraliza configuración, ejemplos, pruebas y resolución de problemas de IMAP/SMTP en `docs/EMAIL_TO_MESH.md`.

### Sintaxis rápida

```text
[mail] lista
[mail] contacto texto mensaje
[mail] 1 texto mensaje
/mail contacto_o_numero texto mensaje
scripts/email-to-mesh mail_contactos
scripts/email-to-mesh mail_add eb2eas eb2eas@example.org
scripts/email-to-mesh mail eb2eas Mensaje desde CLI sin comillas
```

### Conservado

- El resto del README se mantiene sin cambios funcionales.
- La pasarela APRS, el broker, MeshCore, BBS, auditorías, web admin y comandos existentes mantienen su documentación en sus apartados habituales.
- `/aprs`, `/diario`, `/diario_mc`, `/enviar`, `/enviar_mc` y demás comandos existentes no cambian por esta actualización del README.

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


### ✉️ Guía correo ↔ malla

> Documentación detallada de la pasarela `email-to-mesh`: correo→malla,
> malla→correo, libreta de contactos, script CLI sencillo, bot y ejemplos de uso.

📘 **[Abrir guía completa → EMAIL_TO_MESH.md](./docs/EMAIL_TO_MESH.md)**

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

### Envío directo con `/enviar`: Meshtastic, APRS o ambos

`/enviar` es el comando de envío inmediato para Meshtastic y, desde esta versión, también puede seleccionar APRS o doble salida igual que `/diario`.

#### Sintaxis

```text
/enviar [mesh|aprs|ambos] <destino[:canal] | canal N> [aprs <CALL|broadcast>] <texto…>
```

#### Transportes aceptados

| Transporte | Alias aceptados | Qué hace |
|---|---|---|
| `mesh` | `malla`, `meshtastic` | Envía solo a la malla Meshtastic. Es el modo por defecto si no pones transporte. |
| `aprs` | `aprs-only`, `solo-aprs` | Envía solo a APRS mediante la pasarela APRS por UDP. No envía a Meshtastic. |
| `ambos` | `both`, `mesh+aprs`, `aprs+mesh` | Envía a la malla Meshtastic y también a APRS. |

#### Destino Meshtastic

Se mantiene la sintaxis clásica:

```text
/enviar canal 0 Texto al canal 0
/enviar mesh canal 2 Texto al canal 2
/enviar !b03df4cc Texto directo al nodo
/enviar alias_del_nodo Texto directo al alias
/enviar broadcast:1 Texto broadcast en canal 1
```

- `canal N` envía en broadcast por el canal lógico Meshtastic `N`.
- `<destino>:<canal>` permite forzar canal junto al destino.
- `<número|!id|alias>` envía al nodo indicado.
- `forzado` al inicio mantiene el comportamiento anterior para omitir comprobaciones previas cuando corresponda.

#### Destino APRS

En modo `aprs`, el destino se interpreta como APRS:

```text
/enviar aprs EB2ABC-7: Mensaje APRS directo
/enviar aprs broadcast: Mensaje APRS broadcast/status
/enviar aprs EB2ABC-7 Mensaje APRS directo
```

En modo `ambos`, el destino APRS se puede añadir tras el destino Meshtastic con el modificador `aprs <CALL|broadcast>`:

```text
/enviar ambos canal 0 aprs EB2ABC-7 Aviso para malla y APRS
/enviar ambos !b03df4cc aprs broadcast Aviso a nodo Meshtastic y APRS broadcast
/enviar both canal 2 aprs broadcast Mensaje doble
```

Si en modo `ambos` no se indica `aprs <CALL|broadcast>`, APRS usa `broadcast` por defecto.

#### Ejemplos completos y resultado esperado

| Ejemplo | Salida real | Respuesta esperada del bot |
|---|---|---|
| `/enviar canal 0 Hola malla` | Solo Meshtastic canal 0 | `Transporte: MESH`, destino malla `broadcast`, resultado Meshtastic. |
| `/enviar mesh canal 1 Reunión a las 18:00` | Solo Meshtastic canal 1 | `Transporte: MESH`, canal 1, resultado `OK` o `KO`. |
| `/enviar aprs EB2ABC-7: Prueba APRS` | Solo APRS a `EB2ABC-7` | `Transporte: APRS`, destino APRS, partes enviadas. |
| `/enviar aprs broadcast: Estado desde bot` | Solo APRS broadcast/status | `Transporte: APRS`, destino `broadcast`, partes enviadas. |
| `/enviar ambos canal 0 aprs EB2ABC-7 Aviso doble` | Meshtastic canal 0 + APRS a `EB2ABC-7` | Resultado Meshtastic y resultado APRS separados. |
| `/enviar ambos canal 0 Aviso doble sin destino APRS` | Meshtastic canal 0 + APRS broadcast | Resultado Meshtastic y APRS con destino `broadcast`. |

#### Resultado mostrado

Cuando el envío es por malla, el bot muestra una respuesta similar a:

```text
✉️ Envío a broadcast (canal 0)
Transporte: MESH
Malla Meshtastic → Destino: broadcast
Traceroute: —  Hops: 0
Forzado: No
Resultado: OK (broker-queue)
Respuestas en 5s: 0
```

Cuando el envío es solo APRS:

```text
✉️ Envío Meshtastic/APRS
Transporte: APRS
APRS → Destino: EB2ABC-7
Resultado APRS: OK (1/1 partes)
```

Cuando el envío es a ambos:

```text
✉️ Envío a broadcast (canal 0)
Transporte: BOTH
Malla Meshtastic → Destino: broadcast
Resultado: OK (broker-queue)
Respuestas en 5s: 0
APRS → Destino: EB2ABC-7
Resultado APRS: OK (1/1 partes)
```

#### Notas operativas

- El modo clásico sin prefijo sigue funcionando y equivale a `mesh`.
- El envío Meshtastic prioriza la cola del broker, mantiene fallback al pool persistente y al adapter resiliente.
- Broadcast Meshtastic no solicita ACK de aplicación para evitar duplicados.
- APRS se envía por la pasarela configurada en `APRS_CTRL_HOST` / `APRS_CTRL_PORT`.
- APRS divide mensajes largos según `APRS_MAX_LEN`.
- En modo `ambos`, el texto enviado a APRS es el texto limpio del mensaje, no el comando de Telegram.

### Envío directo con `/enviar_mc`: MeshCore, APRS o ambos

`/enviar_mc` es el envío inmediato por canal MeshCore. También puede seleccionar si el mensaje sale solo por MeshCore, solo por APRS o por ambos transportes.

#### Sintaxis

```text
/enviar_mc [mesh|aprs|ambos] <chX|X|canal X> [aprs <CALL|broadcast>] <texto…>
/enviar_mc aprs <CALL|broadcast>: <texto…>
```

#### Transportes aceptados

| Transporte | Alias aceptados | Qué hace |
|---|---|---|
| `mesh` | `malla`, `meshcore`, `mc` | Envía solo a MeshCore. Es el modo por defecto si no pones transporte. |
| `aprs` | `aprs-only`, `solo-aprs` | Envía solo a APRS. No envía a MeshCore ni necesita canal MeshCore. |
| `ambos` | `both`, `mesh+aprs`, `aprs+mesh` | Envía a MeshCore y también a APRS. |

#### Destino MeshCore

Se mantiene la sintaxis clásica:

```text
/enviar_mc ch2 Texto por MeshCore
/enviar_mc [ch2] Texto por MeshCore
/enviar_mc 2 Texto por MeshCore
/enviar_mc canal 2 Texto por MeshCore
/enviar_mc mesh ch2 Texto por MeshCore
```

- `ch2`, `[ch2]`, `2` y `canal 2` son formas equivalentes para `channel_idx=2`.
- El envío se ejecuta mediante el broker con `MESHCORE_SEND`.
- Este comando es para canales MeshCore; para mensajes directos a contacto se mantiene `/enviar_mc_dm` / `/dm_mc`.

#### Destino APRS

En modo solo APRS:

```text
/enviar_mc aprs EB2ABC-7: Mensaje APRS desde comando MeshCore
/enviar_mc aprs broadcast: Mensaje APRS broadcast/status
/enviar_mc aprs EB2ABC-7 Mensaje APRS sin dos puntos
```

En modo `ambos`, el destino APRS se puede indicar tras el canal MeshCore con `aprs <CALL|broadcast>`:

```text
/enviar_mc ambos ch2 aprs EB2ABC-7 Mensaje MeshCore y APRS
/enviar_mc ambos canal 2 aprs broadcast Mensaje MeshCore y APRS broadcast
/enviar_mc both 2 aprs EB2ABC-7 Mensaje doble
```

Si en modo `ambos` no se indica destino APRS, APRS usa `broadcast` por defecto.

#### Ejemplos completos y resultado esperado

| Ejemplo | Salida real | Respuesta esperada del bot |
|---|---|---|
| `/enviar_mc ch2 Hola MeshCore` | Solo MeshCore canal 2 | `Transporte: MESH`, canal MeshCore y resultado MeshCore. |
| `/enviar_mc mesh canal 2 Hola MeshCore` | Solo MeshCore canal 2 | Igual que el modo clásico, indicando transporte. |
| `/enviar_mc aprs EB2ABC-7: Hola APRS` | Solo APRS | `Transporte: APRS`, destino APRS y partes enviadas. |
| `/enviar_mc aprs broadcast: Estado APRS` | Solo APRS broadcast/status | Resultado APRS con destino `broadcast`. |
| `/enviar_mc ambos ch2 aprs EB2ABC-7 Hola doble` | MeshCore canal 2 + APRS a `EB2ABC-7` | Resultado MeshCore y APRS separados. |
| `/enviar_mc ambos ch2 Hola doble sin destino APRS` | MeshCore canal 2 + APRS broadcast | Resultado MeshCore y APRS con destino `broadcast`. |

#### Resultado mostrado

Solo MeshCore:

```text
Envío MeshCore
Transporte: MESH
Malla MeshCore → Canal (channel_idx): 2
Resultado MeshCore: OK
```

Solo APRS:

```text
Envío MeshCore/APRS
Transporte: APRS
APRS → Destino: EB2ABC-7
Resultado APRS: OK (1/1 partes)
```

MeshCore + APRS:

```text
Envío MeshCore
Transporte: BOTH
Malla MeshCore → Canal (channel_idx): 2
Resultado MeshCore: OK
APRS → Destino: EB2ABC-7
Resultado APRS: OK (1/1 partes)
```

#### Notas operativas

- El modo clásico sin prefijo sigue funcionando y equivale a `mesh`.
- En modo `aprs`, no se exige canal MeshCore porque no hay envío MeshCore.
- En modo `ambos`, el texto enviado a APRS es el texto limpio del mensaje, no el comando de Telegram.
- APRS se envía por la pasarela configurada en `APRS_CTRL_HOST` / `APRS_CTRL_PORT`.
- APRS divide mensajes largos según `APRS_MAX_LEN`.

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
- También puedes probar APRS desde los comandos de envío:
  - `/enviar aprs broadcast: Prueba APRS desde enviar` ⇒ solo APRS.
  - `/enviar ambos canal 0 aprs broadcast Prueba doble` ⇒ Meshtastic + APRS.
  - `/enviar_mc aprs broadcast: Prueba APRS desde MeshCore` ⇒ solo APRS.
  - `/enviar_mc ambos ch2 aprs broadcast Prueba doble MeshCore` ⇒ MeshCore + APRS.
- En respuestas correctas verás `Transporte: APRS` o `Transporte: BOTH`, destino APRS y contador de partes.
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

