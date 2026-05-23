# CLAUDE.md — MeshNet-Bot Codebase Guide

## Project Overview

**MeshNet-Bot** is a Python 3.11+ ecosystem for managing Meshtastic mesh radio networks. It provides a Telegram bot interface, a JSONL streaming broker, a bidirectional APRS gateway, multi-node bridges, a BBS bulletin system, a network auditor, and a task scheduler. Everything runs in Docker containers, targeting amd64, arm/v7 (Pi 2/3), and arm64 (Pi 4/5).

**Author / Attribution**: José Miguel Molina (EB2EAS). Attribution must be preserved in all derivatives per the LICENSE.

---

## Repository Layout

```
source/                  # All Python source files
docker/                  # Entrypoint shell scripts and compose samples
docs/                    # Extended markdown documentation
requirements/            # Pinned pip dependency files (split by component)
bridge-bc/               # Bridge config examples
Dockerfile*              # One per service (broker/bot, aprs, bridge, bridgehub, web)
docker-compose.yml       # Main orchestration
docker-compose.rpi.yml   # Raspberry Pi variant (pulls from GHCR)
.env_example             # Template for all environment variables (~420 vars)
README.md                # User-facing documentation (59 KB)
```

---

## Key Source Files

| File | Lines | Role |
|---|---|---|
| `source/Meshtastic_Broker.py` | ~9 300 | Core broker: Meshtastic → JSONL API on :8765, BacklogServer on :8766, BBS, embedded bridge, MeshCore |
| `source/Telegram_Bot_Broker.py` | ~16 900 | Telegram bot with 40+ commands; connects to broker instead of directly to the node |
| `source/meshtastic_to_aprs.py` | ~2 900 | Bidirectional Meshtastic ↔ APRS gateway (KISS TCP + optional APRS-IS) |
| `source/mesh_triple_bridge_brokerhub.py` | ~2 500 | Hub-and-spoke 3-node bridge with optional broker mode |
| `source/auditoria_red.py` | ~1 700 | Network health analysis and audit reports |
| `source/bbs_server.py` | ~2 000 | BBS bulletin/private-message system; SQLite3 + Fernet encryption |
| `source/broker_task.py` | ~1 200 | Persistent task scheduler (daily messages, weather, AEMET) |
| `source/aemet_alerts.py` | ~1 500 | Spanish meteorological alerts (RSS/Atom/CAP ingestion) |
| `source/meshtastic_api_adapter.py` | ~1 200 | API abstraction layer with CLI fallback for nodes/traceroute/telemetry |
| `source/Meshtastic_Relay_API.py` | ~540 | Node-table parsing, aliases, test messages |
| `source/tcpinterface_persistent.py` | ~430 | Persistent TCP connection pool with auto-reconnect |
| `source/positions_store.py` | ~200 | Position JSONL storage; KML/GPX export |
| `source/coverage_backlog.py` | ~700 | Coverage heatmap and KML generation from backlog |
| `source/bridge_in_broker.py` | ~960 | Embedded peer-safe bridge logic used inside the broker |
| `source/news_ingestor.py` | ~440 | RSS feed aggregator for BBS content |
| `source/weather_beacon.py` | ~330 | Dynamic weather template builder |
| `source/aprs_bbs_bridge.py` | ~250 | APRS → BBS message routing |
| `source/broker_resilience.py` | ~180 | Retry/resilience helpers |
| `source/mini_broker.py` | ~210 | Minimal broker for lightweight deployments |

---

## Architecture

```
Meshtastic Node (TCP :4403)
        │
        ▼
 Meshtastic_Broker.py   ←── BBS, bridge_in_broker, broker_task, positions_store, coverage_backlog
        │  JSONL :8765
        │  Control :8766 (BacklogServer)
        ▼
 Telegram_Bot_Broker.py ──► Telegram Bot API
        │
        └──► meshtastic_to_aprs.py ──► KISS TNC :8100 / APRS-IS :14580
        └──► mesh_triple_bridge_brokerhub.py (bridges to nodes B and C)
        └──► auditoria_red.py (on-demand or scheduled audits)
```

- The broker and bot communicate exclusively via TCP (127.0.0.1:8765/8766). They never share process state.
- `FETCH_BACKLOG` on :8766 is the control protocol; messages are newline-delimited JSON.
- Components log with prefixes like `[broker]`, `[bot]`, `[aprs]`, `[bridge]` for grep-friendly filtering.

---

## Development Environment

### Prerequisites
- Python 3.11+
- Docker + Docker Compose (for full stack)
- A Meshtastic node reachable via TCP (or USB/BLE)
- A Telegram Bot token

### Environment Variables
Copy `.env_example` to `.env` and fill in the required values:

```bash
cp .env_example .env
# Required minimums:
# TELEGRAM_TOKEN, ADMIN_IDS
# MESHTASTIC_HOST (or MESH_TRANSPORT=usb/bluetooth)
# BROKER_PORT=8765, BACKLOG_PORT=8766
```

The `.env_example` documents all ~420 variables with inline comments. Never commit `.env`.

### Running with Docker (standard)
```bash
docker compose up -d
docker compose logs -f broker
docker compose logs -f bot
```

### Running on Raspberry Pi
```bash
docker compose -f docker-compose.rpi.yml up -d
```

### Running source files directly
```bash
# Install dependencies
pip install -r requirements/requirements.txt
pip install -r requirements/requirements.bot.txt

# Start broker first
python source/Meshtastic_Broker.py --host 192.168.1.127 --port 8765

# Then start bot in a separate terminal
python source/Telegram_Bot_Broker.py
```

---

## CI/CD

**`.github/workflows/build-ghcr.yml`**:
- Triggers on push to `main`, version tags (`v*`), or manual dispatch.
- Detects which files changed and rebuilds only affected images.
- Publishes multi-arch images to GHCR:
  - `ghcr.io/jmmpcc/meshnet-bot-broker`
  - `ghcr.io/jmmpcc/meshnet-bot-bot`
  - `ghcr.io/jmmpcc/meshnet-bot-aprs`
  - `ghcr.io/jmmpcc/meshnet-bot-bridge`
  - `ghcr.io/jmmpcc/meshnet-bot-bridge-bc`
- Platforms: `linux/amd64`, `linux/arm/v7`, `linux/arm64`

---

## Coding Conventions

### Language & Style
- **Python 3.11+** throughout; no Node/TypeScript.
- Type hints used (`Dict[str, Any]`, `Optional[T]`, `list[dict]`).
- UTF-8 safe; Spanish strings are common (user-facing messages are often in Spanish).
- Timezone-aware datetimes; default timezone is `Europe/Madrid`.

### Async vs Threads
- `Telegram_Bot_Broker.py` uses **asyncio** via python-telegram-bot v20.
- `Meshtastic_Broker.py` is **multi-threaded** (one thread per connection/service).
- Do not mix asyncio event loops across these boundaries; communicate via TCP sockets or thread-safe queues.

### Logging
```python
import logging
logger = logging.getLogger(__name__)
logger.info("[broker] some event")   # always prefix with component tag
```

### Persistence
All persistent state lives under `bot_data/` (mounted as a Docker volume):

| File | Content |
|---|---|
| `positions.jsonl` | GPS positions (SNR, RSSI, timestamp) |
| `coverage.jsonl` | Coverage analysis data |
| `telemetry_log.jsonl` | Sensor telemetry (opt-in via `LOG_TELEMETRY=1`) |
| `broker_offline_log.jsonl` | Messages queued while broker was offline |
| `scheduled_tasks.jsonl` | Persistent task queue |
| `aemet_alerts_state.json` | AEMET alert deduplication state |
| `packet_log.jsonl` | Full packet log (opt-in via `BROKER_ENABLE_PACKET_LOG=1`) |
| `aprs_rx.jsonl` | APRS received frames |

- JSONL files are written with file locks; rotate automatically when >5 MB.
- BBS data uses SQLite3 at `BBS_DB_PATH` (default `bbs_data.db`) with Fernet-encrypted sensitive fields.
- Never hard-code paths; always read from environment variables or config defaults.

### Key Design Patterns
- **Pub/Sub** (`from pubsub import pub`) for intra-broker event distribution.
- **Persistent TCP pool** (`tcpinterface_persistent.py`) avoids Meshtastic connection storms.
- **Hash-based deduplication** with TTL (`DEDUP_TTL=45` seconds) for bridge loops.
- **Token-bucket rate limiting** per bridge side (`RATE_LIMIT_PER_SIDE=8` msg/min).
- **API → CLI fallback** (`meshtastic_api_adapter.py`) for traceroute/telemetry when SDK fails.
- **Graceful degradation**: features degrade quietly when optional services (APRS-IS, OpenWeather) are unreachable.

---

## External Integrations

| System | Protocol / API | Auth |
|---|---|---|
| Meshtastic node | TCP :4403 (SDK) | None (LAN) |
| MeshCore (optional) | Embedded library | Optional |
| APRS RF (TNC) | KISS TCP :8100 | None (local) |
| APRS-IS | TCP :14580 | Passcode |
| Telegram | Bot API (HTTPS) | Token |
| OpenWeather | REST | API key (optional) |
| AEMET | RSS/Atom/CAP | Free, no key |
| RSS feeds | HTTP | Free |
| OpenStreetMap tiles | HTTP | Free |
| is.gd | HTTP | Free |

---

## Testing

There is no automated test suite. Testing is done by:
1. Running the Docker stack and exercising bot commands manually.
2. Multi-arch build validation via GitHub Actions (build success = implicit smoke test).
3. CLI flags on individual modules (e.g. `--verbose`, `--dry-run` where present).

When adding new features:
- Test the broker and bot in isolation, then integrated.
- Verify Docker builds pass on both amd64 and arm64 if Dockerfiles are touched.
- Use `docker compose logs -f` to observe real-time output.

---

## Common Maintenance Tasks

### Update dependencies
Edit the relevant file under `requirements/` and rebuild:
```bash
docker compose build broker
```

### Add a new Telegram command
1. Add a handler function in `source/Telegram_Bot_Broker.py`.
2. Register it via `application.add_handler(CommandHandler(...))`.
3. Add to the `/help` menu and `README.md` command table.

### Add a new broker feature
1. Implement in a new module under `source/` if it's substantial.
2. Import and initialise it in `Meshtastic_Broker.py`.
3. Expose control via BacklogServer (:8766) if the bot needs to trigger it.
4. Persist state to `bot_data/` in JSONL or SQLite.

### Bump version
Version strings appear at the top of each major source file (e.g. `# v7.0.12`). Update them consistently across all modified files and document the change in `README.md` and `docs/Historial_Versiones.md`.

---

## What AI Assistants Should Know

- **Never** commit `.env` or any file with real credentials.
- **Never** modify `docker-compose.rpi.yml` image tags manually — they are updated by the CI pipeline.
- The broker and bot are intentionally separate processes; avoid adding shared in-process state between them.
- Spanish is the primary language for user-facing strings and documentation; English is used in code identifiers and log messages.
- The codebase deliberately avoids heavy ORM layers; JSONL + SQLite is the intentional choice for portability on low-memory Pi hardware.
- When touching `Meshtastic_Broker.py` or `Telegram_Bot_Broker.py`, be aware that these files are very large (~9 K and ~17 K lines). Make targeted, minimal edits and validate the surrounding context before changing shared helpers.
- All timeouts should be explicit (never `time.sleep` without a clear purpose); reconnect loops must use exponential backoff.
- Rate limits and dedup TTLs are configurable via `.env`; do not hard-code magic numbers.
