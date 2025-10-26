# MeshNet “The Boss” — Broker JSONL + Bot de Telegram + Pasarela APRS (Docker)

Este repositorio contiene un *stack* Docker listo para usar que integra:

- **Broker JSONL** para Meshtastic (v5.x) — exporta eventos, backlog y tareas.
- **Bot de Telegram** con comandos de red (vecinos, traceroute, telemetría, enviar, programar, APRS, etc.).
- **Pasarela APRS** (Meshtastic ⇄ APRS KISS y APRS-IS opcional), con filtros por etiqueta `[CHx]`.

> Objetivo: que cualquiera pueda clonar, rellenar su `.env` y levantar los contenedores en minutos.

---

## 🧱 Requisitos

- **Docker** y **Docker Compose v2** (`docker compose`).
- Un **nodo Meshtastic** accesible por TCP (normalmente `IP_DEL_NODO:4403`).
- (Opcional) Un **TNC KISS por TCP** (Direwolf/Soundmodem) en el host: `host.docker.internal:8100` en Windows/macOS o `127.0.0.1:8100` en Linux.
- (Opcional) Credenciales de **APRS-IS** (indicativo con SSID y *passcode*) para subir posiciones etiquetadas.
- Un **bot de Telegram** (Token) y, opcionalmente, lista de administradores.

---

## 🚀 Puesta en marcha rápida

```bash
# 1) Clona el repositorio
git clone https://github.com/tu-usuario/the-boss-docker.git
cd the-boss-docker

# 2) Crea tu fichero de entorno
cp .env-example.txt .env
# 3) Edita .env con tus valores

# 4) Levanta los servicios principales
docker compose -f docker-compose.sample.yml up -d

# 5) Ver logs
docker compose -f docker-compose.sample.yml logs -f broker
docker compose -f docker-compose.sample.yml logs -f bot
docker compose -f docker-compose.sample.yml logs -f aprs
```

> Consejo: Si usas **Direwolf**/**Soundmodem** en el host, arráncalo antes y verifica el puerto TCP (p.ej. 8100).

---

## ⚙️ Variables de entorno (`.env`)

Crea un `.env` en la raíz (puedes partir de `.env-example.txt`). Mínimo, ajusta:

| Clave | Descripción | Ejemplo |
|---|---|---|
| `MESHTASTIC_HOST` | IP/host del nodo Meshtastic (TCPInterface) | `192.168.1.201` |
| `MESHTASTIC_PORT` | Puerto TCP Meshtastic | `4403` |
| `BROKER_PORT` | Puerto **JSONL** del broker hacia clientes | `8765` |
| `BROKER_CTRL_PORT` | Puerto **backlog/ctrl** del broker | `8766` |
| `TELEGRAM_TOKEN` | Token del bot de Telegram | `123456:ABC...` |
| `ADMIN_IDS` | Lista de IDs (coma/semi-colon) admin | `1111,2222` |
| `KISS_HOST` | Host del TNC KISS TCP | `host.docker.internal` (Win/macOS) / `127.0.0.1` (Linux) |
| `KISS_PORT` | Puerto del TNC KISS TCP | `8100` |
| `MESHTASTIC_CH` | Canal lógico por defecto para inyección desde APRS si no hay etiqueta | `0` |
| `BOT_START_DELAY` | Segundos de espera del bot al iniciar | `90` |

**APRS‑IS (opcional):**

| Clave | Descripción | Ejemplo |
|---|---|---|
| `APRSIS_USER` | Indicativo-SSID para APRS‑IS | `EB2XXX-10` |
| `APRSIS_PASSCODE` | *Passcode* del indicativo | `12345` |
| `APRSIS_HOST` | Servidor APRS‑IS | `rotate.aprs2.net` |
| `APRSIS_PORT` | Puerto APRS‑IS | `14580` |
| `APRSIS_FILTER` | Filtro APRS‑IS opcional | `m/50` |

**Ajustes KISS (10 ms/unidad):** `KISS_TXDELAY=30` (300 ms), `KISS_PERSIST=200`, `KISS_SLOTTIME=10`, `KISS_TXTAIL=3`.

**Control/red del broker:**

- El servicio **aprs** usa `network_mode: "service:broker"` para **compartir red** y hablar con broker por `127.0.0.1`.
- En Windows/macOS usa `host.docker.internal` para alcanzar el TNC KISS del host.

---

## 🧩 Servicios y puertos

- **broker**
  - Expone JSONL en `:${BROKER_PORT}` y backlog/ctrl en `:${BROKER_CTRL_PORT}`.
  - Lee del nodo Meshtastic por TCP (`MESHTASTIC_HOST:MESHTASTIC_PORT`).
  - Persiste datos en `./bot_data`.

- **bot**
  - Habla con el broker (`BROKER_PORT`) y backlog/ctrl.
  - Comandos: ver [COMMANDS.md](./COMMANDS.md).

- **aprs**
  - **KISS TCP** hacia tu TNC: `KISS_HOST:KISS_PORT`.
  - **Control UDP** en `127.0.0.1:9464` (compartiendo red con broker).
  - **Broker JSONL** en `127.0.0.1:${BROKER_PORT}`.
  - Sube a **APRS‑IS** si `APRSIS_USER` y `APRSIS_PASSCODE` están definidos.
  - **Reinyecta a malla SOLO** tramas con `[CHx]` / `[CANAL x]`.

---

## 🗂 Datos persistentes

- `./bot_data/positions.jsonl` y `positions_last.json` — últimas posiciones.
- `./bot_data/scheduled_tasks.jsonl` — planificador de mensajes.
- `./bot_data/maps/` — salidas de cobertura (HTML/KML).

> Monta `bot_data` como volumen para persistir entre reinicios.

---

## 🔐 Seguridad y buenas prácticas

- No subas a git tokens ni passcodes: mantenlos solo en `.env`.
- Usa IDs admin reales para limitar comandos avanzados.
- Evita exponer puertos a Internet si no es necesario.

---

## 🧪 Pruebas rápidas

1) **Bot**: `/estado`, `/ver_nodos`, `/vecinos`, `/traceroute !id`.
2) **APRS**: `/aprs canal 0 [CH0] Hola APRS` → debe salir por KISS; con `APRSIS_*` se suben posiciones etiquetadas.
3) **Programación**: `/en 5 canal 0 Recordatorio` → `/tareas`.

---

## 🛠 Troubleshooting

- Bot sin respuesta: revisa `TELEGRAM_TOKEN` y `BOT_START_DELAY`.
- APRS no transmite: comprueba `KISS_HOST:KISS_PORT` y acceso desde el contenedor.
- Tramas a malla: recuerda que SOLO se reinyectan con `[CHx]` / `[CANAL x]` en el comentario.

---

## 📋 Licencia y contribuciones

- PRs e *issues* bienvenidos.
- Adjunta logs recortados y tu `.env` sin secretos al reportar.

---

### Créditos

- Meshtastic® y su comunidad; aprslib / Direwolf / Soundmodem.
- Usuarios testers de “The Boss”.