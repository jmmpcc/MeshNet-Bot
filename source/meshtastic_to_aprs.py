#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meshtastic_to_aprs.py (v7.0.47)
Puente Meshtastic ⇄ APRS vía Soundmodem (KISS TCP 8100) + Control UDP local.

- /aprs (bot) -> UDP local -> TX APRS (troceo automático).
- APRS -> Mesh: si el comentario/status contiene [CHx], se reenvía al canal x del broker.
Con verificacion en APRS-IS:
    python meshtastic_to_aprs_v5.4.py --aprsis-user EB2XXX-10 --aprsis-passcode 12345
Sin verificacion APRS-IS
    python meshtastic_to_aprs_v5.4.py

"""

from __future__ import annotations
import asyncio, hashlib, json, os, re, socket, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple
import aprslib

# === [NUEVO] Canal KISS (0=A, 1=B, etc.) y saneo ASCII ===
import unicodedata

# === [WEB ADMIN] Log jsonl de tramas APRS RX para el mapa del panel ===
# Nota: se escribe en un fichero separado (por defecto bot_data/aprs_rx.jsonl) para NO tocar positions.jsonl.
BOT_DATA_DIR = os.getenv("BOT_DATA_DIR", "bot_data")
APRS_RX_LOG_PATH = os.getenv("BOT_APRS_RX_PATH", os.path.join(BOT_DATA_DIR, "aprs_rx.jsonl"))

# === [WEB ADMIN v7.0.3] APRS RX -> backlog del broker =========================
# Objetivo:
#   - Mantener aprs_rx.jsonl exactamente como estaba.
#   - Duplicar cada RX APRS en el JSONL que el broker usa para FETCH_BACKLOG.
#   - Permitir que el WebPanel vea portnum=APRS_RX sin tocar el broker ni transmitir RF.
#
# Requisito operativo:
#   - El contenedor meshnet-aprs debe compartir el mismo BOT_DATA_DIR que el broker,
#     o bien definir BROKER_OFFLINE_LOG_PATH apuntando al broker_offline_log.jsonl real.
APRS_RX_BROKER_BACKLOG_ENABLED = int(os.getenv("APRS_RX_BROKER_BACKLOG_ENABLED", "1") or "1")
APRS_RX_BROKER_BACKLOG_DEBUG = int(os.getenv("APRS_RX_BROKER_BACKLOG_DEBUG", "0") or "0")
BROKER_OFFLINE_LOG_PATH = os.getenv(
    "BROKER_OFFLINE_LOG_PATH",
    os.getenv("BOT_BROKER_OFFLINE_LOG_PATH", os.path.join(BOT_DATA_DIR, "broker_offline_log.jsonl")),
)
_APRS_BROKER_BACKLOG_LOCK = threading.Lock()

try:
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
except Exception:
    pass

def _aprs_web_append(rec: dict) -> None:
    """Append robusto (best-effort) a APRS_RX_LOG_PATH en formato JSONL."""
    try:
        with open(APRS_RX_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # No interrumpir la pasarela por fallos de IO
        return


def _aprs_broker_backlog_row(rec: dict) -> dict:
    """
    Convierte una trama APRS RX al formato plano que lee FETCH_BACKLOG.

    Uso interno:
      row = _aprs_broker_backlog_row(rec)

    Parámetros:
      rec: diccionario APRS generado en task_kiss_to_mesh(), con callsign, info,
           raw, lat/lon opcionales y timestamp.

    Funcionalidad:
      - Genera portnum=APRS_RX.
      - Incluye rx_time, que es el campo usado por _iter_backlog_jsonl() del broker
        para filtrar since_ts/until_ts.
      - No cambia aprs_rx.jsonl y no transmite RF.
    """
    rec = rec or {}
    ts = int(rec.get("ts") or time.time())
    callsign = str(rec.get("callsign") or "").strip().upper()
    info = str(rec.get("info") or "")
    raw = rec.get("raw")

    row = {
        "ts": ts,
        "rx_time": ts,
        "channel": None,
        "portnum": "APRS_RX",
        "from": f"APRS:{callsign}" if callsign else "APRS",
        "to": str(rec.get("dest") or "APRS"),
        "from_alias": callsign or None,
        "to_alias": str(rec.get("dest") or "APRS"),
        "text": info,
        "info": info,
        "raw": raw,
        "aprs": 1,
        "aprs_rx": 1,
        "aprs_type": rec.get("type"),
        "aprs_callsign": callsign or None,
        "aprs_dest": rec.get("dest"),
        "aprs_path": rec.get("path"),
    }

    # Atajos útiles para parser/mapa si aprslib consiguió posición.
    for key in ("lat", "lon", "course", "speed", "alt", "symbol"):
        if rec.get(key) is not None:
            row[key] = rec.get(key)

    # Estructura compatible con extractores que esperan packet.decoded.
    row["packet"] = {
        "fromId": row["from"],
        "toId": row["to"],
        "rxTime": ts,
        "decoded": {
            "portnum": "APRS_RX",
            "text": info,
            "payload": {
                "callsign": callsign,
                "info": info,
                "raw": raw,
                "lat": row.get("lat"),
                "lon": row.get("lon"),
            },
        },
    }
    row["summary"] = {
        "portnum": "APRS_RX",
        "text": info,
        "from": row["from"],
        "from_alias": callsign or None,
    }
    return row


def _aprs_broker_backlog_append(rec: dict) -> None:
    """
    Añade una RX APRS al broker_offline_log.jsonl para que FETCH_BACKLOG la vea.

    Uso interno:
      _aprs_broker_backlog_append(rec)

    Funcionalidad:
      - Escritura best-effort y protegida con lock local.
      - No interrumpe la pasarela APRS si falla el disco/ruta/permisos.
      - No modifica aprs_rx.jsonl ni altera los filtros [CHx] existentes.
      - No transmite RF; solo hace visible APRS_RX para WebPanel/FETCH_BACKLOG.
    """
    if not APRS_RX_BROKER_BACKLOG_ENABLED:
        return
    try:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(BROKER_OFFLINE_LOG_PATH)))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        row = _aprs_broker_backlog_row(rec)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _APRS_BROKER_BACKLOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        if APRS_RX_BROKER_BACKLOG_DEBUG:
            print(f"[aprs→broker-backlog] APRS_RX append OK path={path} src={row.get('from_alias')} len={len(line)}")
    except Exception as e:
        if APRS_RX_BROKER_BACKLOG_DEBUG:
            print(f"[aprs→broker-backlog] ❌ {type(e).__name__}: {e}")
        return

KISS_CHANNEL = int(os.getenv("KISS_CHANNEL", "0"))
if not (0 <= KISS_CHANNEL <= 15):
    KISS_CHANNEL = 0

_APRS_ALLOWED = set(chr(c) for c in range(32, 127))  # 0x20..0x7E

# --- Lista blanca de indicativos APRS autorizados (RF -> Mesh/Control)
# Formato: variable de entorno APRS_ALLOWED_SOURCES="EA2XXX-7,EA2YYY-9"
APRS_ALLOWED_SOURCES = {
    s.strip().upper()
    for s in os.getenv("APRS_ALLOWED_SOURCES", "").split(",")
    if s.strip()
}

import builtins, sys, time
_builtin_print = builtins.print

def _print_with_ts(*args, **kwargs):
    file = kwargs.pop("file", sys.stdout)
    end = kwargs.pop("end", "\n")
    sep = kwargs.pop("sep", " ")
    flush = kwargs.pop("flush", True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _builtin_print(f"[{ts}]", *args, sep=sep, end=end, file=file, flush=flush, **kwargs)

builtins.print = _print_with_ts


def _aprs_source_allowed(src: str) -> bool:
    """
    Devuelve True si el indicativo de origen está autorizado.
    Si APRS_ALLOWED_SOURCES está vacío, no se filtra nada (todo permitido).
    """
    if not APRS_ALLOWED_SOURCES:
        return True
    return (src or "").strip().upper() in APRS_ALLOWED_SOURCES



def _aprs_ascii(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = s.encode("ascii", "ignore").decode("ascii", "ignore")
    s = "".join(ch if ch in _APRS_ALLOWED else "?" for ch in s)
    return " ".join(s.split())

def _to_ascii7(text: str) -> str:
    """
    Convierte a ASCII 7-bit seguro.
    Sustituye caracteres no válidos por '?' para no romper la trama.
    """
    if not text:
        return ""
    return "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in text)


# =========================
# CONFIG
# =========================
BROKER_HOST = os.getenv("BROKER_HOST", "127.0.0.1").strip()
BROKER_PORT = int(os.getenv("BROKER_PORT", "8765"))

KISS_HOST = os.getenv("KISS_HOST", "127.0.0.1").strip()
KISS_PORT = int(os.getenv("KISS_PORT", "8100"))

MY_CALL = os.getenv("APRS_CALL", "").strip()
GATEWAY_DEST_PREFIX = os.getenv("APRS_GATEWAY_PREFIX", "").strip().upper()
APRS_PATH = [p for p in (os.getenv("APRS_PATH", "WIDE1-1,WIDE2-1").strip() or "").split(",") if p]

MAX_MSG_LEN = int(os.getenv("APRS_MSG_MAX", "67"))
MAX_STATUS_LEN = int(os.getenv("APRS_STATUS_MAX", "67"))

MESHTASTIC_CHANNEL = int(os.getenv("MESHTASTIC_CH", "0"))  # canal por defecto Mesh

# Control UDP local (bot -> APRS)
CONTROL_UDP_HOST = os.getenv("APRS_CTRL_HOST", "127.0.0.1").strip()
CONTROL_UDP_PORT = int(os.getenv("APRS_CTRL_PORT", "9464"))
# Dirección de escucha del gateway. Se separa de APRS_CTRL_HOST porque:
# - APRS_CTRL_HOST es la dirección que usan los clientes para enviar peticiones.
# - APRS_CTRL_BIND es la interfaz donde este proceso abre el socket UDP.
#
# Compatibilidad: si APRS_CTRL_BIND no existe, conserva el comportamiento
# histórico y escucha en APRS_CTRL_HOST. En Raspberry/Docker se recomienda
# APRS_CTRL_BIND=0.0.0.0 junto con una publicación Compose limitada a
# 127.0.0.1 del host, permitiendo el acceso a aplicaciones systemd locales sin
# exponer el control APRS a la LAN.
CONTROL_UDP_BIND = os.getenv("APRS_CTRL_BIND", CONTROL_UDP_HOST).strip() or CONTROL_UDP_HOST

# BacklogServer del broker (para APRS -> Mesh)
BROKER_CTRL_HOST = os.getenv("BROKER_CTRL_HOST", BROKER_HOST)
try:
    BROKER_CTRL_PORT = int(os.getenv("BROKER_CTRL_PORT", str(int(BROKER_PORT) + 1)))
except Exception:
    BROKER_CTRL_PORT = 8766  # fallback

# --- Uplink APRS-IS (aprs.fi) con aprslib ---
# Si APRSIS_USER y APRSIS_PASSCODE están definidos y no vacíos → subimos a APRS-IS.
APRSIS_USER     = os.getenv("APRSIS_USER", "").strip()     # p.ej. "EB2XXX-10"
APRSIS_PASSCODE = os.getenv("APRSIS_PASSCODE", "").strip() # passcode APRS-IS para ese indicativo
APRSIS_HOST     = os.getenv("APRSIS_HOST", "rotate.aprs2.net").strip()
APRSIS_PORT     = int(os.getenv("APRSIS_PORT", "14580"))
APRSIS_FILTER   = os.getenv("APRSIS_FILTER", "").strip()   # opcional, p.ej. "m/50"
HOME_NODE_ID = os.getenv("HOME_NODE_ID", "").strip()

# --- NUEVO: Mirror Mesh -> APRS-IS (para ver canales en APRSDroid) ---
APRSIS_PUSH_ENABLED = int(os.getenv("APRSIS_PUSH_ENABLED", "0") or "0")
APRSIS_PUSH_TO = (os.getenv("APRSIS_PUSH_TO", "") or "").strip().upper()
APRSIS_PUSH_CHANNELS_RAW = (os.getenv("APRSIS_PUSH_CHANNELS", "all") or "all").strip().lower()
APRSIS_PUSH_PREFIX = int(os.getenv("APRSIS_PUSH_PREFIX", "1") or "1")
APRSIS_PUSH_MIN_GAP_S = float(os.getenv("APRSIS_PUSH_MIN_GAP_S", "1.0") or "1.0")

# --- Boletines públicos APRS-IS para emergencias confirmadas ---
# Esta salida reutiliza la misma conexión APRS-IS persistente del gateway.
# Permanece desactivada salvo doble autorización: APRSIS_PUSH_ENABLED y
# APRSIS_EMERGENCY_BULLETIN_ENABLED.
APRSIS_EMERGENCY_BULLETIN_ENABLED = int(os.getenv("APRSIS_EMERGENCY_BULLETIN_ENABLED", "0") or "0")
APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL = (os.getenv("APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL", "high") or "high").strip().lower()
APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC", "300") or "300"))
APRSIS_EMERGENCY_BULLETIN_DEDUP_SEC = max(0.0, float(os.getenv("APRSIS_EMERGENCY_BULLETIN_DEDUP_SEC", "1800") or "1800"))
APRSIS_EMERGENCY_BULLETIN_GROUP = (os.getenv("APRSIS_EMERGENCY_BULLETIN_GROUP", "") or "").strip()

# Diagnóstico manual v7.0.46. No lo usa ninguna salida automática.
APRSIS_LONG_TEST_ENABLED = int(os.getenv("APRSIS_LONG_TEST_ENABLED", "0") or "0")
APRSIS_LONG_TEST_MAX_CHARS = max(80, min(450, int(os.getenv("APRSIS_LONG_TEST_MAX_CHARS", "400") or "400")))
APRSIS_LONG_BULLETIN_TEST_ENABLED = int(os.getenv("APRSIS_LONG_BULLETIN_TEST_ENABLED", "0") or "0")

# Catálogo central de grupos APRS-IS reservados para futuras salidas.
# Solo Emergencias está conectada actualmente a publicación automática; el
# resto queda preparado y documentado, sin provocar tráfico por sí mismo.
APRSIS_AEMET_BULLETIN_GROUP = (os.getenv("APRSIS_AEMET_BULLETIN_GROUP", "AEMET") or "AEMET").strip()
APRSIS_FARMACIAS_BULLETIN_GROUP = (os.getenv("APRSIS_FARMACIAS_BULLETIN_GROUP", "FARMA") or "FARMA").strip()
APRSIS_NEWS_BULLETIN_GROUP = (os.getenv("APRSIS_NEWS_BULLETIN_GROUP", "NEWS") or "NEWS").strip()
APRSIS_SYSTEM_BULLETIN_GROUP = (os.getenv("APRSIS_SYSTEM_BULLETIN_GROUP", "MESH") or "MESH").strip()
APRSIS_TEST_BULLETIN_GROUP = (os.getenv("APRSIS_TEST_BULLETIN_GROUP", "TEST") or "TEST").strip()
APRSIS_EMERGENCY_BULLETIN_STATE_PATH = Path(os.getenv(
    "APRSIS_EMERGENCY_BULLETIN_STATE_PATH",
    os.path.join(os.getenv("BOT_DATA_DIR", "/app/bot_data"), "aprsis_emergency_bulletins.json"),
))
_APRSIS_EMERGENCY_LOCK = asyncio.Lock()
_APRSIS_EMERGENCY_LEVELS = {"low": 10, "medium": 20, "high": 30, "critical": 40}

_APRSIS_PUSH_LAST_TS = 0.0

# --- IDs de mensajes APRS-IS (para evitar dedupe y mejorar visibilidad en clientes) ---
_APRSIS_PUSH_MSGID: dict[str, int] = {}

def _aprsis_next_msgid(dst_call: str) -> str:
    """
    Devuelve un ID de mensaje '{nn}' (00-99) por destino, para evitar supresión de duplicados.
    APRSdroid y otros clientes suelen mostrar mejor los mensajes con ID.
    """
    d = (dst_call or "").strip().upper()
    if not d:
        return "{00}"
    n = int(_APRSIS_PUSH_MSGID.get(d, 0))
    n = (n + 1) % 100
    _APRSIS_PUSH_MSGID[d] = n
    return f"{{{n:02d}}}"


def _parse_push_channels(raw: str) -> Optional[set[int]]:
    """Compatibilidad histórica: canales Meshtastic sin prefijo."""
    cfg = _parse_push_channel_config(raw)
    return cfg.get("meshtastic")


def _parse_push_channel_config(raw: str) -> dict[str, Optional[set[int]]]:
    """
    Parsea la configuración de canales del push APRS-IS por transporte.

    Sintaxis admitida:
      - "all" / "0,1,2"                         -> Meshtastic (legacy)
      - "meshtastic all" / "meshtastic 0,1"     -> Meshtastic
      - "meshcore all" / "meshcore 0,1"         -> MeshCore
      - "meshtastic 0,1 meshcore 2,3"           -> ambos

    Devuelve {transport: canales}; canales None significa ALL. La ausencia de
    clave significa transporte no habilitado. En RADIO_PROFILE=meshcore_only se
    ignora cualquier configuración que no use explícitamente el prefijo meshcore.
    """
    r = (raw or "").strip().lower()
    if not r:
        r = "all"

    aliases = {"meshtastic": "meshtastic", "mesh": "meshtastic", "malla": "meshtastic", "meshcore": "meshcore", "mc": "meshcore"}
    meshcore_only = _radio_profile() == "meshcore_only"
    has_prefix = any(tok in aliases for tok in re.split(r"[\s,;]+", r) if tok)

    if not has_prefix:
        if meshcore_only:
            return {}
        parsed = _parse_push_channel_list(r)
        return {"meshtastic": parsed}

    cfg: dict[str, Optional[set[int]]] = {}
    current: str | None = None
    buckets: dict[str, list[str]] = {"meshtastic": [], "meshcore": []}
    for tok in re.split(r"[\s,;]+", r):
        tok = tok.strip()
        if not tok:
            continue
        if tok in aliases:
            current = aliases[tok]
            continue
        if current is not None:
            buckets[current].append(tok)

    for transport, parts in buckets.items():
        if meshcore_only and transport != "meshcore":
            continue
        if parts:
            cfg[transport] = _parse_push_channel_list(",".join(parts))
    return cfg


def _parse_push_channel_list(raw: str) -> Optional[set[int]]:
    r = (raw or "").strip().lower()
    if not r or r == "all":
        return None
    out = set()
    for part in re.split(r"[,;\s]+", r):
        part = part.strip()
        if not part:
            continue
        if part == "all":
            return None
        if part.isdigit():
            out.add(max(0, min(15, int(part))))
    return out if out else None

def _aprsis_push_is_enabled() -> bool:
    return bool(APRSIS_PUSH_ENABLED) and bool(APRSIS_PUSH_TO) and _aprsis_ready()


def _aprsis_emergency_bulletin_is_enabled() -> bool:
    """Comprueba la doble autorización y la disponibilidad de APRS-IS."""
    return (
        bool(APRSIS_PUSH_ENABLED)
        and bool(APRSIS_EMERGENCY_BULLETIN_ENABLED)
        and _aprsis_ready()
    )


def _load_aprsis_emergency_state() -> dict:
    """Carga el estado persistente de boletines; un fichero corrupto no para el gateway."""
    try:
        data = json.loads(APRSIS_EMERGENCY_BULLETIN_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": 1, "events": {}}
    except FileNotFoundError:
        return {"version": 1, "events": {}}
    except Exception as exc:
        print(f"[APRS-IS BLN] state load WARN: {type(exc).__name__}: {exc}")
        return {"version": 1, "events": {}}


def _save_aprsis_emergency_state(state: dict) -> None:
    """Guarda el estado mediante sustitución atómica para evitar escrituras parciales."""
    try:
        APRSIS_EMERGENCY_BULLETIN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = APRSIS_EMERGENCY_BULLETIN_STATE_PATH.with_suffix(
            APRSIS_EMERGENCY_BULLETIN_STATE_PATH.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, APRSIS_EMERGENCY_BULLETIN_STATE_PATH)
    except Exception as exc:
        print(f"[APRS-IS BLN] state save WARN: {type(exc).__name__}: {exc}")


def _normalize_aprsis_bulletin_group(raw_group: str) -> str:
    """Normaliza el grupo APRS a 0..5 caracteres alfanuméricos ASCII.

    Uso interno:
      group = _normalize_aprsis_bulletin_group(APRSIS_EMERGENCY_BULLETIN_GROUP)

    Parámetros:
      raw_group: nombre configurado por el operador.

    Funcionalidad:
      - Convierte a mayúsculas y ASCII seguro.
      - Elimina espacios, guiones y signos no válidos.
      - Limita a cinco caracteres para que BLNx+grupo ocupe como máximo
        los nueve caracteres del addressee APRS.
      - Una cadena vacía conserva el formato histórico BLN0..BLN9.
    """
    ascii_group = _aprs_ascii(raw_group).upper()
    return "".join(ch for ch in ascii_group if ch.isalnum())[:5]




def _aprsis_bulletin_group_for(source: str) -> str:
    """Devuelve el grupo APRS-IS reservado para una fuente del sistema.

    Uso interno:
      group = _aprsis_bulletin_group_for("emergencias")

    Parámetros:
      source: nombre lógico de la aplicación o familia de avisos.

    Funcionalidad:
      - Centraliza los grupos propuestos EMERG, AEMET, FARMA, NEWS, MESH y TEST.
      - Admite alias habituales en español e inglés.
      - Normaliza siempre a un máximo de cinco caracteres APRS válidos.
      - Devuelve cadena vacía para fuentes desconocidas, evitando publicaciones
        accidentales en un grupo no definido.
      - No activa ninguna salida ni transmite por sí misma.
    """
    key = (source or "").strip().lower().replace("-", "_")
    groups = {
        "emergencias": APRSIS_EMERGENCY_BULLETIN_GROUP,
        "emergency": APRSIS_EMERGENCY_BULLETIN_GROUP,
        "aemet": APRSIS_AEMET_BULLETIN_GROUP,
        "meteorologia": APRSIS_AEMET_BULLETIN_GROUP,
        "weather": APRSIS_AEMET_BULLETIN_GROUP,
        "farmacias": APRSIS_FARMACIAS_BULLETIN_GROUP,
        "farmacia": APRSIS_FARMACIAS_BULLETIN_GROUP,
        "pharmacy": APRSIS_FARMACIAS_BULLETIN_GROUP,
        "news": APRSIS_NEWS_BULLETIN_GROUP,
        "noticias": APRSIS_NEWS_BULLETIN_GROUP,
        "mesh": APRSIS_SYSTEM_BULLETIN_GROUP,
        "meshnet": APRSIS_SYSTEM_BULLETIN_GROUP,
        "sistema": APRSIS_SYSTEM_BULLETIN_GROUP,
        "system": APRSIS_SYSTEM_BULLETIN_GROUP,
        "test": APRSIS_TEST_BULLETIN_GROUP,
        "pruebas": APRSIS_TEST_BULLETIN_GROUP,
    }
    return _normalize_aprsis_bulletin_group(groups.get(key, ""))


def _aprsis_bulletin_name(number: int, group: str | None = None) -> str:
    """Construye BLN0..BLN9 o BLN0GRUPO..BLN9GRUPO."""
    safe_number = max(0, min(9, int(number)))
    safe_group = _normalize_aprsis_bulletin_group(
        _aprsis_bulletin_group_for("emergencias") if group is None else group
    )
    return f"BLN{safe_number}{safe_group}"


def _allocate_aprsis_bulletin_slot(state: dict, event_id: str) -> str:
    """Asigna de forma estable una línea BLN a cada emergencia activa.

    Mantiene el número de una asignación histórica BLN0..BLN9 cuando se activa
    posteriormente un grupo, migrándola de forma natural a BLN0GRUPO.
    """
    events = state.setdefault("events", {})
    existing = events.get(event_id, {}) if isinstance(events.get(event_id), dict) else {}
    slot = str(existing.get("bulletin", "")).strip().upper()
    current_group = _aprsis_bulletin_group_for("emergencias")

    match = re.fullmatch(r"BLN([0-9])([A-Z0-9]{0,5})", slot)
    if match:
        return _aprsis_bulletin_name(int(match.group(1)), current_group)

    used = {
        str(value.get("bulletin", "")).strip().upper()
        for value in events.values()
        if isinstance(value, dict) and not value.get("closed", False)
    }
    for number in range(10):
        candidate = _aprsis_bulletin_name(number, current_group)
        if candidate not in used:
            return candidate

    # Si las diez líneas están ocupadas, se reutiliza una de forma determinista.
    number = int(hashlib.sha256(event_id.encode("utf-8")).hexdigest(), 16) % 10
    return _aprsis_bulletin_name(number, current_group)


async def send_aprsis_emergency_bulletin(
    *, event_id: str, text: str, severity: str, status: str,
) -> dict:
    """Publica una emergencia grave como boletín público APRS-IS.

    No transmite por RF, no usa APRSIS_PUSH_TO y no solicita ACK. La función
    aplica nivel mínimo, deduplicación, intervalo mínimo y estado BLN estable.
    """
    event_id = str(event_id or "").strip()
    severity = str(severity or "").strip().lower()
    status = str(status or "active").strip().lower()
    clean_text = _aprs_ascii(text)

    if not APRSIS_PUSH_ENABLED:
        return {"ok": False, "sent": False, "reason": "aprsis_push_disabled"}
    if not APRSIS_EMERGENCY_BULLETIN_ENABLED:
        return {"ok": False, "sent": False, "reason": "bulletin_disabled"}
    if not _aprsis_ready():
        return {"ok": False, "sent": False, "reason": "aprsis_not_configured"}
    if not event_id or not clean_text:
        return {"ok": False, "sent": False, "reason": "missing_event_or_text"}
    minimum_rank = _APRSIS_EMERGENCY_LEVELS.get(APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL, 30)
    if _APRSIS_EMERGENCY_LEVELS.get(severity, 0) < minimum_rank:
        return {"ok": False, "sent": False, "reason": "severity_below_threshold"}

    now = time.time()
    terminal = status in {"resolved", "cancelled", "expired", "closed"}

    async with _APRSIS_EMERGENCY_LOCK:
        state = _load_aprsis_emergency_state()
        events = state.setdefault("events", {})
        previous = events.get(event_id, {}) if isinstance(events.get(event_id), dict) else {}
        bulletin = _allocate_aprsis_bulletin_slot(state, event_id)
        previous_bulletin = str(previous.get("bulletin", "")).strip().upper()
        bulletin_changed = bool(previous_bulletin and previous_bulletin != bulletin)
        digest = hashlib.sha256(
            f"{event_id}|{severity}|{status}|{bulletin}|{clean_text}".encode("utf-8")
        ).hexdigest()
        last_sent = float(previous.get("last_sent", 0.0) or 0.0)

        if (
            not bulletin_changed
            and previous.get("digest") == digest
            and (now - last_sent) < APRSIS_EMERGENCY_BULLETIN_DEDUP_SEC
        ):
            return {
                "ok": True, "sent": False, "duplicate": True,
                "reason": "duplicate", "bulletin": previous.get("bulletin"),
            }
        if (
            not bulletin_changed
            and last_sent
            and (now - last_sent) < APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC
            and severity != "critical"
            and not terminal
        ):
            return {
                "ok": True, "sent": False, "reason": "rate_limited",
                "retry_after": max(1, int(APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC - (now - last_sent))),
                "bulletin": previous.get("bulletin"),
            }

        body = clean_text
        if terminal and not body.upper().startswith("FIN "):
            body = f"FIN {body}"
        line = _aprsis_tnc2_message_line(bulletin, body, with_msgid=False)
        if not line:
            return {"ok": False, "sent": False, "reason": "line_build_failed"}

        sent = await _aprsis_send_line_safe(line)
        if not sent:
            return {"ok": False, "sent": False, "reason": "aprsis_send_failed", "bulletin": bulletin}

        events[event_id] = {
            "bulletin": bulletin,
            "digest": digest,
            "severity": severity,
            "status": status,
            "last_text": body,
            "last_sent": now,
            "closed": terminal,
            "group": _aprsis_bulletin_group_for("emergencias"),
        }
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_aprsis_emergency_state(state)
        print(f"[APRS-IS BLN] {bulletin} event={event_id} severity={severity} status={status} text={body[:80]}")
        return {"ok": True, "sent": True, "bulletin": bulletin, "line": line}


async def send_aprsis_long_test(text: str) -> dict:
    """Envía una prueba larga exclusivamente a APRS-IS.

    Requiere APRSIS_LONG_TEST_ENABLED=1, reutiliza la conexión APRS-IS
    existente y nunca transmite por KISS/RF. No modifica boletines,
    deduplicación ni MIN_INTERVAL de emergencias.
    """
    if not bool(APRSIS_LONG_TEST_ENABLED):
        return {"ok": True, "sent": False, "reason": "disabled"}
    if not _aprsis_ready():
        return {"ok": False, "sent": False, "reason": "aprsis_unavailable"}
    clean = _aprs_ascii(text).strip()
    if not clean:
        return {"ok": False, "sent": False, "reason": "empty_text"}
    clean = clean[:APRSIS_LONG_TEST_MAX_CHARS]
    line = f"{APRSIS_USER}>APRS,TCPIP*:>{clean}"
    sent = bool(await _aprsis_send_line_safe(line))
    return {
        "ok": sent, "sent": sent,
        "reason": "sent" if sent else "send_failed",
        "chars": len(clean), "text": clean, "line": line,
    }


async def send_aprsis_long_bulletin_test(text: str, bulletin_number: int = 0) -> dict:
    """Envía manualmente un BLNx largo solo a APRS-IS, sin tocar RF ni estado automático."""
    if not bool(APRSIS_LONG_BULLETIN_TEST_ENABLED):
        return {"ok": True, "sent": False, "reason": "disabled"}
    if not _aprsis_ready():
        return {"ok": False, "sent": False, "reason": "aprsis_unavailable"}
    clean = _aprs_ascii(text).strip()
    if not clean:
        return {"ok": False, "sent": False, "reason": "empty_text"}
    clean = clean[:APRSIS_LONG_TEST_MAX_CHARS]
    try:
        number = max(0, min(9, int(bulletin_number)))
    except Exception:
        number = 0
    bulletin = _aprsis_bulletin_name(number)
    src = (APRSIS_USER or MY_CALL or "").strip().upper()
    if not src:
        return {"ok": False, "sent": False, "reason": "missing_source"}
    dst9 = bulletin[:9].ljust(9, " ")
    line = f"{src}>APRS,TCPIP*::{dst9}:{clean}"
    sent = bool(await _aprsis_send_line_safe(line))
    return {"ok": sent, "sent": sent, "reason": "sent" if sent else "send_failed", "bulletin": bulletin, "chars": len(clean), "text": clean, "line": line}

def _aprsis_tnc2_message_line(dst_call: str, text: str, *, with_msgid: bool = True) -> str:
    """
    Construye una línea APRS-IS tipo mensaje (TNC2):

      SRC>APRS,TCPIP*::DEST     :texto{nn}

    - Importante: en APRS un "message packet" lleva "::DEST....:" (doble ':' sin espacios).
    - DEST debe ir a 9 chars (APRS spec).
    - El cuerpo se limita a MAX_MSG_LEN (por defecto 67).
    - Incluye ID {nn} para evitar supresión de duplicados y mejorar visibilidad.
    """
    src = (APRSIS_USER or MY_CALL or "").strip().upper()
    dst = (dst_call or "").strip().upper()
    if not src or not dst:
        return ""

    msg = _aprs_ascii(text)
    if not msg:
        return ""

    msgid = _aprsis_next_msgid(dst) if with_msgid else ""
    reserve = len(msgid)
    max_body = max(1, int(MAX_MSG_LEN) - reserve)

    if len(msg) > max_body:
        msg = msg[:max_body]

    dst9 = (dst[:9]).ljust(9, " ")

    # CRÍTICO: "::DEST9:mensaje" (sin espacios)
    return f"{src}>APRS,TCPIP*::{dst9}:{msg}{msgid}"


def _aprsis_tnc2_message_lines(dst_call: str, text: str, *, with_msgid: bool = True) -> List[str]:
    """Construye una o varias líneas APRS-IS sin recortar el texto completo."""
    dst = (dst_call or "").strip().upper()
    msg = _aprs_ascii(text)
    if not dst or not msg:
        return []

    msgid_reserve = 4 if with_msgid else 0  # formato {nn}
    suffix_reserve = len(" (99/99)")
    part_limit = max(1, int(MAX_MSG_LEN) - msgid_reserve - suffix_reserve)
    parts = _split_by_words(msg, part_limit)
    if len(parts) <= 1:
        line = _aprsis_tnc2_message_line(dst, msg, with_msgid=with_msgid)
        return [line] if line else []

    total = len(parts)
    return [
        line
        for i, part in enumerate(parts, 1)
        if (line := _aprsis_tnc2_message_line(dst, f"{part} ({i}/{total})", with_msgid=with_msgid))
    ]


def _aprsis_status_lines(text: str) -> List[str]:
    """Construye una o varias líneas de estado APRS-IS sin truncar mensajes largos."""
    src = (APRSIS_USER or MY_CALL or "").strip().upper()
    msg = _aprs_ascii(text)
    if not src or not msg:
        return []

    if len(msg) <= int(MAX_STATUS_LEN):
        return [f"{src}>APRS,TCPIP*:>{msg}"]

    suffix_reserve = len(" (99/99)")
    parts = _split_by_words(msg, max(1, int(MAX_STATUS_LEN) - suffix_reserve))
    total = len(parts)
    return [f"{src}>APRS,TCPIP*:>{part} ({i}/{total})" for i, part in enumerate(parts, 1)]

async def _aprsis_send_lines_safe(lines: List[str], *, inter_part_delay: float = 0.12) -> bool:
    """Envía una secuencia de líneas APRS-IS y agrega el resultado de todas las partes."""
    if not lines:
        return False

    ok_all = True
    for idx, line in enumerate(lines, 1):
        sent = await _aprsis_send_line_safe(line)
        ok_all = ok_all and sent
        if idx < len(lines):
            await asyncio.sleep(inter_part_delay)
    return ok_all


async def _aprsis_send_line_safe(line: str) -> bool:
    """
    Envía una línea a APRS-IS si hay cliente conectado.

    OBJETIVO:
    - Mantener compatibilidad (misma firma y comportamiento base).
    - Reintento ante Broken pipe / cierre remoto (errno 32) sin tocar _AprsISClient.
    - Logging inteligente:
        * NO loguear líneas de comentario APRS-IS (empiezan por '#') -> keepalive transparente.
        * Sí loguear tráfico real (paquetes APRS, mensajes, etc.).
    """
    try:
        if not line:
            return False

        c = globals().get("_aprsis_client", None)
        if c is None:
            return False

        # --- Normaliza fin de línea ---
        line_norm = str(line).rstrip("\r\n") + "\n"

        # --- Detecta comentario APRS-IS (keepalive/metadata) ---
        # APRS-IS usa líneas que empiezan por '#'. No interesa llenar la consola con keepalives.
        is_comment = line_norm.lstrip().startswith("#")

        # --- 1er intento ---
        if hasattr(c, "send_line"):
            try:
                await c.send_line(line_norm)

                # Log explícito SOLO si NO es comentario
                if not is_comment:
                    try:
                        preview = line_norm.strip()
                        if len(preview) > 160:
                            preview = preview[:157] + "..."
                        print(f"[aprs→IS push] ✅ TX OK -> {preview}")
                    except Exception:
                        pass

                return True

            except (BrokenPipeError, ConnectionError, OSError) as e:
                # Broken pipe típico: errno 32
                eno = getattr(e, "errno", None)
                if eno not in (None, 32):
                    raise

                # --- Forzar reconexión + 2º intento ---
                try:
                    if hasattr(c, "_is"):
                        c._is = None
                    if hasattr(c, "connect"):
                        await c.connect()

                    await c.send_line(line_norm)

                    # Log SOLO si NO es comentario
                    if not is_comment:
                        try:
                            preview = line_norm.strip()
                            if len(preview) > 160:
                                preview = preview[:157] + "..."
                            print(f"[aprs→IS push] ✅ TX OK (retry) -> {preview}")
                        except Exception:
                            pass

                    return True
                except Exception as e2:
                    # Errores sí se muestran (son relevantes)
                    print(f"[aprs→IS push] ❌ {type(e2).__name__}: {e2}")
                    return False

        # --- Fallbacks (se mantienen) ---
        try:
            if hasattr(c, "sendall"):
                c.sendall(line_norm)
                return True
            if hasattr(c, "send"):
                c.send(line_norm)
                return True
            return False
        except Exception as e:
            print(f"[aprs→IS push] ❌ {type(e).__name__}: {e}")
            return False

    except Exception as e:
        print(f"[aprs→IS push] ❌ {type(e).__name__}: {e}")
        return False


def _aprsis_ready() -> bool:
    return bool(APRSIS_USER and APRSIS_PASSCODE)

# --- De-dup sencillo para evitar doble TX (bot UDP + eco broker) ---
import time

_DEDUP_TTL_S = int(os.getenv("APRS_DEDUP_TTL", "120"))  # segundos
_recent_aprs_keys: dict[str, float] = {}

# --- Debug opcional para APRS ---
import os as _os
APRS_DEBUG = int(_os.getenv("APRS_DEBUG", "0"))  # 0=log desactivo (por defecto), 0=callado

def _aprs_dbg(msg: str) -> None:
    if APRS_DEBUG:
        print(msg)

# === AÑADIR TIMESTAMP EN LOS LOGS DE LA CONSOLA APRS ===
import builtins, time, sys
_original_print = builtins.print

# --- Gate APRS→Mesh: 1=ON (por defecto), 0=OFF ---
APRS_GATE_ENABLED = int(os.getenv("APRS_GATE_ENABLED", "1"))

def _aprs_gate_is_enabled() -> bool:
    return bool(APRS_GATE_ENABLED)

# --- Emergencias APRS → Mesh (configurable por entorno) ---
# Lista de palabras clave que activan el modo emergencia si aparecen en el texto.
# Formato por defecto: "EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA"
_EMERG_KEYWORDS = {
    w.strip().upper()
    for w in os.getenv("APRS_EMERGENCY_KEYWORDS", "EMERGENCIA,EMERGENCY,MAYDAY,SOS,AYUDA").split(",")
    if w.strip()
}

# Lista de destinos APRS (campo DEST) que se consideran de emergencia, p.ej. "EMERGENCY,SOS"
_EMERG_DEST_CALLS = {
    w.strip().upper()
    for w in os.getenv("APRS_EMERGENCY_DESTS", "EMERGENCY,EMERG,SOS").split(",")
    if w.strip()
}

# Canales Mesh de emergencia dedicados (lista separada por comas, 0..15).
# Si está vacía, se usa sólo el canal indicado por [CH x].
_EMERG_MESH_CHANNELS: list[int] = []
for _tok in os.getenv("MESH_EMERGENCY_CHANNELS", "").replace(";", ",").split(","):
    _tok = _tok.strip()
    if not _tok:
        continue
    try:
        _ch_val = int(_tok)
        if 0 <= _ch_val <= 15:
            _EMERG_MESH_CHANNELS.append(_ch_val)
    except Exception:
        continue

# Geo-fencing opcional: radio máximo en km para considerar una emergencia "local".
# 0 o valor no válido → desactiva el filtro (todas se consideran sin distancia).
try:
    _EMERG_MAX_KM = float(os.getenv("APRS_EMERGENCY_MAX_KM", "0").strip() or "0")
    if _EMERG_MAX_KM < 0:
        _EMERG_MAX_KM = 0.0
except Exception:
    _EMERG_MAX_KM = 0.0


# Coordenadas HOME para calcular distancia (si están disponibles).
def _safe_float_env(name: str) -> float | None:
    """
    Intenta leer un float desde una variable de entorno, tolerando coma decimal
    y caracteres extra. Devuelve None si no es válido.
    """
    raw = os.getenv(name)
    if not raw:
        return None
    s = str(raw).strip().lower().replace(",", ".")
    # Deja sólo signos y dígitos/punto
    clean = "".join(ch for ch in s if ch in "+-0123456789.")
    if clean in ("", "+", "-"):
        return None
    try:
        return float(clean)
    except Exception:
        return None


_HOME_LAT = _safe_float_env("HOME_LAT")
_HOME_LON = _safe_float_env("HOME_LON")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Distancia aproximada entre 2 puntos WGS84 en kilómetros.
    Implementación local para mantener el script autosuficiente.
    """
    from math import radians, sin, cos, asin, sqrt
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2.0) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2.0) ** 2
    c = 2 * asin(sqrt(a))
    return r * c


def _classify_aprs_emergency(pkt: dict, ap: dict | None, msg_for_humans: str) -> dict | None:
    """
    Heurística ligera para marcar una trama APRS como emergencia.

    Devuelve un dict con:
      {
        "src": CALL,
        "dest": DEST,
        "path": "WIDE1-1,...",
        "reason": "keyword|dest",
        "text": msg_for_humans,
        "lat": float|None,
        "lon": float|None,
        "dist_km": float|None,
        "is_local": bool|None,
      }
    o None si no se considera emergencia.
    """
    if not pkt:
        return None

    src = (pkt.get("src") or "").strip().upper()
    dest = (pkt.get("dest") or "").strip().upper()
    path = ",".join(pkt.get("path") or [])

    # Texto candidato: mensaje, comentario e info
    t_parts = [
        pkt.get("text") or "",
        (ap or {}).get("comment") or "",
        pkt.get("info") or "",
        msg_for_humans or "",
    ]
    t_all = " ".join(str(p) for p in t_parts if p).strip()
    t_up = t_all.upper()

    reason: str | None = None

    if _EMERG_DEST_CALLS and dest in _EMERG_DEST_CALLS:
        reason = f"dest={dest}"
    elif _EMERG_KEYWORDS and any(k in t_up for k in _EMERG_KEYWORDS):
        reason = "keyword"

    if not reason:
        return None

    lat = None
    lon = None
    if ap and isinstance(ap, dict):
        try:
            if "latitude" in ap and "longitude" in ap:
                lat = float(ap["latitude"])
                lon = float(ap["longitude"])
        except Exception:
            lat = lon = None

    dist = None
    is_local: bool | None = None
    if (
        lat is not None and lon is not None
        and _HOME_LAT is not None and _HOME_LON is not None
        and _EMERG_MAX_KM > 0
    ):
        try:
            dist = _haversine_km(_HOME_LAT, _HOME_LON, lat, lon)
            is_local = dist <= _EMERG_MAX_KM
        except Exception:
            dist = None
            is_local = None

    return {
        "src": src,
        "dest": dest,
        "path": path,
        "reason": reason,
        "text": msg_for_humans,
        "lat": lat,
        "lon": lon,
        "dist_km": dist,
        "is_local": is_local,
    }

# --- Notificación opcional a Telegram para emergencias APRS ---
from html import escape as _html_escape

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()


def _parse_id_list_env(value: str) -> list[int]:
    """
    Convierte una cadena con IDs separados por coma/semicolon en lista de enteros.
    Ignora elementos no numéricos.
    """
    ids: list[int] = []
    if not value:
        return ids
    for tok in value.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            ids.append(int(tok))
        except Exception:
            continue
    return ids


_TELEGRAM_EMERG_CHAT_IDS: list[int] = _parse_id_list_env(
    os.getenv("TELEGRAM_EMERG_CHAT_IDS", "") or os.getenv("ADMIN_IDS", "")
)


def _format_emergency_telegram_text(info: dict, mesh_channels: list[int]) -> str:
    """
    Construye el texto HTML que se enviará por Telegram cuando se detecta
    una emergencia APRS.
    """
    src = info.get("src") or "?"
    dest = info.get("dest") or "?"
    path = info.get("path") or "-"
    reason = info.get("reason") or "-"
    text = info.get("text") or ""
    lat = info.get("lat")
    lon = info.get("lon")
    dist = info.get("dist_km")
    is_local = info.get("is_local")

    scope = "LOCAL" if is_local else ("REMOTA" if is_local is False else "DESCONOCIDA")
    ch_txt = ", ".join(str(c) for c in mesh_channels) if mesh_channels else "-"

    lines = [
        "⚠️ <b>Emergencia APRS recibida</b>",
        f"• Origen: <code>{_html_escape(str(src))}</code>",
        f"• Destino: <code>{_html_escape(str(dest))}</code>",
        f"• PATH: <code>{_html_escape(str(path))}</code>",
        f"• Alcance: <b>{_html_escape(scope)}</b>",
        f"• Canales Mesh destino: <code>{_html_escape(ch_txt)}</code>",
        f"• Motivo detección: <code>{_html_escape(str(reason))}</code>",
    ]

    if lat is not None and lon is not None:
        lines.append(f"• Posición: <code>{lat:.5f}, {lon:.5f}</code>")
        g = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"
        lines.append(f"• Mapa: <a href=\"{_html_escape(g)}\">Google Maps</a>")

    if dist is not None:
        lines.append(f"• Distancia aproximada a HOME: {dist:.1f} km")

    if text:
        lines.append("")
        lines.append("<b>Mensaje:</b>")
        lines.append(_html_escape(str(text)))

    return "\n".join(lines)


def _notify_telegram_emergency_sync(info: dict, mesh_channels: list[int]) -> None:
    """
    Envío síncrono (bloqueante) de una notificación de emergencia a Telegram.
    Se ejecuta normalmente en un executor para no bloquear el loop.
    """
    if not TELEGRAM_TOKEN or not _TELEGRAM_EMERG_CHAT_IDS or not info:
        return
    try:
        import urllib.parse
        import urllib.request
    except Exception:
        return

    text = _format_emergency_telegram_text(info, mesh_channels)

    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data_common = {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }

    for chat_id in _TELEGRAM_EMERG_CHAT_IDS:
        try:
            payload = data_common.copy()
            payload["chat_id"] = str(chat_id)
            data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(base_url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
        except Exception as e:
            print(f"[aprs→telegram] ❌ {type(e).__name__}: {e}")


async def _notify_telegram_emergency(info: dict, mesh_channels: list[int]) -> None:
    """
    Envoltura asíncrona para enviar notificaciones de emergencia a Telegram
    sin bloquear el loop principal.
    """
    if not TELEGRAM_TOKEN or not _TELEGRAM_EMERG_CHAT_IDS or not info:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _notify_telegram_emergency_sync(info, mesh_channels)
        return
    await loop.run_in_executor(None, _notify_telegram_emergency_sync, info, mesh_channels)

def _dedup_key(dest: str, text: str) -> str:
    d = (dest or "broadcast").strip().lower()
    t = re.sub(r"\s+", " ", (text or "").strip())
    return f"{d}|{t}"[:512]

def _dedup_mark(dest: str, text: str) -> None:
    now = time.time()
    _recent_aprs_keys[_dedup_key(dest, text)] = now + _DEDUP_TTL_S
    # Prune
    stale = [k for k, exp in _recent_aprs_keys.items() if exp < now]
    for k in stale:
        _recent_aprs_keys.pop(k, None)

def _dedup_seen(dest: str, text: str) -> bool:
    now = time.time()
    k = _dedup_key(dest, text)
    exp = _recent_aprs_keys.get(k)
    if exp is None:
        return False
    if exp < now:
        _recent_aprs_keys.pop(k, None)
        return False
    return True

# --- Cliente APRS-IS (persistente) usando aprslib (bloqueante => executor) ---
class _AprsISClient:
    def __init__(self, user: str, passcode: str, host: str, port: int, filt: str = ""):
        self.user = user
        self.passcode = passcode
        self.host = host
        self.port = port
        self.filt = filt
        self._is = None
        self._lock = asyncio.Lock()
        self._announced = False  # <<< NUEVO

    def _ensure_sync(self):
        # (Re)conectar si hace falta
        if self._is is None:
            try:
                self._is = aprslib.IS(self.user, passwd=self.passcode, host=self.host, port=self.port)
                self._is.connect()
                if self.filt:
                    try:
                        self._is.sendall(f"filter {self.filt}")
                    except Exception:
                        pass
                if not self._announced:
                    print(f"[aprs→IS] Conectado OK como {self.user} a {self.host}:{self.port} (filtro='{self.filt or '-'}').")
                    print("Subiré SOLO POSICIONES con [CHx] en formato third-party (respetando NOGATE/RFONLY).")
                    self._announced = True
            except Exception as e:
                if not self._announced:
                    print(f"[aprs→IS] ❌ No se pudo conectar a {self.host}:{self.port} como {self.user}: {e}")
                    self._announced = True
                raise

    def _send_line_sync(self, line: str):
        self._ensure_sync()
        self._is.sendall(line)

    async def connect(self):
        """Conecta ahora (login + filter si procede) y deja la sesión preparada."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ensure_sync)


    async def send_line(self, line: str):
        async with self._lock:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._send_line_sync, line)
            except Exception:
                # fuerza reconexión en el siguiente intento
                self._is = None
                raise

_aprsis_client: _AprsISClient | None = None

def _aprs_extract_message_body(info: str) -> str:
    """
    Si 'info' es un APRS message (':ADDRESSEE:texto'), devuelve solo 'texto'.
    Si no lo es, devuelve 'info' tal cual.

    Ejemplos:
      ':EB2EAS-11 :[CH1] hola' -> '[CH1] hola'
      ':DEST     :hola{12}'   -> 'hola{12}'
    """
    if not info:
        return ""
    s = str(info).strip()

    # Formato APRS message: :ADDRESSEE:texto
    # ADDRESSEE suele ser 9 chars (relleno con espacios), pero aceptamos 1..15 por robustez.
    if s.startswith(":"):
        try:
            # Quitar el primer ':' y separar en dos ':'
            rest = s[1:]
            if ":" in rest:
                _addr, body = rest.split(":", 1)
                return (body or "").strip()
        except Exception:
            pass

    return s


def _mesh_add_src_prefix(src: str, msg: str) -> str:
    """
    Asegura que en Mesh se vea el indicativo de origen APRS.

    Caso clave:
      - Si el mensaje APRS es de tipo "message" (formato ':DEST: texto{nn}'),
        lo prefija como: 'SRC: :DEST: ...'

    Robustez:
      - Tolera espacios iniciales antes de ':' (caso real observado en logs).
      - Evita doble prefijo si ya empieza por 'SRC:'.
      - No toca emergencias ya formateadas (llevan 'src=...').
    """
    s = (src or "").strip().upper()
    m = (msg or "")
    if not s or not m:
        return (msg or "").strip()

    # Conserva el texto "limpio" para decisiones
    m_strip = m.strip()
    m_upper = m_strip.upper()

    # No tocar emergencias ya formateadas (llevan src=...)
    if "SRC=" in m_upper:
        return m_strip

    # Evita doble prefijo tipo "EB2EAS-7: ..."
    if m_upper.startswith(s + ":"):
        return m_strip

    # Detecta "APRS message" aunque haya espacios antes del ':'
    # Ejemplos válidos:
    #   ":DEST: Hola{12}"
    #   " :DEST: Hola{12}"
    if m_strip.startswith(":"):
        return f"{s}: {m_strip}"

    # Para otros textos (comentarios/status) no añadimos prefijo para no ensuciar canales
    return m_strip


def _should_not_gate(info_text: str) -> bool:
    """
    Devuelve True si el comentario contiene NOGATE o RFONLY (no subir a APRS-IS).
    """
    t = (info_text or "").upper()
    return ("NOGATE" in t) or ("RFONLY" in t)

def _make_thirdparty_line(pkt: dict, igate_call: str) -> str | None:
    """
    Construye una línea APRS-IS en formato 'third-party', preservando el paquete original:
      IGATE>APRS,TCPIP*,qAR,IGATE:}SRC>DEST,PATH:payload

    Requisitos:
      - pkt debe tener: src, dest, path (lista o vacía) e info (str/bytes)
      - igate_call: tu indicativo de iGate (p.ej. 'EB2XXX-10')

    Devuelve:
      - str con la línea TNC2 lista para enviar a APRS-IS, o
      - None si faltan campos o si el payload incluye NOGATE/RFONLY.
    """
    if not pkt:
        return None

    src = (pkt.get("src") or "").upper().strip()
    dest = (pkt.get("dest") or "").upper().strip()
    path_list = pkt.get("path") or []
    info = pkt.get("info")

    if isinstance(info, bytes):
        try:
            info = info.decode("utf-8", "ignore")
        except Exception:
            info = info.decode("latin-1", "ignore")
    info = (info or "").replace("\r", " ").replace("\n", " ").strip()

    # Campos mínimos
    if not src or not dest or not info:
        return None

    # Respetar NOGATE / RFONLY
    if _should_not_gate(info):
        return None

    # Sanitiza a ASCII visible + espacio (IS no admite binarios)
    info_ascii = "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in info)

    # PATH opcional
    path = ",".join(path_list) if path_list else ""

    # Paquete original (dentro de '}')
    orig = f"{src}>{dest}{(','+path) if path else ''}:{info_ascii}"

    ig = (igate_call or "NOCALL-10").upper().strip()
    # Formato third-party recomendado para iGates RF→IS
    line = f"{ig}>APRS,TCPIP*,qAR,{ig}:" + orig

    # Longitud segura (IS suele aceptar ~512B por línea)
    return line[:510]

# =========================
# === KISS helpers =========
# =========================
_FEND  = 0xC0
_FESC  = 0xDB
_TFEND = 0xDC
_TFESC = 0xDD

def _kiss_escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == _FEND:
            out.extend([_FESC, _TFEND])
        elif b == _FESC:
            out.extend([_FESC, _TFESC])
        else:
            out.append(b)
    return bytes(out)

def _kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == _FESC and i + 1 < n:
            nb = data[i + 1]
            if nb == _TFEND:
                out.append(_FEND); i += 2; continue
            if nb == _TFESC:
                out.append(_FESC); i += 2; continue
        out.append(b); i += 1
    return bytes(out)

def kiss_wrap(ax25_frame: bytes, port: int = 0, cmd: int = 0x00) -> bytes:
    typ = ((port & 0x0F) << 4) | (cmd & 0x0F)
    payload = bytes([typ]) + _kiss_escape(ax25_frame)
    return bytes([_FEND]) + payload + bytes([_FEND])

def kiss_iter_frames_from_buffer(buf: bytearray):
    """
    Consume buf (bytearray) y entrega tramas DATA des-escapadas, dejando resto en buf.
    """
    frames: List[bytes] = []
    while True:
        try:
            start = buf.index(_FEND)
        except ValueError:
            break
        try:
            end = buf.index(_FEND, start + 1)
        except ValueError:
            if start > 0:
                del buf[:start]
            break
        raw = bytes(buf[start + 1:end])
        del buf[:end + 1]
        if not raw:
            continue
        if (raw[0] & 0x0F) != 0x00:  # solo DATA
            continue
        frames.append(_kiss_unescape(raw[1:]))
    return frames

def _hexdump(b: bytes, width: int = 16) -> str:
    s = []
    for i in range(0, len(b), width):
        chunk = b[i:i+width]
        hexs = " ".join(f"{x:02X}" for x in chunk)
        ascii_ = "".join(chr(x) if 32 <= x <= 126 else "." for x in chunk)
        s.append(f"{i:04X}  {hexs:<{width*3}}  {ascii_}")
    return "\n".join(s)


# =========================
# === AX.25 helpers ========
# =========================
def _call_ssid_parts(call: str) -> Tuple[str, int]:
    call = (call or "").upper().strip()
    if "-" in call:
        c, s = call.split("-", 1)
        try:
            ssid = int(s)
        except Exception:
            ssid = 0
    else:
        c, ssid = call, 0
    return c[:6].ljust(6), max(0, min(15, ssid))

def _addr_field(call: str, last: bool = False) -> bytes:
    c6, ssid = _call_ssid_parts(call)
    b = bytearray(7)
    for i, ch in enumerate(c6.encode("ascii", "ignore")[:6]):
        b[i] = (ch << 1) & 0xFE
    b[6] = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
    return bytes(b)

def _decode_ax25_addrs(axhdr: bytes) -> list[str]:
    # axhdr: múltiplo de 7 bytes (DEST,SRC,PATH..., cada 7B)
    out = []
    for i in range(0, len(axhdr), 7):
        c = axhdr[i:i+7]
        if len(c) < 7: break
        call = "".join(chr((c[j] >> 1) & 0x7F) for j in range(6)).strip()
        ssid = (c[6] >> 1) & 0x0F
        last = (c[6] & 0x01) == 0x01
        out.append(f"{call}-{ssid} {'(last)' if last else ''}")
    return out


def build_ax25_ui(dest: str, src: str, path: List[str] | Tuple[str, ...], payload: bytes) -> bytes:
    hops = [dest, src] + list(path or [])
    addrs = [_addr_field(c, last=(i == len(hops) - 1)) for i, c in enumerate(hops)]
    return b"".join(addrs) + b"\x03" + b"\xF0" + (payload or b"")

# =========================
# === Troceo mensajes =====
# =========================
def _split_by_words(text: str, max_len: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ""
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        if len(cur) + len(token) <= max_len:
            cur += token
        else:
            if cur:
                parts.append(cur.rstrip())
            cur = token.lstrip()
            while len(cur) > max_len:  # corte duro si una “palabra” excede
                parts.append(cur[:max_len]); cur = cur[max_len:]
    if cur:
        parts.append(cur.rstrip())
    return parts

def build_aprs_status_chunks(text: str, max_len: int | None = None) -> List[bytes]:
    """
    Construye payloads APRS de estado respetando el límite completo del campo INFO.

    El byte inicial '>' también viaja dentro del payload KISS. Antes se reservaba
    `max_len` solo para el texto y después se anteponía '>', por lo que una
    configuración APRS_STATUS_MAX=67 podía emitir payloads de 68 bytes.
    """
    limit = max(2, int(max_len if max_len is not None else MAX_STATUS_LEN))
    text = _to_ascii7(text)  # <-- Sanitizar aquí
    text_limit = max(1, limit - 1)  # reservar el marcador de estado '>'
    if not text:
        return []
    if len(text) <= text_limit:
        return [b">" + text.encode("ascii", "ignore")]

    # Reservar marcador '>' + sufijo multipart. Usamos el peor caso habitual
    # (99/99) para no generar micro-partes al recalcular el total final.
    suffix_reserve = len(" (99/99)")
    part_limit = max(1, limit - 1 - suffix_reserve)
    parts = _split_by_words(text, part_limit)

    # Si hubiese 100+ partes, el sufijo crece; recalcular con el ancho real.
    total = max(1, len(parts))
    actual_suffix_reserve = len(f" ({total}/{total})")
    if actual_suffix_reserve != suffix_reserve:
        part_limit = max(1, limit - 1 - actual_suffix_reserve)
        parts = _split_by_words(text, part_limit)
        total = max(1, len(parts))

    out: List[bytes] = []
    for i, part in enumerate(parts, 1):
        suffix = f" ({i}/{total})"
        body_limit = max(1, limit - 1 - len(suffix))
        body = part[:body_limit] + suffix
        payload = b">" + body.encode("ascii", "ignore")
        out.append(payload[:limit])
    return out


def build_aprs_message_chunks(dest_call: str, text: str, max_len: int | None = None) -> List[bytes]:
    limit = int(max_len if max_len is not None else MAX_MSG_LEN)
    dest9 = ((dest_call or "").upper().strip() + " " * 9)[:9]
    text = _to_ascii7(text)  # <-- Sanitizar aquí
    parts = _split_by_words(text, limit - len(" (99/99)"))
    if len(parts) <= 1:
        return [f":{dest9}:{text}".encode("ascii", "ignore")]
    n = len(parts)
    return [f":{dest9}:{p} ({i}/{n})".encode("ascii", "ignore") for i, p in enumerate(parts, 1)]




def _build_control_aprs_payloads(dest: str, text: str) -> tuple[str, str, list[bytes], str]:
    """Prepara exactamente los payloads que usaría el control UDP APRS.

    Esta función centraliza la normalización del destino, el saneamiento ASCII y
    el troceado RF. La usan tanto el modo de previsualización como el envío real,
    de forma que el dispatcher de aplicaciones nunca tenga que estimar las
    partes con un algoritmo distinto al gateway.

    Parámetros:
      dest: destino solicitado (``broadcast``/``all`` o indicativo APRS).
      text: texto original que se desea transmitir.

    Retorna ``(dest_norm, text_clean, payloads, dest_hdr)``. ``payloads`` es la
    misma lista de campos INFO que posteriormente se entrega a KISS.
    """
    dest_clean = _aprs_ascii(dest)
    text_clean = _aprs_ascii(text)
    dest_norm = "broadcast" if dest_clean.lower() in ("broadcast", "all") else dest_clean.upper()
    if dest_norm == "broadcast":
        payloads = build_aprs_status_chunks(text_clean)
        dest_hdr = "APRS"
    else:
        payloads = build_aprs_message_chunks(dest_norm, text_clean)
        dest_hdr = dest_norm
    return dest_norm, text_clean, payloads, dest_hdr

# =========================
# === Parse APRS ===========
# =========================
def _decode_addr(addr7: bytes) -> Tuple[str, int, bool]:
    call = "".join(chr((addr7[i] >> 1) & 0x7F) for i in range(6)).strip()
    ssid = (addr7[6] >> 1) & 0x0F
    last = bool(addr7[6] & 0x01)
    return call, ssid, last

def parse_ax25_ui(frame: bytes) -> dict | None:
    try:
        p = memoryview(frame)
        addrs = []; off = 0
        while True:
            if off + 7 > len(p): return None
            a = bytes(p[off:off+7]); off += 7
            addrs.append(_decode_addr(a))
            if a[6] & 0x01: break
        if off + 2 > len(p): return None
        control = p[off]; pid = p[off + 1]; off += 2
        if control != 0x03 or pid != 0xF0: return None
        info_raw = bytes(p[off:])
        try:
            info = info_raw.decode("utf-8", "ignore")
        except Exception:
            info = info_raw.decode("latin-1", "ignore")

        dest_call = f"{addrs[0][0]}-{addrs[0][1]}" if addrs else ""
        src_call  = f"{addrs[1][0]}-{addrs[1][1]}" if len(addrs) > 1 else ""
        path = [f"{c}-{s}" for (c, s, _l) in addrs[2:]] if len(addrs) > 2 else []

        out = {"dest": dest_call, "src": src_call, "path": path,
               "info_raw": info_raw, "info": info}

        if info.startswith(">"):
            out["type"] = "status"
            out["text"] = info[1:].strip()
            return out

        if info.startswith(":") and len(info) >= 11 and ":" in info[10:]:
            # :DEST9:mensaje{nn}
            msg_dest = info[1:10].strip()
            rest = info[10:]
            if rest.startswith(":"): rest = rest[1:]
            text = re.sub(r"\{[ -~]{1,5}\}$", "", rest).strip()  # quita ACK {nn}
            out.update({"type": "message", "msg_dest": msg_dest, "text": text})
            return out

        return out
    except Exception:
        return None

# =========================
# === Canal en comentario ==
# =========================
# Soporta:
#   [CH 1] Texto
#   [CH1] Texto
#   [CANAL 3] Texto
#   [MC1] Texto / [MESHCORE 1] Texto
#   [CH 1+10] Texto  (delay 10 min)
#   [CANAL3+5] Texto (delay 5 min)
#   [MC1/ZARAGOZA] Texto (se acepta el sufijo de nombre generado por aprsis_push)
_CH_REGEX = re.compile(
    r"\[(CH|CANAL|MC|MESHCORE)\s*([0-9]{1,2})(?:\s*([+])\s*([0-9]{1,4}))?(?:/[^\]]*)?\]",
    re.IGNORECASE,
)

def _channel_tag_is_meshcore(comment: str) -> bool:
    """True si la primera etiqueta de canal usa prefijo MeshCore explícito ([MCx]/[MESHCORE x])."""
    if not comment:
        return False
    m = _CH_REGEX.search(comment)
    if not m:
        return False
    return (m.group(1) or "").strip().upper() in {"MC", "MESHCORE"}

def extract_channel_if_tagged(comment: str) -> tuple[Optional[int], str]:
    """
    Devuelve (canal, texto_sin_etiqueta) únicamente si hay [CHx] / [CANAL x] / [CHx+N].
    Ignora el sufijo +N. Si no hay etiqueta, devuelve (None, comment).
    No aplica canal por defecto (evita inyectar sin prefijo).
    """
    if not comment:
        return (None, "")
    m = _CH_REGEX.search(comment)
    if not m:
        return (None, comment.strip())
    try:
        ch = int(m.group(2))
    except Exception:
        return (None, comment.strip())
    ch = max(0, min(15, ch))
    text = (comment[:m.start()] + comment[m.end():]).strip()
    import re as _re
    text = _re.sub(r"\s{2,}", " ", text)
    return (ch, text)


def extract_channel_from_comment(comment: str, default_ch: int = MESHTASTIC_CHANNEL) -> Tuple[int, str]:
    """
    [CH2] Texto..., [CANAL 5] Aviso..., [CH 1+10] ...
    Devuelve (canal, texto_sin_etiqueta). Si no hay etiqueta, (default_ch, comment).
    Ignora el sufijo de programación (+N).
    """
    if not comment:
        return (int(default_ch), "")
    m = _CH_REGEX.search(comment)
    if not m:
        return (int(default_ch), comment.strip())
    try:
        ch = int(m.group(2))
    except Exception:
        ch = int(default_ch)

    ch = max(0, min(15, ch))
    text = (comment[:m.start()] + comment[m.end():]).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return (ch, text)

def extract_channel_and_delay(comment: str) -> tuple[Optional[int], Optional[int], str]:
    """
    Versión extendida para APRS→Mesh:
      - [CH 1] Hola      → (1, None, "Hola")
      - [CH1+10] Aviso   → (1, 10, "Aviso")   (10 minutos)
      - [CANAL 3+5] Test → (3, 5, "Test")

    Además, heurística para casos colapsados:
      - [CH42] → ch=4, delay=2   (cuando 42 > 15)

    delay_min está en minutos si se usa '+N' o se infiere por heurística; si no, None.
    """
    if not comment:
        return (None, None, "")

    m = _CH_REGEX.search(comment)
    if not m:
        return (None, None, comment.strip())

    raw = (m.group(2) or "").strip()
    sign = m.group(3)
    val  = m.group(4)

    ch: Optional[int] = None
    delay_min: Optional[int] = None

    # 1) Caso normal con '+N' explícito: [CH4+2], [CANAL 3+10], etc.
    if sign == "+" and val is not None:
        try:
            ch_val = int(raw)
        except Exception:
            return (None, None, comment.strip())
        ch = max(0, min(15, ch_val))
        try:
            delay_min = max(0, int(val))
        except Exception:
            delay_min = None

    # 2) Caso colapsado sin '+': [CH42] → si 42>15 y tiene 2 dígitos, interpretamos ch=4, delay=2
    else:
        try:
            ch_val = int(raw)
        except Exception:
            return (None, None, comment.strip())

        if ch_val > 15 and len(raw) == 2:
            # heurística específica APRS: primer dígito = canal, segundo = delay (minutos)
            try:
                ch = int(raw[0])
                delay_min = int(raw[1])
            except Exception:
                ch = ch_val
                delay_min = None
        else:
            ch = ch_val

        ch = max(0, min(15, ch))

    # Texto sin la etiqueta
    text = (comment[:m.start()] + comment[m.end():]).strip()
    import re as _re
    text = _re.sub(r"\s{2,}", " ", text)

    return (ch, delay_min, text)


def _is_position_pkt(pkt: dict) -> bool:
    """True si el payload APRS es de posición: empieza por ! / = @"""
    info = pkt.get("info") or ""
    return bool(info) and info[0] in "!/=@"


def _has_ch_tag_in_info(pkt: dict) -> bool:
    """True si en el campo info aparece [CHx] / [CANAL x] (con o sin +N)"""
    return bool(_CH_REGEX.search(pkt.get("info") or ""))


# =========================
# === TX APRS util ========
# =========================
def _tx_aprs_payload(payload: bytes, dest_hdr: str, path_override: Optional[List[str]] = None) -> bool:
    tx_path = APRS_PATH if path_override is None else path_override
    ax25 = build_ax25_ui(dest=dest_hdr, src=MY_CALL,
                         path=[p for p in tx_path if p],
                         payload=payload)
    kiss = kiss_wrap(ax25, port=KISS_CHANNEL)  # [MOD] usa el canal elegido
    try:
        s = socket.create_connection((KISS_HOST, KISS_PORT), timeout=3.0)
        _kiss_init(s)                           # [NUEVO] fija TXDELAY/PERSIST/SLOTTIME
        s.sendall(kiss)
        s.close()
        
        print(f"[ctrl→aprs] TX {len(payload)}B → {dest_hdr}")
        return True
    except Exception as e:
        print(f"[ctrl→aprs] ❌ KISS send error: {e}")
        return False

# === [NUEVO] Parámetros KISS (unidades de 10 ms) + init ===
KISS_TXDELAY = int(os.getenv("KISS_TXDELAY", "30"))   # 300 ms (robusto para 1200 AFSK)
KISS_PERSIST = int(os.getenv("KISS_PERSIST", "200"))
KISS_SLOTTIME = int(os.getenv("KISS_SLOTTIME", "10")) # 100 ms
KISS_TXTAIL  = int(os.getenv("KISS_TXTAIL",  "3"))

try:
    APRS_RF_PART_DELAY_S = max(0.0, float(os.getenv("APRS_RF_PART_DELAY_S", "2.0") or "2.0"))
except Exception:
    APRS_RF_PART_DELAY_S = 2.0
try:
    APRS_RF_BAUD = max(300.0, float(os.getenv("APRS_RF_BAUD", "1200") or "1200"))
except Exception:
    APRS_RF_BAUD = 1200.0


def _aprs_rf_part_gap_s(payload_len: int) -> float:
    """
    Pausa conservadora entre tramas APRS multipart entregadas al TNC.

    Soundmodem puede aceptar varias tramas KISS en el mismo segundo, pero en RF
    no conviene alimentar la siguiente hasta dejar margen para TXDELAY, cola AX.25
    y el tiempo de aire de la parte anterior.
    """
    try:
        payload_len = max(0, int(payload_len))
    except Exception:
        payload_len = 0
    # Direcciones AX.25 + control/PID/FCS/flags/escape KISS aproximados.
    estimated_ax25_bytes = payload_len + 80
    airtime_s = (estimated_ax25_bytes * 10.0) / APRS_RF_BAUD
    kiss_tail_s = max(0, KISS_TXTAIL) * 0.01
    kiss_txdelay_s = max(0, KISS_TXDELAY) * 0.01
    return max(APRS_RF_PART_DELAY_S, kiss_txdelay_s + airtime_s + kiss_tail_s)

def _kiss_param_frame(cmd_id: int, value: bytes, port: int = 0) -> bytes:
    typ = ((int(port) & 0x0F) << 4) | (cmd_id & 0x0F)
    payload = bytes([typ]) + _kiss_escape(value)
    return bytes([_FEND]) + payload + bytes([_FEND])

def _kiss_init(sock: socket.socket, port: int = KISS_CHANNEL) -> None:
    try:
        sock.sendall(_kiss_param_frame(0x01, bytes([max(0, min(255, KISS_TXDELAY))]), port=port))  # TXDELAY
        sock.sendall(_kiss_param_frame(0x02, bytes([max(1, min(255, KISS_PERSIST))]), port=port))  # PERSIST
        sock.sendall(_kiss_param_frame(0x03, bytes([max(1, min(255, KISS_SLOTTIME))]), port=port)) # SLOTTIME
        sock.sendall(_kiss_param_frame(0x04, bytes([max(0, min(255, KISS_TXTAIL))]),  port=port))  # TXTAIL
    except Exception:
        pass



# =========================
# === Broker control =======
# =========================



def _broker_send_text(ch: int, text: str, dest: str | None = None, ack: bool = False, timeout: float = 6.0) -> dict:
    """
    Cliente ligero del BacklogServer del broker: cmd SEND_TEXT.
    """
    req = {
        "cmd": "SEND_TEXT",
        "params": {
            "text": text,
            "dest": (None if (not dest or str(dest).strip().lower() == "broadcast") else str(dest).strip()),
            "ch": int(ch),
            "ack": 1 if ack else 0
        }
    }
    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((BROKER_CTRL_HOST, BROKER_CTRL_PORT), timeout=timeout) as s:
        s.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk: break
            buf += chunk
    line = (buf.decode("utf-8", "ignore") or "").strip()
    try:
        return json.loads(line) if line else {"ok": False, "error": "empty broker reply"}
    except Exception as e:
        return {"ok": False, "error": f"bad json: {e}"}



def _radio_profile() -> str:
    """Devuelve el perfil canónico sin alterar la lógica APRS existente."""
    try:
        from radio_profile import normalize_radio_profile  # type: ignore
        profile = normalize_radio_profile(os.getenv("RADIO_PROFILE"), allow_legacy_empty=True)
        return "" if profile == "legacy" else profile
    except Exception:
        return (os.getenv("RADIO_PROFILE") or "").strip().lower().replace("-", "_")


def _aprs_meshcore_mode() -> bool:
    """
    True cuando APRS→malla debe salir por MeshCore en vez de SEND_TEXT/Meshtastic.

    Por defecto sigue el transporte principal de RADIO_PROFILE. Por tanto se
    activa tanto en ``meshcore_only`` como en el perfil invertido con MeshCore A.
    Puede forzarse con APRS_TO_MESHCORE para despliegues mixtos/controlados.
    """
    raw = (os.getenv("APRS_TO_MESHCORE") or "").strip().lower()
    if raw in {"1", "true", "on", "yes", "si", "sí"}:
        return True
    if raw in {"0", "false", "off", "no"}:
        return False
    try:
        from radio_profile import default_transport_for_radio_profile  # type: ignore
        return default_transport_for_radio_profile(_radio_profile()) == "meshcore"
    except Exception:
        return _radio_profile() in {
            "meshcore_only",
            "meshcore_a_meshtastic_embedded_b",
            "meshcore_a_meshtastic_b",
        }


def _parse_meshcore_channel_map_for_aprs() -> dict[int, dict]:
    """
    Lee MESHCORE_CHANNEL_MAP para resolver [CHx] APRS hacia destino MeshCore.

    Formatos compatibles con el broker:
      - "0:chan:0:PUBLIC,2:chan:1:ZGZ"
      - "0:AB12CD34:PUBLIC,2:EE99AA00:ZGZ"  (contacto/DM)
    Si no hay mapa para un canal, APRS→MeshCore usará channel_idx == CHx.
    """
    out: dict[int, dict] = {}
    raw = (os.getenv("MESHCORE_CHANNEL_MAP") or "").strip()
    if raw:
        for part in raw.split(","):
            item = part.strip()
            if not item or ":" not in item:
                continue
            toks = [t.strip() for t in item.split(":")]
            try:
                ch = int(toks[0])
            except Exception:
                continue
            if len(toks) >= 3 and toks[1].lower() in {"chan", "channel", "canal"}:
                try:
                    out[ch] = {"kind": "chan", "channel_idx": int(toks[2])}
                except Exception:
                    continue
            elif len(toks) >= 2 and toks[1]:
                out[ch] = {"kind": "contact", "contact_prefix": toks[1]}

    # Compat simple: MESHCORE_CH2CONTACT="0:AB12CD34,2:EE99AA00"
    raw2 = (os.getenv("MESHCORE_CH2CONTACT") or "").strip()
    if raw2:
        for part in raw2.split(","):
            item = part.strip()
            if not item or ":" not in item:
                continue
            k, v = item.split(":", 1)
            try:
                ch = int(k.strip())
            except Exception:
                continue
            pref = v.strip()
            if pref and ch not in out:
                out[ch] = {"kind": "contact", "contact_prefix": pref}

    return out


def _broker_send_meshcore_text(ch: int, text: str, timeout: float = 6.0, direct_channel_idx: bool = False) -> dict:
    """
    Envía APRS→MeshCore usando el endpoint MESHCORE_SEND del broker.

    El canal APRS [CHx] se resuelve con MESHCORE_CHANNEL_MAP; si no hay entrada,
    se trata CHx como channel_idx MeshCore para que /escuchar all y APRS funcionen
    en instalaciones meshcore_only simples sin mapa adicional.
    """
    if direct_channel_idx:
        route = {"kind": "chan", "channel_idx": int(ch)}
    else:
        route = _parse_meshcore_channel_map_for_aprs().get(int(ch), {"kind": "chan", "channel_idx": int(ch)})
    params = {"kind": route.get("kind") or "chan", "text": text}
    if params["kind"] in {"contact", "dm"}:
        cp = (route.get("contact_prefix") or "").strip()
        if not cp:
            return {"ok": False, "error": "missing meshcore contact_prefix"}
        params["kind"] = "contact"
        params["contact_prefix"] = cp
    else:
        try:
            params["kind"] = "chan"
            params["channel_idx"] = int(route.get("channel_idx", ch))
        except Exception:
            return {"ok": False, "error": "missing meshcore channel_idx"}

    req = {"cmd": "MESHCORE_SEND", "params": params}
    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((BROKER_CTRL_HOST, BROKER_CTRL_PORT), timeout=timeout) as s:
        s.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = (buf.decode("utf-8", "ignore") or "").strip()
    try:
        resp = json.loads(line) if line else {"ok": False, "error": "empty broker reply"}
    except Exception as e:
        resp = {"ok": False, "error": f"bad json: {e}"}
    if isinstance(resp, dict):
        resp.setdefault("transport", "meshcore")
        resp.setdefault("aprs_channel", int(ch))
    return resp


def _broker_send_mesh_text(ch: int, text: str, dest: str | None = None, ack: bool = False, timeout: float = 6.0, direct_meshcore_channel: bool = False) -> dict:
    """Ruta común APRS→malla, siguiendo el transporte principal del perfil."""
    if _aprs_meshcore_mode():
        if dest and str(dest).strip().lower() != "broadcast":
            # Una ruta APRS seleccionada para MeshCore no puede reutilizar un ID
            # de destino Meshtastic; requiere mapa de canal/contacto MeshCore.
            return {"ok": False, "error": "direct Meshtastic destination unavailable on MeshCore route", "transport": "meshcore"}
        return _broker_send_meshcore_text(ch, text, timeout=timeout, direct_channel_idx=direct_meshcore_channel)
    return _broker_send_text(ch, text, dest=dest, ack=ack, timeout=timeout)

# =========================
# === Helpers APRS→Mesh ===
# =========================

def _parse_ch_and_delay_from_pkt(pkt: dict, default_ch: int = MESHTASTIC_CHANNEL) -> tuple[Optional[int], Optional[int], str]:
    """
    Igual que _pick_ch_and_text, pero devolviendo también delay_min (minutos) si se usa [CH x+N].
    - 'status' y 'message': usan pkt['text']
    - resto: usan pkt['info']
    """
    if not pkt:
        return (None, None, "")

    if pkt.get("type") in {"status", "message"}:
        ch, delay_min, msg = extract_channel_and_delay(pkt.get("text", ""))
        return (ch, delay_min, msg) if (ch is not None and msg) else (None, None, "")

    info = (pkt.get("info") or "").strip()
    if not info:
        return (None, None, "")

    ch, delay_min, msg = extract_channel_and_delay(info)
    return (ch, delay_min, msg) if (ch is not None and msg) else (None, None, "")

def _schedule_aprs_to_mesh(ch: int, msg: str, delay_min: int, src: str) -> None:
    """
    Programación local en este proceso (no en el broker):
      [CH 1+10] Texto  → envía a CH1 dentro de 10 minutos vía _broker_send_text.
    Funciona sin bot y sin Internet.
    """
    delay_sec = max(0, int(delay_min) * 60)

    async def _job():
        try:
            await asyncio.sleep(delay_sec)

            msg_mesh = _mesh_add_src_prefix(src, msg)

            if not _aprs_gate_is_enabled():
                print(f"[aprs→mesh sched] GATE OFF al ejecutar CH{ch} (+{delay_min}m) ← {src}: {msg_mesh[:120]}")
                return

            res = _broker_send_mesh_text(ch, msg_mesh, dest=None, ack=False)
            ok = bool(res.get("ok"))
            print(f"[aprs→mesh sched] CH{ch} (+{delay_min}m) ← {src}: {msg_mesh[:120]} -> {'OK' if ok else 'KO'}")

            # === ECO OPCIONAL AL NODO HOME ===
            if HOME_NODE_ID and not _aprs_meshcore_mode():
                try:
                    eco_txt = f"[APRS eco de {src}] {msg_mesh}"
                    res_eco = _broker_send_mesh_text(ch, eco_txt, dest=HOME_NODE_ID, ack=False)
                    ok_eco = bool(res_eco.get("ok"))
                    print(f"[aprs→mesh ECO] CH{ch} → {HOME_NODE_ID}: {eco_txt[:120]} -> {'OK' if ok_eco else 'KO'}")
                except Exception as _e:
                    print(f"[aprs→mesh ECO] ❌ {type(_e).__name__}: {_e}")
            # === FIN ECO OPCIONAL ===

        except Exception as e:
            print(f"[aprs→mesh sched] ❌ {type(e).__name__}: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_job())
    except RuntimeError:
        # Si no hay loop (caso raro), ejecuta inmediato
        try:
            msg_mesh = _mesh_add_src_prefix(src, msg)

            if not _aprs_gate_is_enabled():
                print(f"[aprs→mesh sched/now] GATE OFF CH{ch} (+{delay_min}m≡0) ← {src}: {msg_mesh[:120]}")
                return

            res = _broker_send_mesh_text(ch, msg_mesh, dest=None, ack=False)
            ok = bool(res.get("ok"))
            print(f"[aprs→mesh sched/now] CH{ch} (+{delay_min}m≡0) ← {src}: {msg_mesh[:120]} -> {'OK' if ok else 'KO'}")

            # === ECO OPCIONAL AL NODO HOME ===
            if HOME_NODE_ID and not _aprs_meshcore_mode():
                try:
                    eco_txt = f"[APRS eco de {src}] {msg_mesh}"
                    res_eco = _broker_send_mesh_text(ch, eco_txt, dest=HOME_NODE_ID, ack=False)
                    ok_eco = bool(res_eco.get("ok"))
                    print(f"[aprs→mesh ECO] CH{ch} → {HOME_NODE_ID}: {eco_txt[:120]} -> {'OK' if ok_eco else 'KO'}")
                except Exception as _e:
                    print(f"[aprs→mesh ECO] ❌ {type(_e).__name__}: {_e}")
            # === FIN ECO OPCIONAL ===

        except Exception as e:
            print(f"[aprs→mesh sched/now] ❌ {type(e).__name__}: {e}")


def _handle_aprs_control_from_rf(src: str, msg: str) -> bool:
    """
    Comandos de control en CH0, por APRS, desde indicativo autorizado:

      [CH 0] APRS ON
      [CH 0] APRS OFF

    Actúan sobre APRS_GATE_ENABLED (gate APRS→Mesh).
    Devuelve True si ha gestionado el comando (para NO reenviar a la malla).
    """
    global APRS_GATE_ENABLED

    t = (msg or "").strip().upper()
    if not t:
        return False

    if t in ("APRS ON", "APRS GATE ON", "APRS-ON", "ON"):
        APRS_GATE_ENABLED = 1
        print(f"[aprs ctrl] {src}: APRS GATE → ON")
        return True

    if t in ("APRS OFF", "APRS GATE OFF", "APRS-OFF", "OFF"):
        APRS_GATE_ENABLED = 0
        print(f"[aprs ctrl] {src}: APRS GATE → OFF")
        return True

    return False


# =========================
# === APRS → Mesh ==========
# =========================
# === NUEVO: extractor unificado de canal+texto desde un paquete APRS ===

def _pick_ch_and_text(pkt: dict, default_ch: int = MESHTASTIC_CHANNEL) -> tuple[int, str] | None:
    """
    Devuelve (canal, texto) SOLO si existe una etiqueta [CHx] / [CANAL x].
    - 'status' y 'message': buscan etiqueta en pkt['text'].
    - Resto (posiciones/otros): buscan en pkt['info'] completa.
    Si no hay etiqueta → None (NO reinyectar).
    """
    if not pkt:
        return None

    if pkt.get("type") in {"status", "message"}:
        ch, msg = extract_channel_if_tagged(pkt.get("text", ""))
        return (ch, msg) if (ch is not None and msg) else None

    info = (pkt.get("info") or "").strip()
    if not info:
        return None

    ch, msg = extract_channel_if_tagged(info)
    return (ch, msg) if (ch is not None and msg) else None


async def task_aprs_to_meshtastic():
    """
    Escucha KISS TCP y reenvía a la malla:
      - '>' status con [CHx]
      - Mensajes dirigidos ':' con [CHx]
      - Posiciones/otros que lleven [CHx] en el comentario dentro de 'info'
    Respeta el flag APRS_GATE_ENABLED (ON/OFF).
    """
    last_channels_raw = None
    push_ch_set = None

    backoff = 2.0
    
    while True:
        try:
            reader, writer = await asyncio.open_connection(KISS_HOST, KISS_PORT)
            buf = bytearray()
            backoff = 2.0
            while True:
                data = await reader.read(4096)
                if not data:
                    raise ConnectionError("KISS closed")
                
                buf.extend(data)

                for fr in kiss_iter_frames_from_buffer(buf):
                    pkt = parse_ax25_ui(fr)
                    if not pkt:
                        continue

                    src  = (pkt.get("src", "?") or "?").strip().upper()
                    dest = pkt.get("dest", "?")
                    path = ",".join(pkt.get("path", []))
                    typ  = pkt.get("type") or "ui"
                    preview = (pkt.get("text") or pkt.get("info") or "").replace("\n", " ")[:160]
                    print(f"[aprs] RX {typ} src={src} dest={dest} path={path} info='{preview}'")

                    # --- Filtro por indicativo de origen (si APRS_ALLOWED_SOURCES está definido) ---
                    if not _aprs_source_allowed(src):
                        _aprs_dbg(f"[aprs] drop(src not allowed) src={src}")
                        continue

                    # === Parseo con aprslib (opcional) para datos de posición (solo log) ===
                    try:
                        tnc2 = f"{src}>{dest}{(',' + path) if path else ''}:{pkt.get('info', '')}"
                        ap = aprslib.parse(tnc2)
                        if 'latitude' in ap and 'longitude' in ap:
                            lat = ap['latitude']; lon = ap['longitude']
                            course = ap.get('course'); speed = ap.get('speed')
                            alt = ap.get('altitude')
                            print(f"[aprs] POS aprslib lat={lat:.6f} lon={lon:.6f}"
                                  f"{'' if course is None else f' crs={int(course):03d}°'}"
                                  f"{'' if speed is None else f' spd={int(speed)}'}"
                                  f"{'' if alt is None else f' alt={int(alt)}'}")
                    except Exception:
                        ap = None

                    # --- [WEB ADMIN] Persistir RX APRS en jsonl para mapa/stream del panel ---
                    # Se guarda SIEMPRE en modo best-effort.
                    # Importante:
                    #   - aprs_rx.jsonl mantiene su comportamiento histórico.
                    #   - broker_offline_log.jsonl recibe una copia APRS_RX solo para observabilidad WebPanel.
                    #   - El reenvío a Mesh sigue dependiendo del filtro [CHx] posterior.
                    rec = None
                    try:
                        rec = {
                            "ts": int(time.time()),
                            "callsign": src,
                            "type": typ,
                            "dest": dest,
                            "path": path,
                            "info": preview,
                            "raw": tnc2 if 'tnc2' in locals() else None,
                        }

                        if isinstance(ap, dict):
                            if "latitude" in ap and "longitude" in ap:
                                rec["lat"] = float(ap.get("latitude"))
                                rec["lon"] = float(ap.get("longitude"))
                            if ap.get("course") is not None:
                                rec["course"] = ap.get("course")
                            if ap.get("speed") is not None:
                                rec["speed"] = ap.get("speed")
                            if ap.get("altitude") is not None:
                                rec["alt"] = ap.get("altitude")
                            if ap.get("symbol") is not None:
                                rec["symbol"] = ap.get("symbol")

                    except Exception as e:
                        print(
                            f"[aprs→broker-backlog] APRS_RX build ERR "
                            f"src={src} err={type(e).__name__}: {e}",
                            flush=True,
                        )
                        rec = None

                    if isinstance(rec, dict):
                        # Guardado histórico APRS local.
                        # Separado del backlog broker para que un fallo en aprs_rx.jsonl
                        # no impida publicar APRS_RX para el WebPanel.
                        try:
                            _aprs_web_append(rec)
                        except Exception as e:
                            print(
                                f"[aprs→web-jsonl] APRS_RX append ERR "
                                f"src={src} err={type(e).__name__}: {e}",
                                flush=True,
                            )

                        # Duplicado de observabilidad hacia backlog del broker.
                        # No transmite RF. No puentea a Mesh. No depende del filtro [CHx].
                        try:
                            print(
                                f"[aprs→broker-backlog] APRS_RX observed append TRY src={src}",
                                flush=True,
                            )
                            _aprs_broker_backlog_append(rec)
                        except Exception as e:
                            print(
                                f"[aprs→broker-backlog] APRS_RX observed append ERR "
                                f"src={src} err={type(e).__name__}: {e}",
                                flush=True,
                            )
                                       
                    # --- Extraer canal + posible delay (+N minutos) desde [CH x] / [CANAL x+N] / [MCx] ---
                    direct_meshcore_channel = _channel_tag_is_meshcore((pkt.get("text") or pkt.get("info") or ""))
                    ch, delay_min, msg = _parse_ch_and_delay_from_pkt(pkt, default_ch=MESHTASTIC_CHANNEL)
                    if ch is None or not msg:
                        _aprs_dbg(f"[aprs] drop(no CH) {pkt.get('type','ui')} src={pkt.get('src','?')}")
                        continue

                    # --- Si es posición APRS, convertir a enlace de mapa ---
                   
                    # --- Procesado de posición APRS (RF) ---
                    # Convertimos la trama RF ya parseada a formato TNC2 estándar
                    ap = None
                    try:
                        tnc2 = f"{src}>{dest}{(',' + path) if path else ''}:{pkt.get('info','')}"
                        ap = aprslib.parse(tnc2)
                    except Exception:
                        ap = None

                    # Si hay coordenadas, generar enlace Google Maps
                    try:
                        if isinstance(ap, dict) and "latitude" in ap and "longitude" in ap:
                            lat = ap["latitude"]
                            lon = ap["longitude"]
                            link = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"
                            msg_clean = (msg or "").strip()
                            msg = f"{msg_clean} {link}" if msg_clean else link
                    except Exception as _e:
                        _aprs_dbg(f"[aprs RF] maplink error: {type(_e).__name__}: {_e}")



                    # --- Comandos de control en CH0 (no reinyectar a Mesh) ---
                    # --- Detección de mensaje de EMERGENCIA APRS (antes de aplicar filtros de CH0/GATE) ---
                    try:
                        emerg_info = _classify_aprs_emergency(pkt, ap, msg)
                    except Exception as _e:
                        emerg_info = None
                        _aprs_dbg(f"[aprs] emergency detect error: {type(_e).__name__}: {_e}")
                    is_emergency = bool(emerg_info)

                    # --- Comandos de control en CH0 (no reinyectar a Mesh) ---
                    #     EXCEPTO si se ha detectado emergencia, en cuyo caso se fuerza el bypass.
                    if ch == 0 and not is_emergency:
                        if _handle_aprs_control_from_rf(src, msg):
                            # Comando gestionado (APRS ON/OFF); no se envía a la malla
                            continue
                        _aprs_dbg(f"[aprs ctrl] CH0 sin comando conocido desde {src}: {msg[:80]}")
                        continue

                    # --- Gate APRS→Mesh ON/OFF ---
                    #     Si el gate está OFF pero el mensaje es de emergencia, se hace bypass igualmente.
                    if (not _aprs_gate_is_enabled()) and (not is_emergency):
                        print(f"[aprs→mesh] GATE OFF: descartado CH{ch} ← {src}: {msg[:120]}")
                        continue

                    # --- Selección de canales Mesh destino para emergencias ---
                    if is_emergency:
                        base_ch = ch if ch is not None else MESHTASTIC_CHANNEL
                        if base_ch is None:
                            base_ch = MESHTASTIC_CHANNEL

                        # Lista de canales finales según geo-fencing y configuración
                        channels: list[int] = []
                        is_local = emerg_info.get("is_local") if emerg_info else None
                        if is_local is True and _EMERG_MESH_CHANNELS:
                            # Emergencia local: canales dedicados + canal original (si es distinto)
                            channels = list(dict.fromkeys(
                                _EMERG_MESH_CHANNELS
                                + ([base_ch] if base_ch not in _EMERG_MESH_CHANNELS else [])
                            ))
                        elif _EMERG_MESH_CHANNELS:
                            # Emergencia remota o sin distancia: sólo canal original para no saturar
                            channels = [base_ch]
                        else:
                            # Sin configuración específica: sólo canal indicado por [CHx]
                            channels = [base_ch]

                        # Prefijo de estado / heartbeat mínimo de red
                        scope_txt = (
                            "LOCAL"
                            if emerg_info and emerg_info.get("is_local")
                            else ("REMOTA" if emerg_info and emerg_info.get("is_local") is False else "DESCONOCIDA")
                        )
                        gate_txt = "ON" if _aprs_gate_is_enabled() else "OFF"
                        prefix = f"[EMERG APRS][{scope_txt}] src={src} gate={gate_txt}"
                        
                        msg_mesh = _mesh_add_src_prefix(src, msg)
                        mesh_text = f"{prefix}\n{msg_mesh}"

                        for ch_target in channels:
                            try:
                                # Envío normal de la emergencia a la malla
                                res = _broker_send_mesh_text(ch_target, mesh_text, dest=None, ack=False)
                                ok = bool(res.get("ok"))
                                print(
                                    f"[aprs→mesh EMERG] CH{ch_target} ← {src}: {mesh_text[:120]} -> {'OK' if ok else 'KO'}"
                                )

                                # --- ECO OPCIONAL AL NODO HOME COMO COMPROBANTE ---
                                if HOME_NODE_ID and not _aprs_meshcore_mode():
                                    try:
                                        eco_txt = f"[APRS eco de {src}] {msg_mesh}"
                                        res_eco = _broker_send_mesh_text(
                                            ch_target, eco_txt, dest=HOME_NODE_ID, ack=False
                                        )
                                        ok_eco = bool(res_eco.get("ok"))
                                        print(
                                            f"[aprs→mesh ECO] CH{ch_target} → {HOME_NODE_ID}: "
                                            f"{eco_txt[:120]} -> {'OK' if ok_eco else 'KO'}"
                                        )
                                    except Exception as _e:
                                        print(f"[aprs→mesh ECO] ❌ {type(_e).__name__}: {_e}")
                                # --- FIN ECO ---
                            except Exception as _e:
                                print(f"[aprs→mesh EMERG] ❌ {type(_e).__name__}: {_e}")

                        # Notificación inmediata a Telegram (mejor esfuerzo, no bloqueante)
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(_notify_telegram_emergency(emerg_info, channels))
                        except Exception as _e:
                            _aprs_dbg(f"[aprs] emergency telegram notify error: {type(_e).__name__}: {_e}")

                        # Emergencias ya gestionadas, no procesar por la ruta normal
                        continue

                    # --- Programación local con [CH x+N] ---
                    if delay_min is not None and delay_min > 0:
                        print(f"[aprs→mesh] PROGRAMADO CH{ch} (+{delay_min}m) ← {src}: {msg[:120]}")
                        _schedule_aprs_to_mesh(ch, msg, delay_min, src)
                    else:
                        # Envío inmediato al broker (como antes)
                        #res = _broker_send_text(ch, msg, dest=None, ack=False)
                        msg_mesh = _mesh_add_src_prefix(src, msg)
                        res = _broker_send_mesh_text(ch, msg_mesh, dest=None, ack=False, direct_meshcore_channel=direct_meshcore_channel)
                        
                        ok = bool(res.get("ok"))
                        print(f"[aprs→mesh] CH{ch} ← {src}: {msg_mesh[:120]}  -> {'OK' if ok else 'KO'}")
                        # --- ECO OPCIONAL AL NODO HOME COMO COMPROBANTE ---
                        if HOME_NODE_ID and not _aprs_meshcore_mode():
                            try:
                                eco_txt = f"[APRS eco de {src}] {msg_mesh}"
                                res_eco = _broker_send_mesh_text(ch, eco_txt, dest=HOME_NODE_ID, ack=False)
                                ok_eco = bool(res_eco.get("ok"))
                                print(f"[aprs→mesh ECO] CH{ch} → {HOME_NODE_ID}: {eco_txt[:120]} -> {'OK' if ok_eco else 'KO'}")
                                    # --- ECO OPCIONAL AL NODO HOME COMO COMPROBANTE (APRS-IS) ---
                                  
                            except Exception as _e:
                                print(f"[aprs→mesh ECO] ❌ {type(_e).__name__}: {_e}")
                        # --- FIN ECO ---

                    # --- (OPCIONAL) Uplink APRS-IS: SOLO posiciones con [CHx], respetando NOGATE/RFONLY ---

                    if _aprsis_ready() and _is_position_pkt(pkt) and _has_ch_tag_in_info(pkt) and not _should_not_gate(pkt.get('info','')):
                        try:
                            global _aprsis_client
                            if _aprsis_client is None:
                                _aprsis_client = _AprsISClient(APRSIS_USER, APRSIS_PASSCODE, APRSIS_HOST, APRSIS_PORT, "")

                            line = _make_thirdparty_line(pkt, APRSIS_USER)
                            if line:
                                ok = await _aprsis_send_line_safe(line)  # normaliza '\n' + retry tras Broken pipe
                                print(f"[aprs→IS] UP {len(line)}B -> {'OK' if ok else 'KO'}")

                       
                        except Exception as e:
                            print(f"[aprs→IS] ❌ {e}")


        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.5)


async def task_aprsis_to_meshtastic():
    """
    Lee el feed APRS-IS (si hay credenciales configuradas) y reinyecta a la malla
    los mensajes que lleven un marcador [CHx] / [CANAL x] en el payload.

    - Respeta APRS_GATE_ENABLED (ON/OFF).
    - Respeta APRS_ALLOWED_SOURCES (lista blanca de indicativos).
    - Respeta NOGATE / RFONLY en el texto.
    - Soporta third-party frames (IGATE>APRS:}SRC>DEST,PATH:payload)
      desenrollando la parte interna antes de parsear canal y mensaje.
    """
    if not _aprsis_ready():
        return

    backoff = 5.0
    # === [FIX] Indicativo propio en APRS-IS para filtrar ecos ===
    _SELF_APRSIS = (APRSIS_USER or "").strip().upper()

    while True:
        try:
            reader, writer = await asyncio.open_connection(APRSIS_HOST, APRSIS_PORT)

            flt = APRSIS_FILTER or "m/50"
            login = (
                f"user {APRSIS_USER} pass -1"
                f"vers MESH-APRS 0.1 filter {flt}\n"
            )
            try:
                writer.write(login.encode("ascii", "ignore"))
                await writer.drain()
            except Exception as e:
                print(f"[aprs←IS] ❌ Error enviando login inicial: {e}")
                writer.close()
                await writer.wait_closed()
                raise

            print(
                f"[aprs←IS] Conectado a {APRSIS_HOST}:{APRSIS_PORT} "
                f"como {APRSIS_USER} con filtro '{flt}'"
            )
            backoff = 5.0

            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("APRS-IS cerró la conexión")

                try:
                    s = line.decode("utf-8", "ignore").strip()
                except Exception:
                    continue

                if not s:
                    continue

                # Log bruto de TODO lo que llega (con APRS_DEBUG=1 lo verás)
                _aprs_dbg(f"[aprs←IS RAW] {s}")

                if s.startswith("#"):
                    continue  # comentarios/keepalive

                if ":" not in s or ">" not in s:
                    _aprs_dbg(f"[aprs←IS] línea no TNC2 descartada: {s[:120]}")
                    continue

                # Cabecera exterior (puede ser IGATE>APRS,...:)
                try:
                    outer_hdr, outer_info = s.split(":", 1)
                except ValueError:
                    _aprs_dbg(f"[aprs←IS] sin ':' descartada: {s[:120]}")
                    continue

                # Detectar third-party: }SRC>DEST,PATH:payload
                if outer_info.startswith("}"):
                    inner = outer_info[1:].strip()
                    if ":" not in inner or ">" not in inner:
                        _aprs_dbg(
                            f"[aprs←IS] 3rd-pty malformado descartado: {s[:120]}"
                        )
                        continue
                    inner_tnc2 = inner
                else:
                    # Trama directa: usamos la línea completa
                    inner_tnc2 = s

                # Ahora trabajamos siempre sobre inner_tnc2
                try:
                    inner_hdr, inner_info = inner_tnc2.split(":", 1)
                except ValueError:
                    _aprs_dbg(
                        f"[aprs←IS] inner TNC2 sin ':' descartado: {inner_tnc2[:120]}"
                    )
                    continue

                try:
                    src = inner_hdr.split(">")[0].strip()
                except Exception:
                    src = "?"

                # === [FIX QUIRÚRGICO] Ignorar eco APRS-IS de nuestro propio indicativo ===
                # APRS-IS puede devolver nuestras propias tramas. Si se procesan como RX,
                # se reinyectan a Mesh y aparecen duplicados/eco.
                if _SELF_APRSIS and src and src.strip().upper() == _SELF_APRSIS:
                    _aprs_dbg(f"[aprs←IS] drop(self-echo) src={src} info={inner_info[:120]}")
                    continue


                # Lista blanca
                if not _aprs_source_allowed(src):
                    _aprs_dbg(f"[aprs←IS] drop(src not allowed) src={src}")
                    continue

                # NOGATE / RFONLY sobre el payload real
                if _should_not_gate(inner_info):
                    _aprs_dbg(f"[aprs←IS] drop(NOGATE/RFONLY) src={src}")
                    continue

                # Paquete sintético para reutilizar el parser de canal/delay
                body_text = _aprs_extract_message_body(inner_info)

                pkt = {
                    "type": "message",
                    "src": src,
                    "dest": None,
                    "info": inner_info,   # mantenemos original para debug/trazas
                    "text": body_text,    # aquí debe estar el texto “real” donde vive [CHx]/[MCx]
                }

                # Observabilidad para bot/web: registrar también las tramas que llegan
                # por APRS-IS (APRSdroid), no solo las recibidas por KISS/RF.
                try:
                    rec = {
                        "ts": int(time.time()),
                        "callsign": src,
                        "type": "message",
                        "dest": None,
                        "path": None,
                        "info": body_text or inner_info,
                        "raw": inner_tnc2,
                        "source": "aprs-is",
                    }
                    _aprs_web_append(rec)
                    _aprs_broker_backlog_append(rec)
                except Exception as _e:
                    _aprs_dbg(f"[aprs←IS→broker-backlog] APRS_RX append ERR src={src}: {type(_e).__name__}: {_e}")

                direct_meshcore_channel = _channel_tag_is_meshcore(body_text)
                ch, delay_min, msg = _parse_ch_and_delay_from_pkt(
                    pkt, default_ch=MESHTASTIC_CHANNEL
                )
                if ch is None or not msg:
                    # No había [CHx]/[CANAL x]/[MCx]
                    _aprs_dbg(
                        f"[aprs←IS] sin [CHx]/[CANAL x]/[MCx] usable desde {src}: {inner_info[:80]}"
                    )
                    continue

                if not _aprs_gate_is_enabled():
                    print(
                        f"[aprs←IS→mesh] GATE OFF: descartado CH{ch} ← {src}: {msg[:120]}"
                    )
                    continue

                # Posición para enlace de mapa (si existe) usando el paquete interno
                # Intentar extraer posición...
                ap = None
                try:
                    ap = aprslib.parse(inner_tnc2)
                except Exception:
                    ap = None

                try:
                    if ap and isinstance(ap, dict) and "latitude" in ap and "longitude" in ap:
                        lat = ap["latitude"]
                        lon = ap["longitude"]
                        link = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"
                        msg_clean = (msg or "").strip()
                        if msg_clean:
                            msg = f"{msg_clean} {link}"
                        else:
                            msg = link
                except Exception as _e:
                    _aprs_dbg(f"[aprs←IS] maplink error: {type(_e).__name__}: {_e}")


                # Comandos CH0 (APRS ON/OFF) vía APRS-IS
                # CH0 se reserva para control SOLO si el mensaje es un comando válido.
                # Si NO es comando, se reenvía como texto normal por el canal 0
                if ch == 0:
                    if _handle_aprs_control_from_rf(src, msg):
                        continue
                    #_aprs_dbg(
                    #    f"[aprs←IS ctrl] CH0 sin comando conocido desde {src}: {msg[:80]}"
                    #)
                    #continue

                # Programación local con [CHx+N]
                if delay_min is not None and delay_min > 0:
                    print(
                        f"[aprs←IS→mesh] PROGRAMADO CH{ch} (+{delay_min}m) ← {src}: {msg[:120]}"
                    )
                    _schedule_aprs_to_mesh(ch, msg, delay_min, src)
                else:
                    #res = _broker_send_text(ch, msg, dest=None, ack=False)
                    msg_mesh = _mesh_add_src_prefix(src, msg)
                    res = _broker_send_mesh_text(ch, msg_mesh, dest=None, ack=False, direct_meshcore_channel=direct_meshcore_channel)

                    
                    ok = bool(res.get("ok"))
                    print(
                        f"[aprs←IS→mesh] CH{ch} ← {src}: {msg[:120]}  -> {'OK' if ok else 'KO'}"
                    )
                    # --- ECO OPCIONAL AL NODO HOME COMO COMPROBANTE (APRS-IS) ---
                    if HOME_NODE_ID and not _aprs_meshcore_mode():
                        try:
                            #eco_txt = f"[APRS-IS eco de {src}] {msg}"
                            eco_txt = f"[APRS-IS eco de {src}] {msg_mesh}"

                            res_eco = _broker_send_mesh_text(
                                ch, eco_txt, dest=HOME_NODE_ID, ack=False
                            )
                            ok_eco = bool(res_eco.get("ok"))
                            print(
                                f"[aprs←IS→mesh ECO] CH{ch} → {HOME_NODE_ID}: "
                                f"{eco_txt[:120]} -> {'OK' if ok_eco else 'KO'}"
                            )
                        except Exception as _e:
                            print(f"[aprs←IS→mesh ECO] ❌ {type(_e).__name__}: {_e}")
                    # --- FIN ECO ---

                   
        except Exception as e:
            print(f"[aprs←IS] ❌ desconectado: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.7)
            continue



async def task_aprsis_connect_on_startup():
    """
    Si APRS-IS está habilitado (user+passcode), intenta conectar al inicio
    y muestra el resultado por consola (OK o error). No reintenta en bucle:
    el resto del código reconectará si hace falta al primer envío real.
    """
    if not _aprsis_ready():
        return
    try:
        global _aprsis_client
        if _aprsis_client is None:
            _aprsis_client = _AprsISClient(APRSIS_USER, APRSIS_PASSCODE, APRSIS_HOST, APRSIS_PORT, "")
        await _aprsis_client.connect()
        # Si llega aquí, ya se anunció "Conectado OK ..." desde _ensure_sync()
    except Exception as e:
        # Ya se anunció el error desde _ensure_sync(); dejamos constancia adicional si quieres:
        print(f"[aprs→IS] ❌ Conexión inicial fallida: {e}")


# =========================
# === Control UDP (bot→APRS)
# =========================

async def task_control_udp():
    """
    Escucha UDP local:
      {"mode":"aprs","dest":"EA2ABC|broadcast","text":"...","path":"WIDE1-1,WIDE2-1"}
      {"mode":"aprs_preview","dest":"EA2ABC|broadcast","text":"..."}
      {"mode":"aprs_gate","enable":1|0}
      {"mode":"aprs_status"}
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    sock.bind((CONTROL_UDP_BIND, CONTROL_UDP_PORT))
    sock.setblocking(False)
    print(f"[ctrl] UDP escuchando en {CONTROL_UDP_BIND}:{CONTROL_UDP_PORT} (clientes={CONTROL_UDP_HOST}:{CONTROL_UDP_PORT})")

    loop = asyncio.get_running_loop()
    global APRS_GATE_ENABLED

    while True:
        try:
            data, addr = await loop.run_in_executor(None, sock.recvfrom, 65536)
        except Exception:
            await asyncio.sleep(0.05)
            continue

        try:
            obj = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            print("[ctrl] ❌ JSON inválido")
            continue

        mode = (obj.get("mode") or obj.get("cmd") or "").strip().lower()
        if not mode:
            continue

        # --- Consulta de estado del gate ---
        if mode == "aprs_status":
            resp = {"ok": True, "aprs_gate_enabled": bool(APRS_GATE_ENABLED)}
            try: sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception: pass
            print(f"[ctrl] status → gate={'ON' if APRS_GATE_ENABLED else 'OFF'}")
            continue

        # --- ON/OFF del gate ---
        if mode in ("aprs_gate", "aprs_on", "aprs_off"):
            if mode == "aprs_on":
                APRS_GATE_ENABLED = 1
            elif mode == "aprs_off":
                APRS_GATE_ENABLED = 0
            else:
                APRS_GATE_ENABLED = 1 if int(obj.get("enable", 1)) else 0
            resp = {"ok": True, "aprs_gate_enabled": bool(APRS_GATE_ENABLED)}
            try: sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception: pass
            print(f"[ctrl] gate → {'ON' if APRS_GATE_ENABLED else 'OFF'} (petición UDP)")
            continue
      
        # --- v7.0.47: diagnóstico BLNx largo, exclusivamente APRS-IS ---
        if mode == "aprsis_long_bulletin_test":
            try:
                resp = await send_aprsis_long_bulletin_test(obj.get("text", ""), obj.get("bulletin_number", 0))
            except Exception as exc:
                resp = {"ok": False, "sent": False, "reason": "internal_error", "error": f"{type(exc).__name__}: {exc}"}
            try:
                sock.sendto(json.dumps(resp, ensure_ascii=False).encode("utf-8"), addr)
            except Exception:
                pass
            continue

        # --- v7.0.46: diagnóstico manual largo, solo APRS-IS ---
        if mode == "aprsis_long_test":
            try:
                resp = await send_aprsis_long_test(obj.get("text", ""))
            except Exception as exc:
                resp = {
                    "ok": False, "sent": False, "reason": "internal_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(f"[APRS-IS LONG TEST] ERROR {resp['error']}")
            try:
                sock.sendto(json.dumps(resp, ensure_ascii=False).encode("utf-8"), addr)
            except Exception:
                pass
            continue

        # --- Boletín público APRS-IS para emergencias graves ---
        if mode == "aprsis_emergency_bulletin":
            try:
                resp = await send_aprsis_emergency_bulletin(
                    event_id=obj.get("event_id", ""),
                    text=obj.get("text", ""),
                    severity=obj.get("severity", ""),
                    status=obj.get("status", "active"),
                )
            except Exception as exc:
                resp = {"ok": False, "sent": False, "reason": "internal_error", "error": f"{type(exc).__name__}: {exc}"}
                print(f"[APRS-IS BLN] ERROR {resp['error']}")
            try:
                sock.sendto(json.dumps(resp, ensure_ascii=False).encode("utf-8"), addr)
            except Exception:
                pass
            continue

        # --- Control del mirror Mesh -> APRS-IS ---
        if mode == "aprsis_push":
            global APRSIS_PUSH_ENABLED, APRSIS_PUSH_TO, APRSIS_PUSH_CHANNELS_RAW, APRSIS_PUSH_PREFIX, APRSIS_PUSH_MIN_GAP_S

            en = obj.get("enabled", None)
            if en is not None:
                try: APRSIS_PUSH_ENABLED = 1 if int(en) else 0
                except Exception: APRSIS_PUSH_ENABLED = 0

            to = obj.get("to", None)
            if to is not None:
                APRSIS_PUSH_TO = (str(to) or "").strip().upper()

            chs = obj.get("channels", None)
            if chs is not None:
                APRSIS_PUSH_CHANNELS_RAW = (str(chs) or "all").strip().lower()

            pref = obj.get("prefix", None)
            if pref is not None:
                try: APRSIS_PUSH_PREFIX = 1 if int(pref) else 0
                except Exception: pass

            gap = obj.get("min_gap_s", None)
            if gap is not None:
                try: APRSIS_PUSH_MIN_GAP_S = max(0.0, float(gap))
                except Exception: pass

            _cfg = _parse_push_channel_config(APRSIS_PUSH_CHANNELS_RAW)
            _cfg_json = {k: ("all" if v is None else sorted(v)) for k, v in _cfg.items()}
            resp = {
                "ok": True,
                "enabled": bool(APRSIS_PUSH_ENABLED),
                "to": APRSIS_PUSH_TO,
                "channels": APRSIS_PUSH_CHANNELS_RAW,
                "channel_config": _cfg_json,
                "prefix": bool(APRSIS_PUSH_PREFIX),
                "min_gap_s": APRSIS_PUSH_MIN_GAP_S,
            }
            try: sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception: pass
            print(f"[ctrl] aprsis_push -> enabled={APRSIS_PUSH_ENABLED} to={APRSIS_PUSH_TO} channels={APRSIS_PUSH_CHANNELS_RAW} cfg={resp['channel_config']} prefix={APRSIS_PUSH_PREFIX} gap={APRSIS_PUSH_MIN_GAP_S}")
            continue



        # --- Previsualización APRS sin RF ni deduplicación ---
        # Permite a aplicaciones externas conocer el número REAL de partes que
        # generaría este mismo gateway antes de solicitar una transmisión.
        if mode == "aprs_preview":
            dest = (obj.get("dest") or "").strip()
            text = (obj.get("text") or "").strip()
            if not dest or not text:
                resp = {"ok": False, "sent": False, "reason": "missing_dest_or_text", "parts": 0}
            else:
                dest_norm, _text_clean, payloads, _dest_hdr = _build_control_aprs_payloads(dest, text)
                resp = {
                    "ok": True,
                    "sent": False,
                    "preview": True,
                    "dest": dest_norm,
                    "parts": len(payloads),
                }
            try:
                sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception:
                pass
            continue

        # --- TX APRS normal ---
        if mode != "aprs":
            continue

        dest = (obj.get("dest") or "").strip()
        text = (obj.get("text") or "").strip()

        path_obj = obj.get("path", None)
        if isinstance(path_obj, list):
            # Lista explícita desde el bot. [] significa sin digipeaters.
            path_override = [str(p).strip() for p in path_obj if str(p).strip()]
        else:
            path_str = (path_obj or "").strip()
            path_override = [p for p in path_str.split(",") if p] if path_str else None
        if not dest or not text:
            print("[ctrl] ❌ falta dest o text")
            try:
                sock.sendto(json.dumps({"ok": False, "error": "missing dest or text"}).encode("utf-8"), addr)
            except Exception:
                pass
            continue

        # Preparar exactamente los mismos payloads que usa ``aprs_preview``.
        dest_norm, text, payloads, dest_hdr = _build_control_aprs_payloads(dest, text)

        # DEDUP
        origin = str(obj.get("origin") or obj.get("src") or "").strip().lower()
        force_tx = str(obj.get("force_tx") or obj.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
        # Los envíos interactivos del bot deben poder repetirse aunque sean
        # idénticos dentro de la ventana TTL. Aun así se marcan antes de TX para
        # que el eco posterior del broker/malla sí quede suprimido.
        ctrl_bypass_dedup = force_tx or origin in ("bot_send", "telegram_aprs")
        if (not ctrl_bypass_dedup) and _dedup_seen(dest_norm, text):
            print(f"[ctrl] duplicado ignorado para dest={dest_norm}")
            try:
                sock.sendto(json.dumps({"ok": False, "duplicate": True, "dest": dest_norm, "parts": 0, "sent": 0, "error": "duplicate_suppressed"}).encode("utf-8"), addr)
            except Exception:
                pass
            continue
        # Marcar ANTES de transmitir para cerrar ventana de carrera:
        # si entra el mismo payload por otra ruta (p.ej. eco broker) mientras
        # aún estamos enviando partes RF, debe quedar suprimido.
        _dedup_mark(dest_norm, text)

        ok_all = True
        sent_count = 0
        total_parts = len(payloads)
        for part_idx, pld in enumerate(payloads, 1):
            ok = _tx_aprs_payload(pld, dest_hdr, path_override=path_override)
            ok_all = ok_all and ok
            if ok:
                sent_count += 1
            if part_idx < total_parts:
                gap_s = _aprs_rf_part_gap_s(len(pld))
                if gap_s > 0:
                    print(f"[ctrl→aprs] pausa multipart {gap_s:.2f}s antes de parte {part_idx + 1}/{total_parts}")
                    await asyncio.sleep(gap_s)

        _dedup_mark(dest_norm, text)
        resp = {
            "ok": bool(ok_all),
            "dest": dest_norm,
            "parts": len(payloads),
            "sent": sent_count,
            "rf": bool(ok_all),
        }
        if not ok_all:
            resp["error"] = "kiss_tx_failed"
        try:
            sock.sendto(json.dumps(resp).encode("utf-8"), addr)
        except Exception:
            pass
        print(f"[ctrl] Resultado: {'OK' if ok_all else 'KO'} para dest={dest_norm} parts={len(payloads)} sent={sent_count}")


# =========================
# === Mesh → APRS (stub) ===
# =========================
# =========================
# === Mesh → APRS (stream broker)
# =========================

_APRS_CMD_RE = re.compile(
    r"^\s*/aprs(?:\s+(?:canal|ch)\s+(\d{1,2}))?\s+([A-Za-z0-9\-]+)\s*:\s*(.+)\s*$",
    re.IGNORECASE
)

async def task_broker_to_aprs():
    """
    Conecta al servidor JSONL del broker (BROKER_HOST:BROKER_PORT),
    detecta /aprs broadcast: ... y /aprs EA2ABC: ... y los transmite por APRS.

    - RF (KISS): build_aprs_* + _tx_aprs_payload
    - APRS-IS: también publica el mensaje en APRS-IS (si hay credenciales)
    - Evita duplicados con dedup
    - ROBUSTO: soporta eventos envueltos del broker (packet/summary/decoded/payload)
    """
    backoff = 2.0

    def _as_dict(x):
        return x if isinstance(x, dict) else {}

    def _first_str(*vals) -> str:
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v
        return ""

    def _first_any(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    def _norm_event(obj: dict):
        """
        Devuelve: (port_upper:str, text:str, ch:int|None)

        Soporta:
          - plano:    {"portnum":"TEXT_MESSAGE_APP","text":"...","channel":2}
          - envuelto: {"packet":{...},"summary":{...}}
          - mixto:    {"decoded":{...}} / {"payload":{...}} en raíz o en packet
        """
        obj = _as_dict(obj)
        summ = _as_dict(obj.get("summary"))
        pkt = _as_dict(obj.get("packet")) if isinstance(obj.get("packet"), dict) else obj

        dec_root = _as_dict(obj.get("decoded"))
        pay_root = _as_dict(obj.get("payload"))
        dec_pkt  = _as_dict(pkt.get("decoded"))
        pay_pkt  = _as_dict(pkt.get("payload"))
        meta_pkt = _as_dict(pkt.get("meta"))

        port = _first_str(
            str(summ.get("portnum") or ""),
            str(dec_pkt.get("portnum") or ""),
            str(dec_root.get("portnum") or ""),
            str(pkt.get("portnum") or ""),
            str(obj.get("portnum") or ""),
        ).upper().strip()

        txt = _first_str(
            summ.get("text"),
            dec_pkt.get("text"),
            pay_pkt.get("text"),
            dec_root.get("text"),
            pay_root.get("text"),
            pkt.get("text"),
            obj.get("text"),
        )

        ch_raw = _first_any(
            summ.get("channel"), summ.get("canal"),
            pkt.get("channel"), pkt.get("channelIndex"),
            meta_pkt.get("channelIndex"),
            dec_pkt.get("channel"), dec_pkt.get("channelIndex"),
            dec_root.get("channel"), dec_root.get("channelIndex"),
            obj.get("channel"), obj.get("channelIndex"),
        )
        try:
            ch = int(ch_raw) if ch_raw is not None else None
        except Exception:
            ch = None

        return port, txt, ch

    while True:
        try:
            print(f"[broker→aprs] Conectando JSONL {BROKER_HOST}:{BROKER_PORT} …")
            reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
            print("[broker→aprs] Conectado. Esperando líneas…")
            backoff = 2.0

            while True:
                raw = await reader.readline()
                if not raw:
                    raise ConnectionError("broker closed")

                try:
                    obj = json.loads(raw.decode("utf-8", "ignore"))
                except Exception:
                    continue

                # DEBUG: confirma que vemos TEXT_MESSAGE_APP con /aprs (también envueltos)
                try:
                    _p, _t, _c = _norm_event(obj)
                    if _p == "TEXT_MESSAGE_APP" and isinstance(_t, str) and _t.lstrip().lower().startswith("/aprs"):
                        print(f"[broker→aprs][DBG] RX ch={_c} from={obj.get('from')} text={_t!r}")
                except Exception:
                    pass


                port, text, ch = _norm_event(obj)

                # Sólo textos de usuario
                if port != "TEXT_MESSAGE_APP":
                    continue
                if not isinstance(text, str) or not text.strip():
                    continue
                if not text.lstrip().lower().startswith("/aprs"):
                    continue

                m = _APRS_CMD_RE.match(text)
                if not m:
                    continue

                # group(1) = canal (opcional, solo informativo para mesh)
                # group(2) = destino APRS
                # group(3) = texto a enviar
                dest_token = _aprs_ascii((m.group(2) or "").strip())
                payload_text = _aprs_ascii((m.group(3) or "").strip())

               
                if not payload_text:
                    continue

                print(f"[broker→aprs][DBG] PARSED dest_token={dest_token!r} payload={payload_text!r} ch={ch}")

                # Normaliza destino + dedup + payloads RF
                if dest_token.lower() in ("broadcast", "all"):
                    dest_norm = "broadcast"
                    dest_hdr = "APRS"
                    
                    if _dedup_seen(dest_norm, payload_text):
                        print(f"[broker→aprs][DBG] DEDUP HIT dest={dest_norm} payload={payload_text!r} -> SKIP")
                        continue

                    payloads = build_aprs_status_chunks(payload_text, MAX_STATUS_LEN)
                else:
                    dest_norm = dest_token.upper()
                    dest_hdr = dest_norm

                    if _dedup_seen(dest_norm, payload_text):
                        print(f"[broker→aprs][DBG] DEDUP HIT dest={dest_norm} payload={payload_text!r} -> SKIP")
                        continue

                    payloads = build_aprs_message_chunks(dest_norm, payload_text, MAX_MSG_LEN)

                # Marcar ANTES del TX (RF/IS) para evitar doble emisión cuando
                # la misma orden llega casi simultáneamente por dos entradas.
                _dedup_mark(dest_norm, payload_text)
                print(f"[broker→aprs][DBG] DEDUP MARK(pre) dest={dest_norm} payload={payload_text!r}")

                # --- 1) RF (KISS) ---
                ok_all = True
                for i, pld in enumerate(payloads, 1):
                    try:
                        print(f"[broker→aprs][DBG] RF TX part {i}/{len(payloads)} dest_hdr={dest_hdr} bytes={len(pld)}")

                        try:
                            prev = pld
                            if isinstance(prev, (bytes, bytearray)):
                                prev = prev.decode("utf-8", "ignore")
                            prev = str(prev).replace("\r", "\\r").replace("\n", "\\n")
                            if len(prev) > 220:
                                prev = prev[:217] + "..."
                            print(f"[broker→aprs][DBG] RF PAYLOAD preview={prev}")
                        except Exception:
                            pass

                        ok = _tx_aprs_payload(pld, dest_hdr)
                        print(f"[broker→aprs][DBG] RF TX part {i}/{len(payloads)} -> {'OK' if ok else 'KO'}")
                        ok_all = ok_all and ok
    
                    except Exception as e:
                        ok_all = False
                        print(f"[broker→aprs][DBG] RF TX EXC part {i}/{len(payloads)}: {type(e).__name__}: {e}")
                    if i < len(payloads):
                        gap_s = _aprs_rf_part_gap_s(len(pld))
                        if gap_s > 0:
                            print(f"[broker→aprs][DBG] RF multipart pause {gap_s:.2f}s before part {i + 1}/{len(payloads)}")
                            await asyncio.sleep(gap_s)


                # --- 2) APRS-IS (opcional) ---
                ok_is = True
                if _aprsis_ready():
                    try:
                        prefix = f"[CH{ch}] " if (ch is not None) else ""
                        full_text = f"{prefix}{payload_text}"
                        if dest_norm == "broadcast":
                            is_lines = _aprsis_status_lines(full_text)
                        else:
                            is_lines = _aprsis_tnc2_message_lines(dest_norm, full_text, with_msgid=True)

                        if not is_lines:
                            ok_is = False
                        else:
                            ok_is = await _aprsis_send_lines_safe(is_lines)
                    except Exception as e:
                        ok_is = False
                        print(f"[broker→IS] ❌ {type(e).__name__}: {e}")

                _dedup_mark(dest_norm, payload_text)
                print(f"[broker→aprs][DBG] DEDUP MARK dest={dest_norm} payload={payload_text!r}")

                print(f"[broker→aprs] {dest_norm} parts={len(payloads)} → RF={'OK' if ok_all else 'KO'} IS={'OK' if ok_is else 'KO'}")

        except Exception as e:
            print(f"[broker→aprs] ❌ {type(e).__name__}: {e} — reintento en {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.5)


import argparse

def _apply_cli_overrides():
    global APRSIS_USER, APRSIS_PASSCODE, APRSIS_HOST, APRSIS_PORT, APRSIS_FILTER
    global APRS_GATE_ENABLED, KISS_HOST, KISS_PORT, BROKER_HOST, BROKER_PORT, MESHTASTIC_CHANNEL, KISS_CHANNEL

    p = argparse.ArgumentParser(prog="meshtastic_to_aprs.py", description="Pasarela Meshtastic ⇄ APRS")
    # --- APRS-IS ---
    p.add_argument("--aprsis-user", help="Indicativo para APRS-IS (ej. EB2XXX-10)")
    p.add_argument("--aprsis-passcode", help="Passcode APRS-IS asociado al indicativo")
    p.add_argument("--aprsis-host", help="Servidor APRS-IS (def. rotate.aprs2.net)")
    p.add_argument("--aprsis-port", type=int, help="Puerto APRS-IS (def. 14580)")
    p.add_argument("--aprsis-filter", help="Filtro opcional, p.ej. 'm/50'")

    # --- Otros útiles ---
    p.add_argument("--aprs-gate-enabled", type=int, choices=[0,1], help="1=ON 0=OFF para APRS→Mesh")
    p.add_argument("--kiss-host", help="Host KISS (def. 127.0.0.1)")
    p.add_argument("--kiss-port", type=int, help="Puerto KISS (def. 8100)")
    p.add_argument("--kiss-channel", type=int, help="Canal KISS 0..15 (def. 0)")
    p.add_argument("--broker-host", help="Host broker (def. 127.0.0.1)")
    p.add_argument("--broker-port", type=int, help="Puerto JSONL broker (def. 8765)")
    p.add_argument("--mesh-channel", type=int, help="Canal por defecto Mesh (def. 0)")

    args = p.parse_args()

    if args.aprsis_user is not None:     APRSIS_USER     = args.aprsis_user.strip()
    if args.aprsis_passcode is not None: APRSIS_PASSCODE = args.aprsis_passcode.strip()
    if args.aprsis_host is not None:     APRSIS_HOST     = args.aprsis_host.strip()
    if args.aprsis_port is not None:     APRSIS_PORT     = int(args.aprsis_port)
    if args.aprsis_filter is not None:   APRSIS_FILTER   = args.aprsis_filter.strip()

    if args.aprs_gate_enabled is not None: APRS_GATE_ENABLED = int(args.aprs_gate_enabled)
    if args.kiss_host is not None:       KISS_HOST = args.kiss_host.strip()
    if args.kiss_port is not None:       KISS_PORT = int(args.kiss_port)
    if args.kiss_channel is not None:    KISS_CHANNEL = max(0, min(15, int(args.kiss_channel)))
    if args.broker_host is not None:     BROKER_HOST = args.broker_host.strip()
    if args.broker_port is not None:     BROKER_PORT = int(args.broker_port)
    if args.mesh_channel is not None:    MESHTASTIC_CHANNEL = int(args.mesh_channel)

async def task_aprsis_uplink_keepalive():
    """
    Mantiene viva la conexión uplink a APRS-IS para evitar que el servidor la cierre por inactividad.

    - APRS-IS acepta líneas de comentario que empiezan por '#'
    - Se envía cada N segundos usando _aprsis_send_line_safe() (normaliza \\n + retry si hay Broken pipe)
    - No interfiere con /aprs ni con la pasarela RF.
    """
    import os, time

    interval_s = int(os.getenv("APRSIS_UPLINK_KEEPALIVE_S", "75"))
    interval_s = max(30, interval_s)

    while True:
        try:
            await asyncio.sleep(interval_s)

            # Si no está listo, no hacemos nada
            if not _aprsis_ready():
                continue

            # Si no hay cliente aún, no forzamos; ya se levantará al primer envío real
            c = globals().get("_aprsis_client", None)
            if c is None:
                continue

            # Comentario keepalive (no es una trama APRS, es para APRS-IS)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            await _aprsis_send_line_safe(f"# keepalive {APRSIS_USER} {ts}")

        except Exception:
            # Nunca romper el bucle por el keepalive
            pass

# =========================
# === Helpers: alias / canal / hops (para APRS-IS push)
# =========================

def _parse_channel_name_by_index_env() -> dict[int, str]:
    """
    Parse robusto de nombres de canal desde .env.

    Formatos admitidos (cualquiera de ellos):
      - CHANNEL_NAME_BY_INDEX="0:ZAR,1:EMERG,2:HAM"
      - CHANNEL_NAME_BY_INDEX="0=ZAR;1=EMERG;2=HAM"
      - CHANNEL_NAME_BY_INDEX="0|ZAR,1|EMERG"
    Devuelve: {0:"ZAR", 1:"EMERG", ...}
    """
    raw = (
        os.getenv("BROKER_CHANNEL_NAMES", "")
        or os.getenv("MESH_CHANNEL_NAMES", "")
        or os.getenv("CHANNEL_NAMES", "")
        or os.getenv("CHANNEL_NAME_BY_INDEX", "")
    ).strip()

    out: dict[int, str] = {}
    if not raw:
        return out

    # Normaliza separadores de pares a coma
    raw_norm = raw.replace(";", ",").strip()
    for part in raw_norm.split(","):
        p = (part or "").strip()
        if not p:
            continue

        # separador key/value
        if ":" in p:
            k, v = p.split(":", 1)
        elif "=" in p:
            k, v = p.split("=", 1)
        elif "|" in p:
            k, v = p.split("|", 1)
        else:
            continue

        k = (k or "").strip()
        v = (v or "").strip()
        if not k or not v:
            continue
        try:
            idx = int(k)
        except Exception:
            continue
        if 0 <= idx <= 15:
            out[idx] = v
    return out


_CHANNEL_NAME_BY_INDEX = _parse_channel_name_by_index_env()


def _evt_first(d: dict, keys: list[str]) -> str:
    """
    Busca el primer valor string no vacío en varias “capas” típicas del broker:
      - root
      - summary
      - packet
      - decoded
      - payload
    """
    if not isinstance(d, dict):
        return ""
    layers = [d]
    for k in ("summary", "packet", "decoded", "payload"):
        obj = d.get(k)
        if isinstance(obj, dict):
            layers.append(obj)

    for layer in layers:
        for kk in keys:
            v = layer.get(kk)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _evt_first_int(d: dict, keys: list[str]) -> int | None:
    """Idem _evt_first pero para ints."""
    if not isinstance(d, dict):
        return None
    layers = [d]
    for k in ("summary", "packet", "decoded", "payload"):
        obj = d.get(k)
        if isinstance(obj, dict):
            layers.append(obj)

    for layer in layers:
        for kk in keys:
            v = layer.get(kk)
            if v is None:
                continue
            try:
                return int(v)
            except Exception:
                continue
    return None


def _compute_hops_real_from_event(evt: dict) -> int | None:
    """
    Hops reales (consistente con el bot):
      hops_real = max(0, hop_start - hop_limit)
    """
    hl = _evt_first_int(evt, ["hop_limit", "hopLimit"])
    hs = _evt_first_int(evt, ["hop_start", "hopStart"])
    if hl is None or hs is None:
        return None
    try:
        return max(0, int(hs) - int(hl))
    except Exception:
        return None


def _short(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip()


def _aprsis_push_event_transport(evt: dict) -> str:
    """Detecta si el evento JSONL procede de MeshCore o de Meshtastic."""
    if not isinstance(evt, dict):
        return "meshtastic"
    pkt = evt.get("packet") if isinstance(evt.get("packet"), dict) else {}
    meta = evt.get("meta") if isinstance(evt.get("meta"), dict) else {}
    pkt_meta = pkt.get("meta") if isinstance(pkt.get("meta"), dict) else {}
    for d in (evt, pkt, meta, pkt_meta):
        if not isinstance(d, dict):
            continue
        if d.get("meshcore") or d.get("meshcore_kind") or d.get("meshcore_chan_idx") is not None:
            return "meshcore"
        origin = str(d.get("origin") or "").strip().lower()
        if origin == "meshcore" or origin.startswith("meshcore"):
            return "meshcore"
    from_id = str(pkt.get("fromId") or evt.get("from") or "").strip().lower()
    if from_id.startswith("meshcore"):
        return "meshcore"
    return "meshtastic"


def _aprsis_push_event_channel(evt: dict, fallback_ch: int | None, transport: str | None = None) -> int | None:
    """
    Devuelve el canal que debe usar aprsis_push para filtrar/prefijar.

    En eventos MeshCore, el campo `channel` del JSONL puede ser el canal lógico
    Meshtastic al que se ha ruteado/injectado el mensaje. Para el push APRS-IS
    interesa el canal MeshCore real (`meshcore_chan_idx`/`channel_idx`).
    """
    t = (transport or _aprsis_push_event_transport(evt)).strip().lower()
    if t == "meshcore":
        mc_ch = _evt_first_int(evt, ["meshcore_chan_idx", "channel_idx", "chan_idx"])
        if mc_ch is not None:
            return mc_ch
    return fallback_ch


def _build_aprsis_push_prefix(evt: dict, ch: int) -> str:
    """
    Prefijo compacto para APRS-IS push (si APRSIS_PUSH_PREFIX=1).
    Ejemplo:
      [ch0/ZAR h2 EB2EAS-5]
    """
    # alias del emisor (si el broker lo aporta)
    alias = _evt_first(evt, ["from_alias", "sender", "fromAlias"])
    alias = _short(alias, 10)

    transport = _aprsis_push_event_transport(evt)

    # nombre de canal: en MeshCore priorizamos la etiqueta real del canal
    # MeshCore; `channel_name` puede ser el canal lógico Meshtastic mapeado.
    if transport == "meshcore":
        ch_name_evt = _evt_first(evt, ["meshcore_chan_tag", "meshcore_channel_tag", "meshcore_channel_name"])
    else:
        ch_name_evt = _evt_first(evt, ["channel_name", "channelName"])
    ch_name = ch_name_evt or ("" if transport == "meshcore" else _CHANNEL_NAME_BY_INDEX.get(int(ch), ""))
    ch_name = _short(ch_name, 8)

    # hops reales
    hops_real = _compute_hops_real_from_event(evt)
    hops_txt = f"h{hops_real}" if isinstance(hops_real, int) else ""

    # etiqueta canal
    ch_label = f"mc{ch}" if transport == "meshcore" else f"ch{ch}"
    if ch_name:
        ch_label = f"{ch_label}/{ch_name}"

    parts = [ch_label]
    if hops_txt:
        parts.append(hops_txt)
    if alias:
        parts.append(alias)

    return "[" + " ".join(parts) + "] "



async def task_mesh_channels_to_aprsis():
    """
    Mesh → APRS-IS (push de canales)

    - Lee el stream JSONL del broker (BROKER_HOST:BROKER_PORT).
    - Normaliza distintos formatos de evento del broker:
        A) plano:    {"portnum":"TEXT_MESSAGE_APP","text":"...","channel":2,...}
        B) envuelto: {"packet":{...},"summary":{...},...}   (type puede variar)
        C) mixto:    {"decoded":{...}} o {"payload":{...}} en raíz o en packet
    - Para cada TEXT_MESSAGE_APP normal (no /aprs) y canal autorizado,
      lo envía a APRS-IS como mensaje dirigido a APRSIS_PUSH_TO.
    - No emite por RF (usa APRS-IS directo).
    """
    global _APRSIS_PUSH_LAST_TS

    backoff = 2.0

    # Cache para recalcular canales al vuelo cuando cambie APRSIS_PUSH_CHANNELS_RAW
    last_channels_raw = None
    push_channel_cfg: dict[str, Optional[set[int]]] = {}

    def _as_dict(x):
        return x if isinstance(x, dict) else {}

    def _first_str(*vals) -> str:
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v
        return ""

    def _first_any(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    def _norm_event(obj: dict):
        """
        Devuelve: (port_upper:str, text:str, ch:int|None)
        """
        obj = _as_dict(obj)

        # Posibles contenedores
        summ = _as_dict(obj.get("summary"))

        # Si hay clave "packet", úsala SIEMPRE (no dependas de obj["type"])
        pkt = _as_dict(obj.get("packet")) if isinstance(obj.get("packet"), dict) else obj

        # decoded/payload pueden estar en raíz o en packet
        dec_root = _as_dict(obj.get("decoded"))
        pay_root = _as_dict(obj.get("payload"))
        dec_pkt  = _as_dict(pkt.get("decoded"))
        pay_pkt  = _as_dict(pkt.get("payload"))

        # Portnum
        port = _first_str(
            str(summ.get("portnum") or ""),
            str(dec_pkt.get("portnum") or ""),
            str(dec_root.get("portnum") or ""),
            str(pkt.get("portnum") or ""),
            str(obj.get("portnum") or ""),
        ).upper().strip()

        # Texto (orden de preferencia)
        txt = _first_str(
            summ.get("text"),
            dec_pkt.get("text"),
            pay_pkt.get("text"),
            dec_root.get("text"),
            pay_root.get("text"),
            pkt.get("text"),
            obj.get("text"),
        )

        # Canal (muchas variantes)
        ch_raw = _first_any(
            summ.get("canal"),
            summ.get("channel"),
            pkt.get("channel"),
            pkt.get("channelIndex"),
            _as_dict(pkt.get("meta")).get("channelIndex"),
            dec_pkt.get("channel"),
            dec_pkt.get("channelIndex"),
            dec_root.get("channel"),
            dec_root.get("channelIndex"),
            obj.get("channel"),
            obj.get("channelIndex"),
        )

        try:
            ch = int(ch_raw) if ch_raw is not None else None
        except Exception:
            ch = None

        return port, txt, ch

    while True:
        writer = None
        try:
            print(f"[mesh→IS push] Conectando JSONL {BROKER_HOST}:{BROKER_PORT} …")
            reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
            print("[mesh→IS push] Conectado.")
            backoff = 2.0

            # Log de configuración al conectar (aunque todavía no lleguen líneas del broker)
            cur_raw = (APRSIS_PUSH_CHANNELS_RAW or "all").strip().lower()
            last_channels_raw = cur_raw
            push_channel_cfg = _parse_push_channel_config(cur_raw)
            print(f"[mesh→IS push] cfg channels={cur_raw} -> {push_channel_cfg or 'NONE'}", flush=True)
                

            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("broker closed")

                if not _aprsis_push_is_enabled():
                    continue

                # Refrescar canales en caliente (si el bot cambia channels=all, etc.)
                cur_raw = (APRSIS_PUSH_CHANNELS_RAW or "all").strip().lower()
                if cur_raw != last_channels_raw:
                    last_channels_raw = cur_raw
                    push_channel_cfg = _parse_push_channel_config(cur_raw)
                    try:
                        print(
                            f"[mesh→IS push] cfg channels={cur_raw} -> "
                            f"{push_channel_cfg or 'NONE'}",
                            flush=True
                        )
                    except Exception:
                        pass

                try:
                    obj = json.loads(line.decode("utf-8", "ignore"))
                except Exception:
                    continue

                port, txt, ch_raw = _norm_event(obj)
                transport = _aprsis_push_event_transport(obj)
                ch = _aprsis_push_event_channel(obj, ch_raw, transport)

                if port != "TEXT_MESSAGE_APP":
                    continue

                if not isinstance(txt, str) or not txt.strip():
                    continue

                # Evita reenviar comandos /aprs (eso ya lo gestiona task_broker_to_aprs)
                if txt.lstrip().lower().startswith("/aprs"):
                    continue

                # Anti-eco de entradas APRS (evita bucles)
                tnorm = txt.strip()
                if tnorm.startswith("[APRS eco de") or tnorm.startswith("[APRS-IS eco de"):
                    continue

                if ch is None:
                    continue

                # Filtro de transporte/canal
                allowed_channels = push_channel_cfg.get(transport)
                if transport not in push_channel_cfg:
                    print(f"[mesh→IS push] skip transport={transport} ch={ch} (cfg={push_channel_cfg or 'NONE'})", flush=True)
                    continue
                if allowed_channels is not None and ch not in allowed_channels:
                    print(f"[mesh→IS push] skip transport={transport} ch={ch} (allowed={allowed_channels})", flush=True)
                    continue

                # Rate limit
                now = asyncio.get_running_loop().time()
                if (now - float(_APRSIS_PUSH_LAST_TS or 0.0)) < float(APRSIS_PUSH_MIN_GAP_S):
                    continue

                # Prefijo enriquecido (ch + nombre canal + hops reales + alias) si APRSIS_PUSH_PREFIX=1
                prefix = _build_aprsis_push_prefix(obj, ch) if APRSIS_PUSH_PREFIX else ""
                push_text = prefix + tnorm
                lines = _aprsis_tnc2_message_lines(APRSIS_PUSH_TO, push_text, with_msgid=True)

                if not lines:
                    continue

                ok = await _aprsis_send_lines_safe(lines)

                if ok:
                    _APRSIS_PUSH_LAST_TS = now
                    print(f"[mesh→IS push] → {APRSIS_PUSH_TO} {transport} parts={len(lines)} {push_text[:80]}")
                else:
                    print(f"[mesh→IS push] ❌ TX FAIL -> {APRSIS_PUSH_TO} transport={transport} ch={ch} parts={len(lines)}")

        except Exception as e:
            print(f"[mesh→IS push] ❌ {type(e).__name__}: {e}")
            try:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.6)


# =========================
# === main =================
# =========================
async def main():
    # Tareas existentes
    tasks = [
        asyncio.create_task(task_broker_to_aprs()),             # Mesh → APRS
        asyncio.create_task(task_aprs_to_meshtastic()),         # APRS RF → Mesh
        asyncio.create_task(task_control_udp()),                # Bot(/aprs) → APRS
        asyncio.create_task(task_aprsis_connect_on_startup()),  # Conexión inicial APRS-IS (uplink)
    ]

    # Recepción APRS-IS → Mesh (downlink)
    if _aprsis_ready():
        print("[aprs←IS] Recepción APRS-IS habilitada (downlink).")
        tasks.append(asyncio.create_task(task_aprsis_to_meshtastic()))
    else:
        print("[aprs←IS] Downlink deshabilitado (faltan credenciales APRSIS_USER/PASSCODE).")

    # Mirror Mesh -> APRS-IS (aprsis_push)
    tasks.append(asyncio.create_task(task_mesh_channels_to_aprsis()))

    # Keepalive uplink APRS-IS
    tasks.append(asyncio.create_task(task_aprsis_uplink_keepalive()))

    await asyncio.gather(*tasks)



if __name__ == "__main__":
    try:
        _apply_cli_overrides()  # <<< NUEVO

        if _aprsis_ready():
            print(f"[aprs→IS] HABILITADO: user={APRSIS_USER} host={APRSIS_HOST}:{APRSIS_PORT} filtro='{APRSIS_FILTER or '-'}'.")
            print("           Se subirán SOLO POSICIONES con etiqueta [CHx] / [CANAL x].")
            print("[aprs←IS] Activado: se recibirán tramas desde APRS-IS y se pasarán a Mesh.")
        else:
            print("[aprs→IS] Deshabilitado (sin credenciales APRSIS_USER + APRSIS_PASSCODE).")
            print("[aprs←IS] Downlink deshabilitado.")

        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bye")
