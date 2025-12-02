#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram_Bot_Broker_v6.1.3 py
-----------------------------
Bot de Telegram integrado con Meshtastic y un Broker TCP opcional.
Conexión preferente a Meshtastic_Relay_API si está disponible; si no, fallback a la CLI 'meshtastic'.

Novedades v4.5:
- /ver_nodos [N|false]: si pasas 'false' no imprime métricas (RSSI/SNR/ruta) y muestra la lista clásica (más ágil).
- Ver nodos enriquecido por defecto: añade RSSI/SNR, ruta y calidad del enlace (🟢🟠🔴) combinando API + broker.
- /enviar y /enviar_ack diferenciados (broadcast vs unicast con ACK), usando TCPInterfacePool persistente.
- Detección de ACK combinada (librería + broker ROUTING_APP) para reducir duplicados.
- Menú contextual oficial de Telegram (SetMyCommands) con opciones distintas para admin y usuario.

Variables de entorno relevantes:
  TELEGRAM_TOKEN, ADMIN_IDS,
  MESHTASTIC_HOST, MESHTASTIC_EXE,
  BROKER_HOST, BROKER_PORT, BROKER_CHANNEL,
  MESHTASTIC_TIMEOUT, TRACEROUTE_TIMEOUT, TELEMETRY_TIMEOUT,
  SEND_LISTEN_SEC, TRACEROUTE_CHECK_BEFORE_SEND,
  ACK_MAX_ATTEMPTS, ACK_WAIT_SEC, ACK_BACKOFF
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shlex
import socket
import sys
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging
from datetime import datetime, timedelta, UTC
import broker_task as broker_tasks

from meshtastic import tcp_interface
from positions_store import read_positions_recent, build_kml, build_gpx

from auditoria_red import auditoria_red_cmd, auditoria_integral_cmd

# === [NUEVO] Helper para compatibilizar funciones sync/async ===
import inspect
from html import escape

from coverage_backlog import build_coverage_from_backlog, build_coverage_combined # NUEVO/ACTUALIZADO v1.1

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

async def maybe_await(obj):
    """
    Si 'obj' es awaitable (corutina, Task, Future), se hace await y se devuelve el resultado.
    Si no, se devuelve tal cual. Evita errores tipo: 'object str can't be used in await expression'.
    """
    if inspect.isawaitable(obj):
        return await obj
    return obj


# --- Telegram PTB v20+ ---
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    ReplyKeyboardRemove,
    ForceReply,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)

# --- Import utilidades del Relay (si están) ---
from Meshtastic_Relay_API import (
    _parse_nodes_table,
    parse_minutes,
    _to_int_safe,
    cargar_aliases_desde_nodes,
    get_visible_nodes_with_hops,
    cargar_aliases_desde_nodes,
)


from meshtastic_api_adapter import (
    api_list_nodes,
    api_traceroute,
    api_request_telemetry,
    api_send_text,
    send_text_simple_with_retry,   # <- usado por send_text_message
    api_get_neighbors_via_pool,             # <- métricas de vecinos API
    DEFAULT_PORT_HOST,
)

from tcpinterface_persistent import TCPInterfacePool

# --- Compat shim para Meshtastic TCPInterface (host -> hostname) ---
try:
    import meshtastic.tcp_interface as _tcp_mod
    _TCP_orig = _tcp_mod.TCPInterface

    def _TCPInterface_Compat(*args, **kwargs):
        if "host" in kwargs and "hostname" not in kwargs:
            kwargs["hostname"] = kwargs.pop("host")
        return _TCP_orig(*args, **kwargs)

    _tcp_mod.TCPInterface = _TCPInterface_Compat
except Exception as _e:
    print(f"[shim TCPInterface] Aviso: {_e}")


# -------------------------
# CONFIGURACIÓN Y CONSTANTES
# -------------------------

# === Bandera global: NO abrir sockets desde el bot (solo broker/CLI cuando toque) ===
_TRUTHY = {"1", "true", "t", "yes", "y", "on"}
DISABLE_BOT_TCP = str(os.getenv("DISABLE_BOT_TCP", "0")).lower() in _TRUTHY  # por defecto ACTIVADO

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "bot_data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


LOG_FILE           = DATA_DIR / "bot.log"
STATS_FILE         = DATA_DIR / "stats.json"
NODES_FILE         = DATA_DIR / "nodos.txt"
SEND_LOG_CSV       = DATA_DIR / "sent_log.csv"
SEND_ACK_LOG_CSV   = DATA_DIR / "sent_ack_log.csv"

# Carpeta y fichero donde guardaremos el backlog offline

OFFLINE_LOG_PATH = os.path.join(DATA_DIR, "broker_offline_log.jsonl")

TOKEN              = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")
    if x.strip().isdigit()
}
MESHTASTIC_HOST    = os.getenv("MESHTASTIC_HOST", "").strip()
MESHTASTIC_EXE     = os.getenv("MESHTASTIC_EXE", "meshtastic").strip()
BROKER_HOST        = os.getenv("BROKER_HOST", "127.0.0.1").strip()
BROKER_PORT        = int(os.getenv("BROKER_PORT", "8765"))
# ===== [NUEVO] Constante BACKLOG_PORT (si no existía) =====
try:
    BACKLOG_PORT
except NameError:
    try:
        BACKLOG_PORT = int(BROKER_PORT) + 1
    except Exception:
        BACKLOG_PORT = 8766  # fallback por si acaso

BROKER_CHANNEL     = int(os.getenv("BROKER_CHANNEL", "0"))

# Tiempos por defecto
TIMEOUT_CMD_S      = int(os.getenv("MESHTASTIC_TIMEOUT", "25"))
TRACEROUTE_TIMEOUT = int(os.getenv("TRACEROUTE_TIMEOUT", "35"))
TELEMETRY_TIMEOUT  = int(os.getenv("TELEMETRY_TIMEOUT", "30"))
SEND_LISTEN_SEC    = int(os.getenv("SEND_LISTEN_SEC", "10"))
TRACEROUTE_CHECK   = os.getenv("TRACEROUTE_CHECK_BEFORE_SEND", "1") == "1"
# Ventana corta de escucha para respuestas de TELEMETRY_APP
# Ventanas de escucha para TELEMETRY_APP
TELEMETRY_LISTEN_SEC = int(os.getenv("TELEMETRY_LISTEN_SEC", "25"))
TELEMETRY_LISTEN_FALLBACK_SEC = int(os.getenv("TELEMETRY_LISTEN_FALLBACK_SEC", "20"))


# ACK (nivel aplicación)
ACK_MAX_ATTEMPTS   = int(os.getenv("ACK_MAX_ATTEMPTS", "3"))
ACK_WAIT_SEC       = int(os.getenv("ACK_WAIT_SEC", "15"))
ACK_BACKOFF        = float(os.getenv("ACK_BACKOFF", "1.7"))
BROADCAST_REQUEST_ACK=1

# Mensajes largos -> se trocean para Telegram
TELEGRAM_MAX_CHARS = 3900

# Estados ConversationHandler (para /enviar)
ASK_SEND_DEST, ASK_SEND_TEXT = range(2)

# Ventana de escucha para métricas rápidas del broker en /ver_nodos enriquecido
METRICS_LISTEN_SEC = float(os.getenv("METRICS_LISTEN_SEC", "5.0"))

# === NUEVO: bandera global para forzar modo API-only en /ver_nodos ===
_TRUTHY = {"1","true","t","yes","y","on"}
NODES_FORCE_API_ONLY = str(os.getenv("NODES_FORCE_API_ONLY","0")).lower() in _TRUTHY

# ── Guardas del job de notificación ───────────────────────────────────────
_NOTIFY_JOB_STARTED = False
try:
    import asyncio
    _NOTIFY_JOB_LOCK = asyncio.Lock()
except Exception:
    _NOTIFY_JOB_LOCK = None  # fallback si algo raro
# ──────────────────────────────────────────────────────────────────────────

DEBUG_KM = (os.getenv("DEBUG_KM", "0").strip().lower() in {"1","true","t","yes","y","on","si","sí"})

# === [NUEVO] Helpers: pausar/reanudar IO + CLI segura con timeout + escritura nodos.txt ===
import os, json, time, signal, subprocess
from typing import List, Tuple

# Ruta nodos.txt (reutiliza si ya la tienes)
try:
    NODES_FILE  # noqa
except NameError:
    BOT_DATA_DIR = os.path.join(os.path.dirname(__file__), "bot_data")
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
    NODES_FILE = os.path.join(BOT_DATA_DIR, "nodos.txt")

# === Helpers de pausa/reanudación del broker + pool + escucha ===
import os, json, time, signal, subprocess, asyncio
from typing import List, Tuple

# === NUEVO: cliente de control del broker vía BacklogServer (127.0.0.1:8766) ===
import socket, json, time
from contextlib import contextmanager

# Usa el mismo origen y reglas que arriba
import os  # asegurarte de tener 'os' importado arriba

BROKER_CTRL_HOST = os.getenv("BROKER_CTRL_HOST", os.getenv("BROKER_HOST", "127.0.0.1")).strip()
try:
    BROKER_CTRL_PORT = int(os.getenv("BROKER_CTRL_PORT", str(int(os.getenv("BROKER_PORT", "8765")) + 1)))
except Exception:
    BROKER_CTRL_PORT = 8766

# --- NUEVO: imports usados por los helpers de pausa/CLI ---
import os, sys, time, json, socket, subprocess, shlex, contextlib

# Si ya tienes MESHTASTIC_HOST/NODES_FILE definidos, se respetan; si no, los define:
if "MESHTASTIC_HOST" not in globals():
    MESHTASTIC_HOST = os.getenv("MESH_NODE_HOST", "")

if "NODES_FILE" not in globals():
    _bot_dir = os.path.dirname(os.path.abspath(__file__))
    NODES_FILE = os.path.join(_bot_dir, "bot_data", "nodos.txt")

# === NUEVO: helpers de parsing para /programar ===
import re
from datetime import datetime
from zoneinfo import ZoneInfo

TZ_EUROPE_MADRID = ZoneInfo("Europe/Madrid")

from telegram import Update

from telegram.ext import (
     ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
 )


try:
    from meshtastic_api_adapter import (
        send_text_simple_with_retry_resilient as _send_resilient,
        build_nodes_mapping_via_pool
    )
except Exception:
    from meshtastic_api_adapter import send_text_simple_with_retry as _send_resilient  # fallback
    # Si no se pudo importar build_nodes_mapping_via_pool, definimos un stub
    def build_nodes_mapping_via_pool(*args, **kwargs) -> Dict[str, str]:
        return {}

# --- NUEVO: comando /reconectar (solo admin) ---
from telegram.ext import CommandHandler

# --- Enviar vía cola del broker (BacklogServer 127.0.0.1:8766) ---
import socket, json, time, os
from contextlib import contextmanager

# --- Necesario para cálculo de distancias en TODAS las funciones ---
import math

# ========= Helpers de ubicación compartidos (DISTANCIA + PROVINCIA/CIUDAD) =========

from html import escape

def _safe_float(v):
    """Convierte '41,7386° N' → 41.7386 (float) o None si falla."""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(",", ".")
        s = "".join(ch for ch in s if ch in "+-0123456789.")
        if s in ("", "+", "-"):
            return None
        return float(s)
    except Exception:
        return None

def _calc_distance_km(lat1, lon1, lat2, lon2):
    """Haversine en km. Redondea a 0.1 km. Devuelve None si falla."""
    try:
        R = 6371.0
        φ1 = math.radians(float(lat1))
        φ2 = math.radians(float(lat2))
        dφ = math.radians(float(lat2) - float(lat1))
        dλ = math.radians(float(lon2) - float(lon1))
        a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(R * c, 1)
    except Exception:
        return None

def _get_province_offline(lat, lon):
    """
    reverse_geocoder offline:
    - prioriza city/name
    - si no hay, admin2 (provincia); si no, admin1 (CCAA)
    """
    try:
        import reverse_geocoder as rg
    except Exception:
        return None
    try:
        res = rg.search((float(lat), float(lon)))
        if isinstance(res, list) and res:
            r = res[0]
            return r.get("name") or r.get("admin2") or r.get("admin1")
    except Exception:
        return None
    return None

def _norm_id(nid: str | None) -> str | None:
    """Normaliza a '!XXXXXXXX'. Acepta '!id', 'id', enteros…"""
    if not nid:
        return None
    s = str(nid).strip()
    if s.startswith("!"):
        return s
    return f"!{s[-8:]}" if len(s) >= 8 else f"!{s}"

def _build_last_positions_map(lookback_minutes: int = 72*60) -> dict[str, tuple[float,float]]:
    """
    Mapa de última posición por nodo: {'!id': (lat, lon)}.
    Fuente 1: BACKLOG (POSITION_APP/TELEMETRY_APP).
    Fuente 2: nodes.txt (Latitude/Longitude o latitudeI/longitudeI).
    """
    posmap: dict[str, tuple[float,float]] = {}

    # 1) Backlog del broker (si existe el RPC)
    try:
        since_ts = int(time.time()) - lookback_minutes*60
        bl = _broker_ctrl("FETCH_BACKLOG",
                          {"since_ts": since_ts,
                           "portnums": ["POSITION_APP", "TELEMETRY_APP"]},
                          timeout=6.0)
        if bl and bl.get("ok"):
            for ev in (bl.get("data") or []):
                nid = _norm_id(ev.get("from") or ev.get("fromId") or ev.get("nodeId"))
                if not nid:
                    continue
                lat = lon = None
                p = ev.get("position") or (ev.get("decoded") or {}).get("position") or {}
                if p:
                    if "latitudeI" in p and "longitudeI" in p:
                        try:
                            lat = float(p["latitudeI"]) / 1e7
                            lon = float(p["longitudeI"]) / 1e7
                        except Exception:
                            lat = lon = None
                    if lat is None and "latitude" in p and "longitude" in p:
                        lat = _safe_float(p.get("latitude"))
                        lon = _safe_float(p.get("longitude"))
                if lat is None or lon is None:
                    t = (ev.get("decoded") or {}).get("telemetry") or {}
                    lat = _safe_float(t.get("lat") or t.get("latitude"))
                    lon = _safe_float(t.get("lon") or t.get("longitude"))
                if lat is not None and lon is not None:
                    posmap[nid] = (lat, lon)  # última gana
    except Exception:
        pass

    # 2) nodes.txt (si existe el parser del relay)
    try:
        rows_file = _parse_nodes_table(NODES_FILE) or []  # ya lo tienes en tu código
        for rf in rows_file:
            nid = _norm_id(rf.get("id") or rf.get("nodeId") or rf.get("fromId"))
            if not nid:
                continue
            lat = rf.get("Latitude") or rf.get("lat") or rf.get("latitude")
            lon = rf.get("Longitude") or rf.get("lon") or rf.get("longitude")
            if (lat is None or lon is None) and (rf.get("latitudeI") is not None):
                try:
                    lat = float(rf["latitudeI"]) / 1e7
                    lon = float(rf.get("longitudeI") or 0.0) / 1e7
                except Exception:
                    lat = lon = None
            lat_f = _safe_float(lat); lon_f = _safe_float(lon)
            if lat_f is not None and lon_f is not None:
                # mantén la prioridad al backlog; solo añade si no existía
                posmap.setdefault(nid, (lat_f, lon_f))
    except Exception:
        pass

    return posmap


from typing import Any, Dict, Optional, Tuple
from telegram.ext import ContextTypes

def _get_home_coords(
    context: ContextTypes.DEFAULT_TYPE,
    posmap: Optional[Dict[str, Dict[str, Any]]] = None,
    lastmap: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """
    HOME por prioridad:
      1) .env HOME_LAT/HOME_LON  (si existen, SIEMPRE se usan)
      2) Cache en context.bot_data["home_lat"/"home_lon"]
      3) .env HOME_NODE_ID si su posición está en posmap
      4) Última posición conocida según lastmap+posmap
      5) Cualquier posición disponible en posmap
    """

    import os
    from dotenv import load_dotenv

    def _sf(v):
        try:
            s = str(v).strip().lower().replace(",", ".")
            s = "".join(ch for ch in s if ch in "+-0123456789.")
            return float(s) if s not in ("", "+", "-") else None
        except Exception:
            return None

    # Asegurarnos de que el .env de /app se ha leído (no pisa variables ya existentes)
    try:
        load_dotenv(dotenv_path="/app/.env", override=False)
    except Exception:
        pass

    debug_km = str(os.getenv("DEBUG_KM", "0")).lower() in ("1", "true", "yes", "on")

    # === 1) PRIORIDAD ABSOLUTA: HOME_LAT / HOME_LON del .env ==================
    la_env = _sf(os.getenv("HOME_LAT"))
    lo_env = _sf(os.getenv("HOME_LON"))
    if la_env is not None and lo_env is not None:
        context.bot_data["home_lat"] = la_env
        context.bot_data["home_lon"] = lo_env
        if debug_km:
            print(f"[KM][HOME] from .env HOME_LAT/HOME_LON → ({la_env}, {lo_env})", flush=True)
        return la_env, lo_env

    # === 2) Cache previa en bot_data ==========================================
    la_bd = context.bot_data.get("home_lat")
    lo_bd = context.bot_data.get("home_lon")
    if isinstance(la_bd, (int, float)) and isinstance(lo_bd, (int, float)):
        if debug_km:
            print(f"[KM][HOME] from bot_data cache → ({la_bd}, {lo_bd})", flush=True)
        return float(la_bd), float(lo_bd)

    # === 3) Intentar HOME_NODE_ID si tiene posición en posmap =================
    posmap = posmap or {}
    home_node_id = (os.getenv("HOME_NODE_ID") or "").strip()
    if home_node_id:
        entry = posmap.get(home_node_id)
        if isinstance(entry, dict):
            la = _sf(entry.get("lat"))
            lo = _sf(entry.get("lon"))
            if la is not None and lo is not None:
                context.bot_data["home_lat"] = la
                context.bot_data["home_lon"] = lo
                if debug_km:
                    print(f"[KM][HOME] from HOME_NODE_ID {home_node_id} → ({la}, {lo})", flush=True)
                return la, lo

    # === 4) Última posición conocida (lastmap + posmap) =======================
    lastmap = lastmap or {}

    def _iter_maps_for_home():
        # primero lastmap (más reciente), luego posmap
        for nid, e in lastmap.items():
            yield nid, e
        for nid, e in posmap.items():
            yield nid, e

    for nid, entry in _iter_maps_for_home():
        if not isinstance(entry, dict):
            continue
        la = _sf(entry.get("lat"))
        lo = _sf(entry.get("lon"))
        if la is not None and lo is not None:
            context.bot_data["home_lat"] = la
            context.bot_data["home_lon"] = lo
            if debug_km:
                print(f"[KM][HOME] from maps nid={nid} → ({la}, {lo})", flush=True)
            return la, lo

    # === 5) Sin coordenadas disponibles =======================================
    if debug_km:
        print("[KM][HOME] sin coordenadas HOME disponibles", flush=True)
    return None, None

def _snr_quality_label(snr) -> str:
    """
    Clasifica la calidad del enlace según el SNR y devuelve texto + icono.

      Muy fuerte:      +5 a +20 dB
      Fuerte:          0 a +5 dB
      Óptimo:          0 a –10 dB
      Utilizable:      –10 a –15 dB
      Crítico:         –15 a –20 dB
      Casi perdido:    < –20 dB
    """
    if snr is None:
        return "desconocida ⚪"

    try:
        s = float(snr)
    except Exception:
        return "desconocida ⚪"

    # Rangos con iconos redondos
    if s >= 5:
        return "muy fuerte 🟢"
    elif 0 <= s < 5:
        return "fuerte 🟢"
    elif -10 <= s < 0:
        return "óptimo 🟡"
    elif -15 <= s < -10:
        return "utilizable 🟠"
    elif -20 <= s < -15:
        return "crítico 🔴"
    else:
        return "casi perdido ⚫"


# ===================== Fin helpers ubicación =====================



def _send_via_broker_queue(text: str, ch: int, dest: str | None = None, ack: bool = False, timeout: float = 3.0) -> dict:
    """
    Envía un texto al broker para que lo transmita usando su TCP activa y dispare el espejo A→B.
    - text: mensaje
    - ch: canal lógico Meshtastic
    - dest: None/'broadcast' para broadcast o '!ID' para unicast
    - ack: True sólo si 'dest' es un '!ID' (unicast con ACK)
    Devuelve: dict con {"ok": bool, ...}
    """
    payload = {
        "cmd": "SEND_TEXT",
        "params": {
            "text": str(text),
            "ch": int(ch),
            "dest": (None if not dest or str(dest).lower() == "broadcast" else str(dest)),
            "ack": bool(ack)
        }
    }
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection((BROKER_CTRL_HOST or "127.0.0.1", int(BROKER_CTRL_PORT)), timeout=timeout) as s:
            s.sendall(data)
            s.settimeout(2.0)
            try:
                resp = s.recv(65535)
                if resp:
                    return json.loads(resp.decode("utf-8", "ignore"))
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": f"broker_queue_error: {type(e).__name__}: {e}"}
    return {"ok": True, "queued": True, "path": "broker-queue"}



# --- /reconectar (admin) → fuerza reset limpio y confirma conexión ---
#Baja 04-11-2025
async def reconectar_cmd_old(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Si ya tienes otra verificación de admins, usa la tuya:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Solo administradores.")
        return

    await update.effective_message.reply_text("🔄 Reseteando broker y reintentando conexión…")

    # 1) Reset limpio en el broker
    r = _broker_ctrl("FORCE_RECONNECT", None, timeout=6.0)
    if not (r and r.get("ok")):
        await update.effective_message.reply_text(
            f"❌ No se pudo forzar el reset: {(r or {}).get('error') or 'sin respuesta'}"
        )
        return

    # 2) Espera activa hasta ver running + connected (máx. ~25s)
    import time, asyncio
    t0 = time.time()
    last = {}
    while time.time() - t0 < 25.0:
        st = _broker_ctrl("BROKER_STATUS", None, timeout=3.0) or {}
        last = st
        if st.get("ok") and st.get("status") == "running" and bool(st.get("connected")):
            await update.effective_message.reply_text("✅ Broker reseteado y **conectado** al nodo.")
            return
        await asyncio.sleep(1.2)

    await update.effective_message.reply_text(
        f"⚠️ Reset enviado, pero **no conecta** al nodo.\n"
        f"Estado: {last.get('status') or '¿?'} • cooldown={last.get('cooldown_remaining')}s • connected={bool(last.get('connected'))}\n"
        f"Revisa que 192.168.1.201:4403 esté accesible y sin otra sesión ocupándolo."
    )

# --- /reconectar (admin) → fuerza reset limpio y confirma conexión ---
# Alta 04-11-2025
async def reconectar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Reinicia la conexión persistente del broker y espera a que marque 'connected=True'.
    - Uso: /reconectar [segundos_espera]
      Si no se indica, espera 35 s por defecto.
    Muestra siempre el host:puerto REAL que reporta el broker en BROKER_STATUS.
    """
    # Autorización básica
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Solo administradores.")
        return

    # Parseo de ventana de espera opcional
    max_wait = 35.0
    try:
        if context.args:
            v = float(context.args[0])
            if v > 0:
                max_wait = min(90.0, v)  # cap sensato
    except Exception:
        pass

    await update.effective_message.reply_text("🔄 Reseteando broker y reintentando conexión…")

    # 1) Reset limpio en el broker
    r = _broker_ctrl("FORCE_RECONNECT", None, timeout=6.0)
    if not (r and r.get("ok")):
        await update.effective_message.reply_text(
            f"❌ No se pudo forzar el reset: {(r or {}).get('error') or 'sin respuesta'}"
        )
        return

    # 2) Espera activa hasta ver running + connected (máx. configurable)
    import time, asyncio
    t0 = time.time()
    last = {}
    while (time.time() - t0) < max_wait:
        st = _broker_ctrl("BROKER_STATUS", None, timeout=3.0) or {}
        last = st

        # Campos extra para mensaje (si el broker los expone)
        node_host = st.get("node_host") or "¿host?"
        node_port = st.get("node_port") or "¿puerto?"

        # Estados
        ok = bool(st.get("ok"))
        status = (st.get("status") or "").lower()
        connected = bool(st.get("connected"))
        cd = int(st.get("cooldown_remaining") or 0)

        # Si estaba en cooldown, dejamos respirar 1 segundo extra tras bajar a 0
        if ok and status == "running" and connected:
            await update.effective_message.reply_text(
                f"✅ Broker reseteado y **conectado** al nodo ({node_host}:{node_port})."
            )
            return

        # Pequeño sleep entre polls
        await asyncio.sleep(1.2)

    # 3) Timeout: informar con datos reales (sin IP fija)
    node_host = last.get("node_host") or "¿host?"
    node_port = last.get("node_port") or "¿puerto?"
    cd = int(last.get("cooldown_remaining") or 0)
    await update.effective_message.reply_text(
        "⚠️ Reset enviado, pero **no conecta** al nodo en el tiempo de espera.\n"
        f"Estado: {last.get('status') or '¿?'} • cooldown={cd}s • connected={bool(last.get('connected'))}\n"
        f"Revisa que **{node_host}:{node_port}** esté accesible y sin otra sesión ocupándolo."
    )


# === [NUEVO] Vecinos vía CLI con pausa breve del broker ===
# === [NUEVO] Vecinos vía CLI con pausa breve del broker ===
def _neighbors_via_cli(max_hops: int = 1, limit: int = 20) -> list[tuple[str, str, int | None]]:
    """
    Devuelve lista de vecinos como [(id, alias, hops_int|None)], filtrando hops <= max_hops.
    Flujo:
      - Pausa suave del broker (with_broker_paused)
      - Ejecuta CLI 'meshtastic --host ... --nodes' con reintentos
      - Parsea salida → id/alias/hops
      - Filtra por hops
      - Reanuda broker al salir del 'with'
    No lanza excepciones (devuelve [] si algo falla).
    """
    try:
        # Pausa para no competir con la TCP persistente
        with with_broker_paused(max_wait_s=8.0):
            ok, raw_lines, reason = _run_cli_nodes_with_retry(
                host=MESHTASTIC_HOST,
                attempts=2,
                first_timeout=18,
                backoff_sec=2
            )
        if not ok or not raw_lines:
            return []

        # Normaliza a líneas tabuladas
        norm = _parse_nodes_cli_to_lines("\n".join(raw_lines))
        out: list[tuple[str, str, int | None]] = []
        import re as _re
        for ln in norm:
            # Formato típico (tras parser): "<id>\t<alias>\t<mins>\t<hops_txt>"
            parts = [p.strip() for p in ln.split("\t")]
            if len(parts) < 2:
                # Fallback: si solo hay id, usa alias=id, hops desconocido
                token = (parts[0] if parts else "")
                if token:
                    out.append((token, token, None))
                continue

            nid, alias = parts[0], parts[1] or parts[0]

            # hops puede venir como "2 hops" o "?" → extrae número si existe
            hops_int = None
            if len(parts) >= 4 and parts[3]:
                m = _re.search(r"(\d+)", parts[3])
                if m:
                    try:
                        hops_int = int(m.group(1))
                    except Exception:
                        hops_int = None

            # Filtro por hops
            if hops_int is not None:
                if hops_int <= max_hops:
                    out.append((nid, alias, hops_int))
            else:
                # Si no pudimos leer hops, admite como "desconocido" solo si max_hops >= 1
                if max_hops >= 1:
                    out.append((nid, alias, None))

        # Orden: 0 hops primero, luego 1 hop, luego unknown; y por alias
        def _key(row):
            h = row[2]
            return (0 if h == 0 else (1 if h == 1 else 2), row[1].lower())
        out.sort(key=_key)
        return out[:max(1, int(limit))]
    except Exception:
        return []





# === [NUEVO] Respuesta segura a Telegram con reintentos (cubre httpx.ConnectError/DNS) ===
import asyncio
import logging

# --- Helpers de normalización/particionado (se usan si están disponibles) ---
try:
    # Si agregaste los helpers en meshtastic_api_adapter
    from meshtastic_api_adapter import _normalize_text_for_mesh as _norm_mesh
    from meshtastic_api_adapter import split_text_for_meshtastic as _split_mesh
except Exception:
    try:
        # O si decides usarlos desde broker_task(s)
        from broker_task import _normalize_text_for_mesh as _norm_mesh  # singular
        from broker_task import split_text_for_meshtastic as _split_mesh
    except Exception:
        # Fallback local mínimo (no rompe)
        import re
        def _norm_mesh(s: str) -> str:
            rep = {'“':'"', '”':'"', '’':"'", '‘':"'", '—':'-', '–':'-', '…':'...', '\u00A0':' '}
            s = s.translate(str.maketrans(rep))
            return re.sub(r'\s+', ' ', s).strip()
        def _split_mesh(text: str, max_bytes: int = 180):
            # Split muy simple por palabras para estimar partes (el broker hace el split bueno)
            parts, cur = [], ""
            for w in text.split():
                cand = (cur + " " + w).strip()
                if len(cand.encode("utf-8")) > max_bytes:
                    if cur:
                        parts.append(cur)
                    cur = w
                else:
                    cur = cand
            if cur:
                parts.append(cur)
            return parts


# === Helper FINAL: ejecutar CLI con exclusividad del broker (compat 2/4 args) ===
# === Helper FINAL: ejecutar CLI con exclusividad del broker (compat 2/4 args) ===
def run_cli_exclusive(cmd: list[str], timeout_s: float) -> tuple[int, str, str, bool]:
    """
    Ejecuta un comando CLI con timeout. Devuelve (rc, stdout, stderr, was_timeout).
    No hace pausas ni reanuda: eso lo hace el caller en el loop principal.
    """
    import subprocess

    def _ensure_str(x) -> str:
        if isinstance(x, bytes):
            try:
                return x.decode("utf-8", "ignore")
            except Exception:
                return x.decode(errors="ignore")
        return x if isinstance(x, str) else (str(x) if x is not None else "")

    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,                # intentamos que ya venga como str
            timeout=float(timeout_s),
            check=False,
            shell=False
        )
        out = _ensure_str(p.stdout)
        err = _ensure_str(p.stderr)
        return (p.returncode, out or "", err or "", False)

    except subprocess.TimeoutExpired as ex:
        # En algunos entornos ex.stdout/err pueden venir como bytes: normaliza.
        out = _ensure_str(getattr(ex, "stdout", ""))
        err = _ensure_str(getattr(ex, "stderr", ""))
        return (124, out or "", err or "", True)

# === [NUEVO] Utilidades de logging enriquecido para errores de red ===

TELEGRAM_BROKER_VERBOSE = bool(int(os.getenv("TELEGRAM_BROKER_VERBOSE", "0")))

_WINERR_EXPLAIN = {
    64:   "El nombre de red especificado ya no está disponible (socket cortado por el peer / SMB-like).",
    1225: "El equipo remoto rechazó la conexión (servicio no aceptando, firewall o cooldown activo).",
    10053:"Conexión abortada por el software en su equipo (corte local / timeout).",
    10054:"Conexión restablecida por el host remoto (corte duro desde el otro extremo).",
}

def _explain_winerror(e: BaseException) -> str:
    try:
        code = getattr(e, "winerror", None) or getattr(e, "errno", None)
        if code in _WINERR_EXPLAIN:
            return f"[WinError {code}] {_WINERR_EXPLAIN[code]}"
        return f"{type(e).__name__}: {e}"
    except Exception:
        return f"{type(e).__name__}: {e}"

def _ts() -> str:
    return datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

def _print(msg: str, force: bool = False):
    if TELEGRAM_BROKER_VERBOSE or force:
        print(msg, flush=True)

def _query_broker_status(host: str = "127.0.0.1", port: int = 8766, timeout: float = 3.0):
    """
    Consulta el BacklogServer (BROKER_STATUS) para informar cooldown/pausa.
    NO lanza excepciones hacia fuera; devuelve dict o None.
    """
    try:
        req = {"cmd": "BROKER_STATUS"}
        line = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(line)
            s.settimeout(timeout)
            data = s.recv(4096)
        resp = json.loads(data.decode("utf-8", "ignore"))
        return resp if isinstance(resp, dict) else None
    except Exception:
        return None

def _print_broker_status(backlog_host="127.0.0.1", backlog_port=8766):
    st = _query_broker_status(backlog_host, backlog_port)
    if not st:
        _print(f"{_ts()} ℹ️  Estado del broker: (no disponible)", force=True)
        return
    status = st.get("status")
    cdrem = st.get("cooldown_remaining")
    _print(f"{_ts()} ℹ️  Estado del broker → status={status}, cooldown_remaining={cdrem}s", force=True)

import pathlib

NOTIFIED_FILE = os.path.join(os.path.dirname(__file__), "bot_data", "notified_done.ids")
TASKS_FILE    = os.path.join(os.path.dirname(__file__), "bot_data", "scheduled_tasks.jsonl")

# === [AÑADIR] Estado runtime y persistente de notificaciones ===

SETTINGS_FILE = os.path.join(DATA_DIR, "bot_settings.json")

def _load_bot_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_bot_settings(d: dict) -> None:
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        import logging
        logging.error(f"[settings] fallo al guardar {SETTINGS_FILE}: {type(e).__name__}: {e}")

# Bandera inicial desde .env (1=on, 0=off)
_NOTIFY_ENV = os.getenv("NOTIFY_DONE", "1")
# valor por defecto

NOTIFY_DONE_ENABLED = bool(int(_NOTIFY_ENV))  
# Sobrescribir con valor persistente si existe
_settings = _load_bot_settings()
if "notify_done_enabled" in _settings:
    NOTIFY_DONE_ENABLED = bool(_settings.get("notify_done_enabled"))

# --- Anti-doble notificación ---
_LAST_SENT_IDS: dict[str, float] = {}  # task_id -> monotonic() cuando se avisó
_LAST_SENT_TTL_SEC = float(os.getenv("NOTIFY_DONE_TTL", "180"))

# — Toggle para activar/desactivar las notificaciones de “tarea ejecutada”
#    (NOTIFY_DONE=0 en .env las apaga)
NOTIFY_DONE_ENABLED = str(os.getenv("NOTIFY_DONE", "1")).strip().lower() not in ("0","false","no","off")

# — Permitir configurar el TTL antirrebote por entorno (por defecto ya era 180.0 s)
try:
    _LAST_SENT_TTL_SEC = float(os.getenv("NOTIFY_DONE_TTL", str(_LAST_SENT_TTL_SEC)))
except Exception:
    pass



# === BEGIN notify_done: persistencia por task_id → last_run_ts ===

def _load_notified_map() -> dict[str, float]:
    """
    Devuelve {task_id: last_run_ts_notificado}.
    Soporta el formato antiguo (solo task_id por línea) interpretándolo como ts=0.0
    y el nuevo TSV: 'task_id\\tlast_run_ts'.
    """
    m: dict[str, float] = {}
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if "\t" in line:
                    tid, ts = line.split("\t", 1)
                    try:
                        m[tid] = float(ts)
                    except Exception:
                        m[tid] = 0.0
                else:
                    # compat viejo: solo id
                    m[line] = 0.0
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return m

def _save_notified_map(d: dict):
    try:
        os.makedirs(os.path.dirname(NOTIFIED_FILE), exist_ok=True)
        tmp = NOTIFIED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for k, v in d.items():
                f.write(f"{k}\t{v}\n")
        os.replace(tmp, NOTIFIED_FILE)
    except Exception as e:
        import logging
        logging.error(
            f"[notify_done] fallo al guardar {NOTIFIED_FILE}: {type(e).__name__}: {e}"
        )


def _iter_tasks_from_file(status: str | None = None):
    """Itera tareas desde JSONL local, opcionalmente filtrando por status."""
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if status and obj.get("status") != status:
                    continue
                yield obj
    except FileNotFoundError:
        return
    except Exception:
        return

def _is_diaria(meta: dict) -> bool:
    via = str(meta.get("via") or "").strip()
    repeat = str(meta.get("repeat") or "").lower()
    return (repeat == "daily") or ("daily_time" in meta) or (via == "/diario")

def _fmt_hlocal(when_utc: str | None) -> str:
    s = when_utc or ""
    if not s:
        return "-"
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except Exception:
            continue
    if not parsed:
        return s
    try:
        return parsed.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s

async def _notify_executed_tasks_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Notifica ejecuciones de tareas.
    SOLO cuando status == "done" y last_run_ts sube respecto a lo ya notificado.
    Evita avisos repetidos con TTL en memoria y guardado inmediato.
    """
    # Lock anti-reentrada (si está disponible)
    lock = globals().get("_NOTIFY_JOB_LOCK")
    if lock is not None:
        if lock.locked():
            return
        async with lock:
            await _notify_executed_tasks_job__impl(context)
    else:
        await _notify_executed_tasks_job__impl(context)


async def _notify_executed_tasks_job__impl(context: ContextTypes.DEFAULT_TYPE):
    # Guard por /notificaciones y .env
    global NOTIFY_DONE_ENABLED
    if not NOTIFY_DONE_ENABLED:
        return

    notified = _load_notified_map()
    sent = 0
    now_mono = time.monotonic()

    def _collect_tasks_done_only():
        # Preferir API del gestor; filtrar por status="done"
        try:
            res = broker_tasks.list_tasks(status="done")
            if isinstance(res, dict):
                return res.get("tasks") or []
            return res or []
        except Exception:
            pass
        # Fallback: JSONL local filtrado por status="done"
        return list(_iter_tasks_from_file(status="done"))

    tasks = _collect_tasks_done_only()
    for t in tasks:
        # Debe venir con status done; si no, saltamos
        if (t.get("status") or "").lower() != "done":
            continue

        tid = t.get("id")
        if not tid:
            continue

        meta = t.get("meta") or {}
        chat_id = meta.get("chat_id")
        if not chat_id:
            continue

        last_run_ts = t.get("last_run_ts")
        try:
            cur_ts = float(last_run_ts) if last_run_ts is not None else 0.0
        except Exception:
            cur_ts = 0.0
        if cur_ts <= 0.0:
            continue

        # a) Persistente: ¿ya notificamos esta ejecución?
        prev_ts = float(notified.get(str(tid), 0.0))
        if cur_ts <= prev_ts:
            continue

        # b) Anti-rebote en memoria (por si falla guardado o hay 2 procesos)
        last_m = float(_LAST_SENT_IDS.get(str(tid), 0.0))
        if (now_mono - last_m) < _LAST_SENT_TTL_SEC:
            continue

        canal = t.get("channel")
        dest  = t.get("destination") or meta.get("dest") or "broadcast"
        via   = meta.get("via") or ""
        when_local_str = _fmt_hlocal(t.get("when_utc"))

        text = (
            "✅ <b>Tarea ejecutada</b>\n"
            f"ID: <code>{escape(str(tid))}</code>\n"
            f"Canal: <code>{escape(str(canal))}</code>  Destino: <code>{escape(str(dest))}</code>\n"
            f"Ejecutada (hora local): <code>{escape(when_local_str)}</code>\n"
            f"Origen: <code>{escape(via)}</code>"
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_to_message_id=meta.get("reply_to") or None,
            )
            # Marcar y persistir INMEDIATAMENTE para minimizar ventana de carrera
            _LAST_SENT_IDS[str(tid)] = now_mono
            notified[str(tid)] = cur_ts
            _save_notified_map(notified)
            sent += 1
        except Exception as e:
            logging.warning(f"[notify_done] fallo al enviar aviso: {type(e).__name__}: {e}")

    if sent:
        logging.info(f"[notify_done] enviados {sent} avisos")


# Ajusta si ya tienes estos valores en tu bot:
       # ← el broker lo fija por defecto (puerto del broker + 1)
BROKER_REQ_TIMEOUT = 8.0

def _broker_rpc(cmd: str, params: dict | None = None) -> dict:
    """Envía una petición JSONL simple al BacklogServer del broker y devuelve el dict."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(BROKER_REQ_TIMEOUT)
        s.connect((BROKER_CTRL_HOST, int(BROKER_CTRL_PORT)))
        req = {"cmd": str(cmd).upper(), "params": (params or {})}
        line = json.dumps(req, ensure_ascii=False) + "\n"
        s.sendall(line.encode("utf-8"))

        # leer UNA línea de respuesta
        data = b""
        t_end = time.time() + BROKER_REQ_TIMEOUT
        while time.time() < t_end:
            ch = s.recv(4096)
            if not ch:
                break
            data += ch
            if b"\n" in ch:
                break

        if not data.strip():
            return {"ok": False, "error": "empty_response"}

        try:
            return json.loads(data.decode("utf-8", "ignore").strip())
        except Exception as e:
            return {"ok": False, "error": f"bad_json: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"rpc_error: {type(e).__name__}: {e}"}
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass

def _broker_is_paused() -> tuple[bool, str | None]:
    """
    Consulta el estado del broker.
    Devuelve (paused, status_text) donde status_text ∈ {"paused","running"} o None si no se pudo.
    """
    r = _broker_rpc("BROKER_STATUS")
    if not r.get("ok"):
        return (False, None)  # si no podemos consultar, no bloqueamos por si acaso
    status = str(r.get("status") or "").lower()
    return (status == "paused", status)

def _broker_send_text(ch: int, text: str, dest: str | None, ack: bool) -> dict:
    """
    Envía texto a la malla por el propio broker (mejor camino).
    - dest=None o "broadcast" → difusión; o "!id" para unicast.
    Devuelve: {"ok":bool, "packet_id":int|None, "error":str|None}
    """
    params = {
        "text": text,
        "dest": (None if (not dest or str(dest).lower()=="broadcast") else str(dest)),
        "ch": int(ch),
        "ack": 1 if bool(ack) else 0,
    }
    r = _broker_rpc("SEND_TEXT", params)
    ok = bool(r.get("ok"))
    out = {"ok": ok, "packet_id": (r.get("packet_id") if ok else None)}
    if not ok:
        out["error"] = r.get("error") or "send_failed"
    return out

# === [NUEVO] Helper para consultar estado profundo del broker por el puerto de control UDP ===
import os, socket, json

def _send_broker_ctrl(cmd: str, extra: dict | None = None, timeout: float = 1.5):
    """
    MODIFICADA: ahora usa el canal JSONL/TCP del BacklogServer (no UDP).
    Mantiene la misma firma pública para no tocar los call-sites.
    """
    try:
        params = dict(extra or {})
    except Exception:
        params = {}
    try:
        # Reutiliza el cliente TCP existente (evita duplicidades y problemas de UDP)
        resp = _broker_rpc(str(cmd).upper(), params)
        return resp if isinstance(resp, dict) else None
    except Exception:
        return None

def _query_broker_status_ctrl(timeout: float = 1.5):
    """
    MODIFICADA: consulta BROKER_STATUS por JSONL/TCP usando _query_broker_status
    y devuelve un dict normalizado compatible con el antiguo retorno (UDP).
    """
    import os

    host = os.getenv("BROKER_CTRL_HOST", os.getenv("BROKER_HOST", "127.0.0.1")) or "127.0.0.1"
    try:
        port = int(os.getenv("BROKER_CTRL_PORT", str(int(os.getenv("BROKER_PORT", "8765")) + 1)))
    except Exception:
        port = 8766

    st = _query_broker_status(host, port, timeout)
    if not isinstance(st, dict):
        return None

    # Normaliza claves para mantener compatibilidad con el código existente
    try:
        connected = bool(st.get("connected"))
    except Exception:
        connected = None

    status_txt = str(st.get("status") or "").lower()
    mgr_paused = (status_txt == "paused") if status_txt else None

    # Campos opcionales que el broker podría no devolver siempre
    tx_blocked = st.get("tx_blocked") if isinstance(st.get("tx_blocked"), bool) else None
    cooldown_remaining = st.get("cooldown_remaining")
    version = st.get("version")
    since = st.get("since")
    node_host = st.get("node_host") or os.getenv("MESHTASTIC_HOST")
    try:
        node_port = int(st.get("node_port")) if st.get("node_port") is not None else 4403
    except Exception:
        node_port = 4403

    return {
        "connected": connected,
        "mgr_paused": mgr_paused,
        "tx_blocked": tx_blocked,
        "cooldown_remaining": cooldown_remaining,
        "version": version,
        "since": since,
        "node_host": node_host,
        "node_port": node_port,
    }


# === [NUEVO] Wrapper para handlers del bot: respeta cooldown del broker ===
def send_text_respecting_cooldown(
    chat_id: int,
    text: str,
    channel: int = 0,
    dest: str | None = None,   # None/"broadcast" o "!id"
    require_ack: bool = False,
    tg_bot=None,               # instancia de telegram.Bot o context.bot
) -> dict:
    """
    1) Si el broker está en cooldown (paused), avisa al usuario y NO intenta enviar.
    2) Si está running, intenta enviar por el broker y reporta resultado.
    """
    # 1) Consultar estado
    paused, status = _broker_is_paused()
    if paused:
        # Mensaje amable al usuario (no “error”, solo estado temporal)
        try:
            if tg_bot is not None:
                tg_bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ El nodo está **reconectando** (cooldown activo). "
                         "Prueba de nuevo en unos segundos.",
                    parse_mode="Markdown",
                )
        except Exception:
            pass
        return {"ok": False, "error": "cooldown_active"}

    # 2) Envío por broker
    res = _broker_send_text(int(channel), text, dest, bool(require_ack))
    if not res.get("ok"):
        # Informa del motivo si lo tenemos
        try:
            if tg_bot is not None:
                tg_bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ No se pudo enviar: {res.get('error','desconocido')}",
                )
        except Exception:
            pass
    else:
        try:
            if tg_bot is not None:
                pid = res.get("packet_id")
                tg_bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Enviado (canal {channel}{', unicast' if (dest and dest!='broadcast') else ', broadcast'})"
                         + (f" • id {pid}" if pid is not None else ""),
                )
        except Exception:
            pass
    return res

# === [NUEVO] Helper común para bloquear comandos de envío durante el cooldown ===
async def _abort_if_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Devuelve True si el broker está 'paused' (cooldown activo) y ya se avisó al usuario.
    Si devuelve True, el caller debe hacer 'return' inmediatamente.
    """
    try:
        paused, status = _broker_is_paused()
    except Exception:
        paused, status = (False, None)

    if paused:
        try:
            await update.effective_message.reply_text(
                "⚠️ El nodo está <b>reconectando</b> (cooldown activo). "
                "Inténtalo de nuevo en breve.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return True
    return False


BOT_MESH_MAX_BYTES = int(os.getenv("BOT_MESH_MAX_BYTES", "180"))
# Retro-compat: varios comandos usan MAX_BYTES
MAX_BYTES = BOT_MESH_MAX_BYTES

def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))

def _validate_len_or_block(texto_norm: str, *, max_bytes: int = BOT_MESH_MAX_BYTES) -> tuple[bool, str]:
    """
    Devuelve (ok, msg_error). ok=False si el mensaje ocupa > max_bytes.
    El broker también trocea, pero aquí BLOQUEAMOS si excede para que el usuario lo corrija.
    """
    b = _utf8_len(texto_norm)
    if b <= max_bytes:
        return True, ""
    # Mensaje de ayuda claro:
    return False, (
        "❌ <b>Mensaje demasiado largo</b>\n"
        f"• Tamaño: <code>{b} bytes</code> (límite: {max_bytes} bytes)\n"
        "• Por favor, acórtalo (puedes recortar el título, quitar comillas tipográficas o usar una URL más corta)."
    )



# === [NUEVO] Resolución unificada de alias y hops ===
def _resolve_alias_and_cache(evt: dict, nodes_map: dict) -> tuple[str, str]:
    """
    Devuelve (alias, id_fmt) para el 'from' del evento.
    - Prioriza alias recibido directamente del broker si viene en evt['from_alias'] o evt['sender'].
    - Si no viene, intenta nodes_map[<from_id>]['longName'] o ['shortName'].
    - Si encuentra alias, actualiza el nodes_map para futuras resoluciones.
    """
    from_id = str(evt.get("from") or "")
    if not from_id:
        return ("", "")

    # 1) Broker suele mandar 'from_alias' o 'sender' si lo tiene:
    alias = (evt.get("from_alias") or evt.get("sender") or "").strip()

    # 2) Cache local de nodos (si no vino en el evento)
    if not alias:
        node_info = nodes_map.get(from_id) or {}
        alias = (node_info.get("longName") or node_info.get("shortName") or "").strip()

    # 3) Si ahora tenemos alias, refrescamos cache para ese id
    if alias:
        cached = nodes_map.get(from_id) or {}
        if ("longName" not in cached) and ("shortName" not in cached):
            nodes_map[from_id] = {"longName": alias, **cached}
        elif not cached.get("longName"):
            cached["longName"] = alias
            nodes_map[from_id] = cached

    return (alias, f"!{from_id[-8:]}" if len(from_id) >= 8 else f"!{from_id}")

def _compute_real_hops(evt: dict) -> int | None:
    """Devuelve hop_limit - hop_start si ambos existen; si no, None."""
    try:
        hl = int(evt.get("hop_limit")) if evt.get("hop_limit") is not None else None
        hs = int(evt.get("hop_start")) if evt.get("hop_start") is not None else None
        if hl is None or hs is None:
            return None
        return max(0, hl - hs)
    except Exception:
        return None



async def _safe_reply_html(message, html_text: str, max_retries: int = 2):
    """
    Envía respuesta HTML a Telegram con reintentos si hay errores de red/DNS.
    No lanza excepción; registra en log si no consigue enviar.
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            await message.reply_text(html_text, parse_mode="HTML", disable_web_page_preview=True)
            return True
        except Exception as e:
            last_err = e
            # Reintento breve solo para errores de red típicos
            try:
                name = type(e).__name__
            except Exception:
                name = "Exception"
            logging.warning(f"[safe_reply] intento {attempt} falló: {name}: {e}")
            await asyncio.sleep(0.8 * attempt)
    logging.error(f"[safe_reply] no se pudo responder al usuario tras {max_retries} intentos: {last_err}")
    return False


# --- Helper: detectar "canal <n>" ---
def _is_broadcast_to_channel(args: list[str]) -> Tuple[bool, Optional[int]]:
    if not args:
        return False, None
    if args[0].lower() == "canal":
        if len(args) >= 2 and (args[1].lstrip("-").isdigit()):
            return True, int(args[1])
        return True, None
    return False, None

# ----- STUB NO BLOQUENATE, SIEMPRE 0
async def _collect_replies_nonblocking(seconds: float) -> int:
    try:
        secs = float(seconds if seconds is not None else 10.0)
    except Exception:
        secs = 10.0
    t0 = time.time()
    while time.time() - t0 < secs:
        await asyncio.sleep(0.25)
        # TODO: sumar respuestas reales si tienes backlog/broker integrado
    return 0

# --- [ACTUALIZADA] Helper: colectar respuestas usando el backlog del broker ---
async def _collect_replies_nonblocking_old_old(seconds: float) -> int:
    """
    Cuenta cuántos nodos distintos han enviado mensajes de texto en la ventana
    de 'seconds' inmediatamente posterior al envío.

    Implementación:
      1) Espera asíncrona 'seconds' sin bloquear el loop del bot.
      2) Pide al BacklogServer del broker (puerto BACKLOG_PORT) los TEXT_MESSAGE_APP
         recibidos desde (ahora - seconds).
      3) Devuelve el número de emisores únicos ('from') con texto no vacío.

    Requisitos:
      - El broker debe estar corriendo con BacklogServer activado (v4.5+).
      - La función fetch_backlog_from_broker(...) ya existe en este bot.
    """
    # 1) Espera no bloqueante
    try:
        secs = float(seconds if seconds is not None else 10.0)
    except Exception:
        secs = 10.0

    # Dormimos sin bloquear el event loop (no usar time.sleep aquí)
    await asyncio.sleep(secs)

    # 2) Consultar backlog al broker en la ventana [now-secs, now]
    try:
        since_ts = int(time.time() - secs)

        # Opcional: si quieres limitar al canal por defecto del bot, usa BROKER_CHANNEL.
        # Dejamos 'channel=None' para contar cualquier canal donde lleguen respuestas.
        resp = fetch_backlog_from_broker(
            host=BROKER_HOST,
            backlog_port=BACKLOG_PORT,
            since_ts=since_ts,
            channel=None,           # <- pon BROKER_CHANNEL si deseas limitar
            limit=2000,             # ventana razonable
            timeout=7.0
        )

        if not isinstance(resp, dict) or not resp.get("ok"):
            # Si no hay backlog disponible o el broker no responde, no rompemos
            return 0

        data = resp.get("data") or []
        if not isinstance(data, list):
            return 0

        # 3) Contar emisores únicos con texto no vacío
        #    El broker ya guarda TEXT_MESSAGE_APP, pero filtramos defensivamente.
        senders = set()
        for obj in data:
            try:
                # Solo mensajes de texto
                if str(obj.get("portnum") or "").upper() != "TEXT_MESSAGE_APP":
                    continue

                # Texto con contenido
                txt = obj.get("text")
                if not isinstance(txt, str) or not txt.strip():
                    continue

                # Emisor válido
                from_id = obj.get("from")
                if not isinstance(from_id, str) or not from_id:
                    continue

                # (Opcional) excluir eco del nodo local si detectas su '!id'
                # local_id = context.bot_data.get("local_node_id")  # si en el futuro lo guardas
                # if local_id and from_id == local_id:
                #     continue

                senders.add(from_id)
            except Exception:
                # Ignoramos filas malformadas sin romper el cómputo
                continue

        return len(senders)

    except Exception:
        # Seguridad total: ante cualquier error, devolvemos 0 para no romper /enviar
        return 0


# --- [NUEVO] Helper mínimo: ¿es envío a canal/broadcast? ---
def _is_broadcast_to_channel(args: list[str]) -> Tuple[bool, Optional[int]]:
    """
    Detecta 'canal <n>' al inicio de args.
    Devuelve: (es_broadcast, channel_index | None)
    """
    if not args:
        return False, None
    if args[0].lower() == "canal":
        if len(args) >= 2 and (args[1].isdigit() or (args[1].startswith("-") and args[1][1:].isdigit())):
            return True, int(args[1])
        # 'canal' sin índice → lo tratamos como no válido para evitar refrescos innecesarios
        return True, None
    return False, None


# --- [NUEVO] Helper seguro para colectar respuestas sin bloquear ---
async def _collect_replies_nonblocking(seconds: float) -> int:
    """
    Intenta contar respuestas durante 'seconds' sin bloquear el bot.
    Si tienes ya un colector integrado (p.ej. broker_tasks o backlog), cámbialo aquí
    para sumar respuestas reales. Por defecto, no bloquea y devuelve 0.
    """
    try:
        secs = float(seconds if seconds is not None else 10.0)
    except Exception:
        secs = 10.0
    # Microespera para no bloquear el loop (sin loops largos)
    t0 = time.time()
    while time.time() - t0 < secs:
        await asyncio.sleep(0.25)
        # TODO: engancha aquí tu lógica real de conteo de respuestas si la tienes
    return 0



# === [NUEVO] Prefetch inicial de nodos por API (antes de conectar pool) ===
import time  # si no lo tienes ya
from meshtastic_api_adapter import api_list_nodes  # NUEVO import

def _prefetch_nodes_on_boot(host: str, port: int = 4403, max_n: int = 50, timeout: float = 6.0):
    """
    Llama al API (TCPInterface efímero), cierra bien el socket y devuelve la lista de nodos.
    No guarda estado global: el que llama decide si cachea algo.
    """
    try:
        nodes = api_list_nodes(
            host=host,
            port=port,
            max_n=max_n,
            timeout_sec=timeout,
            assume_hops_zero=True
        )
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ Prefetch API inicial: {len(nodes)} nodos (pool aún no conectado).", flush=True)
        return nodes
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ Prefetch API inicial falló: {type(e).__name__}: {e}", flush=True)
        return []


def _extract_channel_and_strip(s: str) -> tuple[int, str]:
    """
    Busca "canal N" (o "ch N" / "channel N") en el texto y lo elimina,
    devolviendo (channel, texto_sin_etiqueta).
    - Si no hay canal explícito, devuelve 0 y el texto original.
    """
    m = re.search(r'(?i)\b(?:canal|ch(?:annel)?)\s*(?:=|:)?\s*(\d{1,2})\b', s)
    if not m:
        return 0, s
    ch = max(0, min(int(m.group(1)), 7))  # limita a 0..7 por seguridad
    start, end = m.span()
    s2 = (s[:start] + s[end:]).strip()
    s2 = re.sub(r'\s{2,}', ' ', s2)
    return ch, s2

def _parse_local_dt(date_str: str, time_str: str) -> datetime:
    """
    Convierte 'YYYY-MM-DD' + 'HH:MM' a datetime con tz Europe/Madrid.
    Lanza ValueError si el formato es incorrecto.
    """
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TZ_EUROPE_MADRID)

def _fmt_local(dt: datetime) -> str:
    return dt.astimezone(TZ_EUROPE_MADRID).strftime("%Y-%m-%d %H:%M")


# === [NUEVO] helper: mapa de últimos vistos solo-API (!id -> minutos) ===
def _build_last_seen_map_api_only(max_n: int = 250, timeout_sec: float = 5.0) -> dict[str, int]:
    """
    Devuelve dict {'!id': last_heard_min} usando únicamente la API.
    No lee ni cae a nodos.txt.
    """
    last_seen: dict[str, int] = {}
    try:
        rows = api_list_nodes(MESHTASTIC_HOST, max_n=max_n, timeout_sec=timeout_sec) or []
        for r in rows:
            nid = r.get("id")
            mins = r.get("last_heard_min")
            if isinstance(nid, str) and nid and mins is not None:
                last_seen[nid] = int(mins)
    except Exception:
        # Sin fallback: si la API no lo da, se quedará sin minuto (¿?)
        pass
    return last_seen

# === [NUEVO] helper: últimos vistos SOLO-API con carencia vía broker (sin nodos.txt) ===
def _build_last_seen_map_api_with_broker_fallback(
    max_n: int = 300,
    timeout_sec: float = 5.0,
    lookback_hours: int = 12,
) -> dict[str, int]:
    """
    Devuelve {'!id': last_heard_min}. Estrategia:
      1) API-first: api_list_nodes(...).
      2) Carencia (solo para los que quedaron sin minutos):
         consulta BacklogServer del broker (TEXT_MESSAGE_APP) en una ventana de lookback
         y computa minutos desde el último rx_time por cada '!id' (usando 'from').
    No lee nodos.txt.
    """
    last_seen: dict[str, int] = {}

    # 1) API-first
    try:
        rows = api_list_nodes(MESHTASTIC_HOST, max_n=max_n, timeout_sec=timeout_sec) or []
        for r in rows:
            nid = r.get("id")
            mins = r.get("last_heard_min")
            if isinstance(nid, str) and nid and mins is not None:
                last_seen[nid] = int(mins)
    except Exception:
        pass

    # ¿Hay pendientes sin minuto? si no, terminamos
    # (pero dejamos opción de enriquecer cualquiera si lo prefieres)
    pending_ids: set[str] = set()
    try:
        # Si además tenemos vecinos del API, intentamos cubrirlos con backlog si faltan minutos
        neigh = api_get_neighbors_via_pool(MESHTASTIC_HOST, 4403) or {}
        for raw_id in neigh.keys():
            try:
                nid = raw_id if str(raw_id).startswith("!") else f"!{int(raw_id):08x}"
            except Exception:
                nid = str(raw_id)
            if nid not in last_seen:
                pending_ids.add(nid)
    except Exception:
        pass

    if not pending_ids:
        return last_seen

    # 2) Carencia vía broker backlog
    try:
        since_ts = int(time.time() - int(lookback_hours) * 3600)
        # Usa el helper ya presente en este bot
        resp = fetch_backlog_from_broker(
            host=BROKER_HOST or "127.0.0.1",
            backlog_port=BACKLOG_PORT,
            since_ts=since_ts,
            channel=None,            # todos los canales
            limit=5000,              # ventana razonable
            timeout=10.0
        )
        if not resp.get("ok"):
            return last_seen

        data = resp.get("data") or []
        if not isinstance(data, list):
            return last_seen

        # Último rx_time por '!id' (preferimos 'from')
        latest_by_id: dict[str, int] = {}
        for obj in data:
            try:
                nid_from = obj.get("from")
                rx_time = obj.get("rx_time")
                if not (isinstance(nid_from, str) and nid_from.startswith("!")):
                    continue
                if not isinstance(rx_time, (int, float)):
                    continue
                cur = latest_by_id.get(nid_from)
                if cur is None or int(rx_time) > cur:
                    latest_by_id[nid_from] = int(rx_time)
            except Exception:
                continue

        now = int(time.time())
        for nid in pending_ids:
            ts = latest_by_id.get(nid)
            if ts:
                mins = max(0, int((now - ts) / 60))
                last_seen[nid] = mins
    except Exception:
        # Silencioso: si el broker no está o no hay backlog, no rompemos
        pass

    return last_seen


def _friendly_node(nid: str, nodes_map: dict | None) -> str:
    """
    Devuelve '!id (Alias)' si existe alias en nodes_map, o '!id' si no.
    nodes_map: dict con claves '!id' y valor {'alias': '...'} (como guardas en ver_nodos).
    """
    if not nid:
        return nid
    alias = None
    if nodes_map and isinstance(nodes_map, dict):
        info = nodes_map.get(nid) or nodes_map.get(nid.lstrip("!"))
        if isinstance(info, dict):
            alias = (info.get("alias") or "").strip()
        elif isinstance(info, str):
            alias = info.strip()
    return f"{nid} ({alias})" if alias else nid

# === [NUEVO] Helpers APRS: formateo y última posición de un nodo ===

def _aprslib_deg_to_lat(dm: float) -> tuple[str, str]:
    """
    Convierte latitud decimal a ('DDMM.mm', 'N'|'S').
    """
    if dm is None:
        return "", ""
    sign = 'N' if dm >= 0 else 'S'
    v = abs(float(dm))
    deg = int(v)
    minutes = (v - deg) * 60.0
    return f"{deg:02d}{minutes:05.2f}", sign

def _aprslib_deg_to_lon(dm: float) -> tuple[str, str]:
    """
    Convierte longitud decimal a ('DDDMM.mm', 'E'|'W').
    """
    if dm is None:
        return "", ""
    sign = 'E' if dm >= 0 else 'W'
    v = abs(float(dm))
    deg = int(v)
    minutes = (v - deg) * 60.0
    return f"{deg:03d}{minutes:05.2f}", sign

def _meters_to_feet(m: float | int | None) -> int | None:
    if m is None:
        return None
    try:
        return int(round(float(m) / 0.3048))
    except Exception:
        return None

def _knots_from_kmh(kmh: float | int | None) -> int | None:
    if kmh is None:
        return None
    try:
        return int(round(float(kmh) * 0.539957))
    except Exception:
        return None

def _build_aprs_position_frame(lat: float, lon: float,
                               *, symbol_table: str = '/',
                               symbol_code: str = '>',
                               altitude_m: float | int | None = None,
                               course_deg: float | int | None = None,
                               speed_kmh: float | int | None = None,
                               comment: str = "") -> str | None:
    """
    Devuelve una línea de información APRS de posición (¡no el paquete AX.25 completo!).
    Formato: !DDMM.mmN/DDDMM.mmE<symbol>Comentario...
    - altitude se muestra en pies en el comentario (APRS estándar)
    - course/speed si están presentes: ' cxxx/syy'
    """
    la, ns = _aprslib_deg_to_lat(lat)
    lo, ew = _aprslib_deg_to_lon(lon)
    if not la or not lo or not ns or not ew:
        return None

    # Normaliza symbol table/code
    st = symbol_table if symbol_table in ('/', '\\') else '/'
    sc = symbol_code if isinstance(symbol_code, str) and len(symbol_code) == 1 else '>'

    parts = []
    # Curso/velocidad (knots) opcional
    crs = None if course_deg is None else int(max(0, min(359, int(course_deg))))
    spd_kn = _knots_from_kmh(speed_kmh)

    if crs is not None and spd_kn is not None:
        parts.append(f" c{crs:03d}/s{spd_kn:03d}")

    # Altitud en pies (común en APRS)
    alt_ft = _meters_to_feet(altitude_m)
    if alt_ft is not None:
        parts.append(f" alt {alt_ft}ft")

    if comment:
        # Evita saltos de línea y controla longitud razonable
        c = " " + str(comment).replace("\n", " ").strip()
    else:
        c = ""

    info_field = f"!{la}{ns}{st}{lo}{ew}{sc}{''.join(parts)}{c}"
    return info_field.strip()

def _resolve_node_id_for_aprs(token: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[str | None, str]:
    """
    Resuelve número|!id|alias → !id usando el mapeo existente del bot.
    Devuelve: (!id | None, texto_mostrable_para_usuario)
    """
    arg = (token or "").strip()
    if not arg:
        return None, ""

    # Si ya es !id:
    if arg.startswith("!"):
        return arg, arg

    # Construir/usar mapping existente
    nodes_index = context.user_data.get("nodes_map") or build_nodes_mapping()
    if not nodes_index:
        return None, arg

    # ¿El usuario pasó un número de la última lista?
    if arg.isdigit() and arg in nodes_index:
        return nodes_index[arg], f"#{arg} → {nodes_index[arg]}"

    # ¿Alias?
    key = arg.lower()
    if key in nodes_index:
        return nodes_index[key], f"{arg} → {nodes_index[key]}"

    # ¿El propio id sin '!'?
    if arg in nodes_index:
        v = nodes_index[arg]
        return (v if v.startswith("!") else f"!{v}") if v else None, arg

    return None, arg

def _read_last_position_for(nid: str) -> dict | None:
    """
    Busca la última posición de '!id' (preferencia: positions_store.read_positions_recent;
    si no está disponible/compatible, lee bot_data/positions.jsonl).
    Devuelve un dict con al menos: {'lat','lon','alt'?,'speed_kmh'?,'course_deg'?,'alias'?,'from'?,'ts'?}
    """
    # 1) Intentar positions_store (si la firma cambia, caemos al plan B)
    try:
        from positions_store import read_positions_recent  # ya importado arriba, pero re-import safe
        rows = read_positions_recent(limit=5000)  # <- firma típica en tu proyecto
        # Filtra última del nodo
        best = None
        for r in rows:
            if str(r.get("from") or r.get("id") or "") == nid:
                if (best is None) or int(r.get("ts", 0)) > int(best.get("ts", 0)):
                    best = r
        if best:
            return best
    except Exception:
        pass

    # 2) Fallback: leer JSONL directo
    import os, json
    path = os.path.join("bot_data", "positions.jsonl")
    if not os.path.exists(path):
        return None
    best = None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if str(rec.get("from") or rec.get("id") or "") != nid:
                    continue
                if (best is None) or int(rec.get("ts", 0)) >= int(best.get("ts", 0)):
                    best = rec
    except Exception:
        best = None
    return best

# === [NUEVO] Helpers de troceo APRS en el BOT (para garantizar límite de APRS) ===

def _aprs_max_len() -> int:
    try:
        return int(os.getenv("APRS_MAX_LEN", "67"))
    except Exception:
        return 67

def _wrap_hard(s: str, width: int) -> list[str]:
    """
    Word-wrap con ruptura dura si una 'palabra' excede width.
    """
    s = (s or "").strip()
    if width < 4:
        return [s] if s else []
    out = []
    cur = ""
    for token in re.split(r"(\s+)", s):
        if not token:
            continue
        if token.isspace():
            # si cabe el espacio, lo añadimos; si no, forzamos salto
            if len(cur) + len(token) <= width:
                cur += token
            else:
                if cur.strip():
                    out.append(cur.strip())
                cur = ""
        else:
            # palabra
            if len(cur) + len(token) <= width:
                cur += token
            else:
                if cur.strip():
                    out.append(cur.strip())
                    cur = ""
                # si la palabra excede width, partirla
                while len(token) > width:
                    out.append(token[:width])
                    token = token[width:]
                cur = token
    if cur.strip():
        out.append(cur.strip())
    return out

def _aprs_split_broadcast(text: str, max_len: int | None = None) -> list[str]:
    """
    Divide texto en trozos con sufijo ' (i/N)' respetando longitud APRS.
    """
    if max_len is None:
        max_len = _aprs_max_len()
    raw = (text or "").strip()
    if not raw:
        return []
    # Aproximación inicial suponiendo sufijo de tamaño 6..8
    width_guess = max(10, max_len - 8)
    chunks = _wrap_hard(raw, width_guess)
    # Iterar hasta estabilizar N y anchos reales
    for _ in range(3):
        N = max(1, len(chunks))
        new_chunks = []
        for i, ch in enumerate(chunks, start=1):
            suffix = f" ({i}/{N})"
            width_i = max(8, max_len - len(suffix))
            new_chunks.extend(_wrap_hard(ch, width_i))
        if len(new_chunks) == len(chunks):
            chunks = new_chunks
            break
        chunks = new_chunks
    # Añadir sufijos finales
    N = max(1, len(chunks))
    final = []
    for i, ch in enumerate(chunks, start=1):
        suffix = f" ({i}/{N})"
        width_i = max_len - len(suffix)
        if len(ch) > width_i:
            ch = ch[:width_i]
        final.append(ch + suffix)
    return final

def _aprs_split_directed(text: str, max_len: int | None = None) -> list[str]:
    """
    Divide texto en trozos con sufijo '{nn}' (02 dígitos) para mensajes dirigidos APRS.
    """
    if max_len is None:
        max_len = _aprs_max_len()
    raw = (text or "").strip()
    if not raw:
        return []
    # Reservar 4 caracteres para {nn}
    width = max(8, max_len - 4)
    base = _wrap_hard(raw, width)
    # si alguna pasa (por texto sin espacios), recortar duro
    base = [s[:width] if len(s) > width else s for s in base]
    final = []
    for i, s in enumerate(base, start=1):
        idx = i if i <= 99 else (i % 99 or 99)   # {01}..{99}
        suffix = f"{{{idx:02d}}}"
        if len(s) + len(suffix) > max_len:
            s = s[: max_len - len(suffix)]
        final.append(s + suffix)
    return final

# --- [FIN] Helpers APRS

import threading, time
_exclusive_lock = threading.RLock()
_exclusive_count = 0

def pause_broker_for_exclusive(max_wait_s: float = 6.0) -> bool:
    """
    Pide pausa y bloquea reconexiones del pool para ejecutar una operación exclusiva (CLI).
    Compatible con llamadas anidadas.

    Respeta BOT_PAUSE_MODE:
      - effective == "never" → NO manda BROKER_PAUSE (solo contador).
      - effective == "always" → comportamiento actual.
    """
    global _exclusive_count
    mode = _get_pause_mode_effective()

    with _exclusive_lock:
        _exclusive_count += 1
        if mode == "never":
            # No pedimos pausa al broker, pero mantenemos el contador
            return True

        ok = _broker_ctrl("BROKER_PAUSE").get("ok", False)
        if not ok:
            _exclusive_count -= 1
            return False

    t0 = time.time()
    while time.time() - t0 < max_wait_s:
        st = _broker_ctrl("BROKER_STATUS")
        if st.get("ok") and st.get("status") == "paused":
            return True
        time.sleep(0.15)

    # Timeout → revertir
    resume_broker_after_exclusive()
    return False


def resume_broker_after_exclusive():
    """
    Libera la pausa exclusiva. Si hay más exclusivas anidadas, sólo decrementa el contador.

    Respeta BOT_PAUSE_MODE: en modo "never" no manda BROKER_RESUME.
    """
    global _exclusive_count
    mode = _get_pause_mode_effective()

    with _exclusive_lock:
        if _exclusive_count > 0:
            _exclusive_count -= 1

        if _exclusive_count == 0 and mode != "never":
            _broker_ctrl("BROKER_RESUME")


@contextmanager
def with_broker_paused(max_wait_s: float = 4.0):
    """
    Contexto: pausa el broker y al salir reanuda.

    Respeta BOT_PAUSE_MODE:
      - effective == "never"  → no pausa nunca (modo Raspberry).
      - effective == "always" → siempre pausa (modo Windows si BOT_PAUSE_MODE=auto/always).
    """
    mode = _get_pause_mode_effective()

    if mode == "never":
        # No tocamos al broker, simplemente ejecutamos el bloque.
        try:
            yield True
        finally:
            # No hay nada que reanudar en este modo.
            pass

    # Modo "always": usamos tu lógica existente de pausa exclusiva.
    ok = pause_broker_for_exclusive(max_wait_s=max_wait_s)
    try:
        yield ok
    finally:
        resume_broker_after_exclusive()

# Intentamos localizar funciones de control del broker, si existen
def _try_import_broker_controls():
    """
    Busca funciones de control en tus módulos (best-effort):
      - Meshtastic_Broker_v3.3.3: pause_broker(), resume_broker(), disconnect_all(), connect_all()
      - broker_task: pause_broker(), resume_broker()
    Devuelve dict con callables o None.
    """
    controls = {
        "pause": None,
        "resume": None,
        "disconnect_all": None,
        "connect_all": None,
    }
    # 1) Meshtastic_Broker_v3.3.3
    try:
        import Meshtastic_Broker_v3_3_3 as broker_mod  # type: ignore
    except Exception:
        broker_mod = None
    if broker_mod:
        for name in ("pause_broker", "resume_broker", "disconnect_all", "connect_all"):
            fn = getattr(broker_mod, name, None)
            if callable(fn):
                if name == "pause_broker":
                    controls["pause"] = fn
                elif name == "resume_broker":
                    controls["resume"] = fn
                elif name == "disconnect_all":
                    controls["disconnect_all"] = fn
                elif name == "connect_all":
                    controls["connect_all"] = fn

    # 2) broker_task
    try:
        import broker_task as broker_task_mod  # type: ignore
    except Exception:
        broker_task_mod = None
    if broker_task_mod:
        for name in ("pause_broker", "resume_broker"):
            fn = getattr(broker_task_mod, name, None)
            if callable(fn):
                if name == "pause_broker" and controls["pause"] is None:
                    controls["pause"] = fn
                elif name == "resume_broker" and controls["resume"] is None:
                    controls["resume"] = fn

    return controls

# === CONTROL DEL BROKER A NIVEL DE PROCESO (Windows-friendly) ===
import os, sys, re, time, signal, json, asyncio, subprocess
from typing import List, Tuple, Optional

# Ajusta si tu broker tiene otro nombre de fichero
BROKER_CANDIDATE_FILENAMES = [
    "Meshtastic_Broker_v3_3_3.py",  # preferible renombrar así para imports válidos
    "Meshtastic_Broker_v3.3.3.py",  # por si aún existe con puntos
    "broker_task.py",
]

# ===================== NUEVO – helpers de pausa/CLI =====================

# === Helpers LoRa vía broker (usan _broker_ctrl con {"cmd": ..., "params": {...}}) ===

def _lora_broker_get() -> dict:
    """
    Pide al broker la config LoRa (API real en el broker).
    Requiere que el broker entienda el comando 'LORA_GET'.
    """
    r = _broker_ctrl("LORA_GET", None, 3.5)
    if not r or not r.get("ok"):
        return {}
    data = r.get("data") or {}
    out = {}
    for k in ("ignore_incoming", "ignore_mqtt"):
        v = data.get(k)
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = bool(v)
        elif isinstance(v, str):
            out[k] = v.strip().lower() in ("1", "true", "on", "sí", "si", "yes")
        else:
            out[k] = None
    return out

def _lora_broker_set(updates: dict[str, bool]) -> tuple[bool, str]:
    """
    Pide al broker que actualice los flags LoRa (API real).
    Requiere que el broker entienda 'LORA_SET'.
    """
    clean = {k: bool(v) for k, v in (updates or {}).items() if k in ("ignore_incoming", "ignore_mqtt")}
    if not clean:
        return False, "no_updates"
    r = _broker_ctrl("LORA_SET", clean, 4.0)
    if r and r.get("ok"):
        return True, "broker"
    return False, (r.get("error") if isinstance(r, dict) else "broker_ko")

def _write_atomic(path: str, data: str, encoding: str = "utf-8") -> None:
    """
    Escritura atómica: escribe en un .tmp y hace os.replace al destino.
    No toca nada que ya tengas; úsala solo donde la llames.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding=encoding, newline="\n") as f:
        f.write(data)
    os.replace(tmp, path)


def _broker_ctrl(cmd: str, params: dict | None = None, timeout: float = 3.0) -> dict:
    """
    Envía un comando JSON al BacklogServer del broker:
      - "BROKER_PAUSE" / "BROKER_RESUME" / "BROKER_STATUS"
      - "FETCH_BACKLOG" (ya existente)
    Devuelve dict {ok: bool, ...}
    """
    msg = json.dumps({"cmd": cmd, "params": params or {}}, ensure_ascii=False) + "\n"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((BROKER_CTRL_HOST, BROKER_CTRL_PORT))
        s.sendall(msg.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = (buf.decode("utf-8", "ignore") or "").strip()
        return json.loads(line) if line else {"ok": False, "error": "empty response"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _pause_broker_io_for_cli(context, max_wait_s: float = 4.0) -> str:
    """
    Pausa la conexión persistente del broker (solo 1 vez aunque haya reentradas).
    Usa un contador en context.bot_data["broker_io_pause_count"].
    Devuelve un token (string) para emparejar con el resume.
    """
    token = f"cli-{int(time.time() * 1000)}"
    try:
        cnt = int(context.bot_data.get("broker_io_pause_count", 0))
    except Exception:
        cnt = 0

    if cnt == 0:
        r = _broker_ctrl("BROKER_PAUSE")
        # No fallamos si no puede pausar: devolvemos token igualmente
        if r.get("ok"):
            # Espera a estado "paused"
            t0 = time.time()
            while time.time() - t0 < max_wait_s:
                st = _broker_ctrl("BROKER_STATUS")
                if st.get("ok") and st.get("status") == "paused":
                    break
                time.sleep(0.15)

    context.bot_data["broker_io_pause_count"] = cnt + 1
    return token


def _resume_broker_io_after_cli(context, token: str) -> bool:
    """
    Decrementa el contador de pausa; si llega a 0, ordena reanudar al broker.
    """
    try:
        cnt = int(context.bot_data.get("broker_io_pause_count", 0))
    except Exception:
        cnt = 0

    if cnt <= 1:
        context.bot_data["broker_io_pause_count"] = 0
        r = _broker_ctrl("BROKER_RESUME")
        return bool(r.get("ok"))
    else:
        context.bot_data["broker_io_pause_count"] = cnt - 1
        return True


# === Homogeneización de nombres (aliases) para helpers de pausa CLI ===
# === Homogeneización de nombres (aliases) para helpers de pausa CLI ===
try:
    if 'pause_broker_from_exclusive' not in globals() and 'pause_broker_for_exclusive' in globals():
        pause_broker_from_exclusive = pause_broker_for_exclusive

    if 'resume_broker_from_exclusive' not in globals() and 'resume_broker_after_exclusive' in globals():
        resume_broker_from_exclusive = resume_broker_after_exclusive
except Exception:
    pass



# ===================== MODIFICADA – helper CLI robusto y cross-platform =====================
def _run_cli_nodes_with_retry(
    host: str,
    attempts: int = 2,
    first_timeout: int = 18,
    backoff_sec: int = 2,
) -> tuple[bool, list[str], str]:
    """
    Ejecuta la CLI 'meshtastic --host <host> --nodes' con reintentos y sin usar 'shell=True'
    (evita errores de quoting en Windows).
    Estrategia:
      1) Intenta: [sys.executable, "-m", "meshtastic", "--host", host, "--nodes"]
      2) Si falla, intenta: ["meshtastic", "--host", host, "--nodes"]
    Devuelve: (ok, lines, reason) con 'lines' como lista de líneas no vacías.
    """

    import sys
    import os
    import subprocess

    def _normalize_lines(s: str) -> list[str]:
        s = (s or "").replace("\r\n", "\n")
        return [ln.rstrip() for ln in s.split("\n") if ln.strip()]

    def _try_once(timeout_s: int) -> tuple[bool, list[str], str]:
        last_reason = "unknown"
        # Preferir 'python -m meshtastic' (más estable en Windows)
        variants: list[list[str]] = [
            [sys.executable or "python", "-m", "meshtastic", "--host", host, "--nodes"],
            ["meshtastic", "--host", host, "--nodes"],
        ]

        # Evitar ventanas en Windows
        popen_kwargs = {}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # Asegurar encoding consistente para la CLI
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")

        for argv in variants:
            try:
                # ¡OJO!: shell=False y argv como lista → sin problemas de quoting
                cp = subprocess.run(
                    argv,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    encoding="utf-8",
                    errors="ignore",
                    env=env,
                    **popen_kwargs,
                )
                out = (cp.stdout or "")
                err = (cp.stderr or "")
                if cp.returncode == 0:
                    lines = _normalize_lines(out) or _normalize_lines(err)
                    if lines:
                        return True, lines, ""
                    else:
                        last_reason = "empty output"
                else:
                    # Razón con algo de contexto
                    last_reason = f"rc={cp.returncode}: {(err or out or '').strip() or 'no output'}"
            except subprocess.TimeoutExpired:
                last_reason = "timeout"
            except FileNotFoundError as e:
                # Este suele saltar si el entry-point 'meshtastic' no está en PATH
                last_reason = f"not found: {e}"
            except Exception as e:
                last_reason = f"{type(e).__name__}: {e}"
        return False, [], last_reason

    timeout = int(first_timeout)
    reason = ""
    for attempt in range(max(1, int(attempts))):
        ok, lines, reason = _try_once(timeout)
        if ok:
            return True, lines, ""
        # backoff lineal suave
        try:
            time.sleep(max(0, int(backoff_sec)))
        except Exception:
            pass
        timeout = min(timeout + backoff_sec, first_timeout + 10)

    return False, [], (reason or "failed after retries")
# ===================== /MODIFICADA =====================
# === NUEVO: constructor robusto de mapping (!id/alias -> !id canónico) ===
def build_nodes_mapping_from_list(rows) -> dict:
    """
    Acepta listas de dicts en cualquiera de estas formas:
      - Salida API: cada item puede tener 'id', 'nodeId', 'user': {'id','longName','shortName'}, 'name', etc.
      - Salida fichero nodos.txt parseado: cada item suele tener 'id', 'alias', 'mins', 'hops' (via _parse_nodes_table).
    Devuelve un dict {clave_lower: '!id'} donde 'clave' puede ser:
      '!id', 'id' sin '!', alias (long/short/name), etc.
    """
    mapping = {}
    if not isinstance(rows, (list, tuple)):
        return mapping

    for r in rows:
        if not isinstance(r, dict):
            continue

        user = r.get("user") or {}
        nid = (
            r.get("id")
            or r.get("nodeId")
            or user.get("id")
            or r.get("num")  # por si acaso
            or ""
        )
        nid = str(nid).strip()
        if not nid:
            continue

        # Canon: siempre guardamos tal cual venga 'id'
        canon = nid

        # Candidatos a clave
        alias = (
            r.get("alias")
            or user.get("longName")
            or user.get("shortName")
            or r.get("name")
        )

        candidates = [nid, nid.lstrip("!"), alias]
        for key in candidates:
            if not key:
                continue
            k = str(key).strip().lower()
            if not k:
                continue
            mapping[k] = canon

    return mapping


# ===================== /helpers de pausa/CLI =====================


def _find_broker_script_on_disk() -> Optional[str]:
    """
    Localiza el script del broker en el mismo directorio que este bot.
    """
    base_dir = os.path.abspath(os.path.dirname(__file__))
    for name in BROKER_CANDIDATE_FILENAMES:
        p = os.path.join(base_dir, name)
        if os.path.exists(p):
            return p
    return None

def _list_python_processes_cmdlines() -> List[Tuple[int,str]]:
    """
    Devuelve [(pid, cmdline_str)] de procesos Python.
    Usa psutil si está, si no: 'wmic' (Windows) o 'ps -eo pid,args' (Unix).
    """
    out: List[Tuple[int,str]] = []
    # 1) psutil (si está)
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(attrs=["pid","name","cmdline"]):
            try:
                name = (p.info.get("name") or "").lower()
                if "python" not in name and "py" not in name:
                    continue
                cmd = " ".join(p.info.get("cmdline") or [])
                out.append((int(p.info["pid"]), cmd))
            except Exception:
                continue
        if out:
            return out
    except Exception:
        pass

    # 2) Windows: wmic
    if os.name == "nt":
        try:
            cp = subprocess.run(
                ["wmic","process","where","name='python.exe'","get","ProcessId,CommandLine","/FORMAT:LIST"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=6
            )
            s = cp.stdout or ""
            blocks = [b for b in s.split("\n\n") if "ProcessId=" in b]
            for b in blocks:
                pid = None; cmd = ""
                for line in b.splitlines():
                    if line.startswith("CommandLine="):
                        cmd = line.split("=",1)[1].strip()
                    elif line.startswith("ProcessId="):
                        try:
                            pid = int(line.split("=",1)[1].strip())
                        except Exception:
                            pid = None
                if pid:
                    out.append((pid, cmd))
        except Exception:
            pass
    else:
        # 3) Unix: ps
        try:
            cp = subprocess.run(
                ["ps","-eo","pid,args"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=6
            )
            for line in (cp.stdout or "").splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) != 2: 
                    continue
                pid_s, cmd = parts
                if "python" in cmd:
                    try:
                        out.append((int(pid_s), cmd))
                    except Exception:
                        pass
        except Exception:
            pass
    return out

def _stop_broker_processes(token: dict) -> bool:
    """
    Busca procesos Python cuyo cmdline contenga el script del broker y los termina.
    Guarda pids en token['killed_pids'].
    """
    script_hint = _find_broker_script_on_disk()
    hints = set(BROKER_CANDIDATE_FILENAMES)
    if script_hint:
        hints.add(os.path.basename(script_hint))

    proc_list = _list_python_processes_cmdlines()
    if not proc_list:
        return False

    killed_any = False
    token.setdefault("killed_pids", [])
    for pid, cmd in proc_list:
        cmd_low = (cmd or "").lower()
        if any(h.lower() in cmd_low for h in hints):
            try:
                if os.name == "nt":
                    # Windows: taskkill forzado para evitar zombies
                    subprocess.run(["taskkill","/PID",str(pid),"/T","/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.kill(pid, signal.SIGTERM)
                token["killed_pids"].append(pid)
                killed_any = True
            except Exception:
                try:
                    if os.name != "nt":
                        os.kill(pid, signal.SIGKILL)
                        token["killed_pids"].append(pid)
                        killed_any = True
                except Exception:
                    pass
    return killed_any

def _start_broker_background(token: dict) -> bool:
    """
    Lanza el broker en segundo plano ejecutando el script localizado.
    """
    script = _find_broker_script_on_disk()
    if not script:
        token["broker_relaunched_error"] = "No se localizó el script del broker en el mismo directorio."
        return False

    python_exe = sys.executable or "python"
    popen_kwargs = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.Popen(
            [python_exe, "-u", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_kwargs
        )
        token["broker_relaunched"] = True
        return True
    except Exception as e:
        token["broker_relaunched_error"] = str(e)
        return False


def _parse_nodes_cli_to_lines(stdout: str) -> List[str]:
    """
    Normaliza salida de CLI a líneas tabuladas:
    <id>\t<alias>\t<mins_txt>\t<hops_txt>
    (mins puede ir vacío; hops puede ser '? hops')
    """
    out: List[str] = []
    s = (stdout or "").strip()
    if not s:
        return out

    # Intentar JSON primero
    try:
        data = json.loads(s)
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if isinstance(nodes, list):
            for n in nodes:
                nid = n.get("num") or n.get("id") or n.get("nodeNum")
                user = n.get("user") or {}
                alias = user.get("longName") or user.get("shortName") or str(nid)
                mins_txt = ""  # CLI --nodes no siempre trae lastHeard
                hops = n.get("hops") or n.get("hopLimit")
                hops_txt = f"{hops} hops" if hops is not None else "? hops"
                out.append(f"{nid}\t{alias}\t{mins_txt}\t{hops_txt}")
            return out
    except Exception:
        pass

    # Fallback: texto plano
    for ln in s.splitlines():
        if ln.strip():
            out.append(ln.rstrip())
    return out

def _run_cli_nodes_with_timeout(host: str, timeout_sec: int = 12) -> Tuple[bool, List[str], str]:
    """
    Ejecuta `meshtastic --host <host> --nodes` con timeout duro y kill si excede.
    Devuelve (ok, lines, reason).
    """
    cmd = ["meshtastic", "--host", host, "--nodes"]
    try:
        print(f"⏳ Ejecutando (CLI): {' '.join(cmd)} (timeout {timeout_sec}s)")
        cp = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True  # nuevo grupo p/killpg en Unix; en Windows igualmente aísla
        )
        t0 = time.time()
        while cp.poll() is None and (time.time() - t0) < timeout_sec:
            time.sleep(0.1)

        if cp.poll() is None:
            # timeout → matar proceso
            try:
                if hasattr(os, "killpg"):
                    os.killpg(cp.pid, signal.SIGKILL)
                else:
                    cp.kill()
            except Exception:
                cp.kill()
            return False, [], "CLI_TIMEOUT"

        stdout, stderr = cp.communicate(timeout=2)
        if cp.returncode != 0:
            return False, [], f"CLI_ERR rc={cp.returncode}: {(stderr or 'sin stderr').strip()}"

        lines = _parse_nodes_cli_to_lines(stdout)
        if not lines:
            return False, [], "CLI_EMPTY"

        return True, lines, "CLI_OK"

    except FileNotFoundError:
        return False, [], "CLI_ERR: 'meshtastic' no encontrado"
    except Exception as e:
        return False, [], f"CLI_ERR: {e}"


# === [NUEVO] API primero + fallback CLI, con grabación nodos.txt y logs ===
import os, time, json, subprocess
from typing import List, Tuple




def _safe_makedirs(p: str) -> None:
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass

def _write_text_atomic(path: str, content: str) -> None:
    _safe_makedirs(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)

def _save_nodes_txt(lines: List[str]) -> None:
    """
    Guarda líneas en nodos.txt (formato simple 'id;alias').
    SOLO se llama cuando hay datos válidos (no se machaca con errores).
    """
    body = "\n".join(lines) + ("\n" if lines else "")
    _write_text_atomic(NODES_FILE, body)

def _api_list_nodes_basic(host: str, timeout: float = 10.0) -> Tuple[bool, List[str], str]:
    """
    Intenta listar nodos por API.
    Devuelve (ok, lines, reason). lines en formato ['<id>;<alias>'].
    """
    # Desactivar API si la bandera está activa (si existe en tu proyecto)
    #try:
    #    if DISABLE_BOT_TCP:  # noqa: F821 (si no existe la flag en tu entorno, elimina este bloque)
    #        return False, [], "API_DISABLED"
    #except NameError:
    #    pass

    iface = None
    release = None
    try:
        # 1) Intentar pool persistente si existe
        try:
            from tcpinterface_persistent import get_tcp_pool  # usa tu pool único por (host,port)
            pool = get_tcp_pool()
            iface = pool.acquire(host=host, port=DEFAULT_PORT_HOST, timeout=timeout)  # DEFAULT_PORT_HOST ya lo importas arriba
            release = lambda: pool.release(iface)
        except Exception:
            # 2) TCPInterface directa (vía shim del propio tcpinterface_persistent si es posible)
            TCPInterface = None
            err_import = None
            try:
                # Preferente: tomar TCPInterface del shim, así reutiliza la compatibilidad host/hostname y el pool interno
                from tcpinterface_persistent import TCPInterface as _TCP
                TCPInterface = _TCP
            except Exception as e:
                err_import = e
                try:
                    # Fallback: TCPInterface directo de la librería oficial (sin shim)
                    from meshtastic.tcp_interface import TCPInterface as _TCP
                    TCPInterface = _TCP
                except Exception as e2:
                    return False, [], f"API_ERR: import TCPInterface: {e2 or err_import}"

            # Crear interfaz efímera
            iface = TCPInterface(hostname=host, noProto=False)
            release = lambda: getattr(iface, "close", lambda: None)()

        # Esperar a que se pueble iface.nodes (hasta timeout)
        t0 = time.time()
        while (time.time() - t0) < timeout and not getattr(iface, "nodes", None):
            time.sleep(0.2)

        nodes = getattr(iface, "nodes", {}) or {}
        lines: List[str] = []
        for nid, nd in nodes.items():
            user = (nd.get("user") or {}) if isinstance(nd, dict) else {}
            alias = user.get("longName") or user.get("shortName") or str(nid)
            lines.append(f"{nid};{alias}")

        # Cierre/liberación sin romper si no procede
        try:
            release and release()
        except Exception:
            pass

        if lines:
            return True, lines, "API_OK"
        return False, [], "API_EMPTY"

    except Exception as e:
        try:
            release and release()
        except Exception:
            pass
        return False, [], f"API_ERR: {e}"


# === MODIFICADA: _cli_list_nodes_basic usa retry robusto + pausa broker ===
def _cli_list_nodes_basic(host: str, timeout_sec: int = 20) -> Tuple[bool, List[str], str]:
    """
    Llama a la CLI para listar nodos, pero:
      - Pausa el broker mientras corre la CLI (evita colisiones con la TCP del nodo).
      - Usa _run_cli_nodes_with_retry() que primero prueba 'python -m meshtastic'
        y luego el entry-point 'meshtastic' (más fiable en Windows).
    Devuelve (ok, lines, reason) donde lines = ['<id>;<alias>'].
    """
    try:
        # 1) Pausar el broker para liberar la conexión al nodo
        with with_broker_paused(max_wait_s=8.0):
            ok, raw_lines, reason = _run_cli_nodes_with_retry(
                host=host,
                attempts=2,
                first_timeout=timeout_sec,
                backoff_sec=2
            )
    except Exception as e:
        return False, [], f"CLI_ERR: {type(e).__name__}: {e}"

    if not ok or not raw_lines:
        return False, [], f"CLI_ERR: {reason or 'unknown'}"

    # 2) Normalizar salida a '<id>;<alias>'
    #    _run_cli_nodes_with_retry devuelve líneas crudas; las pasamos por el parser
    norm_lines = _parse_nodes_cli_to_lines("\n".join(raw_lines))
    out: List[str] = []
    for ln in norm_lines:
        # Formatos posibles:
        #   a) "<id>\t<alias>\t<mins>\t<hops>"
        #   b) línea JSON normalizada de la CLI ya convertida por el parser
        parts = [p.strip() for p in ln.split("\t")]
        if len(parts) >= 2:
            nid, alias = parts[0], parts[1] or parts[0]
            if nid:
                out.append(f"{nid};{alias}")
        else:
            # Fallback muy defensivo: si solo hay un token, lo usamos como id y alias igual
            token = (parts[0] if parts else "").strip()
            if token:
                out.append(f"{token};{token}")

    if not out:
        return False, [], "CLI_EMPTY"

    return True, out, "CLI_OK"

async def get_nodes_api_first_then_cli(host: str) -> Tuple[str, List[str], str]:
    """
    Flujo API -> CLI. Devuelve (source, lines, reason).
    - source in {'API','CLI','NONE'}
    - Si hay datos, se graban en nodos.txt
    - Logs por consola con print/log()
    """
    # Log inicial (usa tu log si lo tienes)
    try:
        log("📡 /ver_nodos: Intentando API primero…")
    except Exception:
        print("📡 /ver_nodos: Intentando API primero…")

    ok_api, api_lines, api_reason = _api_list_nodes_basic(host, timeout=8.0)
    if ok_api and api_lines:
        try:
            log(f"✅ API devolvió {len(api_lines)} nodos.")
        except Exception:
            print(f"✅ API devolvió {len(api_lines)} nodos.")
        # Guardar nodos.txt
        _save_nodes_txt(api_lines)
        return "API", api_lines, "OK"

    try:
        log(f"⚠️ API no disponible ({api_reason}). Probando CLI…")
    except Exception:
        print(f"⚠️ API no disponible ({api_reason}). Probando CLI…")

    ok_cli, cli_lines, cli_reason = _cli_list_nodes_basic(host, timeout_sec=20)
    if ok_cli and cli_lines:
        try:
            log(f"✅ CLI devolvió {len(cli_lines)} nodos.")
        except Exception:
            print(f"✅ CLI devolvió {len(cli_lines)} nodos.")
        # Guardar nodos.txt
        _save_nodes_txt(cli_lines)
        return "CLI", cli_lines, "OK"

    try:
        log(f"❌ Sin datos. API={api_reason} • CLI={cli_reason}")
    except Exception:
        print(f"❌ Sin datos. API={api_reason} • CLI={cli_reason}")
    return "NONE", [], f"API={api_reason} • CLI={cli_reason}"



def cli_nodes_allowed(context=None) -> bool:
    """
    Devuelve False si NO debemos usar la CLI para --nodes.
    Regla: si NODES_FORCE_API_ONLY=1 o hay cualquier escucha activa (en este chat u otro), NO CLI.
    """
    if NODES_FORCE_API_ONLY:
        return False
    try:
        if context:
            st = (context.chat_data.get("listen_state") or {})
            if bool(st.get("active")):
                return False
            if (context.bot_data.get("listen_active_count") or 0) > 0:
                return False
    except Exception:
        pass
    return True


# -------------------------
# LOG Y UTILIDADES
# -------------------------

# ===== [NUEVO] Helper para pedir backlog al broker =====

def fetch_backlog_from_broker(host: str,
                              backlog_port: int,
                              since_ts: int | None,
                              channel: int | None,
                              limit: int = 1000,
                              timeout: float = 10.0) -> dict:
    """
    Solicita al broker (BacklogServer) los mensajes TEXT_MESSAGE_APP desde 'since_ts'
    y opcionalmente filtrados por 'channel' (None = todos).
    Devuelve dict: {"ok": True, "data": [ ... ]} o {"ok": False, "error": "..."}.
    """
    req = {
        "cmd": "FETCH_BACKLOG",
        "params": {
            "since_ts": since_ts,
            "until_ts": int(time.time()),
            "channel": channel,
            "portnums": ["TEXT_MESSAGE_APP"],
            "limit": int(limit)
        }
    }

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, backlog_port))
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))

        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)

        raw = b"".join(chunks).decode("utf-8", "ignore").strip()
        if not raw:
            return {"ok": False, "error": "empty response"}

        # El BacklogServer responde una línea JSON
        try:
            return json.loads(raw.splitlines()[-1])
        except Exception:
            return {"ok": False, "error": "invalid json", "raw": raw}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _fmt_db(val, unit):
    try:
        return f"{float(val):.1f} {unit}"
    except Exception:
        return "¿?"

def _link_quality(rssi_dbm, snr_db):
    """
    Heurística simple basada en RSSI/SNR LoRa. Devuelve (emoji, etiqueta).
    Ajusta umbrales si tu red lo requiere.
    """
    r = None if rssi_dbm is None else float(rssi_dbm)
    s = None if snr_db is None else float(snr_db)
    if r is None and s is None:
        return "⚪", "Desconocida"
    if r is None: r = -120.0
    if s is None: s = -20.0
    if s >= 10 and r >= -90:
        return "🟢", "Excelente"
    if (6 <= s < 10) or (-100 <= r < -90):
        return "🟢", "Buena"
    if (3 <= s < 6) or (-110 <= r < -100):
        return "🟠", "Regular"
    return "🔴", "Mala"

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8", errors="ignore") as f:
            f.write(line + "\n")
    except Exception:
        pass

def chunk_text(s: str, limit: int = TELEGRAM_MAX_CHARS) -> List[str]:
    if len(s) <= limit:
        return [s]
    return [s[i:i+limit] for i in range(0, len(s), limit)]

def write_file_safely(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="ignore")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def send_pre(message, text: str) -> None:
    await message.reply_text(f"<pre>{escape(text)}</pre>", parse_mode="HTML")

# -------------------------
# ESTADÍSTICAS SENCILLAS
# -------------------------

def load_stats() -> Dict[str, Any]:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"users": {}, "counts": {}}

def save_stats(stats: Dict[str, Any]) -> None:
    try:
        STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"❗ No se pudo guardar STATS: {e}")

def bump_stat(user_id: int, username: str, command: str) -> None:
    stats = load_stats()
    users = stats.setdefault("users", {})
    counts = stats.setdefault("counts", {})
    u = users.setdefault(str(user_id), {"username": username or "", "last_used": ""})
    u["username"] = username or u.get("username", "")
    u["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
    counts[command] = counts.get(command, 0) + 1
    save_stats(stats)

# -------------------------
# CAPA CLI (fallback)
# -------------------------

def run_command(args: List[str], timeout: int = TIMEOUT_CMD_S) -> str:
    exe = MESHTASTIC_EXE or "meshtastic"
    cmd = [exe] + args
    log(f"💻 Ejecutando: {shlex.join(cmd)}")
    try:
        import subprocess
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, text=True, encoding="utf-8", errors="ignore"
        )
        out = (result.stdout or "").strip()
        if not out:
            out = f"(sin salida) rc={result.returncode}"
        return out
    except subprocess.TimeoutExpired:
        return "⏱ Tiempo excedido ejecutando CLI Meshtastic"
    except FileNotFoundError:
        return f"❗ No se encontró el ejecutable '{exe}'. Ajusta MESHTASTIC_EXE o PATH."
    except Exception as e:
        return f"❗ Error ejecutando CLI: {e}"

# -------------------------
# RELAY opcional
# -------------------------

RELAY = None
def _try_import_relay() -> None:
    global RELAY
    if RELAY is not None:
        return
    try:
        if str(Path.cwd()) not in sys.path:
            sys.path.insert(0, str(Path.cwd()))
        import Meshtastic_Relay_API as relay  # noqa
        RELAY = relay
        log("🔗 Meshtastic_Relay_API importado correctamente (modo preferente).")
    except Exception as e:
        RELAY = None
        log(f"ℹ️ Meshtastic_Relay_API no disponible, usaré CLI. Detalle: {e}")

def _relay_has(*names: str) -> Optional[str]:
    if RELAY is None:
        return None
    for n in names:
        if hasattr(RELAY, n):
            return n
    return None

# -------------------------
# API: NODOS, TRACEROUTE, TELEMETRÍA, ENVÍO
# -------------------------
import os, time

# ====== NUEVA: asegura directorio bot_data y fichero nodos.txt ======
def ensure_nodes_path_exists() -> None:
    try:
        dirpath = os.path.dirname(NODES_FILE)
        if dirpath and not os.path.isdir(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        if not os.path.exists(NODES_FILE):
            # Crear fichero vacío; la sync real vendrá después
            with open(NODES_FILE, "w", encoding="utf-8") as f:
                f.write("")
    except Exception as e:
        log(f"⚠️ No se pudo preparar NODES_FILE: {e}")

# ====== NUEVA: refresca nodos si el fichero está vacío/antiguo ======

def ensure_nodes_file_fresh(max_age_s: int = 300, max_rows: int = 50, force_if_empty: bool = True) -> None:
    """
    Asegura que NODES_FILE existe y tiene datos recientes.
    - Refresca si no existe, si es más viejo que max_age_s o si está vacío (cuando force_if_empty=True).
    """
    need_refresh = False
    try:
        st = os.stat(NODES_FILE)
        age = time.time() - st.st_mtime
        if age > max_age_s:
            need_refresh = True
        elif force_if_empty:
            try:
                rows = _parse_nodes_table(NODES_FILE)
                if not rows:
                    need_refresh = True
            except Exception:
                need_refresh = True
    except FileNotFoundError:
        need_refresh = True

    if need_refresh:
        try:
            sync_nodes_and_save(max_rows)
        except Exception as e:
            log(f"⚠️ No se pudo refrescar nodos por CLI: {e}")


from pathlib import Path  # Asegúrate de tener este import al inicio del archivo

def sync_nodes_and_save(n_max: int = 20) -> None:
    """
    Sincroniza nodos vía CLI 'meshtastic --host ... --nodes' y guarda el resultado crudo en NODES_FILE.
    Durante la ejecución de la CLI, PAUSA el broker como en traceroute_cmd usando 'with_broker_paused(...)'.
    """
    args = ["--host", MESHTASTIC_HOST, "--nodes"]

    try:
        # ⏸️ Pausar broker IO (idéntico espíritu a traceroute_cmd)
        with with_broker_paused(max_wait_s=4.0):
            out = run_command(args)  # tu wrapper existente para ejecutar la CLI
    except Exception as e:
        log(f"⚠️ CLI --nodes falló: {e}")
        return

    # --- NUEVO: proteger nodos.txt ante timeout o salida sin datos ---
    try:
        text = (out or "").strip()
        # Caso 1: timeout explícito de la CLI
        if "Tiempo excedido ejecutando CLI Meshtastic" in text:
            log("⚠️ CLI --nodes: timeout; se conserva NODES_FILE existente (no se sobrescribe).")
            return

        # Caso 2: salida vacía o casi vacía → no tiene sentido borrar nodos previos
        if not text:
            log("⚠️ CLI --nodes devolvió salida vacía; se conserva NODES_FILE existente.")
            return
    except Exception as e:
        # Si algo raro pasa al analizar la salida, mejor no tocar el fichero
        log(f"⚠️ Error analizando salida de CLI --nodes; se conserva NODES_FILE. Detalle: {e}")
        return


    try:
        # Evitar error "'str' object has no attribute 'parent'": pasar Path
        write_file_safely(Path(NODES_FILE), out)
    except Exception as e:
        log(f"⚠️ No se pudo escribir NODES_FILE: {e}")


def load_nodes_with_hops(n_max: int = 20) -> List[Tuple[str, str, int, Optional[int]]]:
    """
    Devuelve [(id, alias, mins, hops)] ordenados por 'mins' asc.
    - Con DISABLE_BOT_TCP=1: NO usa API, solo lee nodos.txt (sin abrir sockets).
    - Si DISABLE_BOT_TCP=0: API-first y enriquece hops con nodos.txt (como antes).
    """

    # === MODO SIN TCP DESDE BOT: solo fichero ===
    if DISABLE_BOT_TCP:
        out: List[Tuple[str, str, int, Optional[int]]] = []
        try:
            rows_file = _parse_nodes_table(NODES_FILE)
            for r in rows_file:
                nid = (r.get("id") or "").strip()
                if not nid:
                    continue
                ali = (r.get("alias") or "").strip() or nid
                # minutos “last seen”
                mins = None
                for k in ("mins", "last_heard_min", "lastSeenMin", "last_seen_min"):
                    v = r.get(k)
                    if v is not None:
                        try:
                            mins = int(float(str(v)))
                            break
                        except Exception:
                            pass
                if mins is None:
                    mins = 9_999
                # hops
                hops = None
                for k in ("hops", "hops_text"):
                    v = r.get(k)
                    if v is not None:
                        try:
                            hv = int(float(str(v)))
                            hops = hv
                            break
                        except Exception:
                            pass
                out.append((nid, ali, mins, hops))
        except Exception as e:
            log(f"⚠️ Fallback NODES_FILE falló: {e}")

        out.sort(key=lambda x: x[2])
        return out[:n_max]

    # === MODO API-FIRST (solo si no está desactivado) ===
    log("📡 Intentando obtener nodos vía API…")
    rows = api_list_nodes(MESHTASTIC_HOST, max_n=max(50, n_max)) or []
    out: List[Tuple[str, str, int, Optional[int]]] = []
    for r in rows[:n_max]:
        mins = r.get("last_heard_min")
        out.append((
            r["id"],
            r.get("alias") or r["id"],
            mins if mins is not None else 9_999,
            r.get("hops")  # puede venir None
        ))

    if out:
        log(f"✅ API devolvió {len(out)} nodos.")
        # Guardar nodos.txt y enriquecer hops desde fichero (idéntico a tu versión actual)
        try:
            with open(NODES_FILE, "w", encoding="utf-8") as f:
                for nid, alias, mins, hops in out:
                    f.write(f"{nid}\t{alias}\t{mins} min\t{hops or '?'} hops\n")
        except Exception as e:
            log(f"⚠️ No se pudo escribir nodos.txt desde API: {e}")

        try:
            rows_file = _parse_nodes_table(NODES_FILE)
            hops_map: Dict[str, int] = {}
            for rf in rows_file:
                nid = (rf.get("id") or "").strip()
                if not nid:
                    continue
                hv = None
                if rf.get("hops") is not None:
                    try:
                        hv = int(float(str(rf.get("hops"))))
                    except Exception:
                        pass
                if hv is None and rf.get("hops_text") is not None:
                    try:
                        hv = int(float(str(rf.get("hops_text"))))
                    except Exception:
                        pass
                if hv is not None:
                    hops_map[nid] = hv

            out = [
                (nid, alias, mins, hops if hops is not None else hops_map.get(nid))
                for (nid, alias, mins, hops) in out
            ]
        except Exception as e:
            log(f"⚠️ Enriquecimiento de hops desde NODES_FILE falló: {e}")

        out.sort(key=lambda x: x[2])
        return out[:n_max]

    # 2) Fallback a fichero si la API vino vacía
    out2: List[Tuple[str, str, int, Optional[int]]] = []
    try:
        rows_file = _parse_nodes_table(NODES_FILE)
        for r in rows_file:
            nid = (r.get("id") or "").strip()
            if not nid:
                continue
            ali = (r.get("alias") or "").strip() or nid
            mins = None
            for k in ("mins", "last_heard_min", "lastSeenMin", "last_seen_min"):
                v = r.get(k)
                if v is not None:
                    try:
                        mins = int(float(str(v)))
                        break
                    except Exception:
                        pass
            if mins is None:
                mins = 9_999

            hops = None
            for k in ("hops", "hops_text"):
                v = r.get(k)
                if v is not None:
                    try:
                        h = int(float(str(v)))
                        hops = h
                        break
                    except Exception:
                        pass

            out2.append((nid, ali, mins, hops))
    except Exception as e:
        log(f"⚠️ Fallback NODES_FILE falló: {e}")

    out2.sort(key=lambda x: x[2])
    return out2[:n_max]

# === NUEVA ===
def load_nodes_with_hops_api_only(n_max: int = 20) -> List[Tuple[str, str, int, Optional[int]]]:
    """
    Igual que load_nodes_with_hops(), pero usa SOLO la API (sin CLI).
    Si la API no trae nada, cae a leer el fichero NODES_FILE (sin refrescarlo).
    """
    from meshtastic_api_adapter import api_list_nodes_api_only
    out: List[Tuple[str, str, int, Optional[int]]] = []

    # 1) API-only
    rows = api_list_nodes_api_only(MESHTASTIC_HOST, max_n=max(50, n_max)) or []
    for r in rows[:n_max]:
        mins = r.get("last_heard_min")
        out.append((r["id"], r.get("alias") or r["id"], mins if mins is not None else 9_999, r.get("hops")))

    if out:
        # Enriquecer hops con fichero (si existe), sin refrescarlo
        try:
            rows_file = _parse_nodes_table(NODES_FILE)
            hops_map: Dict[str, int] = {}
            for rf in rows_file:
                nid = (rf.get("id") or "").strip()
                if not nid:
                    continue
                hv = None
                if rf.get("hops") is not None:
                    hv = _to_int_safe(str(rf.get("hops")))
                if hv is None and rf.get("hops_text") is not None:
                    hv = _to_int_safe(str(rf.get("hops_text")))
                if hv is not None:
                    hops_map[nid] = hv
            out = [(nid, alias, mins, hops if hops is not None else hops_map.get(nid))
                   for (nid, alias, mins, hops) in out]
        except Exception as e:
            log(f"⚠️ Enriquecimiento de hops desde NODES_FILE (API-only) falló: {e}")

        out.sort(key=lambda x: x[2])
        return out[:n_max]

    # 2) Fallback a fichero SIN refrescar (nunca CLI aquí)
    try:
        rows_file = _parse_nodes_table(NODES_FILE)
        for r in rows_file:
            nid = (r.get("id") or "").strip()
            if not nid:
                continue
            ali = (r.get("alias") or "").strip() or nid
            mins = None
            for k in ("mins", "last_heard_min", "lastSeenMin", "last_seen_min"):
                v = r.get(k)
                if v is not None:
                    mins = _to_int_safe(str(v))
                    if mins is not None:
                        break
            if mins is None:
                mins = 9_999

            hops = None
            for k in ("hops", "hops_text"):
                v = r.get(k)
                if v is not None:
                    h = _to_int_safe(str(v))
                    if h is not None:
                        hops = h
                        break

            out.append((nid, ali, mins, hops))
    except Exception as e:
        log(f"⚠️ Fallback NODES_FILE (API-only) falló: {e}")

    out.sort(key=lambda x: x[2])
    return out[:n_max]


def build_nodes_mapping(n_max: int = 50) -> Dict[str, str]:
    nodes = load_nodes_with_hops(n_max)
    mapping: Dict[str, str] = {}
    for i, (nid, alias, _m, _h) in enumerate(nodes, start=1):
        mapping[str(i)] = nid
        mapping[nid] = nid
        if alias:
            mapping[alias.lower()] = nid
    try:
        alias_dict = cargar_aliases_desde_nodes(str(NODES_FILE))
        for nid, ali in alias_dict.items():
            if ali:
                mapping[ali.lower()] = nid
    except Exception:
        pass
    return mapping


@dataclass
class TraceResult:
    ok: bool
    hops: int
    route: List[str] = field(default_factory=list)
    raw: str = ""

def parse_traceroute_output(out: str) -> TraceResult:
    """
    Parsea la salida del CLI de meshtastic --traceroute admitiendo flechas
    '->', '→' y también '-->' por compatibilidad con logs antiguos.
    Devuelve:
      - ok: True si parece que hubo ruta o mensaje de 'Route traced'
      - hops: número de saltos (len(route) - 1)
      - route: lista con !IDs (si se pueden extraer) o fragmentos crudos
      - raw: salida original recortada
    """
    raw = out.strip()

    # 1) Normalizar flechas a '->' (acepta '→' y '-->')
    normalized = out.replace("→", "->").replace("-->", "->")

    # 2) Señales de éxito: flechas o texto "Route traced"
    has_arrow = "->" in normalized
    ok = ("Route traced" in normalized) or has_arrow

    route: List[str] = []
    hops = 0

    if has_arrow:
        # Split robusto por flecha con posibles espacios
        parts = [p.strip() for p in re.split(r"\s*->\s*", normalized) if p.strip()]
        # Intentar extraer !IDs por hop; si no hay, usar el texto del hop
        extracted_ids: List[str] = []
        for p in parts:
            m = re.search(r"!?[0-9a-fA-F]{8}", p)
            if m:
                extracted_ids.append(m.group(0))
        route = extracted_ids if extracted_ids else parts
        hops = max(0, len(route) - 1)

    elif ok:
        # Formato "Route traced: !aaaa -> !bbbb ..." (sin flecha capturada)
        ids = re.findall(r"!?[0-9a-fA-F]{8}", normalized)
        if ids:
            route = ids
            hops = max(0, len(ids) - 1)

    return TraceResult(ok=ok, hops=hops, route=route, raw=raw)


def traceroute_node_old(node_id: str, timeout: int = TRACEROUTE_TIMEOUT) -> TraceResult:
    _try_import_relay()
    fn = _relay_has("check_route_detallado")
    if fn:
        try:
            estado, hops, path, raw = getattr(RELAY, fn)(node_id)
            ok = "✔" in str(estado)
            return TraceResult(ok=ok, hops=int(hops), route=list(path), raw=str(raw))
        except Exception as e:
            log(f"⚠️ traceroute via relay falló: {e}. Probando API…")
    res = api_traceroute(MESHTASTIC_HOST, node_id, timeout=timeout)
    return TraceResult(ok=bool(res["ok"]), hops=int(res["hops"]), route=list(res["route"]), raw=str(res["raw"]))

def traceroute_node(node_id: str, timeout: int = TRACEROUTE_TIMEOUT) -> TraceResult:
    """
    Traceroute SOLO por API usando la interfaz persistente del pool TCP.
    Sin RELAY y sin CLI. Devuelve TraceResult(ok, hops, route, raw).
    """
    from tcpinterface_persistent import TCPInterfacePool as _Pool
    import inspect, re

    dest = (node_id or "").strip()
    if not dest:
        return TraceResult(ok=False, hops=0, route=[], raw="dest_id vacío")

    host = MESHTASTIC_HOST
    port = 4403

    # 1) Obtener iface del pool (sin abrir sockets nuevos si ya está)
    iface = None
    try:
        if hasattr(_Pool, "get_iface_wait"):
            iface = _Pool.get_iface_wait(timeout=min(float(timeout), 4.0), interval=0.3)
        else:
            # compat: get() + ensure_connected() si existe
            try:
                iface = _Pool.get(host, port)
            except Exception:
                iface = None
            ensure_fn = getattr(_Pool, "ensure_connected", None)
            if (iface is None) and callable(ensure_fn):
                try:
                    ensure_fn(host, port, timeout=min(float(timeout), 4.0))
                    iface = _Pool.get(host, port)
                except Exception:
                    iface = None
    except Exception as e:
        return TraceResult(ok=False, hops=0, route=[], raw=f"no_iface: {e}")

    if iface is None:
        return TraceResult(ok=False, hops=0, route=[], raw="no_iface")

    # 2) Ejecutar traceroute probando firmas típicas de la API
    def _do_tr_with_iface(iface_obj, did: str) -> TraceResult:
        candidates = [
            ("traceroute",     {"node_id": did, "timeout": timeout}),
            ("traceroute",     {"dest_id": did, "timeout": timeout}),
            ("traceroute",     {"id": did,      "timeout": timeout}),
            ("sendTraceroute", {"dest_id": did, "timeout": timeout}),
            ("tracerouteNode", {"dest_id": did, "timeout": timeout}),
        ]
        last_err = None
        for name, proposed_kwargs in candidates:
            fn = getattr(iface_obj, name, None)
            if not callable(fn):
                continue
            try:
                # Filtrar kwargs a la firma real para evitar TypeError
                kwargs = proposed_kwargs
                try:
                    sig = inspect.signature(fn)
                    accepted = set(sig.parameters.keys())
                    kwargs = {k: v for k, v in proposed_kwargs.items() if k in accepted}
                except Exception:
                    pass

                res = fn(**kwargs) if kwargs else fn(did)

                # Normalizar resultado
                hops, route = None, None
                if isinstance(res, dict):
                    hops  = res.get("hops") or res.get("hopCount")
                    route = res.get("path") or res.get("route") or res.get("nodes")
                elif isinstance(res, (list, tuple)):
                    route = list(res)
                    hops  = (len(route) - 1) if route else 0
                elif isinstance(res, str):
                    # Si es string, intenta parseo (!ids en el texto)
                    ids = re.findall(r"![0-9a-fA-F]{8}", res)
                    if ids:
                        route = [i.strip() for i in ids]
                        hops  = max(0, len(route) - 1)

                if route and isinstance(route, list):
                    route = [str(x) for x in route]
                if hops is None and route:
                    hops = max(0, len(route) - 1)

                ok = bool(route and len(route) >= 2) or (hops is not None)
                return TraceResult(ok=bool(ok), hops=int(hops or 0), route=route or [], raw=str(res))
            except Exception as e:
                last_err = e
                continue

        return TraceResult(ok=False, hops=0, route=[], raw=f"API traceroute no disponible: {last_err}")

    # 3) Ejecutar con la iface del pool
    return _do_tr_with_iface(iface, dest)



def send_text_message(node_id: Optional[str], text: str, canal: int = 0) -> tuple[str, Optional[int]]:
    """
    MODIFICADA: usa send_text_simple_with_retry_resilient() para reconectar el pool TCP
    si el primer intento falla por socket/timeout y reintenta 1 vez.
    Devuelve (texto_resultado, packet_id|None); añade etiqueta '+reconnect' si ocurrió.
    """
    try:
        # Preferimos la versión resiliente; si no está disponible aún, caemos a la original.
        try:
            from meshtastic_api_adapter import send_text_simple_with_retry_resilient as _send
        except ImportError:
            from meshtastic_api_adapter import send_text_simple_with_retry as _send

        res = _send(
            host=MESHTASTIC_HOST,
            port=4403,
            text=text,
            dest_id=(node_id or None),
            channel_index=int(canal),
            want_ack=False
        )

        pid = None
        if isinstance(res, dict):
            pid = res.get("packet_id")
        pid = int(pid) if pid is not None else None

        if isinstance(res, dict) and res.get("ok"):
            tag = "API-pool+retry"
            if res.get("reconnected"):
                tag += "+reconnect"
            msg = f"OK ({tag}){f' • packet_id={pid}' if pid else ''}"
            return msg, pid

        # KO: intenta mostrar causa y si hubo reconexión
        err = ""
        if isinstance(res, dict):
            err = res.get("error") or ""
        tag = " (tras reconexión)" if (isinstance(res, dict) and res.get("reconnected")) else ""
        return (f"KO{tag}: {err or str(res)}", pid)

    except Exception as e:
        return f"KO: {e}", None



# === NUEVO: adapter de envío para broker_tasks (CORREGIDO: usa iface del broker) ===
def _tasks_send_adapter(channel: int, message: str, destination: str, require_ack: bool) -> dict:
    """
    1) Intentar enviar por la MISMA conexión TCP del broker (iface_mgr) para no abrir 2 sesiones al nodo.
    2) Si no es posible (no iniciado / no conectado / error), caer al adapter resiliente.
    Devuelve: {ok: bool, packet_id?: int, error?: str}
    """
    import time

    # Normalizar destino: None/"broadcast" => broadcast real (destinationId=None), "!id" => unicast
    dest_id = None if (not destination or str(destination).lower() == "broadcast") else str(destination)

    # 1) Preferente: usar la interfaz activa del broker (BROKER_IFACE_MGR/pool)
    try:
        mgr = globals().get("BROKER_IFACE_MGR") or globals().get("IFACE_POOL") or globals().get("POOL")
        if mgr is not None:
            # Espera hasta 6s a que haya iface; si no, ensure_connected + reintento
            iface = None
            t_end = time.time() + 6.0
            while time.time() < t_end and iface is None:
                if hasattr(mgr, "get_iface"):
                    iface = mgr.get_iface()
                elif hasattr(mgr, "get_interface"):
                    iface = mgr.get_interface()
                else:
                    iface = getattr(mgr, "iface", None)
                if iface is None:
                    time.sleep(0.3)

            if iface is None:
                ensure_fn = getattr(mgr, "ensure_connected", None)
                host = globals().get("MESHTASTIC_HOST") or globals().get("RUNTIME_MESH_HOST")
                port = globals().get("MESHTASTIC_PORT") or globals().get("RUNTIME_MESH_PORT") or 4403
                if callable(ensure_fn) and host:
                    try:
                        ensure_fn(host, port, timeout=6.0)
                    except Exception:
                        pass
                # reintento de obtener iface
                if hasattr(mgr, "get_iface"):
                    iface = mgr.get_iface()
                elif hasattr(mgr, "get_interface"):
                    iface = mgr.get_interface()
                else:
                    iface = getattr(mgr, "iface", None)

            if iface is None:
                raise RuntimeError("iface no disponible (todavía no conectado)")

            # ⚠️ Broadcast correcto: destinationId=None (NO "^all")
            pkt = iface.sendText(
                message,
                destinationId=(dest_id if dest_id else None),
                wantAck=bool(require_ack and dest_id),   # ACK sólo tiene sentido en unicast
                wantResponse=False,
                channelIndex=int(channel),
            )

            # Extraer packet_id de dict u objeto
            pid = None
            if isinstance(pkt, dict):
                pid = pkt.get("id") or ((pkt.get("_packet") or {}).get("id"))
            else:
                pid = getattr(pkt, "id", None)
            try:
                pid = int(pid) if pid is not None else None
            except Exception:
                pid = None

            # Si se pide ACK unicast y la iface lo soporta, esperar confirmación
            if require_ack and dest_id and pid is not None and hasattr(iface, "waitForAck"):
                try:
                    ok_ack = bool(iface.waitForAck(pid, timeout=15.0))
                except Exception:
                    ok_ack = False
                return {"ok": ok_ack, "packet_id": pid, "error": (None if ok_ack else "NO_APP_ACK")}

            # Broadcast o sin ACK → OK con el envío
            return {"ok": True, "packet_id": pid, "error": None}

    except Exception:
        # seguimos al fallback
        pass

    # 2) Fallback: usar el adapter resiliente (abrirá conexión efímera si el broker no puede)
    try:
        try:
            from meshtastic_api_adapter import send_text_simple_with_retry_resilient as _send
        except Exception:
            from meshtastic_api_adapter import send_text_simple_with_retry as _send  # fallback

        host = globals().get("MESHTASTIC_HOST") or globals().get("RUNTIME_MESH_HOST") or "127.0.0.1"
        port = globals().get("MESHTASTIC_PORT") or globals().get("RUNTIME_MESH_PORT") or 4403

        res = _send(
            host=host,
            port=port,
            text=message,
            dest_id=dest_id,                 # aquí también: broadcast = None
            channel_index=int(channel),
            want_ack=bool(require_ack),
        )
        ok = bool(res.get("ok"))
        pid = res.get("packet_id")
        return {"ok": ok, "packet_id": pid, "error": (None if ok else res.get("error"))}
    except Exception as e:
        return {"ok": False, "packet_id": None, "error": f"{type(e).__name__}: {e}"}

# === NUEVO: adapter de reconexión para broker_tasks ===
def _tasks_reconnect_adapter() -> bool:
    """
    Intenta reabrir el pool TCP si el primer envío falla por timeout/socket.
    """
    try:
        from meshtastic_api_adapter import mesh_reconnect
        return bool(mesh_reconnect(host=MESHTASTIC_HOST, port=4403))
    except Exception:
        return False

async def _try_send_via_pool_iface_with_wait(
    pool_cls,
    text: str,
    dest_id: str | None,
    channel_index: int,
    want_ack: bool,
    timeout_wait_iface: float = 3.0,
) -> tuple[bool, int | None, str | None]:
    """
    Intenta enviar usando la interfaz persistente del pool, esperando hasta ~3s
    si el pool está reconectando. No abre nuevas sesiones.
    Devuelve (ok, packet_id, error).
    """
    try:
        iface = getattr(pool_cls, "get_iface_wait", None)
        if callable(iface):
            iface = pool_cls.get_iface_wait(timeout=timeout_wait_iface)
        else:
            # compat: intenta getters actuales + espera breve manual
            import time as _t  # <-- evitar sombreamiento de 'time' global
            iface = None
            for _ in range(10):
                if hasattr(pool_cls, "get_iface"):
                    iface = pool_cls.get_iface()
                elif hasattr(pool_cls, "get_interface"):
                    iface = pool_cls.get_interface()
                else:
                    iface = getattr(pool_cls, "iface", None)
                if iface is not None:
                    break
                _t.sleep(0.3)  # <-- usar el alias local

        if iface is None:
            return (False, None, "NO_IFACE")

        pkt = iface.sendText(
            text,
            destinationId=(dest_id or "^all"),
            wantAck=bool(want_ack),
            wantResponse=False,
            channelIndex=int(channel_index),
        )
        # packet_id robusto
        pid = None
        if isinstance(pkt, dict):
            pid = pkt.get("id") or ((pkt.get("_packet") or {}).get("id"))
        else:
            pid = getattr(pkt, "id", None)
        try:
            pid = int(pid) if pid is not None else None
        except Exception:
            pid = None

        if want_ack and dest_id and pid is not None and hasattr(iface, "waitForAck"):
            try:
                ok_ack = bool(iface.waitForAck(pid, timeout=15.0))
            except Exception:
                ok_ack = False
            return (ok_ack, pid, (None if ok_ack else "NO_APP_ACK"))

        return (True, pid, None)
    except Exception as e:
        return (False, None, f"{type(e).__name__}: {e}")

# -------------------------
# ENVÍO CON ACK
# -------------------------

async def _wait_ack_from_broker(packet_id: int, seconds: int) -> Tuple[bool, str, Optional[str]]:
    if not BROKER_HOST or not BROKER_PORT or seconds <= 0:
        return False, "BROKER_OFF", None
    try:
        reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
    except Exception as e:
        log(f"⚠️ No se pudo conectar al broker para ACK: {e}")
        return False, "BROKER_CONNECT_FAIL", None

    key_candidates = ("requestId", "request_id", "original_id", "originalId", "id")
    reason_keys = ("errorReason", "error_reason")

    end_ts = time.time() + seconds
    try:
        while time.time() < end_ts:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if obj.get("type") != "packet":
                continue

            pkt = obj.get("packet", {}) or {}
            dec = pkt.get("decoded", {}) or {}
            if dec.get("portnum") != "ROUTING_APP":
                continue

            routing = dec.get("routing", {}) or {}

            # referencia al paquete enviado
            ref_id = None
            for k in key_candidates:
                if k in routing:
                    ref_id = routing.get(k); break
            if ref_id is None:
                continue
            try:
                if str(int(ref_id)) != str(int(packet_id)):
                    continue
            except Exception:
                if str(ref_id) != str(packet_id):
                    continue

            # quién envía la confirmación
            hdr = dec.get("header", {}) or {}
            ack_from = hdr.get("fromId") or pkt.get("fromId") or None

            # motivo
            reason = "NONE"
            for rk in reason_keys:
                if rk in routing:
                    reason = str(routing.get(rk) or "NONE"); break

            ack_ok = reason.upper() == "NONE"
            return ack_ok, reason, ack_from
    except Exception as e:
        log(f"⚠️ Error esperando ACK: {e}")
    finally:
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass

    return False, "TIMEOUT", None


async def _wait_ack_any(iface, packet_id: int, seconds: int) -> Tuple[bool, str]:
    async def _wait_lib():
        fn = getattr(iface, "waitForAck", None)
        if callable(fn):
            try:
                ok = await asyncio.to_thread(fn, packet_id, seconds)
                return bool(ok), "LIB_WAITFORACK"
            except Exception:
                return False, "LIB_ERROR"
        return False, "LIB_UNAVAILABLE"

    async def _wait_broker():
        ok, reason = await _wait_ack_from_broker(packet_id, seconds)
        return ok, reason or "BROKER"

    t1 = asyncio.create_task(_wait_lib())
    t2 = asyncio.create_task(_wait_broker())
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED, timeout=seconds)

    ok, reason = False, "TIMEOUT"
    for t in done:
        try:
            res_ok, res_reason = await t
            if res_ok:
                ok, reason = True, res_reason
                break
            else:
                reason = res_reason
        except Exception:
            pass

    for t in pending:
        t.cancel()

    return ok, reason

async def send_with_ack_retry(node_id: str | None,
                              texto: str,
                              canal: int | None,
                              attempts: int,
                              wait_s: float,
                              backoff: float):
    """
    Envío con ACK:
    - Unicast: reintentos con espera combinada (lib+broker) para reducir duplicados.
    - Broadcast: (ACK no existe) se envía una sola vez (evitar duplicados inútiles).
    """
    dest_id = None if (node_id is None or str(node_id).lower() == "broadcast") else node_id
    canal = int(canal if canal is not None else BROKER_CHANNEL)
    host = MESHTASTIC_HOST

    if dest_id is None and attempts < 1:
        attempts = 1

    last_reason = ""
    last_packet_id = None

    for i in range(1, max(1, attempts) + 1):
        try:
            iface = TCPInterfacePool.get(host)
            pkt = await asyncio.to_thread(
                iface.sendText,
                texto,
                destinationId=(dest_id or "^all"),
                wantAck=True,
                wantResponse=False,
                channelIndex=canal
            )

            pid = None
            if isinstance(pkt, dict):
                pid = pkt.get("id") or (pkt.get("_packet", {}) or {}).get("id")
            else:
                pid = getattr(pkt, "id", None)
            last_packet_id = None if pid is None else int(pid)

            if last_packet_id is not None and dest_id is not None:
                ack_ok, reason = await _wait_ack_any(iface, last_packet_id, int(wait_s))
                if ack_ok:
                    return {"ok": True, "attempts": i, "packet_id": last_packet_id}
                last_reason = reason or "timeout"
            else:
                last_reason = "NO_PACKET_ID" if dest_id is not None else "BROADCAST_NO_ACK"

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            last_reason = type(e).__name__
            await asyncio.sleep(0.5)

        if dest_id is not None and i < attempts:
            delay = float(wait_s) * (float(backoff) ** (i - 1))
            await asyncio.sleep(delay)

    return {"ok": False, "attempts": attempts, "packet_id": last_packet_id, "reason": last_reason or "unknown"}

# -------------------------
# RESOLUCIÓN DESTINO+CANAL
# -------------------------

# Reemplaza COMPLETAMENTE esta función en Telegram_Bot_Broker_API_v4.3.py

DEST_PAT_ID_CH    = re.compile(r"^(?P<dest>![0-9a-fA-F]{8}|broadcast)(?::(?P<ch>\d+))?$", re.I)
DEST_PAT_ALIAS_CH = re.compile(r"^(?P<alias>[a-zA-Z0-9_\-\. ]+):(?P<ch>\d+)$")

def parse_dest_channel_and_text(args: List[str], nodes_map: Dict[str, str]) -> Tuple[Optional[str], int, str, bool]:
    """
    Soporta:
      - /enviar canal N <texto>                  -> broadcast implícito en canal N
      - /enviar broadcast[:N] <texto>            -> broadcast explícito
      - /enviar !id[:N] <texto>                  -> unicast explícito
      - /enviar <alias|#indice>[:N] <texto>      -> unicast por alias/índice
      - opcional 'forzado' como primer token

    CORREGIDO: si no se reconoce destino tras 'canal N', todo lo que quede se toma como texto
    (antes se descartaba la primera palabra por error).
    """
    canal = BROKER_CHANNEL
    forced = False

    toks = [t for t in (args or []) if t and t.strip()]
    if not toks:
        return None, canal, "", forced

    # 'forzado' al inicio
    if toks and toks[0].lower() == "forzado":
        forced = True
        toks = toks[1:] or []

    # 'canal N' al inicio
    if len(toks) >= 2 and toks[0].lower() == "canal":
        try:
            canal = int(toks[1])
        except Exception:
            pass
        toks = toks[2:]  # quitar 'canal' y el índice

    if not toks:
        # No hay destino ni texto
        return None, canal, "", forced

    dest_token = toks[0].strip()

    # 1) Formatos explícitos: '!id[:ch]' o 'broadcast[:ch]'
    m = DEST_PAT_ID_CH.match(dest_token)
    if m:
        d = m.group("dest")
        ch = m.group("ch")
        if ch is not None:
            try:
                canal = int(ch)
            except Exception:
                pass
        node_id = None if d.lower() == "broadcast" else d
        text = " ".join(toks[1:]).strip()
        return node_id, canal, text, forced

    # 2) '<alias>[:ch]' explícito
    m2 = DEST_PAT_ALIAS_CH.match(dest_token)
    if m2:
        alias = m2.group("alias").strip().lower()
        ch = m2.group("ch")
        if ch is not None:
            try:
                canal = int(ch)
            except Exception:
                pass
        node_id = nodes_map.get(alias, alias)
        if node_id and not node_id.startswith("!"):
            node_id = nodes_map.get(node_id, node_id)
        if node_id and node_id.startswith("!"):
            text = " ".join(toks[1:]).strip()
            return node_id, canal, text, forced
        # Si no resolvió a !id, caemos a heurística general (posible broadcast implícito)

    # 3) Heurística general: índice / !id / alias / broadcast literal
    key = dest_token
    node_id: Optional[str] = None

    if key.lower() == "broadcast":
        node_id = None
        text = " ".join(toks[1:]).strip()
        return node_id, canal, text, forced

    if key.isdigit():
        node_id = nodes_map.get(key, key)
        if node_id and not node_id.startswith("!"):
            node_id = nodes_map.get(str(node_id).lower(), node_id)
    elif key.startswith("!"):
        node_id = key
    else:
        node_id = nodes_map.get(key.lower())

    if node_id and node_id.startswith("!"):
        # Unicast reconocido -> el texto va SIN el token destino
        text = " ".join(toks[1:]).strip()
        return node_id, canal, text, forced

    # 4) Ningún destino reconocido -> BROADCAST IMPLÍCITO
    #    CORRECCIÓN: el texto es TODO 'toks' (no descartar la primera palabra)
    text = " ".join(toks).strip()
    return None, canal, text, forced



# -------------------------
# BROKER: MÉTRICAS Y ESCUCHA BREVE
# -------------------------

def _get(d: dict, path: str, default=None):
    cur = d
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

#29-08-2025 08:25 horas
def extract_hop_limit(pkt: dict) -> int | None:
    # Busca en varias rutas habituales
    for path in (
        "meta.hopLimit",
        "hop_limit",
        "raw.hop_limit",
        "rxMetadata.hopLimit",
        "decoded.header.hopLimit",
    ):
        v = _get(pkt, path)
        if isinstance(v, (int, float)):
            return int(v)
    # Fallback por claves directas
    for k in ("hop_limit", "hopLimit"):
        v = pkt.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None

def extract_hop_start(pkt: dict) -> int | None:
    for path in (
        "meta.hopStart",
        "hop_start",
        "raw.hop_start",
        "rxMetadata.hopStart",
        "decoded.header.hopStart",
    ):
        v = _get(pkt, path)
        if isinstance(v, (int, float)):
            return int(v)
    for k in ("hop_start", "hopStart"):
        v = pkt.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None

def extract_relay_node(pkt: dict) -> int | str | None:
    # Puede venir como int o string; devolvemos lo que haya
    for path in (
        "meta.relayNode",
        "relay_node",
        "raw.relay_node",
        "rxMetadata.relayNode",
        "decoded.header.relayNode",
        "decoded.relay_node",
    ):
        v = _get(pkt, path)
        if isinstance(v, (int, float, str)):
            return int(v) if isinstance(v, (int, float)) else str(v)
    for k in ("relay_node", "relayNode", "relay"):
        v = pkt.get(k)
        if isinstance(v, (int, float, str)):
            return int(v) if isinstance(v, (int, float)) else str(v)
    return None

def extract_rssi(pkt: dict) -> Optional[float]:
    v = _get(pkt, "meta.rxRssi")
    if isinstance(v, (int, float)): return float(v)
    for key in ("rssi", "rxRssi", "rx_rssi"):
        v = pkt.get(key)
        if isinstance(v, (int, float)): return float(v)
    v = _get(pkt, "raw.rx_rssi")
    if isinstance(v, (int, float)): return float(v)
    v = _get(pkt, "rxMetadata.rssi")
    if isinstance(v, (int, float)): return float(v)
    return None

def extract_snr(pkt: dict) -> Optional[float]:
    v = _get(pkt, "meta.rxSnr")
    if isinstance(v, (int, float)): return float(v)
    for key in ("snr", "rxSnr", "rx_snr"):
        v = pkt.get(key)
        if isinstance(v, (int, float)): return float(v)
    v = _get(pkt, "raw.rx_snr")
    if isinstance(v, (int, float)): return float(v)
    v = _get(pkt, "rxMetadata.snr")
    if isinstance(v, (int, float)): return float(v)
    return None

# ====== NUEVO: utilidades comunes para parseo y booleanos ======

def _get_any(d: dict, keys: list[str], default=None):
    for k in keys:
        v = _get(d, k, None) if "." in k else d.get(k)
        if v is not None:
            return v
    return default

def _to_bool(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes", "si", "sí"):
        return True
    if s in ("0", "false", "off", "no"):
        return False
    return None

# ====== NUEVO: parseo de TELEMETRY_APP ======
def parse_telemetry_fields(pkt: dict) -> dict:
    """
    Extrae campos típicos de TELEMETRY_APP desde distintas variantes de payload.
    Devuelve dict con claves estándar si las encuentra.
    """
    dec = pkt.get("decoded", {}) or {}
    data = dec.get("data", {}) or {}

    # Algunas builds meten datos métricos bajo distintos contenedores
    roots = [data, dec, pkt, data.get("deviceMetrics", {}) or {}, data.get("metrics", {}) or {}]

    def find(keys, cast=float):
        for r in roots:
            v = _get_any(r, keys, default=None)
            if v is None:
                continue
            try:
                return cast(v)
            except Exception:
                try:
                    return float(str(v).replace(",", "."))
                except Exception:
                    return v
        return None

    def fint(keys):
        v = find(keys, cast=float)
        try:
            return None if v is None else int(v)
        except Exception:
            return None

    out = {
        # Batería
        "battery_pct":  find(["battery_pct", "battery", "batteryLevel", "battery_percent", "batteryPercent", "data.batteryLevel"]),
        "battery_v":    find(["battery_v", "voltage", "bat_voltage", "batteryVoltage"]),
        # Ambiente
        "temp_c":       find(["temp_c", "temperature_c", "air_temperature", "temperature"]),
        "humidity_pct": find(["humidity_pct", "relative_humidity", "humidity"]),
        "pressure_hpa": find(["pressure_hpa", "barometric_pressure", "pressure"]),
        # Solar / carga
        "solar_v":      find(["solar_voltage", "solar_v", "panel_voltage", "v_solar"]),
        "charge_ma":    find(["charge_current", "charge_ma", "chargingCurrent"]),
        # Altitud/GPS (si viniera)
        "alt_m":        find(["altitude_m", "altitude"]),
        # Señal (por si viene aquí)
        "rssi":         extract_rssi(pkt),
        "snr":          extract_snr(pkt),
    }

    # Normalización básica: % y rangos
    if out["battery_pct"] is not None:
        try:
            bp = float(out["battery_pct"])
            if bp > 1.0:  # ya es %
                out["battery_pct"] = round(bp, 1)
            else:         # proporción
                out["battery_pct"] = round(bp * 100.0, 1)
        except Exception:
            pass

    return out

# ====== NUEVO: recolección detallada de TELEMETRY_APP durante una ventana ======
async def collect_telemetry_details(dest_id: Optional[str], channel: Optional[int], seconds: int = 15) -> list[dict]:
    """
    Escucha el broker y devuelve una lista de dicts con métricas parseadas
    para TELEMETRY_APP del dest_id y channel indicados (si se especifican).
    """
    out: list[dict] = []
    if not BROKER_HOST or not BROKER_PORT or seconds <= 0:
        return out

    try:
        reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
    except Exception as e:
        log(f"⚠️ No se pudo abrir socket al broker para telemetría detallada: {e}")
        return out

    end_ts = time.time() + seconds
    try:
        while time.time() < end_ts:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if obj.get("type") != "packet":
                continue

            pkt = obj.get("packet", {}) or {}
            dec = pkt.get("decoded", {}) or {}
            if dec.get("portnum") != "TELEMETRY_APP":
                continue

            # Filtrado por canal (si procede)
            ch = _extract_channel_index_from_packet(pkt)
            if channel is not None and isinstance(ch, int) and ch != channel:
                continue

            # Filtrado por origen (si procede)
            frm = _extract_from_id(pkt) or ""
            if dest_id and frm != dest_id:
                continue

            parsed = parse_telemetry_fields(pkt) or {}
            if parsed:
                # adjuntamos quién y canal
                parsed["from"] = frm or "¿?"
                parsed["channel"] = ch
                out.append(parsed)
    except Exception as e:
        log(f"⚠️ Error recolectando telemetría: {e}")
    finally:
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass

    return out


def _extract_channel_index_from_packet(pkt: Dict[str, Any]) -> Optional[int]:
    try:
        ch = pkt.get("meta", {}).get("channelIndex", None)
        if ch is not None:
            ci = _to_int_safe(str(ch))
            if ci is not None:
                return ci
    except Exception:
        pass
    try:
        ch = pkt.get("channel", None)
        if ch is not None:
            ci = _to_int_safe(str(ch))
            if ci is not None:
                return ci
    except Exception:
        pass
    try:
        rxm = pkt.get("rxMetadata", None)
        if isinstance(rxm, dict):
            ch = rxm.get("channel", None)
            if ch is not None:
                ci = _to_int_safe(str(ch))
                if ci is not None:
                    return ci
    except Exception:
        pass
    try:
        dec = pkt.get("decoded", None)
        if isinstance(dec, dict):
            ch = dec.get("channel", None)
            if ch is not None:
                ci = _to_int_safe(str(ch))
                if ci is not None:
                    return ci
            data = dec.get("data", None)
            if isinstance(data, dict):
                ch = data.get("channel", None)
                if ch is not None:
                    ci = _to_int_safe(str(ch))
                    if ci is not None:
                        return ci
            hdr = dec.get("header", None)
            if isinstance(hdr, dict):
                ch = hdr.get("channelIndex", None)
                if ch is not None:
                    ci = _to_int_safe(str(ch))
                    if ci is not None:
                        return ci
    except Exception:
        pass
    return None

async def collect_broker_metrics(seconds: float = METRICS_LISTEN_SEC,
                                only_channel: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    metrics: Dict[str, Dict[str, Any]] = {}
    if not BROKER_HOST or not BROKER_PORT or seconds <= 0:
        return metrics

    try:
        reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
    except Exception:
        return metrics

    end_ts = time.time() + float(seconds)
    try:
        while time.time() < end_ts:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if obj.get("type") != "packet":
                continue

            pkt = obj.get("packet", {}) or {}
            ch = _extract_channel_index_from_packet(pkt)

            if only_channel is not None and isinstance(ch, int) and ch != only_channel:
                continue

            frm = _extract_from_id(pkt) or ""
            if not (isinstance(frm, str) and frm.startswith("!")):
                continue

            rssi = extract_rssi(pkt)
            snr  = extract_snr(pkt)
            if rssi is None and snr is None:
                summ = obj.get("summary") or {}
                rssi = rssi if rssi is not None else summ.get("rssi")
                snr  = snr  if snr  is not None else summ.get("snr")

            if rssi is None and snr is None:
                continue

            cur = metrics.get(frm) or {}
            def _score(_rssi, _snr):
                s = -9999 if _snr is None else float(_snr)
                r = -9999 if _rssi is None else float(_rssi)
                return (s, r)

            if not cur or _score(rssi, snr) > _score(cur.get("rssi"), cur.get("snr")):
                metrics[frm] = {"rssi": rssi, "snr": snr, "ts": time.time(), "channel": ch}
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return metrics

async def quick_broker_listen_telemetry(dest_id: Optional[str],
                                        channel: Optional[int] = None,
                                        seconds: int = 10) -> Tuple[int, Dict[str, int]]:
    """
    Escucha breve del broker para contar respuestas TELEMETRY_APP.
    - Si dest_id está definido, cuenta solo TELEMETRY_APP cuyo fromId == dest_id.
    - Si channel está definido, filtra por ese channelIndex.
    Devuelve (total, por_tipo) donde por_tipo es {'TELEMETRY_APP': N} por ahora.
    """
    total = 0
    by_type: Dict[str, int] = {}

    if not BROKER_HOST or not BROKER_PORT or seconds <= 0:
        return total, by_type

    try:
        reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
    except Exception as e:
        log(f"⚠️ No se pudo conectar al broker para escucha de telemetría: {e}")
        return total, by_type

    end_ts = time.time() + seconds
    try:
        while time.time() < end_ts:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if obj.get("type") != "packet":
                continue

            pkt = obj.get("packet", {}) or {}
            dec = pkt.get("decoded", {}) or {}
            port = dec.get("portnum")

            # Solo TELEMETRY_APP
            if port != "TELEMETRY_APP":
                continue

            # Filtrado por canal (si procede)
            ch = _extract_channel_index_from_packet(pkt)
            if channel is not None and isinstance(ch, int) and ch != channel:
                continue

            # Filtrado por origen (si procede)
            frm = _extract_from_id(pkt) or ""
            if dest_id and frm != dest_id:
                continue

            total += 1
            by_type["TELEMETRY_APP"] = by_type.get("TELEMETRY_APP", 0) + 1

    except Exception as e:
        log(f"⚠️ Error en escucha puntual de TELEMETRY_APP: {e}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return total, by_type

async def quick_broker_listen(dest_id: Optional[str], channel: Optional[int], seconds: int) -> int:
    if not BROKER_HOST or not BROKER_PORT or seconds <= 0:
        return 0

    count = 0
    try:
        reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
    except Exception as e:
        log(f"⚠️ No se pudo conectar al broker para confirmación: {e}")
        return 0

    try:
        end_ts = time.time() + seconds
        while time.time() < end_ts:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if obj.get("type") != "packet":
                continue
            pkt = obj.get("packet", {})
            dec = pkt.get("decoded", {}) or {}
            if dec.get("portnum") != "TEXT_MESSAGE_APP":
                continue

            ch = _extract_channel_index_from_packet(pkt)

            if channel is not None and isinstance(ch, int) and ch != channel:
                continue
            hdr = dec.get("header", {}) or {}
            frm = hdr.get("fromId", "")
            if dest_id:
                if frm == dest_id:
                    count += 1
            else:
                count += 1
    except Exception as e:
        log(f"⚠️ Error en escucha puntual: {e}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return count

# -------------------------
# TELEGRAM: MENÚ Y COMANDOS
# -------------------------
# ====== MODIFICADA: menú principal con botón LoRa ======
def main_menu_kb(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    admin = is_admin(user_id) if user_id is not None else False
    buttons = [
        [InlineKeyboardButton("📡 Ver nodos", callback_data="ver_nodos")],
        [
            InlineKeyboardButton("🧭 Traceroute", callback_data="traceroute"),
            InlineKeyboardButton("🛰️ Telemetría", callback_data="telemetria"),
        ],
        [
            InlineKeyboardButton("✉️ Enviar", callback_data="enviar"),
            InlineKeyboardButton("✅ Enviar con ACK", callback_data="enviar_ack"),
        ],
        [
            InlineKeyboardButton("👂 Escuchar", callback_data="escuchar"),
            InlineKeyboardButton("⏹️ Parar escucha", callback_data="parar_escucha"),
        ],
        [InlineKeyboardButton("👥 Vecinos", callback_data="vecinos")],
        [InlineKeyboardButton("⚙️ LoRa", callback_data="lora")],   # ← NUEVO
        [InlineKeyboardButton("🧪 Estado", callback_data="estado")],
        [InlineKeyboardButton("ℹ️ Ayuda", callback_data="ayuda")],
    ]
    if admin:
        buttons.append([InlineKeyboardButton("📊 Estadística", callback_data="estadistica")])
    return InlineKeyboardMarkup(buttons)

# ====== MODIFICADA: set_bot_menu añade /lora ======
async def set_bot_menu(app: Application) -> None:
    default_cmds = [
        BotCommand("ayuda", "Ayuda completa (comandos y parámetros)"),
        BotCommand("start", "Mostrar menú principal"),
        BotCommand("menu", "Abrir menú principal"),
        BotCommand("enviar", "Enviar a nodo/broadcast (canal, alias, forzado)"),
        BotCommand("enviar_ack", "Enviar con ACK (reintentos)"),
        BotCommand("escuchar", "Escuchar broker (canal/all)"),
        BotCommand("parar_escucha", "Detener la escucha del broker"),
        BotCommand("traceroute", "Traceroute a un nodo (!id|número|alias) [Timeout] sg. espera"),
        BotCommand("rt", "Alias de /traceroute"),
        BotCommand("traceroute_status", "Ver los últimos traceroute"),   # ← NUEVO
        BotCommand("telemetria", "Telemetría a un nodo ([!id|alias] [max_n|timeout] [timeout]) + historico"),
        BotCommand("lora", "Configurar LoRa: ignore_* (status/set)"),  # ← NUEVO
        BotCommand("ver_nodos", "Ver últimos nodos o sincronizar: /ver_nodos [max_n] [timeout]"),   
        BotCommand("refrescar_nodos", "Refrescar nodos: /refrescar_nodos [api|cli] [Nodos]max [Timeout]sg"),   
        BotCommand("vecinos", "Listar vecinos directos:  /vecinos [max_n] [hops_mode]"),        
        BotCommand("estado", "Comprobar estado host/broker"),
        BotCommand("programar", "<YYYY-MM-DD HH:MM> <destino[:canal] | canal N> <texto...> Programar envío en fecha/hora"),
        BotCommand("diario", "<HH:MM[,HH:MM,...]> [mesh|aprs|ambos] [grupo <id>] <destino[:canal] | canal N | CALL|broadcast> [aprs <CALL|broadcast>:] <texto>  — Envío(s) diario(s)"),
        BotCommand("mis_diarios", "Listar tareas diarias (/mis_diarios [pending|done|failed|canceled] [grupo <id>])"),
        BotCommand("parar_diario_grupo", "Detener todas las diarias de un grupo"),
        BotCommand("parar_diario", "Detener un envío diario por ID"),
        BotCommand("en", "<minutos|m1,m2,...> <destino[:canal] | canal N> <texto…> Programar envío en +minutos"),
        BotCommand("manana", "<HH:MM> <destino[:canal] | canal N> <texto…> Programar envío mañana a HH:MM"),
        BotCommand("tareas", "Listar tareas programadas /tareas [pending|done|failed|canceled]"),
        BotCommand("cancelar_tarea", "Cancelar tarea por ID"),
        BotCommand("position", "Ver últimas posiciones /position <N> [min] | /position <!id|alias> [min] [N]"),
        BotCommand("position_mapa", "Ver últimas mapa de posiciones GPS ([N] [T] (mn))"),
        BotCommand("cobertura", "Mapa de cobertura: heatmap + circulos. cobertura [!id|alias] [Xh] [entorno]"),
        BotCommand("auditoria_red", "Auditoría rápida de red (SNR/hops/recomendaciones)"),
        BotCommand("auditoria_integral", "Auditoría completa de la red (carga LoRa y tráfico)"),
        BotCommand("canales", "Ver canales configurados en el nodo"),
        BotCommand("aprs", "/aprs [en] [min1,min2,..] | [canal N] texto | /aprs N texto | /aprs CALL: texto"),
        BotCommand("aprs_on", "Activa el gate APRS→Mesh (tráfico recibido en APRS SE reenviará a la malla)"),
        BotCommand("aprs_off", "Desactiva el gate APRS→Mesh (tráfico recibido en APRS No se reenviará a la malla)"),
        BotCommand("reconectar", "Forzar reconexión del broker [/reconectar [seg]]"),
        BotCommand("notificaciones", "Activar/Desactivar avisos de tareas"),
        BotCommand("bloquear", "Bloquea ids /bloquear <id1, id2,...> Bloquea IDs indicados /bloquear lista Lista IDs actuales"),
        BotCommand("desbloquear", "Desbloquea IDs /desbloquear <id1,id2,...>")
    ]
    await app.bot.set_my_commands(default_cmds, scope=BotCommandScopeDefault())

    admin_cmds = default_cmds + [BotCommand("estadistica", "Uso del bot (solo admin)")]
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            log(f"❗ set_my_commands admin {admin_id}: {e}")

# ====== MODIFICADA: callbacks del menú, añade 'lora' ======
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ver_nodos":
        await ver_nodos_cmd(update, context)

    elif data == "traceroute":
        await query.message.reply_text("Introduce número|!id|alias para traceroute.", reply_markup=ForceReply())
        context.user_data["await_traceroute"] = True

    elif data == "telemetria":
        await query.message.reply_text("Introduce telemetria [!id|alias] [max_n|timeout] [timeout] para solicitar telemetría.", reply_markup=ForceReply())
        context.user_data["await_telemetry"] = True

    elif data == "enviar":
        await query.message.reply_text(
            "Destino (número|!id|alias|broadcast). Puedes indicar canal así: !id:2 • alias:5 • broadcast:1",
            reply_markup=ForceReply()
        )
        context.user_data["await_send_dest"] = True

    elif data == "enviar_ack":
        await query.message.reply_text(
            "Formato: <número|!id|alias|broadcast[:canal]> <texto…> [reintentos=N espera=S backoff=X]\n"
            "Ej.: alias:5 reintentos=5 espera=10 backoff=1.5 Mensaje crítico",
            reply_markup=ForceReply()
        )
        context.user_data["await_enviar_ack"] = True

    elif data == "escuchar":
        await escuchar_cmd(update, context)

    elif data == "parar_escucha":
        await parar_escucha_cmd(update, context)

    elif data == "vecinos":
        await vecinos_cmd(update, context)

    elif data == "lora":  # ← NUEVO: botón muestra status directo
        # Llamamos al status por comodidad
        context.args = ["status"]
        await lora_cmd(update, context)

    elif data == "ayuda":
        await ayuda(update, context)
    
    elif data == "estado":
        await estado_cmd(update, context)

    elif data == "estadistica":
        await estadistica_cmd(update, context)

# ---- Básicos

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bump_stat(update.effective_user.id, update.effective_user.username or "", "start")
    await set_bot_menu(context.application)
    text = (
        "🤖 Meshtastic Bot listo.\n"
        f"- Nodo: {MESHTASTIC_HOST}\n"
        f"- Broker: {BROKER_HOST}:{BROKER_PORT} canal {BROKER_CHANNEL}\n\n"
        "Elige una opción:"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=main_menu_kb(update.effective_user.id)
    )

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bump_stat(update.effective_user.id, update.effective_user.username or "", "menu")
    await update.effective_message.reply_text(
        "Menú principal:",
        reply_markup=main_menu_kb(update.effective_user.id)
    )


# ====== NUEVA: envío seguro de HTML en trozos (Telegram limita ~4096 chars) ======
async def _send_html_chunks(update: Update, html_text: str, block_title: str = "Ayuda", maxlen: int = 3900) -> None:
    """
    Envía 'html_text' dividido en varios mensajes < maxlen (por seguridad bajo 4096).
    Intenta cortar por líneas en blanco, luego por líneas normales.
    Mantiene parse_mode=HTML y desactiva web previews.
    """
    from telegram import constants

    text = html_text.strip()
    if len(text) <= maxlen:
        await update.effective_message.reply_text(
            text, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True
        )
        return

    # Intento 1: cortar por doble salto de línea
    paragraphs = text.split("\n\n")
    current = ""
    chunks = []

    for p in paragraphs:
        candidate = (current + ("\n\n" if current else "") + p).strip()
        if len(candidate) <= maxlen:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Si el párrafo individual ya excede, cortamos por líneas
            if len(p) > maxlen:
                lines = p.splitlines()
                buf = ""
                for line in lines:
                    cand2 = (buf + ("\n" if buf else "") + line).strip()
                    if len(cand2) <= maxlen:
                        buf = cand2
                    else:
                        if buf:
                            chunks.append(buf)
                        buf = line
                if buf:
                    chunks.append(buf)
                current = ""
            else:
                current = p

    if current:
        chunks.append(current)

    # Envío con encabezados de página
    total = len(chunks)
    for i, ch in enumerate(chunks, start=1):
        header = f"<b>{block_title} ({i}/{total})</b>\n\n"
        # Asegura que cabemos con el encabezado:
        if len(header) + len(ch) > maxlen:
            # Si truena, mandamos sin header este bloque.
            msg_txt = ch
        else:
            msg_txt = header + ch
        await update.effective_message.reply_text(
            msg_txt, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True
        )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ayuda — Ayuda completa del bot (HTML-safe para Telegram).
    Versión extendida, dividida automáticamente en partes si excede el límite de Telegram.
    """
    from telegram import constants

    s_intro = (
        "<b>Ayuda — Bot Mesh v4.3</b>\n"
        "Integración API Meshtastic con reserva CLI y broker JSONL. Estrategia API-first con reconexión automática.\n"
        "El broker permite escuchar eventos y consultar histórico sin abrir nuevas conexiones al nodo.\n"
    )

    s_conv = (
        "────────────────────────────────────────────────\n"
        "<b>Convenciones y notas</b>\n"
        "• <b>Destino</b>: número (de <code>/ver_nodos</code> o <code>/vecinos</code>), <code>!id</code>, alias, o "
        "<code>broadcast</code>/<code>all</code>.\n"
        "• <b>Canal</b>: sufijo <code>:N</code> (ej. <code>!c94a4b9a:0</code>, <code>alias:5</code>, <code>broadcast:1</code>) "
        "o <code>canal N</code> con broadcast.\n"
        "• <b>Frescura</b> (mins): minutos desde el último visto. En API: <code>last_heard</code>; en tablas: “mins”.\n"
        "• <b>Reintentos</b>, <b>espera</b>, <b>backoff</b> disponibles en envíos.\n"
        "• <b>ACK en broadcast</b>: éxito si al menos un nodo confirma dentro de la ventana.\n"
        "• <b>Menú oficial</b>: botón Menú con opciones según rol (usuario/administrador).\n"
        "• <b>Conexión</b>: una única conexión TCP persistente al nodo; múltiples clientes se conectan al broker local.\n"
    )

    s_mensajeria = (
        "────────────────────────────────────────────────\n"
        "<b>Mensajería</b>\n"
        "• <code>/enviar &lt;destino[:canal]&gt; &lt;texto…&gt;</code> — Envío normal. Soporta <code>broadcast[:N]</code>.\n"
        "  Ej.: <code>/enviar canal 0 Buenos dias</code> • <code>/enviar !b03df4cc:2 Hola</code>\n"
        "• <code>/enviar_ack &lt;destino[:canal]&gt; &lt;texto…&gt; [reintentos=N espera=S backoff=X]</code> — Solicita ACK en unicast.\n"
        "  En broadcast, se considera éxito si al menos un nodo confirma.\n"
        "  Ej.: <code>/enviar_ack broadcast:1 reintentos=5 espera=10 backoff=1.5 Mensaje critico</code>\n"
        "  Notas: Resultado muestra OK/KO del envío; Confirmacion indica alias o !id que confirmó si hubo ACK.\n"
    )

    s_programacion = (
        "────────────────────────────────────────────────\n"
        "<b>Programación de envíos</b>\n"
        "• <code>/programar YYYY-MM-DD HH:MM destino[:canal] | canal N texto… [ack]</code>\n"
        "  Programa un envío en hora local Europe/Madrid. <code>ack</code> al final fuerza ACK (solo unicast).\n"
        "  Ej.: <code>/programar 2025-09-02 09:30 canal 0 broadcast Buenos dias a todos</code>\n"
        "        <code>/programar 2025-09-02 21:45 !b03df4cc:1 Aviso critico ack</code>\n"
        "• <code>/tareas [pending|done|failed|canceled]</code> — Lista tareas (por defecto pending).\n"
        "• <code>/cancelar_tarea TASK_ID</code> — Cancela una tarea por ID.\n"
        "<b>Atajos</b>\n"
        "• <code>/en &lt;cantidad&gt; &lt;unidad&gt; &lt;destino[:canal] | canal N&gt; &lt;texto…&gt; [ack]</code>\n"
        "  Programa relativo. Unidades: s, m, h, d. Ej.: <code>/en 45 m canal 0 Reunir datos</code>\n"
        "• <code>/manana HH:MM &lt;destino[:canal] | canal N&gt; &lt;texto…&gt; [ack]</code>\n"
        "  Programa para mañana a la hora indicada. Ej.: <code>/manana 08:15 canal 0 Recordatorio</code>\n"
    )

    s_diario = (
        "────────────────────────────────────────────────\n"
        "<b>Programación diaria</b>\n"
        "• <code>/diario &lt;HH:MM[,HH:MM…]&gt; [mesh|aprs|ambos] [grupo &lt;id&gt;] "
        "&lt;destino[:canal] | canal N | CALL|broadcast&gt; [aprs &lt;CALL|broadcast&gt;:] &lt;texto…&gt;</code>\n"
        "   Repite el envío todos los días a las horas indicadas (zona <i>Europe/Madrid</i>).\n"
        "\n"
        "<u>Modos de transporte</u>:\n"
        "• <code>mesh</code>  → Solo malla Meshtastic.\n"
        "• <code>aprs</code>  → Solo APRS (pasarela). Usa <code>CALL</code> (p.ej. <code>EA2XXX-10</code>) o <code>broadcast</code>.\n"
        "• <code>ambos</code> → Envío por MESH y, además, reenvío por APRS.\n"
        "   En modo ambos puedes fijar destino APRS con el token <code>aprs &lt;CALL|broadcast&gt;</code> en la línea.\n"
        "\n"
        "<u>Gestión</u>:\n"
        "• <code>/mis_diarios [pending|done|failed|canceled] [grupo &lt;group_id&gt;]</code> → Listar (con texto, transporte y grupo).\n"
        "• <code>/parar_diario &lt;task_id&gt;</code> → Detener una tarea diaria por ID.\n"
        "• <code>/parar_diario_grupo &lt;group_id&gt;</code> → Detener todas las diarias del grupo.\n"
        "\n"
        "<u>Ejemplos</u>:\n"
        "• <code>/diario 08:30 mesh canal 0 Parte diario</code>\n"
        "• <code>/diario 22:00 aprs EB2EAS-11: Mensaje de prueba</code>\n"
        "• <code>/diario 07:45 ambos canal 1 aprs broadcast Aviso general</code>\n"
        "• <code>/diario 08:30,14:00,21:45 ambos grupo mantenimiento canal 2 aprs broadcast Aviso en tres horarios</code>\n"
        "• <code>/mis_diarios pending grupo daily-mantenimiento</code>\n"
        "• <code>/parar_diario_grupo daily-mantenimiento</code>\n"
    )


    s_telemetria = (
        "────────────────────────────────────────────────\n"
        "<b>Telemetría enriquecida</b>\n"
        "• <code>/telemetria [!id|alias] [max_n|timeout] [timeout]</code>\n"
        "  Sin destino: lista telemetría reciente. Con destino: filtra ese nodo. <code>max_n</code> limita muestras o usa <code>timeout</code> (s).\n"
        "  Campos habituales si existen: bateria (%/V), temperatura (°C), humedad (%), presion (hPa), altitud (m), solar (V), carga (mA), RSSI (dBm), SNR (dB).\n"
        "  Notas: no aplica broadcast; si el canal esperado no entrega en tiempo, reintento breve sin filtro de canal.\n"
        "  Ej.: <code>/telemetria !9eeb1328 20</code> • <code>/telemetria nodo_juan 8 6</code>\n"
    )

    s_nodos = (
        "────────────────────────────────────────────────\n"
        "<b>Nodos</b>\n"
        "• <code>/ver_nodos [N|false]</code> — Lista enriquecida con RSSI, SNR, ruta y calidad. <code>false</code> imprime rapido.\n"
        "  -> Se incluyen hops reales cuando hay metadatos: <code>hops = hop_limit - hop_start</code>.\n"
    )

    s_refresnodos = (
        "\n\n<b>Refresco de tabla</b>\n"
        "• <code>/refrescar_nodos</code>  (auto)\n"
        "• <code>/refrescar_nodos api 50</code>  (API-only, 50 máx.)\n"
        "• <code>/refrescar_nodos cli 100 12</code>  (CLI-only, 100 máx., 12s timeout)\n"
    )

    s_vecinos = (
        "<b>🧭 /vecinos [max_n] [timeout] [hops_mode]</b>\n\n"
        "Lista vecinos directos o indirectos, usando el broker o (opcionalmente) la CLI.\n\n"
        "<b>Parámetros:</b>\n"
        "• <code>max_n</code> — máximo de nodos a mostrar (defecto: 20).\n"
        "• <code>timeout</code> — espera al broker en segundos (defecto: 4.0).\n"
        "• <code>hops_mode</code> — filtra por hops:\n"
        "   • 0 (defecto) → directos (hops=0)\n"
        "   • 1 → vecinos a 1 salto\n"
        "   • >0 / >=1 / 1+ / indirectos → solo indirectos\n"
        "   • all / * / todos → sin filtro (todos)\n"
        "<b>🧭 /vecinos>=[hops] o /vecinos[hops]</b>\n\n"
        "<b>Ejemplos:</b>\n"
        "• <code>/vecinos</code> → directos en últimos 60 min (20 máx).\n"
        "• <code>/vecinos 30</code> → directos en últimos 30 min.\n"
        "• <code>/vecinos all</code> → todos los vecinos (ignora hops).\n"
        "• <code>/vecinos>=2</code> → muestra vecinos con más o igual a 2 hops.\n"
    )
   
    s_rutas = (
        "────────────────────────────────────────────────\n"
        "<b>Rutas</b>\n"
        "• <code>/traceroute &lt;!id|alias&gt;  [timeout_s] segundos de espera</code> — Traza ruta por API/CLI. \n"
        "  Ej.: <code>/traceroute !33691d30 </code>\n"
    )

    s_cobertura = (
        "────────────────────────────────────────────────\n"
        "<b>Mapa de cobertura</b>\n"
        "• <code>/cobertura [!id|alias] [Xh] [entorno] 'entorno' {urbano, suburbano, abierto}. Por defecto: urbano</code> — Genera un mapa HTML con heatmap y círculos desde posiciones históricas.\n"
        "  Sin destino: /cobertura  todos los nodos. Con destino: solo ese nodo. Por defecto, últimas 24 h.\n"
        "  Ej.: <code>/cobertura 12h</code> • <code>/cobertura !xxxxxxx 48h suburbano</code>\n"
        "       <code>/cobertura !xxxxxxxxx</code> • <code>/cobertura !xxxxxxx abierto</code>\n"
        "  Salida: enlace KML descargable. Los puntos muestran alias si está disponible.\n"
    )

    s_escucha = (
        "────────────────────────────────────────────────\n"
        "<b>Escucha y broker</b>\n"
        "• <code>/escuchar [N|all]</code> — Suscribe a <code>TEXT_MESSAGE_APP</code> desde el broker. <code>N</code>=canal lógico; <code>all</code>=todos.\n"
        "  Incluye metadatos cuando existen: <code>rx_rssi</code>, <code>rx_snr</code>, <code>hop_limit</code>, <code>hop_start</code>, <code>relay_node</code>.\n"
        "• <code>/parar_escucha</code> — Detiene la escucha.\n"
        "Notas\n"
        "• La escucha no bloquea envíos. El broker reconecta si cae la sesión con el nodo.\n"
        "• Cuando la escucha está parada, los mensajes entrantes se registran en el backlog para reenvío posterior.\n"
    )

    s_lora = (
        "────────────────────────────────────────────────\n"
        "<b>Configuración LoRa (/lora)</b>\n"
        "• <code>/lora status</code> — Muestra <code>lora.ignore_incoming</code> y <code>lora.ignore_mqtt</code>.\n"
        "• <code>/lora ignore_incoming on|off</code> — Ignorar o aceptar recepción RF.\n"
        "• <code>/lora ignore_mqtt on|off</code> — Ignorar o aceptar envío a MQTT.\n"
        "• <code>/lora set ignore_incoming=on ignore_mqtt=off</code> — Ajuste múltiple.\n"
        "Si la API no expone setters, se aplica reserva con <code>meshtastic --set lora.*</code>.\n"
    )

    s_estado = (
        "────────────────────────────────────────────────\n"
        "<b>Estado y administración</b>\n"
        "• <code>/estado</code> — Comprueba host, puerto, broker y latencias básicas.\n"
        "  Si el puerto de control UDP está disponible, también muestra <code>mgr_paused</code>, "
        "<code>tx_blocked</code> y <code>cooldown</code>.\n"
        "• <code>/broker_status</code> — Estado interno del broker (conexión al nodo, <i>manager paused</i>, "
        "TX guard, cooldown, versión y nodo objetivo). Añade <code>raw</code> para ver JSON crudo.\n"
    )

    s_broker_admin = (
        "────────────────────────────────────────────────\n"
        "<b>Control del broker</b>\n"
        "• <code>/broker_resume</code> — Limpia el cooldown y hace <i>resume()</i> del manager. "
        "Úsalo si ves envíos bloqueados por <code>TX_BLOCKED</code> o el broker quedó en <code>mgr_paused</code>.\n"
        "• <code>/force_reconnect [grace_s]</code> — Reinicia el pool de conexiones, limpia <code>TX_BLOCKED</code> y el cooldown, "
        "y reintenta conexión en limpio. <code>grace_s</code> (opcional) establece una ventana de gracia para evitar "
        "escalado si vuelve a caer de inmediato.\n"
        "\n"
        "<b>Ejemplos:</b>\n"
        "• <code>/broker_status</code>\n"
        "• <code>/broker_status raw</code>\n"
        "• <code>/broker_resume</code>\n"
        "• <code>/force_reconnect</code>\n"
        "• <code>/force_reconnect 30</code>\n"
        "\n"
        "<b>Notas:</b>\n"
        "• Requiere que el bot tenga acceso al puerto de control UDP del broker "
        "(variables <code>BROKER_CTRL_HOST</code> y <code>BROKER_CTRL_PORT</code>, p.ej. <code>broker</code> y <code>8766</code>).\n"
        "• Tras ejecutar, puedes verificar el estado con <code>/broker_status</code> o volver a <code>/estado</code>.\n"
    )


    s_params = (
        "────────────────────────────────────────────────\n"
        "<b>Parámetros comunes</b>\n"
        "• <code>reintentos=N</code>  • <code>espera=S</code> (segundos)  • <code>backoff=X</code>  • <code>maxhops=H</code>\n"
    )

    s_errores = (
        "────────────────────────────────────────────────\n"
        "<b>Errores frecuentes</b>\n"
        "• KO: Timed out waiting for connection completion — Reintentar. Confirmar que el broker no monopoliza la conexión.\n"
        "• Unexpected OSError … terminating meshtastic reader — Caída de socket; el broker reconecta. Revisar red/IP del nodo.\n"
        "• Aviso: No hay pool TCP inicializado — Inicializar pool al arrancar y definir <code>MESHTASTIC_HOST/PORT</code>.\n"
        "• Broadcast sin ACK — Puede haberse retransmitido igualmente; la ausencia de ACK no implica fallo RF.\n"
    )

    s_ejemplos = (
        "────────────────────────────────────────────────\n"
        "<b>Ejemplos</b>\n"
        "• <code>/enviar canal 0 Hola a todos</code>\n"
        "• <code>/enviar !9eeb1328:2 Mensaje directo</code>\n"
        "• <code>/enviar_ack !077f73a7 Requiere confirmacion</code>\n"
        "• <code>/en 45m canal 0 Aviso en 45 minutos</code>\n"
        "• <code>/mañana 08:15 canal 0 Recordatorio</code>\n"
        "• <code>/diario 08:15 mesh canal 0 Recordatorio</code>\n"
        "• <code>/mis_diarios pending</code>\n"
        "• <code>/telemetria !9eeb1328 20</code>\n"
        "• <code>/vecinos 10 60</code>\n"
        "• <code>/traceroute !33691d30 60</code>\n"
        "• <code>/cobertura 24</code>\n"
        "• <code>/lora status</code>\n"
    )

    s_cli = (
        "────────────────────────────────────────────────\n"
        "<b>Broker por línea de comandos</b>\n"
        "• Programar: <code>python Meshtastic_Broker_v5.8.py schedule --when \"2025-09-02 09:30\" --channel 0 --dest broadcast --msg \"Buenos dias\"</code>\n"
        "• Listar:    <code>python Meshtastic_Broker_v5.8.py tasks --status pending</code>\n"
        "• Cancelar:  <code>python Meshtastic_Broker_v5.8.py cancel --id TASK_ID</code>\n"
    )

    s_aprs = (
        "📡 <b>APRS</b>\n"
        "Envía desde la malla a APRS-IS o a un indicativo concreto.\n\n"
        "<b>Formatos:</b>\n"
        "• <code>/aprs canal N &lt;texto&gt;</code>\n"
        "• <code>/aprs N &lt;texto&gt;</code>\n"
        "• <code>/aprs &lt;CALL|broadcast&gt;: &lt;texto&gt;</code>\n"
        "• <code>/aprs en &lt;min|m1,m2,&gt; canal N &lt;texto&gt;</code>\n"
        "  (Si no indicas canal, usa el por defecto del bot)\n\n"
        "<b>Ejemplos:</b>\n"
        "• <code>/aprs broadcast Saludos desde la red Meshtastic</code>\n"
        "• <code>/aprs EB2ABC-10 Hola EB2ABC, QSO en 144.800</code>\n"
        "• <code>/aprs canal 1 EB7XYZ-7 Estoy operativo en sierra</code>\n\n"
        "<b>Notas:</b>\n"
        "• <code>broadcast</code> envía como <i>status</i> APRS (no a un destinatario concreto).\n"
        "• <code>CALL</code> envía como <i>message</i> APRS a ese indicativo (p.ej. EB2XXX-7).\n"
        "• Se respetan los límites de tamaño APRS: el bot divide en varias tramas si hace falta.\n"
        "\n"
        "────────────────────────────────────────────────\n"
        "<b>APRS desde RF → malla Meshtastic</b>\n"
        "Desde APRS (walkie, cliente APRS) puedes inyectar mensajes en la malla usando etiquetas en el comentario:\n"
        "\n"
        "<b>Envío inmediato a la malla</b>\n"
        "• <code>[CH n] texto</code>\n"
        "• <code>[CANAL n] texto</code>\n"
        "Ejemplos:\n"
        "• <code>[CH 1] Hola a todos</code>\n"
        "• <code>[CANAL4] Revisión enlace oeste</code>\n"
        "El texto se envía inmediatamente al canal Mesh <code>n</code>.\n"
        "\n"
        "<b>Envío programado desde APRS</b>\n"
        "• <code>[CH n+M] texto</code>  (M = minutos de retraso)\n"
        "Ejemplos:\n"
        "• <code>[CH 3+10] Aviso en 10 minutos</code>\n"
        "• <code>[CANAL1+5] Recordatorio breve</code>\n"
        "El gateway APRS programa el envío localmente y, pasado el tiempo, manda el mensaje al canal Mesh.\n"
        "\n"
        "<b>Control del gateway APRS→Mesh</b>\n"
        "Solo desde indicativos autorizados en <code>APRS_ALLOWED_SOURCES</code>:\n"
        "• <code>[CH 0] APRS ON</code>   → habilita el reenvío APRS → Mesh\n"
        "• <code>[CH 0] APRS OFF</code>  → deshabilita temporalmente el reenvío\n"
        "Cualquier otro texto en <code>[CH 0]</code> se ignora por seguridad.\n"
        "\n"
        "<b>Posiciones APRS → enlace de mapa</b>\n"
        "Cuando el paquete APRS lleva coordenadas, el gateway añade un enlace clicable:\n"
        "• <code>https://maps.google.com/?q=lat,lon</code>\n"
        "junto con el comentario APRS si existe.\n"
        "Ejemplo de lo que verá la malla:\n"
        "• <code>qrv R70-R72 sdr:in91np.ddns.net:8073 Abierto https://maps.google.com/?q=41.638500,-0.903833</code>\n"
    )

    s_auditorias = (
        "────────────────────────────────────────────────\n"
        "<b>Auditorías de red</b>\n"
        "• <code>/auditoria_red [horas]</code>\n"
        "  Diagnóstico rápido usando cobertura/posiciones recientes.\n"
        "  • Calcula por vecino: SNR (p50/p90/avg), hops (avg/máx), RSSI medio y muestras válidas.\n"
        "  • Sugiere configuración para tu nodo: rol (CLIENT/CLIENT_MUTE/ROUTER), potencia TX, <i>hop_limit</i>, baliza y telemetría.\n"
        "  • Adjunta CSV (métricas por vecino) y JSON (resumen + recomendación).\n"
        "  • Por defecto analiza 72 h. Ej.: <code>/auditoria_red 48</code>\n"
        "\n"
        "• <code>/auditoria_integral [horas]</code>\n"
        "  Auditoría completa con carga de canal y mapa de calor.\n"
        "  • Analiza: <code>coverage.jsonl</code>, <code>positions.jsonl</code>, <code>messages.jsonl</code>, "
        "<code>telemetry.jsonl</code>, <code>broker_offline_log.jsonl</code> (si existen).\n"
        "  • Incluye: SNR/RSSI/hops, duplicados, payload medio, distribución por aplicación, estimación de <i>airtime</i>/duty-cycle LoRa.\n"
        "  • Genera: CSV de vecinos, CSV de recomendaciones por vecino, JSON integral y mapa de calor HTML.\n"
        "  • Por defecto 72 h. Ej.: <code>/auditoria_integral 168</code>\n"
        "\n"
        "<u>Notas</u>:\n"
        "• Los umbrales se ajustan por variables de entorno: <code>AUD_*</code> (p.ej. <code>AUD_HOPS_MAX</code>, "
        "<code>AUD_P90_STRONG</code>, <code>AUD_BEACON_R</code>, <code>AUD_BEACON_C</code>…).\n"
        "• Salidas en <code>bot_data/reportes/</code>. El mapa HTML se guarda también ahí (o en <code>BOT_MAPS_DIR</code> si está definido).\n"
    )

    full = "\n\n".join([
        s_intro, s_conv, s_mensajeria, s_programacion, s_diario, s_telemetria, s_nodos, s_refresnodos, s_vecinos,
        s_rutas, s_cobertura, s_escucha, s_lora, s_estado, s_broker_admin,  s_params, s_errores,
        s_ejemplos, s_cli, s_aprs, s_auditorias
    ])

    await _send_html_chunks(update, full, block_title="Ayuda")

# ---- Vecinos

def get_direct_neighbors_from_table(max_n: int = 20, max_hops: int = 0) -> List[Tuple[str, str, Optional[int], int]]:
    """
    Vecinos desde la tabla (--nodes) filtrando por hops <= max_hops.
    Devuelve lista ordenada por 'visto hace' ascendente:
      [(id, alias, mins, hops)]
    - mins puede ser None si no hay dato claro.
    """
    try:
        rows = _parse_nodes_table(NODES_FILE)
    except Exception:
        return []

    out: List[Tuple[str, str, Optional[int], int]] = []

    for r in rows:
        # HOPS: acepta 'hops' numérico o 'hops_text' tipo '0 hops'
        hops_raw = r.get("hops_text") if r.get("hops_text") is not None else r.get("hops")
        hops = _to_int_safe(str(hops_raw)) if hops_raw is not None else None
        if hops is None:
            # si no sabemos los hops, descartamos para no mezclar
            continue
        if hops > max_hops:
            continue

        nid = (r.get("id") or "").strip()
        if not nid:
            continue
        alias = (r.get("alias") or "").strip() or nid

        # 'visto hace' (mins): intentamos varias claves; si no, parseamos texto
        mins: Optional[int] = None
        for k in ("mins", "last_heard_min", "lastSeenMin", "last_seen_min"):
            v = r.get(k)
            if v is not None:
                mins = _to_int_safe(str(v))
                if mins is not None:
                    break
        if mins is None:
            mins = parse_minutes(
                r.get("last_seen_text", "")
                or r.get("since", "")
                or r.get("last_heard", "")
                or ""
            )

        out.append((nid, alias, mins, hops))

    # Orden por 'mins' asc (None al final)
    out.sort(key=lambda x: (x[2] is None, x[2] if x[2] is not None else 10**9))
    return out[:max_n]


def _build_alias_fallback_from_nodes_file() -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    try:
        rows = _parse_nodes_table(NODES_FILE)
        for r in rows:
            nid = (r.get("id") or "").strip()
            alias = (r.get("alias") or "").strip()
            if nid and alias and not alias.startswith("!"):
                alias_map[nid] = alias
    except Exception as e:
        log(f"⚠️ _build_alias_fallback_from_nodes_file: {e}")
    return alias_map

# --- Utils de selección de nodos “últimos vistos” ---

from concurrent.futures import ThreadPoolExecutor, as_completed

ROUTE_CACHE = {}  # {node_id: (when, result)}

def _load_last_seen_nodes(max_n: int, freshness_min: int) -> list[dict]:
    """
    Lee últimos vistos de nodos.txt y filtra por frescura (minutos).
    Si el fichero está vacío/inexistente, fuerza un refresco por CLI una vez.
    """
    nodes = load_nodes_file_safe(max_n) or []
    if not nodes:
        # ⚠️ Retro-compat: forzar un refresco UNA VEZ para comportarse como versiones anteriores
        ensure_nodes_file_fresh(max_age_s=0, max_rows=max_n)
        nodes = load_nodes_file_safe(max_n) or []
    if not nodes:
        return []

    cutoff = datetime.now(UTC) - timedelta(minutes=freshness_min)
    recent = [n for n in nodes if datetime.fromtimestamp(n.get("last_heard", 0), UTC) >= cutoff]
    recent.sort(key=lambda n: n.get("last_heard", 0), reverse=True)
    return recent[:max_n]

def _fallback_neighbor_table(max_n: int) -> list[dict]:
    """Si no hay fichero de últimos vistos, usa la neighbor table de la API."""
    try:
        table = api_get_neighbors_via_pool(MESHTASTIC_HOST, 4403) or {}  # { "!id": {...} }
    except Exception:
        table = {}

    lst = []
    for nid, info in table.items():
        alias = info.get("alias") or nid
        ts = info.get("last_heard", 0)
        lst.append({"id": nid, "alias": alias, "last_heard": ts})
    lst.sort(key=lambda x: x.get("last_heard", 0), reverse=True)
    return lst[:max_n]

def _pick_nodes_for_scan(max_n: int, freshness_min: int, ctx) -> list[dict]:
    candidates = _load_last_seen_nodes(max_n, freshness_min)
    if not candidates:
        candidates = _fallback_neighbor_table(max_n)
        ctx["source"] = "tabla"
    else:
        ctx["source"] = "ultimos"
    return candidates

# --- Traceroute paralelo con timeouts cortos y cache ---
# Cache de rutas
ROUTE_CACHE: dict[str, tuple[datetime, dict]] = {}

def _build_alias_fallback_from_nodes_file() -> dict:
    mapping = {}
    try:
        rows = _parse_nodes_table(NODES_FILE)
        for r in rows:
            nid = (r.get("id") or "").strip()
            ali = (r.get("alias") or "").strip()
            if nid and ali and not ali.startswith("!"):
                mapping[nid] = ali
    except Exception:
        pass
    try:
        mapping.update(cargar_aliases_desde_nodes(str(NODES_FILE)) or {})
    except Exception:
        pass
    return mapping

def utc_now():
    return datetime.now(UTC)

def utc_from_ts(ts: float):
    return datetime.fromtimestamp(ts, UTC)


def _traceroute_fast(node_id: str, channel: int = 0,
                     hop_timeout: float = 1.2, max_hops: int = 5, total_timeout: float = 3.5):
    # Cache 5 min
    now = utc_now()
    cached = ROUTE_CACHE.get(node_id)
    if cached:
       cached_ts = cached[0]
       # por si alguna vez guardaste naive en cache:
       if cached_ts.tzinfo is None:
           cached_ts = cached_ts.replace(tzinfo=UTC)
       if (now - cached_ts).total_seconds() < 300:
           return cached[1]
    try:
        # Firma correcta: host, dest_id, timeout
        res = api_traceroute(MESHTASTIC_HOST, node_id, timeout=int(total_timeout))
        if isinstance(res, dict) and res.get("ok"):
            alias_map = _build_alias_fallback_from_nodes_file()
            path_ids = res.get("route") or []
            res["path_ids"] = path_ids
            res["path_aliases"] = [alias_map.get(n, n) for n in path_ids]
        ROUTE_CACHE[node_id] = (now, res)
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}

from datetime import UTC, datetime
import time



# --- Carga segura de "últimos vistos" desde nodos.txt (sin CLI) ---
def  load_nodes_file_safe(max_n: int = 50) -> list[dict]:
    """
    Devuelve [{'id','alias','mins','last_heard'}] ordenados por 'mins' asc.
    Lee el fichero NODES_FILE parseado por _parse_nodes_table().
    """
    try:
        rows = _parse_nodes_table(NODES_FILE)
    except Exception as e:
        log(f"⚠️ load_nodes_file_safe: {e}")
        rows = []

    out = []
    now = time.time()
    for r in rows:
        nid   = (r.get("id") or "").strip()
        alias = (r.get("alias") or "").strip()
        mins  = parse_minutes(r.get("last_seen_text", "") or "")
        if not nid:
            continue
        last_heard = now - (mins or 0) * 60
        out.append({
            "id": nid,
            "alias": alias if (alias and not alias.startswith("!")) else "",
            "mins": mins,
            "last_heard": last_heard
        })

    out.sort(key=lambda d: (d.get("mins") if d.get("mins") is not None else 10**9))
    return out[:max_n]

# ---- Ver nodos

def format_nodes_list(nodes: List[Tuple[str, str, int, Optional[int]]]) -> Tuple[List[str], Dict[str, str]]:
    alias_fallback: Dict[str, str] = {}
    try:
        rows = _parse_nodes_table(NODES_FILE)
        for r in rows:
            nid = (r.get("id") or "").strip()
            ali = (r.get("alias") or "").strip()
            if nid and ali and not ali.startswith("!"):
                alias_fallback[nid] = ali
    except Exception as e:
        log(f"⚠️ format_nodes_list/_parse_nodes_table: {e}")

    try:
        alias_fallback2 = cargar_aliases_desde_nodes(str(NODES_FILE))
        if isinstance(alias_fallback2, dict):
            for k, v in alias_fallback2.items():
                if k and v and not str(v).startswith("!"):
                    alias_fallback[k] = v
    except Exception:
        pass

    lines: List[str] = []
    mapping: Dict[str, str] = {}

    for i, (nid, alias_api, mins, hops) in enumerate(nodes, start=1):
        alias_api = (alias_api or "").strip()
        alias_ok = alias_api if (alias_api and not alias_api.startswith("!")) else ""
        shown_alias = alias_ok or alias_fallback.get(nid, "") or nid

        line = f"{i}. {shown_alias} ({nid}) — visto hace {mins} min"
        if hops is not None:
            line += f" — hops: {hops}"
        lines.append(line)

        mapping[str(i)] = nid
        mapping[nid] = nid
        if shown_alias and not shown_alias.startswith("!"):
            mapping[shown_alias.lower()] = nid

    return lines, mapping

# === NUEVA ===
def _is_listen_active(context) -> bool:
    """
    Devuelve True si este chat tiene una escucha activa del broker.
    """
    st = context.chat_data.get("listen_state") or {}
    return bool(st.get("active"))

# =========================
# ver_nodos_cmd — COMPLETA (km + ciudad/provincia)
# =========================

# =========================
# ver_nodos_cmd — wrapper sobre /vecinos (sin filtro de hops)
# =========================
async def ver_nodos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /ver_nodos [max_n] [timeout]

    Implementación simplificada y robusta:
    - Reutiliza exactamente la misma lógica que /vecinos_cmd.
    - No aplica filtro de hops (equivalente a /vecinos con "todos los hops").
    - Mantiene el parámetro max_n y timeout.

    Sintaxis:
      /ver_nodos
      /ver_nodos 30
      /ver_nodos 30 60
    """

    user = update.effective_user
    bump_stat(user.id, user.username or "", "ver_nodos")

    # Parseo de argumentos local (solo para max_n y timeout)
    args = context.args or []

    # max_n
    try:
        max_n = int(args[0]) if len(args) >= 1 and str(args[0]).lstrip("-").isdigit() else 20
    except Exception:
        max_n = 20

    # timeout (se reenvía a vecinos_cmd, que ya lo entiende)
    try:
        timeout = int(args[1]) if len(args) >= 2 and str(args[1]).lstrip("-").isdigit() else 60
    except Exception:
        timeout = 60

    # Preparamos args para vecinos_cmd:
    #   /vecinos [max_n] [timeout] [hops_mode]
    # Aquí NO queremos filtrar hops, así que usamos hops_mode="all"
    context.args = [str(max_n), str(timeout), "all"]

    # Reutilizamos la lógica probada de vecinos_cmd
    return await vecinos_cmd(update, context)


async def position_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /position <N> [min]
    - Muestra las últimas posiciones (≤ min) con:
      * Distancia (km) desde HOME_LAT/HOME_LON del .env (parseo tolerante)
      * Ciudad (reverse_geocoder: name → admin2 → admin1)
      * Mantiene alt, SNR, RSSI y enlace a Google Maps
    """
    # === Garantiza que el .env está cargado en este proceso ===
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path="/app/.env", override=True)
    except Exception:
        pass

    if not context.args:
        await update.effective_message.reply_text("Uso: /position <Nodos> [T] [min]")
        return
    
    args = context.args + [None, None]
    max_nodes = int(args[0]) if args[0] and str(args[0]).isdigit() else 10
    last_min = int(args[1]) if args[1] and str(args[1]).isdigit() else 60

    rows = read_positions_recent(last_min, max_nodes)
    if not rows:
        await update.effective_message.reply_text("📍 Sin posiciones recientes.")
        return

    # === Cache alias/nodos ===
    nodes_map = context.user_data.get("nodes_map")
    if nodes_map is None:
        try:
            nodes_map = build_nodes_mapping()
            context.user_data["nodes_map"] = nodes_map
        except Exception:
            nodes_map = {}

    # Helpers locales

    from datetime import datetime

    def _to_float_coord(v):
        if v is None:
            return None
        try:
            if isinstance(v, (int, float)): return float(v)
            s = str(v).strip().replace(",", ".")
            s = "".join(ch for ch in s if ch in "+-0123456789.")
            if s in ("", "+", "-"): return None
            return float(s)
        except Exception:
            return None

    def _haversine_km(lat1, lon1, lat2, lon2):
        try:
            R = 6371.0
            dlat = math.radians(float(lat2) - float(lat1))
            dlon = math.radians(float(lon2) - float(lon1))
            a = math.sin(dlat/2)**2 + math.cos(math.radians(float(lat1))) * math.cos(math.radians(float(lat2))) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            return round(R * c, 1)
        except Exception:
            return None

    _rg_resolver = None
    def _ensure_rg():
        nonlocal _rg_resolver
        if _rg_resolver is not None: return
        try:
            import reverse_geocoder as rg
            def _rg(lat, lon):
                try:
                    res = rg.search([(float(lat), float(lon))])
                    if isinstance(res, list) and res:
                        r = res[0]
                        return r.get("name") or r.get("admin2") or r.get("admin1") or None
                except Exception:
                    return None
                return None
            _rg_resolver = _rg
        except Exception:
            _rg_resolver = None

    def _place_of(lat, lon):
        if lat is None or lon is None: return None
        _ensure_rg()
        if _rg_resolver is None: return None
        try:
            return _rg_resolver(lat, lon)
        except Exception:
            return None

    # HOME del .env (tolerante)
    la = os.getenv("HOME_LAT"); lo = os.getenv("HOME_LON")
    home_lat = _to_float_coord(la) if la is not None else None
    home_lon = _to_float_coord(lo) if lo is not None else None

    # Envío con chunks (tu función existente)
    async def send_message_chunks(message_text, max_length=4096):
        if len(message_text) <= max_length:
            await update.effective_message.reply_html(message_text, disable_web_page_preview=True)
            return
        lines = message_text.split('\n')
        current_chunk = ""
        for line in lines:
            if len(line) > max_length:
                if current_chunk:
                    await update.effective_message.reply_html(current_chunk, disable_web_page_preview=True)
                    current_chunk = ""
                while line:
                    chunk_size = max_length
                    cut_pos = line.rfind(' ', 0, chunk_size) if len(line) > chunk_size else len(line)
                    if cut_pos == -1: cut_pos = chunk_size
                    await update.effective_message.reply_html(line[:cut_pos], disable_web_page_preview=True)
                    line = line[cut_pos:].lstrip()
                continue
            test_chunk = current_chunk + '\n' + line if current_chunk else line
            if len(test_chunk) > max_length:
                if current_chunk:
                    await update.effective_message.reply_html(current_chunk, disable_web_page_preview=True)
                current_chunk = line
            else:
                current_chunk = test_chunk
        if current_chunk:
            await update.effective_message.reply_html(current_chunk, disable_web_page_preview=True)

    # Construcción del mensaje
    lines = [f"📍 Últimas posiciones (≤{last_min} min):"]
    for i, r in enumerate(rows, 1):
        nid_raw = str(r.get("id") or "")
        nid = nid_raw.lstrip("!")
        id_str = f"!{nid}" if nid else "!?"

        alias = (r.get("alias") or r.get("name") or r.get("shortName") or r.get("longName") or "").strip()
        if not alias and nid:
            info = (nodes_map or {}).get(nid) or {}
            for k in ("alias", "name", "shortName", "longName"):
                v = info.get(k)
                if isinstance(v, str) and v.strip():
                    alias = v.strip()
                    break
        head = id_str if (not alias or alias in (nid, nid_raw, id_str)) else f"{alias} ({id_str})"

        lat, lon = r.get("lat"), r.get("lon")
        if lat is None and isinstance(r.get("latitude_i"), int): lat = r["latitude_i"] / 1e7
        if lon is None and isinstance(r.get("longitude_i"), int): lon = r["longitude_i"] / 1e7

        have_coords = False
        try:
            lat_f = float(lat); lon_f = float(lon)
            have_coords = True
            gmap = f"https://maps.google.com/?q={lat_f},{lon_f}"
            line = f"{i}. {head} — {lat_f:.5f},{lon_f:.5f}"
        except Exception:
            gmap = None
            line = f"{i}. {head}"

        if r.get("alt") is not None:
            try:    line += f" • alt {float(r['alt']):.1f} m"
            except: line += f" • alt {r['alt']} m"
        if r.get("rx_snr") is not None:
            try:    line += f" • SNR {float(r['rx_snr']):.1f} dB"
            except: line += f" • SNR {r['rx_snr']} dB"
        if r.get("rx_rssi") is not None:
            try:    line += f" • RSSI {float(r['rx_rssi']):.1f} dBm"
            except: line += f" • RSSI {r['rx_rssi']} dBm"

                # Distancia + Ciudad/Provincia (robusto)
        try:
            def _f(v):
                try:
                    if v is None: return None
                    if isinstance(v, (int, float)): return float(v)
                    s = str(v).strip().replace(",", ".")
                    s = "".join(ch for ch in s if ch in "+-0123456789.")
                    if s in ("", "+", "-"): return None
                    return float(s)
                except Exception:
                    return None

            la = _f(os.getenv("HOME_LAT"))
            lo = _f(os.getenv("HOME_LON"))
            lt = _f(lat_f if have_coords else None)
            ln = _f(lon_f if have_coords else None)

            dist_txt = None
            place_txt = None

            if la is not None and lo is not None and lt is not None and ln is not None:
                try:
                    dkm = _haversine_km(la, lo, lt, ln)
                    if dkm is not None:
                        dist_txt = f"{dkm:.1f}"
                except Exception:
                    pass

            if lt is not None and ln is not None:
                try:
                    p = _place_of(lt, ln)
                    if p: place_txt = p
                except Exception:
                    pass

            if dist_txt is not None or place_txt is not None:
                line += " • 📍 "
                line += (dist_txt + " km") if dist_txt is not None else "? km"
                line += " — "
                line += place_txt if place_txt is not None else "?"
        except Exception:
            # Si algo fallase, nunca rompemos la salida
            pass

        # Timestamp
        try:
            ts_num = int(r.get("ts") or r.get("rx_time"))
            ts = datetime.fromtimestamp(ts_num).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = "—"

        if gmap:
            line += f"\n   ⏱️ {ts} • 🌍 <a href=\"{gmap}\">Ver en Google Maps</a>"
        else:
            line += f"\n   ⏱️ {ts}"
        lines.append(line)

    await send_message_chunks("\n".join(lines))


async def position_mapa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Uso: /position_mapa <kml|gpx> [N] [min]")
        return

    fmt = (context.args[0] or "").lower()
    max_nodes = int(context.args[1]) if len(context.args) > 1 and str(context.args[1]).isdigit() else 50
    last_min  = int(context.args[2]) if len(context.args) > 2 and str(context.args[2]).isdigit() else 120

    rows = read_positions_recent(last_min, max_nodes)
    if not rows:
        await update.effective_message.reply_text("📍 Sin posiciones.")
        return

    # === MOD: cachear mapa de nodos para resolver alias si el registro no lo trae ===
    nodes_map = context.user_data.get("nodes_map")
    if nodes_map is None:
        try:
            nodes_map = build_nodes_mapping()   # usa tu helper existente
            context.user_data["nodes_map"] = nodes_map
        except Exception:
            nodes_map = {}
    # === FIN MOD ===

    # === MOD: enriquecer filas con 'name' consistente: "Alias (!id)" o "!id"
    enriched: list[dict] = []
    for r in rows:
        rec = dict(r)  # copia defensiva para no mutar el original

        nid_raw = str(rec.get("id") or "")
        nid = nid_raw.lstrip("!")
        id_str = f"!{nid}" if nid else "!?"

        alias = (rec.get("alias") or rec.get("name") or rec.get("shortName") or rec.get("longName") or "").strip()
        if not alias and nid:
            info = (nodes_map or {}).get(nid) or {}
            for k in ("alias", "name", "shortName", "longName"):
                v = info.get(k)
                if isinstance(v, str) and v.strip():
                    alias = v.strip()
                    break

        # Cabecera sin duplicaciones
        display_name = id_str if (not alias or alias in (nid, nid_raw, id_str)) else f"{alias} ({id_str})"
        rec["name"] = display_name   # <-- clave que usarán build_kml/build_gpx
        # (opcional) garantizar lat/lon float si usas *_i
        if rec.get("lat") is None and isinstance(rec.get("latitude_i"), int):
            rec["lat"] = rec["latitude_i"] / 1e7
        if rec.get("lon") is None and isinstance(rec.get("longitude_i"), int):
            rec["lon"] = rec["longitude_i"] / 1e7

        enriched.append(rec)
    # === FIN MOD ===

    # Construir fichero
    if fmt == "kml":
        data, ext, mime = build_kml(enriched), "kml", "application/vnd.google-earth.kml+xml"
    else:
        data, ext, mime = build_gpx(enriched), "gpx", "application/gpx+xml"

    import tempfile, os
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        await update.effective_message.reply_document(
            open(tmp_path, "rb"),
            filename=f"positions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}",
            caption=f"🗺️ {len(enriched)} posiciones en {ext.upper()}"
        )
    finally:
        os.remove(tmp_path)



# ---- Traceroute / Telemetría

def _resolve_node_id(text: str, context) -> str:
    mapping = context.user_data.get("nodes_map") or build_nodes_mapping()
    return mapping.get(text, mapping.get(text.lower(), text))

# --- Helpers para recordar la última lista numerada y resolver destinos ---
import time
from typing import List, Dict, Any, Tuple, Optional

SELECTOR_TTL = 600  # segundos (10 min) que una lista numerada permanece “válida”

def _norm_id(nid: str) -> str:
    """Asegura formato '!xxxxxxxx' si viene sin exclamación."""
    nid = (nid or "").strip()
    return nid if nid.startswith("!") else (f"!{nid}" if nid else "")

def remember_numbered_list(context, source: str, rows: List[Dict[str, Any]]) -> None:
    """
    Guarda la última lista numerada mostrada al usuario para que comandos con índice
    (p. ej. '/telemetria 2') apunten a ESTA lista y no a otra.
    rows: elementos en el mismo orden que mostraste, con al menos:
        - 'id'    (str) en formato '!xxxxxxxx'
        - 'alias' (str) alias legible (o el propio id si no hay alias)
    """
    safe_rows = []
    for r in rows:
        rid = _norm_id(r.get("id", ""))
        alias = r.get("alias") or rid
        if rid:
            safe_rows.append({"id": rid, "alias": alias})

    context.user_data["last_selector"] = {
        "source": source,           # "vecinos" o "ver_nodos", etc.
        "ts": time.time(),          # timestamp para TTL
        "rows": safe_rows           # misma lista (mismo ORDER) que vio el usuario
    }


def resolver_alias_o_id(nid, context=None):
    """
    Compat: resuelve un destino dado como número de la última lista, !id o alias.
    Devuelve (node_id, alias_txt). Si no resuelve, devuelve (None, None).

    - Mantiene las llamadas antiguas: resolver_alias_o_id(nid)
    - Si hay 'context', usa su 'nodes_map'; si no, cae a build_nodes_mapping()
    - Delegamos en resolve_destination_token cuando está disponible
    """
    try:
        token = (str(nid) if nid is not None else "").strip()
        if not token:
            return (None, None)

        # 1) Construir índice de nodos (preferimos el cache del usuario)
        nodes_index = None
        if context is not None:
            nodes_index = (getattr(context, "user_data", {}) or {}).get("nodes_map")

        if not nodes_index and "build_nodes_mapping" in globals():
            # Fallback a la última lista persistida (nodos.txt o similar)
            nodes_index = build_nodes_mapping()

        # 2) Delegar en la función unificada si existe
        if "resolve_destination_token" in globals():
            node_id, alias_txt, _source = resolve_destination_token(token, context, nodes_index)
        else:
            # Fallback mínimo si no estuviera disponible (evita romper llamadas)
            node_id, alias_txt = None, None
            t = token
            # !id directo
            if t.startswith("!") and len(t) > 1:
                node_id, alias_txt = t, None
            # número de lista
            elif t.isdigit() and nodes_index and isinstance(nodes_index, dict):
                idx = int(t)
                if idx in nodes_index:
                    info = nodes_index[idx]
                    node_id = info.get("id") or info.get("num") or info.get("nodeId")
                    alias_txt = info.get("alias") or info.get("longName") or info.get("user") or None
            # alias directo (búsqueda simple por alias en índice)
            elif nodes_index and isinstance(nodes_index, dict):
                low = t.lower()
                for _k, info in nodes_index.items():
                    ali = (info.get("alias") or info.get("longName") or info.get("user") or "").strip()
                    if ali and ali.lower() == low:
                        node_id = info.get("id") or info.get("num") or info.get("nodeId")
                        alias_txt = ali
                        break

        # 3) Normalizar y devolver
        if not node_id or node_id == "^all":
            return (None, None)
        return (node_id, alias_txt)
    except Exception:
        return (None, None)


def resolve_destination_token(
    token: str,
    context,
    nodes_index_fallback: Optional[Dict[str, str]] = None
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Convierte un “destino” textual a (node_id, alias, source_used).
    Acepta:
      - 'broadcast'/'all'/'^all'
      - '!id'
      - número (índice de la última lista mostrada /vecinos o /ver_nodos)
      - alias (si pasas un índice fallback alias->id)
    Devuelve:
      - node_id (str) en formato '!xxxxxxxx' o '^all' para broadcast
      - alias (str) “bonito” para mostrar
      - source_used: 'literal', 'vecinos', 'ver_nodos' o 'fallback'
    """
    tok = (token or "").strip()

    # Broadcast
    if tok.lower() in ("broadcast", "all", "^all"):
        return "^all", "broadcast", "literal"

    # !id explícito
    if tok.startswith("!"):
        rid = _norm_id(tok)
        return rid, tok.lstrip("!"), "literal"

    # Número → última lista numerada válida
    if tok.isdigit():
        sel = context.user_data.get("last_selector")
        if sel and (time.time() - sel.get("ts", 0) <= SELECTOR_TTL):
            rows = sel.get("rows") or []
            idx = int(tok) - 1
            if 0 <= idx < len(rows):
                rid = rows[idx]["id"]
                alias = rows[idx].get("alias") or rid
                return rid, alias, sel.get("source") or "selector"

    # Alias → fallback global (opcional)
    if nodes_index_fallback:
        rid = nodes_index_fallback.get(tok)
        if rid:
            return _norm_id(rid), tok, "fallback"

    # No resuelto
    return None, None, None

def send_telemetry_via_api(pool, host: str, port: int, node_id: str) -> Tuple[bool, str]:
    """
    Envía solicitud de telemetría a node_id probando múltiples firmas de la lib Meshtastic.
    Devuelve (ok, how) donde 'how' describe qué método/args funcionó.
    No lanza CLI ni cierra sockets (usa el pool persistente).
    """
    iface = pool.get(host=host, port=port)

    names = ("requestTelemetry", "sendRequestTelemetry", "request_telemetry", "sendTelemetry")
    kw_bases = (
        {"destinationId": node_id},
        {"dest": node_id},
        {"id": node_id},
    )
    telem_variants = ("device_metrics", "DEVICE_METRICS", 0, 1)  # por compatibilidad
    tried = []

    for name in names:
        fn = getattr(iface, name, None)
        if not callable(fn):
            continue

        # 1) intenta sin telemetryType
        for base in kw_bases:
            try:
                r = fn(**base)  # algunas versiones devuelven None; consideramos OK si no hay excepción
                return True, f"{name}{base}"
            except TypeError as e:
                tried.append(f"{name}{base} -> {e.__class__.__name__}")
            except Exception as e:
                # errores de runtime reales (socket, etc.) -> repropaga
                raise

        # 2) si es sendTelemetry, intenta con telemetryType explícito
        if name == "sendTelemetry":
            for base in kw_bases:
                for t in telem_variants:
                    args = base.copy()
                    args["telemetryType"] = t
                    try:
                        r = fn(**args)
                        return True, f"{name}{args}"
                    except TypeError as e:
                        tried.append(f"{name}{args} -> {e.__class__.__name__}")
                    except Exception as e:
                        raise

        # 3) último: positional (por si alguna firma no usa kwargs)
        try:
            r = fn(node_id)
            return True, f"{name}({node_id})"
        except TypeError as e:
            tried.append(f"{name}({node_id}) -> {e.__class__.__name__}")
        except Exception as e:
            raise

    # Si no encontramos ninguna firma válida
    return False, " / ".join(tried[:6]) + (" ..." if len(tried) > 6 else "")

# ====== NUEVO: helpers de API/CLI para flags LoRa ======

def _lora_cli_get() -> dict:
    """
    Intenta obtener config LoRa via CLI: 'meshtastic --get lora'
    Devuelve dict parcial con flags si los encuentra.
    """
    out = run_command(["--host", MESHTASTIC_HOST, "--get", "lora"], timeout=TIMEOUT_CMD_S)
    # Parsing flexible (buscamos 'ignore_incoming' y 'ignore_mqtt')
    flags = {}
    try:
        # Intento 1: si devolviera JSON (algunas builds)
        j = json.loads(out)
        lora = j.get("lora", j) if isinstance(j, dict) else {}
        for k in ("ignore_incoming", "ignore_mqtt"):
            if k in lora:
                flags[k] = bool(lora[k])
    except Exception:
        # Intento 2: texto, líneas tipo 'ignore_incoming: true'
        for line in out.splitlines():
            if "ignore_incoming" in line:
                flags["ignore_incoming"] = _to_bool(line.split(":")[-1].strip())
            if "ignore_mqtt" in line:
                flags["ignore_mqtt"] = _to_bool(line.split(":")[-1].strip())
    return flags

def _lora_cli_set(updates: dict[str, bool]) -> tuple[bool, str]:
    """
    Establece flags via CLI: 'meshtastic --set lora.ignore_incoming true'
    """
    msgs = []
    for k, v in updates.items():
        val = "true" if v else "false"
        flag = f"lora.{k}"
        out = run_command(["--host", MESHTASTIC_HOST, "--set", flag, val], timeout=TIMEOUT_CMD_S)
        msgs.append(f"{flag}={val} → {out[:120].strip()}")
    return True, " | ".join(msgs)

# ====== REEMPLAZO: helpers de API (solo pool, sin CLI y sin crear sockets) ======
from typing import Tuple, Dict, Any, Optional

def _lora_api_get(pool, host: str, port: int) -> dict:
    """
    Lee flags desde la API usando **solo** la interfaz existente del pool.
    - NO crea sockets nuevos (usa pool.peek).
    - Si no hay interfaz en el pool, devuelve {} (no inventa ni persiste).
    """
    iface = None
    try:
        # NO crear conexiones: usamos peek (añadido en TCPInterfacePool)
        iface = getattr(pool, "peek", None)(host, port) if pool else None
    except Exception:
        iface = None

    if iface is None:
        # No hay interfaz viva → no hacemos nada y devolvemos vacío.
        return {}

    # Firmas posibles, evitando romper distintas versiones del lib
    for name in ("getModuleConfig", "get_module_config", "getModule", "getConfig"):
        fn = getattr(iface, name, None)
        if not callable(fn):
            continue
        try:
            # Intento con módulo 'lora' si la firma lo permite
            res = fn("lora") if getattr(fn, "__code__", None) and fn.__code__.co_argcount >= 2 else fn()
            # Normalizar a dict
            if not isinstance(res, dict):
                try:
                    res = dict(res)  # best-effort
                except Exception:
                    continue
            lora = res.get("lora", res)
            out = {}
            for k in ("ignore_incoming", "ignore_mqtt"):
                if k in lora:
                    bv = lora.get(k)
                    # normalización a bool/None
                    if isinstance(bv, bool):
                        out[k] = bv
                    elif isinstance(bv, (int, float)):
                        out[k] = bool(bv)
                    elif isinstance(bv, str):
                        out[k] = (bv.strip().lower() in ("1", "true", "on", "sí", "si", "yes"))
                    else:
                        out[k] = None
            # Si no hay claves, seguimos probando firma; si hay, devolvemos
            if out:
                return out
        except Exception:
            continue

    return {}

def _lora_api_set(pool, host: str, port: int, updates: Dict[str, bool]) -> Tuple[bool, str]:
    """
    Establece flags vía API usando **solo** la interfaz existente del pool.
    - NO crea sockets nuevos (usa pool.peek).
    - Si no hay interfaz en el pool, devuelve (False, "no_iface").
    """
    iface = None
    try:
        iface = getattr(pool, "peek", None)(host, port) if pool else None
    except Exception:
        iface = None

    if iface is None:
        return False, "no_iface"

    # Filtra únicamente parámetros válidos y normaliza a bool
    clean: Dict[str, bool] = {}
    for k, v in updates.items():
        if k not in ("ignore_incoming", "ignore_mqtt"):
            continue
        clean[k] = bool(v)
    if not clean:
        return False, "no_updates"

    tried = []

    # Variante 1: setModuleConfig("lora", {...})
    for name in ("setModuleConfig", "set_module_config"):
        fn = getattr(iface, name, None)
        if callable(fn):
            try:
                fn("lora", clean)
                return True, f"{name}('lora', {clean})"
            except Exception as e:
                tried.append(f"{name}: {type(e).__name__}")

    # Variante 2: setConfig(module='lora', values={...}) o similar
    for name in ("setConfig", "set_module"):
        fn = getattr(iface, name, None)
        if callable(fn):
            try:
                fn(module="lora", values=clean)
                return True, f"{name}(module='lora', values={clean})"
            except Exception as e:
                tried.append(f"{name}: {type(e).__name__}")

    # Variante 3: setters granulares si existieran
    for k, v in clean.items():
        for name in (f"set_{k}", f"set{k.title().replace('_','')}", "setModuleParam"):
            fn = getattr(iface, name, None)
            if callable(fn):
                try:
                    if name == "setModuleParam":
                        fn("lora", k, v)
                    else:
                        fn(v)
                    return True, f"{name}({k}={v})"
                except Exception as e:
                    tried.append(f"{name}: {type(e).__name__}")

    return False, " ; ".join(tried[:5]) if tried else "unsupported"


# ====== REEMPLAZO: comando /lora (solo vía broker → API real en el broker, sin sockets nuevos) ======
async def lora_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /lora status
    /lora ignore_incoming on|off
    /lora ignore_mqtt on|off
    /lora set ignore_incoming=on ignore_mqtt=off
    """
    bump_stat(update.effective_user.id, update.effective_user.username or "", "lora")
    args = [a.strip() for a in (context.args or []) if a and a.strip()]

    # Sin args → ayuda
    if not args:
        await update.effective_message.reply_text(
            "Uso:\n"
            "• /lora status\n"
            "• /lora ignore_incoming on|off\n"
            "• /lora ignore_mqtt on|off\n"
            "• /lora set ignore_incoming=on ignore_mqtt=off"
        )
        return

    sub = args[0].lower()

    # ---- STATUS: pedir al broker (el broker habla con la radio por API)
    if sub == "status":
        cfg = _lora_broker_get()
        if not cfg:
            await update.effective_message.reply_text(
                "⚠️ No se pudo leer la configuración LoRa desde el broker.\n"
                "Comprueba que el broker está en marcha y expone LORA_GET."
            )
            return
        ii = cfg.get("ignore_incoming")
        im = cfg.get("ignore_mqtt")
        await update.effective_message.reply_text(
            "⚙️ LoRa (broker/API)\n"
            f"• lora.ignore_incoming = {ii}\n"
            f"• lora.ignore_mqtt = {im}"
        )
        return

    # ---- SET SENCILLO
    if sub in ("ignore_incoming", "ignore_mqtt"):
        if len(args) < 2:
            await update.effective_message.reply_text("Falta valor: on|off")
            return
        val = _to_bool(args[1])
        if val is None:
            await update.effective_message.reply_text("Valor no válido. Usa on|off.")
            return

        ok, how = _lora_broker_set({sub: bool(val)})
        if not ok:
            await update.effective_message.reply_text(f"❌ No se pudo actualizar en el broker ({how}).")
            return

        await update.effective_message.reply_text(f"✳️ {sub} → {bool(val)}  ({how})")
        return

    # ---- SET COMPUESTO
    if sub == "set":
        updates: dict[str, bool] = {}
        for tok in args[1:]:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            k = k.strip().lower()
            if k not in ("ignore_incoming", "ignore_mqtt"):
                continue
            bv = _to_bool(v)
            if bv is not None:
                updates[k] = bool(bv)

        if not updates:
            await update.effective_message.reply_text(
                "Nada que actualizar. Ej.: /lora set ignore_incoming=on ignore_mqtt=off"
            )
            return

        ok, how = _lora_broker_set(updates)
        if not ok:
            await update.effective_message.reply_text(f"❌ No se pudo actualizar en el broker ({how}).")
            return

        pretty = ", ".join([f"{k}={updates[k]}" for k in updates])
        await update.effective_message.reply_text(f"✳️ set {pretty}  ({how})")
        return

    # Subcomando desconocido
    await update.effective_message.reply_text("Subcomando no reconocido. Usa: status | ignore_incoming | ignore_mqtt | set")

# ---- Enviar (flujo existente mejorado)

def _append_send_log_row(row: List[Any]) -> None:
    new_file = not SEND_LOG_CSV.exists()
    try:
        with SEND_LOG_CSV.open("a", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            if new_file:
                w.writerow(["timestamp","dest","canal","texto","forzado","traceroute_ok","hops","respuestas"])
            w.writerow(row)
    except Exception as e:
        log(f"⚠️ No se pudo escribir log de envío: {e}")

def _packet_id_from_send(pkt) -> int | None:
    if isinstance(pkt, dict):
        if "id" in pkt:
            try: return int(pkt["id"])
            except: return None
        if "_packet" in pkt and isinstance(pkt["_packet"], dict) and "id" in pkt["_packet"]:
            try: return int(pkt["_packet"]["id"])
            except: return None
    try:
        pid = getattr(pkt, "id", None)
        return int(pid) if pid is not None else None
    except:
        return None

# =========[ MODIFICADA COMPLETA – con verificación local en la respuesta ]=========

# === NUEVO o ACTUALIZADO: /aprs con disparo UDP a la pasarela ===
import socket, json
from html import escape

try:
    APRS_CTRL_HOST
except NameError:
    APRS_CTRL_HOST = "127.0.0.1"
try:
    APRS_CTRL_PORT
except NameError:
    APRS_CTRL_PORT = 9464


from typing import List

def _parse_minutes_list(spec: str) -> List[int]:
    """'5' o '5,10,25' → [5] o [5,10,25]; filtra vacíos, valida >0."""
    out: List[int] = []
    for p in spec.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            v = int(p)
            if v > 0:
                out.append(v)
        except ValueError:
            continue
    return out

async def aprs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Formatos aceptados (inmediato):
      • /aprs canal N <texto>
      • /aprs N <texto>
      • /aprs <CALL|broadcast>: <texto> [canal N]
    Formatos nuevos (programado; múltiple con comas):
      • /aprs en M canal N <texto>         (M = 5  o  5,10,25)
      • /aprs en M N <texto>               (atajo: N equivale a 'canal N')
    Troceo APRS inmediato: si el texto excede APRS_MAX_LEN (p.e. 67), se trocea.
    """
    # === bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "aprs")
    except Exception:
        pass

    # args
    args = (context.args or []) + [None]
    target = (args[0] or "").strip()
    if not target:
        await _safe_reply_html(
            update.effective_message,
            "Uso: <code>/aprs canal N &lt;texto&gt;</code>  |  <code>/aprs en &lt;min|m1,m2,...&gt; N &lt;texto&gt;</code>"
        )
        return ConversationHandler.END
    
    args = context.args or []
    raw = " ".join(args).strip()
    APRS_LEN = _aprs_max_len()


    # ──────────────────────────────
    # RUTA PROGRAMADA: "/aprs en …"
    # ──────────────────────────────
    if args and args[0].lower() == "en":
        if len(args) < 3:
            await _safe_reply_html(update.effective_message, "Uso: <code>/aprs en &lt;min|m1,m2,...&gt; N &lt;texto&gt;</code>")
            return

        minutes_spec = args[1]
        minutes_list = _parse_minutes_list(minutes_spec)
        if not minutes_list:
            try:
                m = int(minutes_spec)
                if m <= 0:
                    raise ValueError
                minutes_list = [m]
            except Exception:
               await _safe_reply_html(update.effective_message, "Minutos no válidos. Ejemplos: <code>5</code>  |  <code>5,10,25</code>")
               return

        # admitir dos sintaxis: "canal N <texto>" o "N <texto>"
        canal = None
        texto = ""
        # Caso explícito "canal N"
        if len(args) >= 4 and args[2].lower() == "canal" and args[3].lstrip("-").isdigit():
            canal = int(args[3])
            texto = " ".join(args[4:]).strip()
        # Atajo: "N <texto>"
        elif args[2].lstrip("-").isdigit():
            canal = int(args[2])
            texto = " ".join(args[3:]).strip()
        else:
            await _safe_reply_html(
                update.effective_message,
                "Faltan parámetros. Usa: <code>/aprs canal N &lt;texto&gt;</code>  |  "
                "<code>/aprs en &lt;min|m1,m2,...&gt; N &lt;texto&gt;</code>"
            )
            return

        if not texto:
            await _safe_reply_html(update.effective_message, "Falta el texto a enviar.")

            return

        # Normalización/validación Mesh (la pasarela APRS troceará a su límite)
        MAX_BYTES = 180
        texto_norm = _norm_mesh(texto)
        if len(texto_norm.encode("utf-8")) > MAX_BYTES:
            await _safe_reply_html(update.effective_message, "❌ Mensaje demasiado largo para Mesh (≤ 180 bytes). Acórtalo.")
            return

        est_parts = len(_split_mesh(texto_norm, max_bytes=MAX_BYTES))

        try:
            import broker_task as _bt
        except Exception as e:
            await _safe_reply_html(update.effective_message, f"❌ Error al cargar scheduler: <code>{escape(type(e).__name__)}</code>: <code>{escape(str(e))}</code>")
            return

        # Meta común para ambas tareas (Mesh y APRS)
        base_meta = {
            "scheduled_by": update.effective_user.username or str(update.effective_user.id),
            "via": "/aprs",
            "aprs_dest": "broadcast",
            "bot_est_parts": est_parts,
            # Para notificación de ejecución (si la usas en el broker/bot)
            "chat_id": update.effective_chat.id,
            "reply_to": update.effective_message.message_id,
        }

        ids, errors = [], []
        for mins in minutes_list:
            when_local_dt = datetime.now(TZ_EUROPE_MADRID) + timedelta(minutes=mins)
            when_local_str = when_local_dt.strftime("%Y-%m-%d %H:%M")

            # 1) Tarea principal: envío por Mesh
            try:
                res_mesh = _bt.schedule_message(
                    when_local=when_local_str,
                    channel=int(canal),
                    message=texto_norm,
                    destination="broadcast",
                    require_ack=False,
                    meta={
                        **base_meta,
                        # Transporte explícito por Mesh
                        "transport": "mesh",
                    },
                )
                if isinstance(res_mesh, dict) and res_mesh.get("ok"):
                    ids.append(res_mesh.get("task", {}).get("id", "?"))
                else:
                    errors.append(f"{mins}min")
                    try:
                        print(f"[bot:/aprs en] NOK Mesh ({mins}min) canal={canal} res={res_mesh!r}", flush=True)
                    except Exception:
                        pass
            except Exception as e:
                errors.append(f"{mins}min:Mesh:{type(e).__name__}")
                try:
                    print(f"[bot:/aprs en] EXC Mesh ({mins}min) canal={canal} {type(e).__name__}: {e}", flush=True)
                except Exception:
                    pass

            # 2) Tarea gemela: APRS-only (no depende de que Mesh vaya bien)
            try:
                _bt.schedule_message(
                    when_local=when_local_str,
                    channel=int(canal),
                    message=texto_norm,
                    destination="broadcast",
                    require_ack=False,
                    meta={
                        **base_meta,
                        # Forzamos transporte "aprs" para que el scheduler
                        # use _aprs_forward_via_udp() directamente.
                        "transport": "aprs",
                    },
                )
            except Exception as e:
                # No marcamos error "duro": lo dejamos solo en log.
                try:
                    print(f"[bot:/aprs en] EXC APRS ({mins}min) canal={canal} {type(e).__name__}: {e}", flush=True)
                except Exception:
                    pass


        # <<< AÑADE AQUÍ EL LOG >>>
        try:
            print(f"[bot:/aprs en] Programadas {len(ids)} tareas APRS canal={canal} mins={minutes_list} IDs={ids} ERRORS={errors}", flush=True)
        except Exception:
            pass

        # <<< FIN LOG >>>
        # Normaliza/filtra errores vacíos
        errors = [e.strip() for e in errors if e and e.strip()]

        if ids and not errors:
            
            await _safe_reply_html(
                update.effective_message,
                "📡 <b>APRS+Mesh programado</b>: <code>{n}</code> envío(s) al canal <code>{ch}</code> en <code>{mins}</code> min.\n"
                "IDs: <code>{ids}</code>".format(
                    n=len(minutes_list),
                    ch=escape(str(canal)),
                    mins=escape(",".join(str(m) for m in minutes_list)),
                    ids=escape(", ".join(str(i) for i in ids)),
                )
            )
        elif ids and errors:
            await _safe_reply_html(
                update.effective_message,
                "⚠️ <b>APRS programado parcialmente</b>.\n"
                "IDs OK: <code>{ids}</code><br>"
                "Fallos: <code>{err}</code>".format(
                    ids=escape(", ".join(str(i) for i in ids)),
                    err=escape(", ".join(errors))
                )
            )
        else:
            await _safe_reply_html(update.effective_message, "❌ No se pudo programar APRS.")

        
        return

    # ──────────────────────────────
    # RUTA INMEDIATA (intacta)
    # ──────────────────────────────
    def _udp_send(dest: str, text: str):
        ctrl = {"mode": "aprs", "dest": dest, "text": text}
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(json.dumps(ctrl).encode("utf-8"), (APRS_CTRL_HOST, APRS_CTRL_PORT))
        finally:
            try: s.close()
            except Exception: pass

    def _send(mesh_dest: str, canal_int: int, text: str):
        # 1) Inyecta en Mesh
        raw_for_mesh = f"/msg {mesh_dest}: {text}" if not text.lower().startswith("/msg ") else text
        node_id = None  # broadcast Mesh por defecto
        mesh_result, packet_id = send_text_message(node_id, raw_for_mesh, canal=canal_int)

        # 2) APRS troceado
        dest_for_aprs = mesh_dest.lower()
        if dest_for_aprs in ("broadcast", "all"):
            chunks = _aprs_split_broadcast(text, APRS_LEN) or [text[:APRS_LEN]]
            for part in chunks:
                _udp_send("broadcast", part)
                time.sleep(0.15)
        else:
            chunks = _aprs_split_directed(text, APRS_LEN) or [text[:APRS_LEN]]
            for part in chunks:
                _udp_send(dest_for_aprs, part)
                time.sleep(0.15)

        aprs_status = f"OK ({len(chunks)} parte{'s' if len(chunks)!=1 else ''})"
        html = (
            "<b>APRS</b> → enviado a Mesh y pasarela.\n"
            f"Destino: <code>{escape(mesh_dest)}</code>\n"
            f"Canal Mesh: <code>{canal_int}</code>\n"
            f"Chunks APRS: <code>{len(chunks)}</code> (máx={APRS_LEN})\n"
            f"Mesh: <code>{escape(mesh_result)}</code> {('packet_id='+str(packet_id)) if packet_id else ''}\n"
            f"Pasarela APRS: <code>{escape(aprs_status)}</code>"
        ).strip()
        return html

    # nuevos atajos inmediatos
    dest_clean = None
    canal = BROKER_CHANNEL
    texto_final = ""
    ok_simple = False
    if len(args) >= 2:
        if args[0].lower() == "canal" and args[1].lstrip("-").isdigit():
            canal = int(args[1]); texto_final = " ".join(args[2:]).strip()
            dest_clean = "broadcast"; ok_simple = True
        elif args[0].lstrip("-").isdigit():
            canal = int(args[0]); texto_final = " ".join(args[1:]).strip()
            dest_clean = "broadcast"; ok_simple = True

    if ok_simple:
        if not texto_final:
            await _safe_reply_html(
                update.effective_message,
                "Falta el texto. Uso: <code>/aprs canal N &lt;texto&gt;</code>  |  <code>/aprs N &lt;texto&gt;</code>"
            )
            return
        html = _send("broadcast", canal, texto_final)
    
        await _safe_reply_html(update.effective_message, html)
        # si tu _safe_reply_html no admite disable_preview, usa:
        # await _safe_reply_html(update.effective_message, html)


        return

    # compat clásica: "<CALL|broadcast>: <texto> [canal N]"
    if not raw or ":" not in raw:
        await _safe_reply_html(
            update.effective_message,
            "Uso:<br>"
            "• <code>/aprs canal N &lt;texto&gt;</code><br>"
            "• <code>/aprs N &lt;texto&gt;</code><br>"
            "• <code>/aprs &lt;CALL|broadcast&gt;: &lt;texto&gt; [canal N]</code><br>"
            "• <code>/aprs en &lt;min|m1,m2,...&gt; canal N &lt;texto&gt;</code><br>"
            "• <code>/aprs en &lt;min|m1,m2,...&gt; N &lt;texto&gt;</code>"
        )
        return

    m_ch = re.search(r"(?i)\bcanal\s+(\d{1,2})\b$", raw)
    if m_ch:
        try: canal = int(m_ch.group(1))
        except Exception: canal = BROKER_CHANNEL
        raw = raw[:m_ch.start()].strip()

    dest_part, text_part = raw.split(":", 1)
    dest_clean = dest_part.strip() or "broadcast"
    texto_final = text_part.strip()
    if not texto_final:
        await _safe_reply_html(update.effective_message, "Falta el texto tras ‘:’.")
        return

    html = _send(dest_clean, canal, texto_final)
    await _safe_reply_html(update.effective_message, html)


import socket, json
from html import escape

async def aprs_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Activa el gate APRS→Mesh (tráfico recibido en APRS se reenviará a la malla).
    """
    bump_stat(update.effective_user.id, update.effective_user.username or "", "aprs_on")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        msg = {"mode":"aprs_gate","enable":1}
        s.sendto(json.dumps(msg).encode("utf-8"), (APRS_CTRL_HOST, APRS_CTRL_PORT))
        try:
            data,_ = s.recvfrom(4096)
            ack = json.loads(data.decode("utf-8", "ignore"))
            st = "ON" if ack.get("aprs_gate_enabled") else "OFF"
            await update.effective_message.reply_text(f"✅ APRS→Mesh: <b>{st}</b>", parse_mode="HTML")
        except Exception:
            await update.effective_message.reply_text("✅ APRS→Mesh: <b>ON</b>", parse_mode="HTML")
    finally:
        try: s.close()
        except Exception: pass

async def aprs_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Desactiva el gate APRS→Mesh (lo recibido desde APRS NO se reenvía).
    """
    bump_stat(update.effective_user.id, update.effective_user.username or "", "aprs_off")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        msg = {"mode":"aprs_gate","enable":0}
        s.sendto(json.dumps(msg).encode("utf-8"), (APRS_CTRL_HOST, APRS_CTRL_PORT))
        try:
            data,_ = s.recvfrom(4096)
            ack = json.loads(data.decode("utf-8", "ignore"))
            st = "ON" if ack.get("aprs_gate_enabled") else "OFF"
            await update.effective_message.reply_text(f"✅ APRS→Mesh: <b>{st}</b>", parse_mode="HTML")
        except Exception:
            await update.effective_message.reply_text("✅ APRS→Mesh: <b>OFF</b>", parse_mode="HTML")
    finally:
        try: s.close()
        except Exception: pass

# (Opcional) estado rápido
async def aprs_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bump_stat(update.effective_user.id, update.effective_user.username or "", "aprs_status")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        msg = {"mode":"aprs_status"}
        s.sendto(json.dumps(msg).encode("utf-8"), (APRS_CTRL_HOST, APRS_CTRL_PORT))
        try:
            data,_ = s.recvfrom(4096)
            ack = json.loads(data.decode("utf-8", "ignore"))
            st = "ON" if ack.get("aprs_gate_enabled") else "OFF"
            await update.effective_message.reply_text(f"ℹ️ Estado APRS→Mesh: <b>{st}</b>", parse_mode="HTML")
        except Exception:
            await update.effective_message.reply_text("ℹ️ Estado APRS→Mesh: <i>desconocido</i>", parse_mode="HTML")
    finally:
        try: s.close()
        except Exception: pass



async def enviar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /enviar canal <n> <texto>
    /enviar <número|!id|alias> <texto>
    - NO refresca nodos ni llama a API; usa sólo nodos.txt (cargar_aliases_desde_nodes).
    - Envío priorizando la cola del BROKER (dispara bridge A→B) con fallback al pool y adapter resiliente.
    - Broadcast (node_id=None) sin ACK; unicast sin ACK aquí (para evitar duplicados).
    - Añade feedback local: '✅ Nodo local confirmó transmisión' si ok y hay packet_id.
    """

    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    bump_stat(update.effective_user.id, update.effective_user.username or "", "enviar")
    msg = update.effective_message
    args = context.args or []

    # --- Construir mapping SIN llamar a API ---
    nodes_map: Dict[str, str] = context.user_data.get("nodes_map") or {}
    if not nodes_map:
        try:
            alias_dict = cargar_aliases_desde_nodes(str(NODES_FILE))
            for nid, ali in (alias_dict or {}).items():
                if not nid:
                    continue
                nid_norm = nid if str(nid).startswith("!") else f"!{int(nid):08x}" if str(nid).isdigit() else str(nid)
                if ali:
                    nodes_map[ali.strip().lower()] = nid_norm
                nodes_map[nid_norm] = nid_norm
            if "nodes_map" in context.user_data and isinstance(context.user_data["nodes_map"], dict):
                for k, v in list(context.user_data["nodes_map"].items()):
                    if k.isdigit() and v.startswith("!"):
                        nodes_map[k] = v
        except Exception:
            nodes_map = {}
        context.user_data["nodes_map"] = nodes_map

    # --- Parsear destino/canal/texto + flag 'forzado' ---
    node_id, canal, texto, forced_flag = parse_dest_channel_and_text(args, nodes_map)
    # === NUEVO (guardia parser 'canal <n> <texto>') ===
    # Repara casos donde el parser original deja texto=None en "/enviar canal N <texto>"
    if (not texto) and len(args) >= 3:
        a0 = str(args[0]).lower()
        # soporta "forzado canal N ..." también
        if a0 == "forzado" and len(args) >= 4 and str(args[1]).lower() == "canal" and str(args[2]).lstrip("-").isdigit():
            forced_flag = True
            canal = int(args[2])
            node_id = None
            texto = " ".join(args[3:]).strip()
        elif a0 == "canal" and str(args[1]).lstrip("-").isdigit():
            canal = int(args[1])
            node_id = None
            texto = " ".join(args[2:]).strip()

    # Si aún no hay texto, mantenemos la validación actual
    if not texto:
        await msg.reply_text(
            "Uso:\n"
            "• <b>/enviar canal 0</b> <i>texto</i>\n"
            "• <b>/enviar</b> <i>número|!id|alias</i> <i>texto</i>\n"
            "Añade <b>forzado</b> al inicio para omitir traceroute previo.",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    is_broadcast = node_id is None

    # --- (Opcional) Traceroute previo si NO es forzado y es unicast ---
    traceroute_ok = None
    hops = 0
    if TRACEROUTE_CHECK and (not forced_flag) and (not is_broadcast):
        try:
            res = traceroute_node(node_id, timeout=min(TRACEROUTE_TIMEOUT, 20))
            traceroute_ok = bool(res.ok)
            hops = int(res.hops or 0)
            if not traceroute_ok:
                forced_flag = True
        except Exception:
            traceroute_ok = None

    # ======================================================================
    # PRIORIDAD 1: Enviar por la COLA del BROKER (dispara bridge A→B)
    # ======================================================================
    send_ok = False
    packet_id = None
    send_error = None
    used_path = "broker-queue"

    try:
        res = await asyncio.to_thread(
            _send_via_broker_queue,
            texto,                 # text
            int(canal),            # ch
            (node_id or None),     # '!ID' o None/broadcast
            False                  # wantAck=False aquí (tu flujo original)
        )
        send_ok = bool(res.get("ok", False))
        if not send_ok:
            send_error = res.get("error") or "broker_queue_not_ok"
        # La cola del broker no devuelve packet_id -> queda None (tu lógica ya lo contempla)
    except Exception as e:
        send_ok = False
        send_error = f"{type(e).__name__}: {e}"

    # ======================================================================
    # PRIORIDAD 2: Fallback → MISMA conexión persistente (pool)
    # ======================================================================
    if not send_ok:
        used_path = "pool-persistente"
        try:
            pool_cls = None
            try:
                pool_cls = context.application.bot_data.get("tcp_pool")
            except Exception:
                pool_cls = None

            host = context.bot_data.get("mesh_host")
            port = context.bot_data.get("mesh_port", 4403)
            timeout_iface = 6.0

            iface = None
            if pool_cls is not None:
                # 1) get_iface_wait (o espera manual hasta 6s)
                try:
                    if hasattr(pool_cls, "get_iface_wait"):
                        iface = pool_cls.get_iface_wait(timeout=timeout_iface, interval=0.3)
                    else:
                        import time as _t
                        t_end = time.time() + timeout_iface
                        while time.time() < t_end:
                            if hasattr(pool_cls, "get_iface"):
                                iface = pool_cls.get_iface()
                            elif hasattr(pool_cls, "get_interface"):
                                iface = pool_cls.get_interface()
                            else:
                                iface = getattr(pool_cls, "iface", None)
                            if iface is not None:
                                break
                            _t.sleep(0.3)
                except Exception:
                    iface = None

                # 2) si no hay iface: ensure_connected + reintento de get_iface
                if iface is None:
                    try:
                        ensure_fn = getattr(pool_cls, "ensure_connected", None)
                        if callable(ensure_fn) and host:
                            ensure_fn(host, port, timeout=timeout_iface)
                        if hasattr(pool_cls, "get_iface"):
                            iface = pool_cls.get_iface()
                        elif hasattr(pool_cls, "get_interface"):
                            iface = pool_cls.get_interface()
                        else:
                            iface = getattr(pool_cls, "iface", None)
                    except Exception:
                        iface = None

            if iface is not None:
                # ⚠️ CORREGIDO: broadcast => destinationId=None (NO "^all")
                dest_for_send = node_id if node_id else None
                pkt = iface.sendText(
                    texto,
                    destinationId=dest_for_send,
                    wantAck=False,
                    wantResponse=False,
                    channelIndex=int(canal),
                )
                # Extraer packet_id de forma robusta
                if isinstance(pkt, dict):
                    packet_id = pkt.get("id") or ((pkt.get("_packet") or {}).get("id"))
                else:
                    packet_id = getattr(pkt, "id", None)
                try:
                    packet_id = int(packet_id) if packet_id is not None else None
                except Exception:
                    packet_id = None

                send_ok = True
            else:
                send_ok = False
                send_error = send_error or "no_iface_pool"

        except Exception as e:
            send_ok = False
            send_error = f"{type(e).__name__}: {e}"

    # ======================================================================
    # PRIORIDAD 3: Fallback → adapter resiliente del pool (tu flujo existente)
    # ======================================================================
    if not send_ok:
        used_path = "api-pool+retry"
        try:
            try:
                from meshtastic_api_adapter import send_text_simple_with_retry_resilient as _send
            except Exception:
                from meshtastic_api_adapter import send_text_simple_with_retry as _send

            res = _send(
                host=MESHTASTIC_HOST,
                port=4403,
                text=texto,
                dest_id=(node_id or None),  # broadcast = None (ya estaba bien aquí)
                channel_index=int(canal),
                want_ack=False
            )
            send_ok = bool(res.get("ok"))
            packet_id = res.get("packet_id")
            if not send_ok:
                send_error = res.get("error") or str(res)
        except Exception as e:
            send_ok = False
            send_error = f"{type(e).__name__}: {e}"

    # --- Log CSV (igual que antes) ---
    try:
        SEND_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
        with SEND_LOG_CSV.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                (node_id or "broadcast"),
                canal,
                (packet_id or ""),
                ("OK" if send_ok else f"KO:{send_error or ''}"),
                texto.replace("\n", " ")[:200]
            ])
    except Exception:
        pass

    # --- Respuesta al usuario ---
    dst_txt = "broadcast" if is_broadcast else _friendly_node(node_id, nodes_map)
    tag_tr_ok = ("✔️" if traceroute_ok else ("❌" if traceroute_ok is False else "—"))
    tag_forzado = "Sí" if forced_flag else "No"

    # Escucha corta no bloqueante (solo para broadcast)
    replies = 0
    try:
        if is_broadcast and SEND_LISTEN_SEC > 0:
            replies = await _collect_replies_nonblocking(SEND_LISTEN_SEC)
    except Exception:
        replies = 0

    # Confirmación local si hay packet_id en OK
    local_tx_line = ""
    if send_ok and packet_id:
        local_tx_line = "✅ Nodo local confirmó transmisión (mensaje emitido por radio)\n"

    if send_ok:
        txt = (
            f"✉️ Envío a {('broadcast' if is_broadcast else 'nodo')} (canal {canal})\n"
            f"Destino: <b>{escape(dst_txt)}</b>\n"
            f"Traceroute: {tag_tr_ok}  Hops: {hops}\n"
            f"Forzado: {tag_forzado}\n"
            f"Resultado: <b>OK</b> ({used_path})"
            f"{f' • packet_id={packet_id}' if packet_id else ''}\n"
            f"{local_tx_line}"
        )
        if is_broadcast:
            txt += f"Respuestas en {SEND_LISTEN_SEC}s: <b>{replies}</b>"
        await _safe_reply_html(msg, txt)
    else:
        txt = (
            f"✉️ Envío a {('broadcast' if is_broadcast else 'nodo')} (canal {canal})\n"
            f"Destino: <b>{escape(dst_txt)}</b>\n"
            f"Traceroute: {tag_tr_ok}  Hops: {hops}\n"
            f"Forzado: {tag_forzado}\n"
            f"Resultado: <b>KO</b> ({used_path}): {escape(send_error or 'desconocido')}\n"
        )
        if is_broadcast:
            txt += f"Respuestas en {SEND_LISTEN_SEC}s: <b>{replies}</b>"
        await _safe_reply_html(msg, txt)

    return ConversationHandler.END

async def enviar_ack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /enviar_ack [reintentos=N espera=S backoff=X] <dest|broadcast[:canal] | canal N> <texto…>
      - Unicast (!id/alias/índice): intenta usar broker-queue con ACK; si no está disponible, usa pool con waitForAck y fallback de reintentos.
      - Broadcast (explícito o 'canal N'): no existe ACK de aplicación → broker-queue primero para disparar bridge A→B.
    """
    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    bump_stat(update.effective_user.id, update.effective_user.username or "", "enviar_ack")

    args = context.args or []
    attempts, wait_s, backoff, rest = _extract_ack_params(args)

    # Mapa alias/índice → !id
    nodes_map = context.user_data.get("nodes_map")
    if not nodes_map:
        nodes_map = build_nodes_mapping(80)
        context.user_data["nodes_map"] = nodes_map

    # ❗ Resolver sin tocar 'args'. Si tras 'canal N' no hay destino, será broadcast implícito
    node_id, canal, texto, _forced = parse_dest_channel_and_text(rest, nodes_map)

    if not texto:
        await update.effective_message.reply_text(
            "Uso: /enviar_ack [reintentos=N espera=S backoff=X] "
            "<número|!id|alias|broadcast[:canal] | canal N> <texto…>"
        )
        return ConversationHandler.END

    # ===== BROADCAST (no hay ACK de aplicación) =====
    if node_id is None:
        out = None
        pid = None

        # PRIORIDAD 1: broker-queue (dispara bridge A→B)
        used_path = "broker-queue"
        try:
            res = await asyncio.to_thread(
                _send_via_broker_queue,
                texto,                 # text
                int(canal),            # ch
                None,                  # broadcast
                False                  # ack=False, no hay ACK de app en broadcast
            )
            if bool(res.get("ok", False)):
                out = "OK (broker-queue)"
                pid = None  # el broker-queue no devuelve packet_id
            else:
                out = None
        except Exception:
            out = None

        # PRIORIDAD 2: pool persistente (como antes) si broker-queue no está
        if out is None:
            used_path = "pool-persistente"
            try:
                pool_cls = context.application.bot_data.get("tcp_pool")
                iface = None
                if pool_cls is not None:
                    if hasattr(pool_cls, "get_iface_wait"):
                        iface = pool_cls.get_iface_wait(timeout=3.0, interval=0.3)
                    else:
                        import time as _t
                        for _ in range(10):
                            if hasattr(pool_cls, "get_iface"):
                                iface = pool_cls.get_iface()
                            elif hasattr(pool_cls, "get_interface"):
                                iface = pool_cls.get_interface()
                            else:
                                iface = getattr(pool_cls, "iface", None)
                            if iface is not None:
                                break
                            _t.sleep(0.3)
                if iface is not None:
                    # 🛠️ Broadcast correcto: destinationId=None (NO '^all')
                    pkt = iface.sendText(
                        texto,
                        destinationId=None,
                        wantAck=False,
                        wantResponse=False,
                        channelIndex=int(canal),
                    )
                    if isinstance(pkt, dict):
                        pid = pkt.get("id") or ((pkt.get("_packet") or {}).get("id"))
                    else:
                        pid = getattr(pkt, "id", None)
                    try:
                        pid = int(pid) if pid is not None else None
                    except Exception:
                        pid = None
                    out = "OK (pool persistente)"
            except Exception:
                out = None
                pid = None

        # PRIORIDAD 3: adapter resiliente si tampoco hubo pool
        if out is None:
            used_path = "api-pool+retry"
            out, pid = send_text_message(None, texto, canal=canal)
            if out:
                out = f"{out} (api-pool+retry)"

        # “Confirmación de red” opcional vía broker (no es ACK de app)
        ack_cloud = ""
        ack_ok = False
        reason = "BROADCAST_NO_ACK"
        if pid is not None:
            ok_ack, reason_b, ack_from = await _wait_ack_from_broker(int(pid), int(ACK_WAIT_SEC))
            if ok_ack:
                alias_map = _build_alias_fallback_from_nodes_file()
                ali = alias_map.get(ack_from or "", "")
                who = f"{ali} ({ack_from})" if ali else (ack_from or "¿?")
                ack_cloud = f"\nConfirmación de red: ✅ {who}"
                ack_ok = True
                reason = "CLOUD_OK"
            else:
                ack_cloud = "\nConfirmación de red: ⚠️ (sin confirmación en tiempo)"
                reason = reason_b or "TIMEOUT"

        respuestas = await quick_broker_listen(None, canal, SEND_LISTEN_SEC)

        resumen = (
            f"✉️ Envío a broadcast (canal {canal})\n"
            f"Resultado: {out or 'KO'} • vía {used_path}{ack_cloud}\n"
            f"Respuestas en {SEND_LISTEN_SEC}s: {respuestas}"
        )
        for ch in chunk_text(resumen):
            await send_pre(update.effective_message, ch)

        _append_send_ack_log_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "broadcast", canal,
            (texto[:200] + "…") if len(texto) > 200 else texto,
            1,
            "1" if ack_ok else "0",
            reason,
            pid or "",
        ])
        return ConversationHandler.END

    # ===== UNICAST con ACK y reintentos =====
    traceroute_ok = None
    hops = 0
    if TRACEROUTE_CHECK:
        try:
            res = traceroute_node(node_id, timeout=min(TRACEROUTE_TIMEOUT, 25))
            traceroute_ok = bool(res.ok)
            hops = int(res.hops or 0)
        except Exception:
            traceroute_ok = None
            hops = 0

    # PRIORIDAD 1: broker-queue con ack=True (dispara bridge A→B)
    #   Nota: el broker-queue no devuelve packet_id; por tanto aquí reportamos "queued".
    used_path = "broker-queue"
    result = None
    try:
        res = await asyncio.to_thread(
            _send_via_broker_queue,
            texto,
            int(canal),
            node_id,     # unicast
            True         # wantAck=True (el broker pedirá ACK al nodo destino)
        )
        if bool(res.get("ok", False)):
            result = {
                "ok": True,              # marcado OK por encolado y solicitud con ACK
                "attempts": 1,
                "reason": "BROKER_QUEUED",  # no hay packet_id aquí
                "packet_id": None,
            }
    except Exception:
        result = None

    # PRIORIDAD 2: pool persistente con waitForAck si broker-queue no está
    if result is None:
        used_path = "pool-persistente"
        try:
            pool_cls = context.application.bot_data.get("tcp_pool")
            iface = None
            if pool_cls is not None:
                if hasattr(pool_cls, "get_iface_wait"):
                    iface = pool_cls.get_iface_wait(timeout=3.0, interval=0.3)
                else:
                    import time as _t
                    for _ in range(10):
                        if hasattr(pool_cls, "get_iface"):
                            iface = pool_cls.get_iface()
                        elif hasattr(pool_cls, "get_interface"):
                            iface = pool_cls.get_interface()
                        else:
                            iface = getattr(pool_cls, "iface", None)
                        if iface is not None:
                            break
                        _t.sleep(0.3)
            if iface is not None:
                pkt = iface.sendText(
                    texto,
                    destinationId=node_id,   # unicast
                    wantAck=True,
                    wantResponse=False,
                    channelIndex=int(canal),
                )
                pid = None
                if isinstance(pkt, dict):
                    pid = pkt.get("id") or ((pkt.get("_packet") or {}).get("id"))
                else:
                    pid = getattr(pkt, "id", None)
                try:
                    pid = int(pid) if pid is not None else None
                except Exception:
                    pid = None

                ok_ack = False
                if pid is not None and hasattr(iface, "waitForAck"):
                    try:
                        ok_ack = bool(iface.waitForAck(pid, timeout=15.0))
                    except Exception:
                        ok_ack = False

                result = {
                    "ok": bool(ok_ack),
                    "attempts": 1,
                    "reason": ("POOL_OK" if ok_ack else "NO_APP_ACK"),
                    "packet_id": pid,
                }
        except Exception:
            result = None

    # PRIORIDAD 3: reintentos/backoff por adapter resiliente
    if result is None:
        used_path = "api-pool+retry"
        result = await send_with_ack_retry(node_id, texto, canal, attempts, wait_s, backoff)

    dest_txt = node_id
    if result.get("ok"):
        resumen = (
            f"✅ ACK enviado/recibido para {dest_txt} (canal {canal})\n"
            f"Intentos: {result['attempts']}  •  packet_id: {result.get('packet_id')}\n"
            f"Vía: {used_path}"
        )
    else:
        resumen = (
            f"⚠️ Sin ACK para {dest_txt} (canal {canal})\n"
            f"Intentos: {result.get('attempts', '?')}  •  Motivo: {result.get('reason','')}\n"
            f"packet_id: {result.get('packet_id')}  •  Vía: {used_path}"
        )

    for ch in chunk_text(resumen):
        await send_pre(update.effective_message, ch)

    _append_send_ack_log_row([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        dest_txt,
        canal,
        (texto[:200] + "…") if len(texto) > 200 else texto,
        result.get("attempts"),
        "1" if result.get("ok") else "0",
        result.get("reason", ""),
        result.get("packet_id", ""),
    ])
    return ConversationHandler.END


def _is_admin(user_id: int) -> bool:
    admins = os.getenv("ADMIN_IDS", "")
    ids = {int(x) for x in admins.replace(";", ",").split(",") if x.strip().isdigit()}
    return user_id in ids

async def notificaciones_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /notificaciones [on|off|estado]  → Activa/Desactiva o muestra el estado
    Alias: /notify, /notifs
    Solo administradores (ADMIN_IDS).
    """
    global NOTIFY_DONE_ENABLED
    user = update.effective_user
    if not user:
        await update.effective_message.reply_text("⛔ No se pudo identificar al usuario.")
        return
    # Aviso si no es admin, pero NO salimos (para verificar que el handler responde)
    is_admin = _is_admin(user.id)
    if not is_admin:
        await update.effective_message.reply_text("⚠️ No eres admin; sólo verás el estado.", disable_web_page_preview=True)


    arg = (context.args[0].strip().lower() if context.args else "estado")

    if arg in ("on", "activar", "1", "true", "sí", "si"):
        NOTIFY_DONE_ENABLED = True
        s = _load_bot_settings()
        s["notify_done_enabled"] = True
        _save_bot_settings(s)
        await update.effective_message.reply_text("🔔 Notificaciones de tareas: **ACTIVADAS**", parse_mode="Markdown")
    elif arg in ("off", "desactivar", "0", "false", "no"):
        NOTIFY_DONE_ENABLED = False
        s = _load_bot_settings()
        s["notify_done_enabled"] = False
        _save_bot_settings(s)
        await update.effective_message.reply_text("🔕 Notificaciones de tareas: **DESACTIVADAS**", parse_mode="Markdown")
    else:
        estado = "ACTIVADAS 🔔" if NOTIFY_DONE_ENABLED else "DESACTIVADAS 🔕"
        fuente = "persistente" if "notify_done_enabled" in _load_bot_settings() else "(.env)"
        await update.effective_message.reply_text(
            f"Estado actual: **{estado}**\nOrigen: {fuente}\n\nUso: /notificaciones on | off | estado",
            parse_mode="Markdown"
        )




# === NUEVO: render de vecinos directos (broker) con SNR/RSSI/last_seen ===
from html import escape as _esc

def _render_direct_neighbors_broker(max_mins: int, snr_min: float | None, max_n: int) -> tuple[str, list[tuple[str, str, float | None, float | None, int | None]]]:
    """
    Devuelve (texto_html, filas) de vecinos directos usando las métricas del broker/API.
    filas = [(nid, alias, snr, rssi, last_seen_min)]
    - Filtra por 'max_mins' (visto en los últimos N minutos).
    - Filtra por 'snr_min' si se indica.
    - Ordena por recencia (menor last_seen_min primero).
    - Limita a 'max_n'.
    """
    try:
        # 1) Métricas de vecinos directos por API/pool persistente
        neigh = api_get_neighbors_via_pool(MESHTASTIC_HOST, 4403) or {}
    except Exception:
        neigh = {}

    # 2) Mapa de "últimos vistos" (API + carencia vía backlog)
    last_seen_map = _build_last_seen_map_api_with_broker_fallback(max_n=300, timeout_sec=5.0, lookback_hours=12)

    rows: list[tuple[str, str, float | None, float | None, int | None]] = []
    for raw_id, info in neigh.items():
        try:
            nid = raw_id if str(raw_id).startswith("!") else f"!{int(raw_id):08x}"
        except Exception:
            nid = str(raw_id) if str(raw_id).startswith("!") else f"!{str(raw_id)}"

        alias = (info.get("alias") or info.get("name") or info.get("longName") or info.get("shortName") or nid).strip()
        snr = info.get("snr")
        rssi = info.get("rssi")
        last_m = last_seen_map.get(nid)

        # filtro por "últimos N minutos" si tenemos minuto
        if isinstance(last_m, int) and max_mins is not None:
            if last_m > int(max_mins):
                continue

        # filtro SNR mínimo si procede
        if (snr_min is not None) and (snr is not None):
            try:
                if float(snr) < float(snr_min):
                    continue
            except Exception:
                pass

        rows.append((nid, alias, (None if snr is None else float(snr)),
                              (None if rssi is None else float(rssi)),
                              (None if last_m is None else int(last_m))))

    # Orden por recencia (last_seen_min asc; None al final)
    def _key(r):
        lm = r[4]
        return (0, lm) if isinstance(lm, int) else (1, 1_000_000)

    rows.sort(key=_key)
    rows = rows[:max(1, int(max_n))]

    # Render
    if not rows:
        return "🧭 <b>Vecinos directos (broker)</b>:\n\n(sin coincidencias)", []

    lines = ["🧭 <b>Vecinos directos (broker)</b>:\n"]
    for i, (nid, alias, snr, rssi, last_m) in enumerate(rows, start=1):
        seen_txt = "—"
        if isinstance(last_m, int):
            if last_m <= 1:
                seen_txt = "hace ≤1m"
            else:
                seen_txt = f"hace {last_m}m"
        snr_txt  = "—" if snr  is None else f"{snr:.2f} dB"
        rssi_txt = "—" if rssi is None else f"{rssi:.0f} dBm"
        lines.append(f"{i}. {_esc(alias)} ({_esc(nid)}) — RSSI: {rssi_txt} — SNR: {snr_txt} — hops: 0 — visto {seen_txt}")

    return "\n".join(lines), rows

def _parse_hops_filter(token: str) -> tuple[int | None, int | None]:
    """
    Parsea expresiones como:
      '=0', '0', '>=1', '<=3', '>2', '<4'
    Devuelve (min_hops, max_hops), donde None significa sin límite.
    Ejemplos:
      '>=1' → (1, None)
      '<=3' → (None, 3)
      '=0' o '0' → (0, 0)
      '>2' → (3, None)
      '<4' → (None, 3)
      '5'  → (5, 5)
    """
    if not token:
        return None, None

    import re
    s = token.strip().replace(" ", "")
    m = re.match(r'^(>=|<=|=|>|<)?\s*(\d+)$', s)
    if not m:
        return None, None

    op, num_s = m.groups()
    try:
        n = int(num_s)
    except Exception:
        return None, None

    if op in (None, "=", ""):
        return n, n
    if op == ">=":
        return n, None
    if op == "<=":
        return None, n
    if op == ">":
        return n + 1, None
    if op == "<":
        return None, n - 1 if n > 0 else 0
    return None, None



async def vecinos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /vecinos [max_n] [hops_max] [timeout]
    Igual que /ver_nodos en lógica, formato y enriquecimiento (km + ciudad/provincia),
    con la ÚNICA diferencia de filtrar por hops <= hops_max si se indica.
    Parámetros:
      - max_n     : cantidad máxima a mostrar (por defecto 20)
      - hops_max  : filtra nodos con hops <= hops_max (por defecto None = sin filtro)
      - timeout   : opcional, sólo para el tercer fallback (pool TCP). Por defecto 4.0 s
    """
    bump_stat(update.effective_user.id, update.effective_user.username or "", "vecinos")

    # 0) .env
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path="/app/.env", override=True)
    except Exception:
        pass

    # 1) Args
    args = context.args or []

    def _to_int(x, default=None):
        try:
            return int(x) if str(x).lstrip("-").isdigit() else default
        except Exception:
            return default

    def _is_num_str(s: str) -> bool:
        if s is None:
            return False
        ss = str(s).strip()
        return ss.count(".") <= 1 and ss.replace(".", "", 1).lstrip("-").isdigit()

    max_n    = _to_int(args[0] if len(args) > 0 else None, 20)
    hops_max = _to_int(args[1] if len(args) > 1 else None, None)

    try:
        timeout = float(args[2]) if (len(args) > 2 and _is_num_str(args[2])) else 4.0
    except Exception:
        timeout = 4.0

    pool = context.bot_data.get("tcp_pool")
    host = context.bot_data.get("mesh_host")
    port = context.bot_data.get("mesh_port", 4403)
    if not host:
        await update.effective_message.reply_text("⚠️ Config no inicializada (host).")
        return ConversationHandler.END

    now = int(time.time())
    now2 = int(time.time())

    # ---------- Helpers ----------
    def _fmt_db(val, unit):
        try: return f"{float(val):.1f} {unit}"
        except Exception: return "—"

    def fmt_ago(sec):
        if sec is None: return "—"
        m, s = divmod(max(0, int(sec)), 60)
        h, m = divmod(m, 60)
        if h: return f"{h}h {m}m"
        if m: return f"{m}m {s}s"
        return f"{s}s"

    def _get_any(d: dict, *keys, default=None):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    def _compute_hops_relaxed(evt: dict) -> int | None:
        try:
            hl = _get_any(evt, "hop_limit", "hopLimit")
            hs = _get_any(evt, "hop_start", "hopStart")
            if hl is None or hs is None:
                r0 = evt.get("routing") or {}
                if hl is None: hl = _get_any(r0, "hop_limit", "hopLimit")
                if hs is None: hs = _get_any(r0, "hop_start", "hopStart")
            if hl is None or hs is None:
                return None
            hl = int(hl); hs = int(hs)
            return max(0, min(hl - hs, 7))
        except Exception:
            return None

    def _norm_id(s: str) -> str:
        s = (s or "").strip()
        if not s: return s
        return s if s.startswith("!") else (f"!{s[-8:]}" if len(s) >= 8 else f"!{s}")

    def _to_float_coord(v) -> float | None:
        if v is None: return None
        try:
            if isinstance(v, (int, float)): return float(v)
            s = str(v).strip().replace(",", ".")
            s = "".join(ch for ch in s if ch in "+-0123456789.")
            if s in ("", "+", "-"): return None
            return float(s)
        except Exception:
            return None

    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float | None:
        try:
            R = 6371.0
            dlat = math.radians(float(lat2) - float(lat1))
            dlon = math.radians(float(lon2) - float(lon1))
            a = math.sin(dlat/2)**2 + math.cos(math.radians(float(lat1))) * math.cos(math.radians(float(lat2))) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            return round(R * c, 1)
        except Exception:
            return None

    _rg_resolver = None
    def _ensure_rg():
        nonlocal _rg_resolver
        if _rg_resolver is not None: return
        try:
            import reverse_geocoder as rg
            def _rg(lat: float, lon: float) -> str | None:
                try:
                    res = rg.search([(float(lat), float(lon))])
                    if isinstance(res, list) and res:
                        r = res[0]
                        return r.get("name") or r.get("admin2") or r.get("admin1") or None
                except Exception:
                    return None
                return None
            _rg_resolver = _rg
        except Exception:
            _rg_resolver = None

    def _place_of(lat: float | None, lon: float | None) -> str | None:
        if lat is None or lon is None: return None
        _ensure_rg()
        if _rg_resolver is None: return None
        try: return _rg_resolver(lat, lon)
        except Exception: return None

    def _extract_pos(ev: dict):
        try:
            pkt = ev.get("packet") if isinstance(ev, dict) else None
            nid = _norm_id(
                _get_any(ev, "from", "fromId", "nodeId", "id")
                or (_get_any(pkt or {}, "from") if isinstance(pkt, dict) else None)
            )
            lat = _get_any(ev, "lat", "latitude", "Latitude")
            lon = _get_any(ev, "lon", "longitude", "Longitude")
            if lat is None and _get_any(ev, "latitudeI") is not None:
                try:
                    lat = float(ev["latitudeI"]) / 1e7
                    lon = float(_get_any(ev, "longitudeI") or 0.0) / 1e7
                except Exception:
                    lat = lon = None
            if (lat is None or lon is None) and isinstance(pkt, dict):
                dec = pkt.get("decoded") or {}
                pos = dec.get("position") or {}
                if pos:
                    if pos.get("latitudeI") is not None:
                        try:
                            lat = float(pos["latitudeI"]) / 1e7
                            lon = float(_get_any(pos, "longitudeI") or 0.0) / 1e7
                        except Exception:
                            lat = lon = None
                    else:
                        lat = lat or _get_any(pos, "lat", "latitude")
                        lon = lon or _get_any(pos, "lon", "longitude")
            la = _to_float_coord(lat); lo = _to_float_coord(lon)
            ts = _get_any(ev, "ts", "time", "rx_time", "rxTime")
            if ts is None and isinstance(pkt, dict):
                ts = _get_any(pkt, "rxTime") or _get_any((pkt.get("decoded") or {}).get("position") or {}, "time")
            try:
                ts = int(ts) if ts is not None else None
            except Exception:
                ts = None
            return nid, la, lo, ts
        except Exception:
            return None, None, None, None
    # -----------------------------

    # ====================================================
    # 1) Broker LIST_NODES + FETCH_BACKLOG
    # ====================================================
    try:
        r = _broker_ctrl("LIST_NODES", {"limit": max(50, max_n * 2)}, timeout=min(10.0, max(2.0, timeout + 1.0)))
        if r and r.get("ok"):
            data = r.get("data") or []

            norm = []
            for n in data:
                nid   = _norm_id(n.get("nodeId") or n.get("id") or n.get("fromId"))
                alias = (n.get("alias") or nid or "").strip()
                snr   = _get_any(n, "snr","SNR","rx_snr","rxSNR")
                rssi  = _get_any(n, "rssi","RSSI","rx_rssi","rxRSSI")
                last  = _get_any(n, "last","lastHeard","last_heard","heard","last_seen","ts")
                try:
                    last = int(last) if last is not None else None
                except Exception:
                    last = None

                hops = _get_any(n, "hops","HOPS","hop_count","hopCount")
                if hops is None:
                    try:
                        hops = _compute_real_hops(n)  # si existe
                    except Exception:
                        hops = None
                if hops is None:
                    hops = _compute_hops_relaxed(n)
                try:
                    hops = int(hops) if hops is not None else 0
                except Exception:
                    hops = 0

                norm.append({"id": nid, "alias": alias, "snr": snr, "rssi": rssi, "last": last, "hops": hops})

            # Filtro inicial
            if hops_max is not None:
                try:
                    hmax = int(hops_max)
                    norm = [n for n in norm if (n.get("hops") is not None and int(n["hops"]) <= hmax)]
                except Exception:
                    pass

            # Backlog → last/pos
            lastmap: dict[str, int] = {}
            posmap:  dict[str, tuple[float, float]] = {}
            try:
                since_ts = now2 - 12*3600
                bl = _broker_ctrl(
                    "FETCH_BACKLOG",
                    {"since_ts": since_ts,
                     "portnums": ["TEXT_MESSAGE_APP", "POSITION_APP", "TELEMETRY_APP", "NODEINFO_APP"]},
                    timeout=5.0
                )
                if bl and bl.get("ok"):
                    for ev in (bl.get("data") or []):
                        nid_ev, la_ev, lo_ev, ts_ev = _extract_pos(ev)
                        if nid_ev and ts_ev is not None:
                            if (nid_ev not in lastmap) or (ts_ev > lastmap[nid_ev]):
                                lastmap[nid_ev] = ts_ev
                        if nid_ev and la_ev is not None and lo_ev is not None:
                            posmap[nid_ev] = (la_ev, lo_ev)
            except Exception:
                pass

            # nodos.txt → enriquecer (hops/pos)
            try:
                rows_file = _parse_nodes_table(NODES_FILE) or []
            except Exception:
                rows_file = []

            try:
                def _to_int_hops(v) -> int | None:
                    if v is None: return None
                    try:
                        s = str(v).strip().lower()
                        for junk in ("hops","hop","≈","~"): s = s.replace(junk,"")
                        s = s.replace(",", ".")
                        s = "".join(ch for ch in s if ch in "+-0123456789.")
                        if s in ("", "+", "-"): return None
                        i = int(round(float(s)))
                        return i if i >= 0 else None
                    except Exception:
                        return None

                hops_map: dict[str, int] = {}
                for rf in rows_file:
                    nid_f = _norm_id(rf.get("id") or rf.get("nodeId") or rf.get("fromId"))
                    if not nid_f: continue
                    hv = None
                    for k in ("hops","HOPS","Hops","hop","Hop","HOP","hops_text","hopsText"):
                        if k in rf and rf[k] is not None:
                            hv = _to_int_hops(rf[k]); 
                            if hv is not None: break
                    if nid_f and hv is not None:
                        hops_map[nid_f] = hv

                if hops_map:
                    for x in norm:
                        nid_x = _norm_id(x.get("id"))
                        if nid_x in hops_map and (x.get("hops") in (None, 0)):
                            x["hops"] = hops_map[nid_x]
            except Exception:
                pass

            try:
                for rf in rows_file:
                    nid = _norm_id(rf.get("id") or rf.get("nodeId") or rf.get("fromId"))
                    if not nid: continue
                    lat = (rf.get("Latitude") or rf.get("lat") or rf.get("latitude"))
                    lon = (rf.get("Longitude") or rf.get("lon") or rf.get("longitude"))
                    if (lat is None or lon is None) and (rf.get("latitudeI") is not None):
                        try:
                            lat = float(rf["latitudeI"]) / 1e7
                            lon = float(rf.get("longitudeI") or 0.0) / 1e7
                        except Exception:
                            lat = lon = None
                    lat_f = _to_float_coord(lat); lon_f = _to_float_coord(lon)
                    if lat_f is not None and lon_f is not None:
                        posmap[nid] = (lat_f, lon_f)
            except Exception:
                pass

            # Calcular ago/orden
            for x in norm:
                x["ago"] = (now2 - x["last"]) if x["last"] is not None else None

            # === GUARD HOPS: volver a filtrar justo antes de pintar ===
            if hops_max is not None:
                try:
                    hmax = int(hops_max)
                    norm = [n for n in norm if (n.get("hops") is not None and int(n["hops"]) <= hmax)]
                except Exception:
                    pass
            # ==========================================================

            norm.sort(key=lambda x: (x["ago"] if x["ago"] is not None else 10**9))
            if max_n and max_n > 0:
                norm = norm[:max_n]

            # HOME
            try:
                home_lat, home_lon = _get_home_coords(context, posmap=posmap, lastmap=lastmap)
            except Exception:
                home_lat = _to_float_coord(os.getenv("HOME_LAT"))
                home_lon = _to_float_coord(os.getenv("HOME_LON"))
                if (home_lat is None or home_lon is None) and posmap:
                    try:
                        best_nid = None; best_ts = -1
                        for nid_k, ts_v in (lastmap or {}).items():
                            if nid_k in posmap and ts_v > best_ts:
                                best_ts = ts_v; best_nid = nid_k
                        if best_nid:
                            home_lat, home_lon = posmap[best_nid]
                        else:
                            nid0 = next(iter(posmap.keys()))
                            home_lat, home_lon = posmap[nid0]
                    except Exception:
                        pass

            # Render
            lines = []
            for i, n0 in enumerate(norm, 1):
                nid   = n0["id"]
                alias = n0["alias"]
                snr   = n0.get("snr")
                rssi  = n0.get("rssi")
                hops  = n0.get("hops", 0)
                ago_t = fmt_ago(n0.get("ago"))

                # NUEVO: calcular calidad de enlace a partir del SNR
                quality = _snr_quality_label(snr)
                
                dist_txt = "?"
                place_txt = "?"

                try:
                    lat = lon = None
                    if nid in posmap:
                        lat, lon = posmap[nid]

                    def _f(v):
                        try:
                            if v is None: return None
                            if isinstance(v, (int, float)): return float(v)
                            s = str(v).strip().replace(",", ".")
                            s = "".join(ch for ch in s if ch in "+-0123456789.")
                            if s in ("", "+", "-"): return None
                            return float(s)
                        except Exception:
                            return None

                    la = _f(home_lat); lo = _f(home_lon)
                    lt = _f(lat);      ln = _f(lon)

                    if la is not None and lo is not None and lt is not None and ln is not None:
                        dkm = _haversine_km(la, lo, lt, ln)
                        if dkm is not None:
                            dist_txt = f"{dkm:.1f}"

                    if lt is not None and ln is not None:
                        try:
                            p = _place_of(lt, ln) or _get_province_offline(lt, ln)
                        except Exception:
                            p = None
                        if p:
                            place_txt = p
                except Exception:
                    pass

                parts = [
                    f"{i}. {alias} ({nid}) - ",
                    f"Visto hace {ago_t}\n",
                    f" SNR: {_fmt_db(snr,'dB')} ({quality})\n",
                    f" hops: {hops}\n",
                    f"📍 <b>{dist_txt}</b> km — <b>{place_txt}</b>",
                ]
                lines.append("".join(parts))

            await update.effective_message.reply_text(
                "📡 Últimos vecinos (broker):\n\n" + ("\n\n".join(lines) if lines else "(sin datos)"),
                parse_mode="HTML"
            )
            return ConversationHandler.END
    except Exception:
        pass  # broker falló → seguimos

    # ====================================================
    # 2) FALLBACK: nodos.txt + filtro hops
    # ====================================================
    try:
        tuples = get_visible_nodes_with_hops(NODES_FILE)

        # filtro temprano
        if hops_max is not None:
            def _to_int_hops(v):
                if v is None: return None
                try:
                    s = str(v).strip().lower()
                    for junk in ("hops","hop","≈","~"): s = s.replace(junk,"")
                    s = s.replace(",", ".")
                    s = "".join(ch for ch in s if ch in "+-0123456789.")
                    return int(float(s))
                except Exception:
                    return None
            try:
                hmax = int(hops_max)
                tuples = [t for t in tuples if (t[3] is not None and _to_int_hops(t[3]) is not None and _to_int_hops(t[3]) <= hmax)]
            except Exception:
                pass

        if max_n and max_n > 0:
            tuples = tuples[:max_n]

        if tuples:
            posmap_file: dict[str, tuple[float,float]] = {}
            try:
                rows_file = _parse_nodes_table(NODES_FILE) or []
                for rf in rows_file:
                    nid = _norm_id(rf.get("id") or rf.get("nodeId") or rf.get("fromId"))
                    if not nid: continue
                    lat = (rf.get("Latitude") or rf.get("lat") or rf.get("latitude"))
                    lon = (rf.get("Longitude") or rf.get("lon") or rf.get("longitude"))
                    if (lat is None or lon is None) and (rf.get("latitudeI") is not None):
                        try:
                            lat = float(rf["latitudeI"]) / 1e7
                            lon = float(rf.get("longitudeI") or 0.0) / 1e7
                        except Exception:
                            lat = lon = None
                    lat_f = _to_float_coord(lat); lon_f = _to_float_coord(lon)
                    if lat_f is not None and lon_f is not None:
                        posmap_file[nid] = (lat_f, lon_f)
            except Exception:
                posmap_file = {}

            home_lat = _to_float_coord(os.getenv("HOME_LAT"))
            home_lon = _to_float_coord(os.getenv("HOME_LON"))
            if (home_lat is None or home_lon is None) and posmap_file:
                try:
                    _, (la0, lo0) = next(iter(posmap_file.items()))
                    home_lat, home_lon = la0, lo0
                except Exception:
                    pass

            # === GUARD HOPS también aquí justo antes de pintar ===
            if hops_max is not None:
                try:
                    hmax = int(hops_max)
                    tuples = [t for t in tuples if (t[3] is not None and _to_int_hops(t[3]) is not None and _to_int_hops(t[3]) <= hmax)]
                except Exception:
                    pass
            # =====================================================

            lines_out = []
            for i, (nid, alias, mins, hops) in enumerate(tuples, start=1):
                nid = _norm_id(nid); alias = (alias or nid).strip()
                mins_i = parse_minutes(mins) if mins is not None else 0
                ago_t = fmt_ago(mins_i * 60 if isinstance(mins_i, (int, float)) else None)
                hops_t = f"{hops}" if (hops is not None and str(hops).strip() != "") else "?"

                dist_txt = "?"
                place_txt = "?"
                if nid in posmap_file and home_lat is not None and home_lon is not None:
                    lat, lon = posmap_file[nid]
                    dkm = _haversine_km(home_lat, home_lon, lat, lon)
                    if dkm is not None: dist_txt = f"{dkm:.1f}"
                    try:
                        p = _place_of(lat, lon) or _get_province_offline(lat, lon)
                    except Exception:
                        p = None
                    if p: place_txt = p

                lines_out.append(
                    f"{i}. {alias} ({nid}) — visto hace {ago_t} — hops: {hops_t} — 📍 {dist_txt} km — {place_txt}"
                )

            await update.effective_message.reply_text(
                "📡 Últimos vecinos (nodos.txt):\n\n" + ("\n\n".join(lines_out) if lines_out else "(sin datos)"),
                disable_web_page_preview=True
            )
            return ConversationHandler.END
    except Exception:
        pass

    # ====================================================
    # 3) Pool (compatibilidad)
    # ====================================================
    nodes = []
    def _extract_nodes_from_iface(iface):
        raw_nodes = getattr(iface, "nodes", None)
        iterable = raw_nodes.values() if isinstance(raw_nodes, dict) else (
            raw_nodes if isinstance(raw_nodes, list) else (getattr(iface, "getNodes", lambda: [])() or [])
        )
        out = []
        for n in (iterable or []):
            usr = n.get("user") or {}
            uid = usr.get("id") or n.get("id") or n.get("num") or n.get("nodeId")
            alias = usr.get("longName") or usr.get("shortName") or n.get("name") or uid or "¿sin_alias?"
            metrics = n.get("deviceMetrics") or n.get("metrics") or {}
            snr = metrics.get("snr", n.get("snr"))
            last_heard = n.get("lastHeard") or n.get("last_heard") or n.get("heard")
            last_heard = int(last_heard) if isinstance(last_heard, (int, float)) else 0
            ago = (now - last_heard) if last_heard else None
            out.append({"id": _norm_id(uid), "alias": alias, "snr": snr, "ago": ago})
        out.sort(key=lambda x: (x["ago"] if x["ago"] is not None else 10**9))
        if max_n and max_n > 0: out[:] = out[:max_n]
        return out

    try:
        iface = None
        if hasattr(pool, "get_iface_wait"):
            iface = pool.get_iface_wait(timeout=timeout, interval=0.3)
        else:
            t_end = time.time() + float(timeout)
            while time.time() < t_end:
                try:
                    iface = getattr(pool, "get_iface", getattr(pool, "get_interface", lambda *a, **k: None))()
                except Exception:
                    iface = getattr(pool, "iface", None)
                if iface is not None:
                    break
                time.sleep(0.3)
        if iface is not None:
            nodes = _extract_nodes_from_iface(iface)
    except Exception:
        nodes = []

    if not nodes:
        await update.effective_message.reply_text("📡 Últimos vecinos:\n\n(sin datos ahora mismo)")
        return ConversationHandler.END

    lines = []
    for i, n in enumerate(nodes, 1):
        alias = str(n.get("alias") or n.get("id") or "¿sin_alias?").strip()
        nid   = n.get("id") or "¿id?"
        snr   = n.get("snr")
        ago   = fmt_ago(n.get("ago"))
        snr_txt = f"{snr:.2f} dB" if isinstance(snr, (int, float)) else "—"

        # NUEVO: icono de calidad
        quality = _snr_quality_label(snr)

        lines.append(f"{i}. {alias} ({nid}) — SNR: {snr_txt} ({quality}) — visto hace {ago}")

    await update.effective_message.reply_text("📡 Últimos vecinos:\n\n" + "\n\n".join(lines))
    return ConversationHandler.END



# === REHECHA: /vecinosX (atajo) ===
async def vecinosX_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /vecinosX  → hops_max = X
    Admite opcionalmente 1 argumento: [max_n]
      p.ej.: /vecinos5 10  → hops ≤ 5, mostrar 10 nodos
    """
    try:
        cmd = (update.message.text or "").split()[0]  # ej. "/vecinos5"
        import re
        m = re.match(r"^/vecinos(\d+)$", cmd)
        if not m:
            await update.effective_message.reply_text("❌ Uso: /vecinosX donde X es el número de hops.")
            return ConversationHandler.END

        hops_max = int(m.group(1))
        # si el usuario añade 1 número adicional, lo tratamos como max_n
        args = context.args or []
        try:
            max_n = int(args[0]) if args and str(args[0]).lstrip("-").isdigit() else 20
        except Exception:
            max_n = 20

        # Reusar exactamente la misma lógica que /vecinos → inlínicamente:
        # Para no duplicar, simplemente reasignamos context.args y delegamos
        context.args = [str(max_n), str(hops_max)]
        return await vecinos_cmd(update, context)

    except Exception as e:
        try:
            await update.effective_message.reply_text(f"❌ Error en /vecinosX: {e}")
        except Exception:
            pass
        return ConversationHandler.END

# === NUEVO: helpers de paginación para Telegram (inline keyboard) ===
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from html import escape as _esc_html

PAGINATION_MAX_CHARS = 3900               # margen bajo el límite de Telegram
PAGINATION_STORE_TTL = 900                # 15 min, limpieza perezosa

def _build_pages_from_lines(header_html: str, lines: list[str], max_chars: int = PAGINATION_MAX_CHARS) -> list[str]:
    """
    Construye páginas HTML-safe combinando cabecera + líneas, sin exceder 'max_chars'.
    Devuelve lista de páginas (texto HTML).
    """
    header = (header_html or "").rstrip() + "\n\n" if header_html else ""
    pages = []
    cur = header
    for ln in lines:
        add = (ln + "\n")
        if len(cur) + len(add) > max_chars:
            pages.append(cur.rstrip())
            cur = ""
        cur += add
    if cur.strip():
        pages.append(cur.rstrip())
    return pages or [header.strip() or "(sin contenido)"]

def _pagination_keyboard(token: str, page_idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (page_idx - 1) % total
    next_idx = (page_idx + 1) % total
    txt = f"Página {page_idx+1}/{total}"
    kb = [
        [
            InlineKeyboardButton("⟵ Anterior", callback_data=f"vecinos:{token}:{prev_idx}"),
            InlineKeyboardButton(txt, callback_data=f"vecinos:{token}:{page_idx}"),
            InlineKeyboardButton("Siguiente ⟶", callback_data=f"vecinos:{token}:{next_idx}"),
        ]
    ]
    return InlineKeyboardMarkup(kb)

def _store_pages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, pages: list[str]) -> tuple[str, int]:
    """
    Guarda páginas en context.bot_data para recuperación por callback.
    Devuelve (token, created_ts).
    """
    token = uuid.uuid4().hex[:16]
    bucket = context.bot_data.setdefault("vecinos_pages", {})
    now = int(time.time())
    bucket[token] = {"pages": pages, "ts": now, "chat_id": int(chat_id)}
    # Limpieza perezosa
    try:
        for k, v in list(bucket.items()):
            if (now - int(v.get("ts", 0))) > PAGINATION_STORE_TTL:
                bucket.pop(k, None)
    except Exception:
        pass
    return token, now

def _get_pages(context: ContextTypes.DEFAULT_TYPE, token: str, chat_id: int) -> list[str] | None:
    bucket = context.bot_data.get("vecinos_pages") or {}
    obj = bucket.get(token)
    if not obj:
        return None
    if int(obj.get("chat_id", 0)) != int(chat_id):
        return None
    return obj.get("pages")

# === NUEVO: callback de paginación para /vecinos ===
from telegram.ext import CallbackQueryHandler

async def vecinos_pager_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("vecinos:"):
        return
    try:
        _, token, idx_s = q.data.split(":")
        page_idx = int(idx_s)
    except Exception:
        await q.answer("Página inválida")
        return

    pages = _get_pages(context, token, update.effective_chat.id)
    if not pages:
        await q.answer("Sesión expirada. Vuelve a ejecutar /vecinos")
        return

    total = len(pages)
    page_idx %= total

    try:
        await q.edit_message_text(
            pages[page_idx],
            parse_mode="HTML",
            reply_markup=_pagination_keyboard(token, page_idx, total),
            disable_web_page_preview=True
        )
    except Exception:
        # Si no podemos editar (mensaje muy viejo, etc.), al menos responde
        await q.answer("No se pudo actualizar el mensaje.")

# 25-11-2025 18:14
async def traceroute_cmd_ANTERIOR(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /traceroute <!id|alias>  [timeout_s]
      - Prefiere ejecutar el traceroute vía broker (BacklogServer) y leer los TRACEROUTE_APP del backlog.
      - Si el broker no puede lanzarlo, fallback CLI con: PAUSAR → ejecutar CLI → REANUDAR.
    """
    import asyncio, time, json as _j, socket as _s, os

    # 1) cooldown
    try:
        if await _abort_if_cooldown(update, context):
            return ConversationHandler.END
    except Exception:
        pass

    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "traceroute")
    except Exception:
        pass

    # 2) args
    args = (context.args or []) + [None]
    target = (args[0] or "").strip()
    if not target:
        await update.effective_message.reply_text("Uso: /traceroute <!id|alias> [timeout_s]")

        return ConversationHandler.END

    # --- timeout opcional ---
    raw_t = (args[1] or "")
    txt = str(raw_t).strip().lower()

    # alias rápidos
    if txt in {"slow", "lento"}:
        txt = "60"
    elif txt in {"slow2", "muylento"}:
        txt = "90"

    def _isnum(s: str) -> bool:
        try:
            float(s)
            return True
        except Exception:
            return False

    try:
        timeout = float(txt) if _isnum(txt) else 30.0  # por defecto 30 s
    except Exception:
        timeout = 30.0

    # límites de seguridad
    timeout = max(5.0, min(120.0, timeout))

    # --- resolver alias seguro ---
    def _alias_for(node_id: str) -> str:
        nid = (node_id or "").strip()
        if not nid:
            return ""
        try:
            if '_build_alias_fallback_from_nodes_file' in globals() and callable(globals()['_build_alias_fallback_from_nodes_file']):
                a = _build_alias_fallback_from_nodes_file(nid)
                if a:
                    return str(a)
        except Exception:
            pass
        try:
            if 'resolver_alias_o_id' in globals() and callable(globals()['resolver_alias_o_id']):
                a = resolver_alias_o_id(nid)
                if a and a.startswith("!"):
                    pass
                else:
                    return str(a)
        except Exception:
            pass
        return ""

    def _canon_node_id(x) -> str:
        """
        Normaliza un ID de nodo para que sea una cadena aceptable por el CLI:
        - Acepta strings, tuplas/listas/dicts con candidatos.
        - Prefiere forma con '!' + hex; si recibe hex pelado, añade '!'.
        - Limpia comillas/residuos y fuerza minúsculas.
        """
        import string
        hexd = set(string.hexdigits)

        def _is_hex(s: str) -> bool:
            s = (s or "").strip()
            return bool(s) and all(ch in hexd for ch in s)

        # a) aplanar candidatos
        cands: list[str] = []
        if isinstance(x, (list, tuple, set)):
            for v in x:
                if v is None: 
                    continue
                cands.append(str(v).strip())
        elif isinstance(x, dict):
            for v in x.values():
                if v is None:
                    continue
                cands.append(str(v).strip())
        else:
            cands.append(str(x).strip())

        # b) preferir los que ya empiezan por '!' y sean hex válidos
        for c in cands:
            s = c.strip().strip("'\"")
            if s.startswith("!"):
                body = s[1:].strip().strip("'\"")
                # limpiar posibles residuos de tuple -> quitar ')', ',', etc.
                body = body.replace(")", "").replace("(", "").replace(",", "").strip()
                if _is_hex(body):
                    return "!" + body.lower()

        # c) sino, si hay hex pelado, prepender '!'
        for c in cands:
            s = c.strip().strip("'\"").replace(")", "").replace("(", "").replace(",", " ").split()[0]
            if _is_hex(s):
                return "!" + s.lower()

        # d) fallback: primer token limpio
        for c in cands:
            s = c.strip().strip("'\"").replace(")", "").replace("(", "").replace(",", " ").split()[0]
            if s:
                return s
        return ""




    def _isnum(s: str) -> bool:
        return s.replace(".", "", 1).isdigit() if s else False

    try:
        timeout = float(args[1]) if _isnum(str(args[1])) else 12.0
    except Exception:
        timeout = 12.0
    timeout = max(4.0, min(45.0, timeout))

    # 3) contexto (compat mayúsc/minúsc)
    bd = context.bot_data
    pool = bd.get("tcp_pool") or bd.get("TCP_POOL")
    host = bd.get("mesh_host") or bd.get("meshtastic_host") or bd.get("MESHTASTIC_HOST") or "127.0.0.1"
    port = bd.get("mesh_port") or bd.get("meshtastic_port") or bd.get("MESHTASTIC_PORT") or 4403
    if not pool:
        await update.effective_message.reply_text("⚠️ Config no inicializada: falta TCP_POOL en bot_data.")
        return ConversationHandler.END

    backlog_host = bd.get("backlog_host") or "127.0.0.1"
    backlog_port = int(bd.get("backlog_port") or 8766)

    # Feedback inmediato
    try:
        await update.effective_message.reply_text(
            f"🔎 Iniciando traceroute hacia <code>{target}</code> (timeout {int(timeout)} s)…",
            parse_mode="HTML"
        )
    except Exception:
        pass

    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    raw_id = None
    try:
        if 'resolver_alias_o_id' in globals() and callable(globals()['resolver_alias_o_id']):
            raw_id = resolver_alias_o_id(target)  # puede devolver tuple/list/etc.
    except Exception:
        raw_id = None

    node_id = _canon_node_id(raw_id or target)
    if not node_id or not node_id.startswith("!"):
        await update.effective_message.reply_text(
            f"⚠️ No pude normalizar el destino '{target}' a un !id válido."
        )
        return ConversationHandler.END


    # 4) util cierre iface efímero
    def _force_close_iface(iface) -> None:
        try:
            for m in ("close", "disconnect", "shutdown", "stop", "dispose"):
                if hasattr(iface, m) and callable(getattr(iface, m)):
                    try:
                        getattr(iface, m)()
                    except Exception:
                        pass
            s = getattr(iface, "_socket", None) or getattr(iface, "socket", None)
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        except Exception:
            pass

    # 5) helpers BacklogServer
    async def _broker_cmd(cmd: str, params: dict, recv_timeout: float = 5.0) -> dict | None:
        # helper global si existe
        try:
            if 'fetch_backlog_from_broker' in globals() and callable(globals()['fetch_backlog_from_broker']):
                return await fetch_backlog_from_broker(cmd, params=params)
        except Exception:
            pass
        # TCP crudo
        try:
            with _s.create_connection((backlog_host, backlog_port), timeout=3.0) as s:
                s.sendall((_j.dumps({"cmd": cmd, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"))
                s.settimeout(recv_timeout)
                buf = b""
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    buf += b
                    if b"\n" in b:
                        break
            txt = buf.decode("utf-8", "ignore").strip()
            return _j.loads(txt) if txt else None
        except Exception:
            return None

    async def _fetch_traceroute_frames(since_ts: int, limit=300) -> list[dict]:
        portnums = ["TRACEROUTE_APP", "ROUTING_APP", "ADMIN_APP:TRACEROUTE", "ADMIN_TRACEROUTE"]
        res = await _broker_cmd("FETCH_BACKLOG", {
            "since_ts": int(since_ts),
            "until_ts": None,
            "channel": None,
            "portnums": portnums,
            "limit": int(limit)
        })
        rows = (res or {}).get("data") or (res or {}).get("items") or []
        out = []
        for r in rows:
            ts = r.get("ts") or r.get("rxTime") or r.get("timestamp") or r.get("time")
            fr = r.get("from") or r.get("fromId") or r.get("from_id")
            dec = (r.get("decoded") or {})
            hop = r.get("hop") or dec.get("hop") or dec.get("hop_index")
            via = r.get("via") or dec.get("viaNode") or r.get("relay_node")
            out.append({"ts": ts, "from": fr, "hop": hop if isinstance(hop, int) else None, "via": via, "raw": r})
        out.sort(key=lambda x: (x["ts"] if isinstance(x["ts"], (int, float)) else 0))
        return out

    # 5.1) helpers de pausa/reanudación para CLI (idéntico espíritu a /vecinos)
    async def _pause_broker_for_cli(reason: str, secs: int) -> None:
        """
        Intenta pausar el broker (y/o el pool local) para liberar el socket 4403
        antes de llamar al CLI. Best-effort, no falla si no existe.
        """
        # a) pedir al broker que pause
        try:
            # Comando dedicado si existe
            r = await _broker_cmd("BROKER_PAUSE", {"reason": reason, "secs": int(secs)})
            if not (isinstance(r, dict) and (r.get("ok") or r.get("status") == "ok")):
                # genérico CTRL
                await _broker_cmd("CTRL", {"action": "pause", "reason": reason, "secs": int(secs)})
        except Exception:
            pass
        # b) pausar pool local si expone API
        try:
            if hasattr(pool, "pause_mgr"):
                pool.pause_mgr()
            elif hasattr(pool, "pause"):
                pool.pause()
            # soltar iface activa si hay API
            if hasattr(pool, "drop_iface"):
                try: pool.drop_iface(host, port)
                except Exception: pass
        except Exception:
            pass
        # pequeño colchón
        await asyncio.sleep(1.5)

    async def _resume_broker_after_cli() -> None:
        """Intenta reanudar broker/pool tras el CLI."""
        try:
            r = await _broker_cmd("BROKER_RESUME", {})
            if not (isinstance(r, dict) and (r.get("ok") or r.get("status") == "ok")):
                await _broker_cmd("CTRL", {"action": "resume"})
        except Exception:
            pass
        try:
            if hasattr(pool, "resume_mgr"):
                pool.resume_mgr()
            elif hasattr(pool, "resume"):
                pool.resume()
        except Exception:
            pass
        await asyncio.sleep(0.4)

    # 6) === Lanzado ===
    since_ts = int(time.time())
    launched = False
    used_adapter = False
    used_cli_fallback = False

    # Intento 0: interfaz del pool ya abierta (sin efímeros)
    iface = None
    try:
        if hasattr(pool, "get_iface_wait"):
            iface = pool.get_iface_wait(timeout=min(4.0, timeout/2), interval=0.25)
        elif hasattr(pool, "get_iface"):
            iface = pool.get_iface()
    except Exception:
        iface = None

    # Intento 1: adaptador API si existe (no abre sockets nuevos)
    if iface:
        try:
            from meshtastic_api_adapter import traceroute as adapter_traceroute  # type: ignore
            adapter_traceroute(iface, node_id, channel=None, timeout=timeout)
            launched = True
            used_adapter = True
        except Exception:
            pass

    # Intento 2: broker (RUN_TRACEROUTE / RUN_CLI)
    if not launched:
        resA = await _broker_cmd("RUN_TRACEROUTE", {"target": node_id, "timeout": int(timeout)})
        if isinstance(resA, dict) and (resA.get("ok") or resA.get("status") == "ok"):
            launched = True
        else:
            resB = await _broker_cmd("RUN_CLI", {"action": "traceroute", "target": node_id, "timeout": int(timeout)})
            if isinstance(resB, dict) and (resB.get("ok") or resB.get("status") == "ok"):
                launched = True

    # Intento 3: efímero con acquire (permitimos crear si no hay)
    if not launched and hasattr(pool, "acquire") and callable(pool.acquire):
        temp_iface = None
        try:
            try:
                temp_iface = pool.acquire(host, port, timeout=5.0, reuse_only=False)
            except TypeError:
                temp_iface = pool.acquire(host, port, timeout=5.0)
            if temp_iface and hasattr(temp_iface, "traceroute") and callable(getattr(temp_iface, "traceroute")):
                temp_iface.traceroute(node_id)
                launched = True
        except Exception:
            pass
        finally:
            if temp_iface:
                try:
                    if hasattr(temp_iface, "release"):
                        temp_iface.release()
                except Exception:
                    pass
                _force_close_iface(temp_iface)

    # Intento 4: CLI (último recurso) — PAUSAR (await) → CLI (hilo) → REANUDAR (await)
    if not launched:
        used_cli_fallback = True

        import sys

        def _build_cli_variants(host_str: str, node: str) -> list[list[str]]:
            py = sys.executable or "python"
            return [
                [py, "-m", "meshtastic", "--host", host_str, "--traceroute", str(node)],
                ["meshtastic", "--host", host_str, "--traceroute", str(node)],
            ]

        def _parse_cli_hops(output: str) -> list[str]:
             # Normaliza a texto por si 'output' llegó como bytes en un caso extremo
            if isinstance(output, (bytes, bytearray)):
                try:
                    output = output.decode("utf-8", "ignore")
                except Exception:
                    output = output.decode(errors="ignore")
                    
            lines = []
            for raw in (output or "").splitlines():
                s = raw.strip()
                if not s:
                    continue
                low = s.lower()
                if low.startswith("hop ") or low.startswith("hop\t") or low.startswith("hop:"):
                    lines.append(s); continue
                if "->" in s:
                    lines.append(s); continue
                if (s[:1].isdigit() and (":" in s or ")" in s)):
                    lines.append(s)
            return lines

        launched = False
        cli_hops_lines: list[str] = []

        # 1) Pausa en el LOOP principal (no pasamos corutinas al hilo)
        try:
            await update.effective_message.reply_text("⏸️ Pausando conexión para ejecutar CLI…")
        except Exception:
            pass
        await _pause_broker_for_cli(reason="cli_traceroute", secs=int(timeout) + 10)

        try:
            # Pequeña espera extra en Windows para soltar el socket (evita reconexión inmediata)
            await asyncio.sleep(1.5)

            for cmd in _build_cli_variants(host, node_id):
                # Mensaje con el comando (después de pausar)
                try:
                    await update.effective_message.reply_text(
                        f"💻 Ejecutando: <code>{' '.join(cmd)}</code>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

                # 2) Ejecutar CLI en hilo — helper con 2 args (cmd, timeout)
                rc, out, err, was_to = await asyncio.to_thread(run_cli_exclusive, cmd, float(timeout))
                combined = (out or "") + ("\n" + err if err else "")
                parsed = _parse_cli_hops(combined)

                if was_to or rc == 124:
                    await update.effective_message.reply_text("⏰ Traceroute sin respuesta en el tiempo límite.")
                    continue

                ok = (rc == 0) or bool(parsed)
                if ok:
                    launched = True
                    if parsed:
                        cli_hops_lines = parsed
                    break

                show = combined.strip()
                if len(show) > 1500:
                    show = show[:1500] + "\n…(truncado)…"
                await update.effective_message.reply_text(
                    f"⚠️ CLI código {rc}. Salida:\n<pre>{show}</pre>", parse_mode="HTML"
                )
        finally:
            # 3) Reanudar en el LOOP principal
            try:
                await update.effective_message.reply_text("▶️ Reanudando conexión…")
            except Exception:
                pass
            await _resume_broker_after_cli()

        # Si el CLI produjo hops, devuélvelos directamente
        if launched and cli_hops_lines:
            header = f"🛰️ <b>Traceroute (CLI)</b> → <code>{node_id}</code>\n"
            body = "\n".join(cli_hops_lines)
            await update.effective_message.reply_text(header + body, parse_mode="HTML")
            return ConversationHandler.END


    # margen para que lleguen los frames al backlog
    await asyncio.sleep(0.9)

    # 7) === Espera respuestas TRACEROUTE_APP en backlog ===
    deadline = time.time() + timeout
    hops = []
    while time.time() < deadline:
        frames = await _fetch_traceroute_frames(since_ts=since_ts, limit=400)
        new = []
        for f in frames:
            dec = (f.get("raw") or {}).get("decoded") or {}
            dst = dec.get("dst") or dec.get("dstId") or dec.get("to") or None
            if dst:
                dnorm = _norm(str(dst))
                nnorm = _norm(str(node_id))
                if dnorm != nnorm and (dnorm[1:] if dnorm.startswith("!") else dnorm) != (nnorm[1:] if nnorm.startswith("!") else nnorm):
                    continue
            new.append(f)
        if new:
            hops = new
            break
        await asyncio.sleep(0.8)

    # 8) Salidas
    if not launched and not hops:
        await update.effective_message.reply_text("❌ No se pudo lanzar el traceroute (broker/interfaz) ni hay respuestas en el backlog.")
        return ConversationHandler.END

    if not hops:
        await update.effective_message.reply_text("⏳ Traceroute lanzado, pero sin respuestas dentro del tiempo de espera.")
        return ConversationHandler.END

    # Orden por hop si existe, si no por ts
    def _key_sort(f):
        hop = f.get("hop")
        ts = f.get("ts")
        if isinstance(hop, int):
            return (0, hop, ts if isinstance(ts, (int, float)) else 0)
        return (1, 10**9, ts if isinstance(ts, (int, float)) else 0)

    hops.sort(key=_key_sort)

    # tiempos relativos
    t0 = next((f.get("ts") for f in hops if isinstance(f.get("ts"), (int, float))), None)
    total_secs = 0.0
    if t0 is not None:
        last_ts = t0
        for f in hops:
            ts = f.get("ts") if isinstance(f.get("ts"), (int, float)) else None
            if ts is None:
                f["dt"] = None
                f["t_rel"] = None
                continue
            f["dt"] = float(ts - last_ts) if last_ts is not None else None
            f["t_rel"] = float(ts - t0)
            last_ts = ts
        total_secs = float(last_ts - t0) if last_ts is not None else 0.0

    def _fmt_time(ts):
        import time as _t
        return _t.strftime("%H:%M:%S", _t.localtime(ts)) if isinstance(ts, (int, float)) else "—"

    def _fmt_secs(x):
        if x is None:
            return "—"
        if x < 1.0:
            return f"{x*1000:.0f} ms"
        return f"{x:.1f} s"

    lines = []
    resumen = f"🧭 Traceroute a {node_id} — saltos: {len(hops)}"
    if total_secs and total_secs > 0:
        resumen += f" • duración: {_fmt_secs(total_secs)}"
    if used_cli_fallback:
        resumen += " • ⚠️ fallback CLI (pausa/reanuda)"
    if used_adapter:
        resumen += " • API adapter"
    lines.append(resumen)

    idx = 0
    for f in hops:
        idx += 1
        hop = f.get("hop")
        hop_s = f"hop {hop}" if hop is not None else f"hop {idx}"
        fr = f.get("from") or ""
        via = f.get("via") or ""
        fr_alias = _alias_for(fr)
        via_alias = _alias_for(via)
        fr_label = f"{fr_alias} ({fr})" if fr_alias else str(fr or "—")
        via_label = f"{via_alias} ({via})" if via and via_alias else (via or None)
        ts_s  = _fmt_time(f.get("ts"))
        dt_s  = _fmt_secs(f.get("dt"))
        trel_s= _fmt_secs(f.get("t_rel"))
        core = f"  • {hop_s}"
        if via_label:
            core += f"  via {via_label}"
        core += f"  from {fr_label}"
        extras = f"[t={ts_s}"
        if f.get("dt") is not None:
            extras += f", +{dt_s}"
        if f.get("t_rel") is not None:
            extras += f", T={trel_s}"
        extras += "]"
        lines.append(f"{core}  {extras}")

    text = "\n".join(lines)
    if len(text) > 3900:
        await update.effective_message.reply_text(text[:3900])
        resto = text[3900:]
        while resto:
            await update.effective_message.reply_text(resto[:3900])
            resto = resto[3900:]
    else:
        await update.effective_message.reply_text(text)

    return ConversationHandler.END

async def traceroute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /traceroute <!id|alias>  [timeout_s]
      - Prefiere ejecutar el traceroute vía broker (BacklogServer) y leer los TRACEROUTE_APP del backlog.
      - Si el broker no puede lanzarlo, fallback CLI con: PAUSAR → ejecutar CLI → REANUDAR.
    """
    import asyncio, time, json as _j, socket as _s, os

    # 1) cooldown
    try:
        if await _abort_if_cooldown(update, context):
            return ConversationHandler.END
    except Exception:
        pass

    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "traceroute")
    except Exception:
        pass

    # 2) args
    args = (context.args or []) + [None]
    target = (args[0] or "").strip()
    if not target:
        await update.effective_message.reply_text("Uso: /traceroute <!id|alias> [timeout_s]")
        return ConversationHandler.END

    # --- timeout opcional (UNIFICADO) ---
    raw_t = (args[1] or "")
    txt = str(raw_t).strip().lower()

    # alias rápidos
    if txt in {"slow", "lento"}:
        txt = "60"
    elif txt in {"slow2", "muylento"}:
        txt = "90"

    def _isnum(s: str) -> bool:
        try:
            float(s)
            return True
        except Exception:
            return False

    # valor base y límites de seguridad
    try:
        timeout = float(txt) if _isnum(txt) else 12.0  # por defecto 12 s (como venías usando en la práctica)
    except Exception:
        timeout = 12.0

    # límites de seguridad (mantengo 4–45 como tenías en el segundo bloque)
    timeout = max(4.0, min(45.0, timeout))

    # --- resolver alias seguro ---
    def _alias_for(node_id: str) -> str:
        nid = (node_id or "").strip()
        if not nid:
            return ""
        try:
            if '_build_alias_fallback_from_nodes_file' in globals() and callable(globals()['_build_alias_fallback_from_nodes_file']):
                a = _build_alias_fallback_from_nodes_file(nid)
                if a:
                    return str(a)
        except Exception:
            pass
        try:
            if 'resolver_alias_o_id' in globals() and callable(globals()['resolver_alias_o_id']):
                a = resolver_alias_o_id(nid)
                if a and a.startswith("!"):
                    pass
                else:
                    return str(a)
        except Exception:
            pass
        return ""

    def _canon_node_id(x) -> str:
        """
        Normaliza un ID de nodo para que sea una cadena aceptable por el CLI:
        - Acepta strings, tuplas/listas/dicts con candidatos.
        - Prefiere forma con '!' + hex; si recibe hex pelado, añade '!'.
        - Limpia comillas/residuos y fuerza minúsculas.
        """
        import string
        hexd = set(string.hexdigits)

        def _is_hex(s: str) -> bool:
            s = (s or "").strip()
            return bool(s) and all(ch in hexd for ch in s)

        # a) aplanar candidatos
        cands: list[str] = []
        if isinstance(x, (list, tuple, set)):
            for v in x:
                if v is None:
                    continue
                cands.append(str(v).strip())
        elif isinstance(x, dict):
            for v in x.values():
                if v is None:
                    continue
                cands.append(str(v).strip())
        else:
            cands.append(str(x).strip())

        # b) preferir los que ya empiezan por '!' y sean hex válidos
        for c in cands:
            s = c.strip().strip("'\"")
            if s.startswith("!"):
                body = s[1:].strip().strip("'\"")
                body = body.replace(")", "").replace("(", "").replace(",", "").strip()
                if _is_hex(body):
                    return "!" + body.lower()

        # c) sino, si hay hex pelado, prepender '!'
        for c in cands:
            s = c.strip().strip("'\"").replace(")", "").replace("(", "").replace(",", " ").split()[0]
            if _is_hex(s):
                return "!" + s.lower()

        # d) fallback: primer token limpio
        for c in cands:
            s = c.strip().strip("'\"").replace(")", "").replace("(", "").replace(",", " ").split()[0]
            if s:
                return s
        return ""

    # 3) contexto (compat mayúsc/minúsc)
    bd = context.bot_data
    pool = bd.get("tcp_pool") or bd.get("TCP_POOL")
    host = bd.get("mesh_host") or bd.get("meshtastic_host") or bd.get("MESHTASTIC_HOST") or "127.0.0.1"
    port = bd.get("mesh_port") or bd.get("meshtastic_port") or bd.get("MESHTASTIC_PORT") or 4403
    if not pool:
        await update.effective_message.reply_text("⚠️ Config no inicializada: falta TCP_POOL en bot_data.")
        return ConversationHandler.END

    backlog_host = bd.get("backlog_host") or "127.0.0.1"
    backlog_port = int(bd.get("backlog_port") or 8766)

    # Feedback inmediato
    try:
        await update.effective_message.reply_text(
            f"🔎 Iniciando traceroute hacia <code>{target}</code> (timeout {int(timeout)} s)…",
            parse_mode="HTML"
        )
    except Exception:
        pass

    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    raw_id = None
    try:
        if 'resolver_alias_o_id' in globals() and callable(globals()['resolver_alias_o_id']):
            raw_id = resolver_alias_o_id(target)  # puede devolver tuple/list/etc.
    except Exception:
        raw_id = None

    node_id = _canon_node_id(raw_id or target)
    if not node_id or not node_id.startswith("!"):
        await update.effective_message.reply_text(
            f"⚠️ No pude normalizar el destino '{target}' a un !id válido."
        )
        return ConversationHandler.END

    # 4) util cierre iface efímero
    def _force_close_iface(iface) -> None:
        try:
            for m in ("close", "disconnect", "shutdown", "stop", "dispose"):
                if hasattr(iface, m) and callable(getattr(iface, m)):
                    try:
                        getattr(iface, m)()
                    except Exception:
                        pass
            s = getattr(iface, "_socket", None) or getattr(iface, "socket", None)
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        except Exception:
            pass

    # 5) helpers BacklogServer
    async def _broker_cmd(cmd: str, params: dict, recv_timeout: float = 5.0) -> dict | None:
        # helper global si existe
        try:
            if 'fetch_backlog_from_broker' in globals() and callable(globals()['fetch_backlog_from_broker']):
                return await fetch_backlog_from_broker(cmd, params=params)
        except Exception:
            pass
        # TCP crudo
        try:
            with _s.create_connection((backlog_host, backlog_port), timeout=3.0) as s:
                s.sendall((_j.dumps({"cmd": cmd, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"))
                s.settimeout(recv_timeout)
                buf = b""
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    buf += b
                    if b"\n" in b:
                        break
            txt = buf.decode("utf-8", "ignore").strip()
            return _j.loads(txt) if txt else None
        except Exception:
            return None

    async def _fetch_traceroute_frames(since_ts: int, limit=300) -> list[dict]:
        portnums = ["TRACEROUTE_APP", "ROUTING_APP", "ADMIN_APP:TRACEROUTE", "ADMIN_TRACEROUTE"]
        res = await _broker_cmd("FETCH_BACKLOG", {
            "since_ts": int(since_ts),
            "until_ts": None,
            "channel": None,
            "portnums": portnums,
            "limit": int(limit)
        })
        rows = (res or {}).get("data") or (res or {}).get("items") or []
        out = []
        for r in rows:
            ts = r.get("ts") or r.get("rxTime") or r.get("timestamp") or r.get("time")
            fr = r.get("from") or r.get("fromId") or r.get("from_id")
            dec = (r.get("decoded") or {})
            hop = r.get("hop") or dec.get("hop") or dec.get("hop_index")
            via = r.get("via") or dec.get("viaNode") or r.get("relay_node")
            out.append({"ts": ts, "from": fr, "hop": hop if isinstance(hop, int) else None, "via": via, "raw": r})
        out.sort(key=lambda x: (x["ts"] if isinstance(x["ts"], (int, float)) else 0))
        return out

    # 5.1) helpers de pausa/reanudación para CLI (idéntico espíritu a /vecinos)
    async def _pause_broker_for_cli(reason: str, secs: int) -> None:
        """
        Intenta pausar el broker (y/o el pool local) para liberar el socket 4403
        antes de llamar al CLI. Best-effort, no falla si no existe.
        """
        # a) pedir al broker que pause
        try:
            r = await _broker_cmd("BROKER_PAUSE", {"reason": reason, "secs": int(secs)})
            if not (isinstance(r, dict) and (r.get("ok") or r.get("status") == "ok")):
                await _broker_cmd("CTRL", {"action": "pause", "reason": reason, "secs": int(secs)})
        except Exception:
            pass
        # b) pausar pool local si expone API
        try:
            if hasattr(pool, "pause_mgr"):
                pool.pause_mgr()
            elif hasattr(pool, "pause"):
                pool.pause()
            if hasattr(pool, "drop_iface"):
                try:
                    pool.drop_iface(host, port)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(1.5)

    async def _resume_broker_after_cli() -> None:
        """Intenta reanudar broker/pool tras el CLI."""
        try:
            r = await _broker_cmd("BROKER_RESUME", {})
            if not (isinstance(r, dict) and (r.get("ok") or r.get("status") == "ok")):
                await _broker_cmd("CTRL", {"action": "resume"})
        except Exception:
            pass
        try:
            if hasattr(pool, "resume_mgr"):
                pool.resume_mgr()
            elif hasattr(pool, "resume"):
                pool.resume()
        except Exception:
            pass
        await asyncio.sleep(0.4)

    # 6) === Lanzado ===
    since_ts = int(time.time())
    launched = False
    used_adapter = False
    used_cli_fallback = False

    # Intento 0: interfaz del pool ya abierta (sin efímeros)
    iface = None
    try:
        if hasattr(pool, "get_iface_wait"):
            iface = pool.get_iface_wait(timeout=min(4.0, timeout/2), interval=0.25)
        elif hasattr(pool, "get_iface"):
            iface = pool.get_iface()
    except Exception:
        iface = None

    # Intento 1: adaptador API si existe (no abre sockets nuevos)
    if iface:
        try:
            from meshtastic_api_adapter import traceroute as adapter_traceroute  # type: ignore
            adapter_traceroute(iface, node_id, channel=None, timeout=timeout)
            launched = True
            used_adapter = True
        except Exception:
            pass

    # Intento 2: broker (RUN_TRACEROUTE / RUN_CLI)
    if not launched:
        resA = await _broker_cmd("RUN_TRACEROUTE", {"target": node_id, "timeout": int(timeout)})
        if isinstance(resA, dict) and (resA.get("ok") or resA.get("status") == "ok"):
            launched = True
        else:
            resB = await _broker_cmd("RUN_CLI", {"action": "traceroute", "target": node_id, "timeout": int(timeout)})
            if isinstance(resB, dict) and (resB.get("ok") or resB.get("status") == "ok"):
                launched = True

    # Intento 3: efímero con acquire (permitimos crear si no hay)
    if not launched and hasattr(pool, "acquire") and callable(pool.acquire):
        temp_iface = None
        try:
            try:
                temp_iface = pool.acquire(host, port, timeout=5.0, reuse_only=False)
            except TypeError:
                temp_iface = pool.acquire(host, port, timeout=5.0)
            if temp_iface and hasattr(temp_iface, "traceroute") and callable(getattr(temp_iface, "traceroute")):
                temp_iface.traceroute(node_id)
                launched = True
        except Exception:
            pass
        finally:
            if temp_iface:
                try:
                    if hasattr(temp_iface, "release"):
                        temp_iface.release()
                except Exception:
                    pass
                _force_close_iface(temp_iface)

    # Intento 4: CLI (último recurso) — PAUSAR (await) → CLI (hilo) → REANUDAR (await)
    if not launched:
        used_cli_fallback = True

        import sys

        def _build_cli_variants(host_str: str, node: str) -> list[list[str]]:
            py = sys.executable or "python"
            return [
                [py, "-m", "meshtastic", "--host", host_str, "--traceroute", str(node)],
                ["meshtastic", "--host", host_str, "--traceroute", str(node)],
            ]

        def _parse_cli_hops(output: str) -> list[str]:
            # Normaliza a texto por si 'output' llegó como bytes en un caso extremo
            if isinstance(output, (bytes, bytearray)):
                try:
                    output = output.decode("utf-8", "ignore")
                except Exception:
                    output = output.decode(errors="ignore")

            lines = []
            for raw in (output or "").splitlines():
                s = raw.strip()
                if not s:
                    continue
                low = s.lower()
                if low.startswith("hop ") or low.startswith("hop\t") or low.startswith("hop:"):
                    lines.append(s)
                    continue
                if "->" in s:
                    lines.append(s)
                    continue
                if (s[:1].isdigit() and (":" in s or ")" in s)):
                    lines.append(s)
            return lines

        launched = False
        cli_hops_lines: list[str] = []

        # 1) Pausa en el LOOP principal (no pasamos corutinas al hilo)
        try:
            await update.effective_message.reply_text("⏸️ Pausando conexión para ejecutar CLI…")
        except Exception:
            pass
        await _pause_broker_for_cli(reason="cli_traceroute", secs=int(timeout) + 10)

        try:
            # Pequeña espera extra en Windows para soltar el socket (evita reconexión inmediata)
            await asyncio.sleep(1.5)

            for cmd in _build_cli_variants(host, node_id):
                try:
                    await update.effective_message.reply_text(
                        f"💻 Ejecutando: <code>{' '.join(cmd)}</code>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

                rc, out, err, was_to = await asyncio.to_thread(run_cli_exclusive, cmd, float(timeout))
                combined = (out or "") + ("\n" + err if err else "")
                parsed = _parse_cli_hops(combined)

                if was_to or rc == 124:
                    await update.effective_message.reply_text("⏰ Traceroute sin respuesta en el tiempo límite.")
                    continue

                ok = (rc == 0) or bool(parsed)
                if ok:
                    launched = True
                    if parsed:
                        cli_hops_lines = parsed
                    break

                show = combined.strip()
                if len(show) > 1500:
                    show = show[:1500] + "\n…(truncado)…"
                await update.effective_message.reply_text(
                    f"⚠️ CLI código {rc}. Salida:\n<pre>{show}</pre>", parse_mode="HTML"
                )
        finally:
            try:
                await update.effective_message.reply_text("▶️ Reanudando conexión…")
            except Exception:
                pass
            await _resume_broker_after_cli()

        if launched and cli_hops_lines:
            header = f"🛰️ <b>Traceroute (CLI)</b> → <code>{node_id}</code>\n"
            body = "\n".join(cli_hops_lines)
            await update.effective_message.reply_text(header + body, parse_mode="HTML")
            return ConversationHandler.END

    # margen para que lleguen los frames al backlog
    await asyncio.sleep(0.9)

    # 7) === Espera respuestas TRACEROUTE_APP en backlog ===
    deadline = time.time() + timeout
    hops = []
    while time.time() < deadline:
        frames = await _fetch_traceroute_frames(since_ts=since_ts, limit=400)
        new = []
        for f in frames:
            dec = (f.get("raw") or {}).get("decoded") or {}
            dst = dec.get("dst") or dec.get("dstId") or dec.get("to") or None
            if dst:
                dnorm = _norm(str(dst))
                nnorm = _norm(str(node_id))
                if dnorm != nnorm and (dnorm[1:] if dnorm.startswith("!") else dnorm) != (nnorm[1:] if nnorm.startswith("!") else nnorm):
                    continue
            new.append(f)
        if new:
            hops = new
            break
        await asyncio.sleep(0.8)

    # 8) Salidas
    if not launched and not hops:
        await update.effective_message.reply_text("❌ No se pudo lanzar el traceroute (broker/interfaz) ni hay respuestas en el backlog.")
        return ConversationHandler.END

    if not hops:
        await update.effective_message.reply_text("⏳ Traceroute lanzado, pero sin respuestas dentro del tiempo de espera.")
        return ConversationHandler.END

    # Orden por hop si existe, si no por ts
    def _key_sort(f):
        hop = f.get("hop")
        ts = f.get("ts")
        if isinstance(hop, int):
            return (0, hop, ts if isinstance(ts, (int, float)) else 0)
        return (1, 10**9, ts if isinstance(ts, (int, float)) else 0)

    hops.sort(key=_key_sort)

    # tiempos relativos
    t0 = next((f.get("ts") for f in hops if isinstance(f.get("ts"), (int, float))), None)
    total_secs = 0.0
    if t0 is not None:
        last_ts = t0
        for f in hops:
            ts = f.get("ts") if isinstance(f.get("ts"), (int, float)) else None
            if ts is None:
                f["dt"] = None
                f["t_rel"] = None
                continue
            f["dt"] = float(ts - last_ts) if last_ts is not None else None
            f["t_rel"] = float(ts - t0)
            last_ts = ts
        total_secs = float(last_ts - t0) if last_ts is not None else 0.0

    def _fmt_time(ts):
        import time as _t
        return _t.strftime("%H:%M:%S", _t.localtime(ts)) if isinstance(ts, (int, float)) else "—"

    def _fmt_secs(x):
        if x is None:
            return "—"
        if x < 1.0:
            return f"{x*1000:.0f} ms"
        return f"{x:.1f} s"

    lines = []
    resumen = f"🧭 Traceroute a {node_id} — saltos: {len(hops)}"
    if total_secs and total_secs > 0:
        resumen += f" • duración: {_fmt_secs(total_secs)}"
    if used_cli_fallback:
        resumen += " • ⚠️ fallback CLI (pausa/reanuda)"
    if used_adapter:
        resumen += " • API adapter"
    lines.append(resumen)

    idx = 0
    for f in hops:
        idx += 1
        hop = f.get("hop")
        hop_s = f"hop {hop}" if hop is not None else f"hop {idx}"
        fr = f.get("from") or ""
        via = f.get("via") or ""
        fr_alias = _alias_for(fr)
        via_alias = _alias_for(via)
        fr_label = f"{fr_alias} ({fr})" if fr_alias else str(fr or "—")
        via_label = f"{via_alias} ({via})" if via and via_alias else (via or None)
        ts_s = _fmt_time(f.get("ts"))
        dt_s = _fmt_secs(f.get("dt"))
        trel_s = _fmt_secs(f.get("t_rel"))
        core = f"  • {hop_s}"
        if via_label:
            core += f"  via {via_label}"
        core += f"  from {fr_label}"
        extras = f"[t={ts_s}"
        if f.get("dt") is not None:
            extras += f", +{dt_s}"
        if f.get("t_rel") is not None:
            extras += f", T={trel_s}"
        extras += "]"
        lines.append(f"{core}  {extras}")

    text = "\n".join(lines)
    if len(text) > 3900:
        await update.effective_message.reply_text(text[:3900])
        resto = text[3900:]
        while resto:
            await update.effective_message.reply_text(resto[:3900])
            resto = resto[3900:]
    else:
        await update.effective_message.reply_text(text)

    return ConversationHandler.END



# === NUEVO HANDLER: alias corto /rt que reutiliza /traceroute ===
async def rt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /rt <!id|alias|número>
    Alias directo a /traceroute sin duplicar lógica.
    """
    return await traceroute_cmd(update, context)


# === NUEVO: /traceroute_status [N] | /traceroute_status <!id|alias> ===
async def traceroute_status_cmd_anterior(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /traceroute_status [N]
    /traceroute_status <!id|alias>
      - Sin args: muestra el último registro.
      - Con N: muestra los últimos N (máx 10).
      - Con !id|alias: muestra el último para ese destino.
    """
    import os, json, time
    from html import escape

    LOG_PATH = os.path.join("bot_data", "broker_traceroute_log.jsonl")
    args = context.args or []

    # Intento de parseo de argumento
    count = 1
    filter_dest = None
    if args:
        tok = args[0].strip()
        # ¿!id o alias?
        if tok.startswith("!") or (len(tok) >= 3 and not tok.isdigit()):
            filter_dest = tok
        else:
            try:
                count = max(1, min(10, int(tok)))
            except Exception:
                count = 1

    # Leer JSONL (si no existe, responder amable)
    if not os.path.isfile(LOG_PATH):
        await update.effective_message.reply_text(
            "ℹ️ Aún no hay registros de traceroute en el bot."
        )
        return ConversationHandler.END

    rows = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rows.append(obj)
                except Exception:
                    continue
    except Exception as e:
        await update.effective_message.reply_text(f"❌ No se pudo leer el log: {e}")
        return ConversationHandler.END

    if not rows:
        await update.effective_message.reply_text("ℹ️ Log vacío por ahora.")
        return ConversationHandler.END

    # Filtrar por destino (si se pidió). Admitimos alias parcial (best-effort).
    def _matches(r):
        if not filter_dest:
            return True
        d = (r.get("dest") or "")
        a = (r.get("dest_alias") or "")
        tok = filter_dest.lower()
        return tok in d.lower() or tok in a.lower()

    rows = [r for r in rows if _matches(r)]
    if not rows:
        await update.effective_message.reply_text("ℹ️ No hay registros que coincidan con ese destino.")
        return ConversationHandler.END

    # Ordenar por ts desc y cortar a N
    rows.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)
    rows = rows[:count]

    # Formateo
    def _fmt_row(r):
        ts = int(r.get("ts", 0))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "¿?"
        dest = r.get("dest") or "¿?"
        alias = r.get("alias") or ""
        hops = r.get("hops")
        ok = "✅" if r.get("ok") else "❌"
        src = r.get("source") or "?"
        ruta = r.get("route") or []
        head = f"{ok} <b>{escape(dest)}</b>" + (f" ({escape(alias)})" if alias else "")
        hops_s = f"<b>Hops</b>: {int(hops) if hops is not None else '¿?'} • <i>fuente</i>: {escape(src)} • {escape(when)}"
        path_s = "  " + "  →  ".join(escape(x) for x in ruta) if ruta else "  (sin detalle de ruta)"
        return f"{head}\n{hops_s}\n{path_s}"

    body = "\n\n".join(_fmt_row(r) for r in rows)
    await _safe_reply_html(update.effective_message, body)
    return ConversationHandler.END

# === [ACTUALIZADA] /traceroute_status [N] | /traceroute_status <!id|alias> ===
async def traceroute_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /traceroute_status [N]
    /traceroute_status <!id|alias>
      - Sin args: muestra el último registro.
      - Con N: muestra los últimos N (máx 10).
      - Con !id|alias: muestra el último para ese destino (por id o alias).
    Muestra alias en cada hop y en el destino si están disponibles en nodes.txt.
    """
    import os, json, time
    from html import escape

    LOG_PATH = os.path.join("bot_data", "broker_traceroute_log.jsonl")
    args = context.args or []

    # --- Parseo de argumento (igual que versión previa) ---
    count = 1
    filter_dest = None
    if args:
        tok = args[0].strip()
        if tok.startswith("!") or (len(tok) >= 3 and not tok.isdigit()):
            filter_dest = tok
        else:
            try:
                count = max(1, min(10, int(tok)))
            except Exception:
                count = 1

    # --- Cargar log JSONL ---
    if not os.path.isfile(LOG_PATH):
        await update.effective_message.reply_text("ℹ️ Aún no hay registros de traceroute en el bot.")
        return ConversationHandler.END

    rows = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rows.append(obj)
                except Exception:
                    continue
    except Exception as e:
        await update.effective_message.reply_text(f"❌ No se pudo leer el log: {e}")
        return ConversationHandler.END

    if not rows:
        await update.effective_message.reply_text("ℹ️ Log vacío por ahora.")
        return ConversationHandler.END

    # --- Cargar alias desde nodes.txt (reutilizamos util existente) ---
    try:
        alias_dict = cargar_aliases_desde_nodes(str(NODES_FILE))
    except Exception:
        alias_dict = {}

    def _alias_of(bang: str) -> str:
        if not isinstance(bang, str):
            return ""
        key = bang if bang.startswith("!") else f"!{bang}"
        return (alias_dict.get(key) or "").strip()

    # --- Filtro por destino (si se pidió) ---
    def _matches(r):
        if not filter_dest:
            return True
        d = (r.get("dest") or "")
        a = (r.get("dest_alias") or "")
        tok = filter_dest.lower()
        return tok in d.lower() or tok in a.lower()

    rows = [r for r in rows if _matches(r)]
    if not rows:
        await update.effective_message.reply_text("ℹ️ No hay registros que coincidan con ese destino.")
        return ConversationHandler.END

    # Ordenar por fecha desc y recortar
    rows.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)
    rows = rows[:count]

    # --- Formateo con alias por hop ---
    def _fmt_hop(bang: str) -> str:
        ali = _alias_of(bang)
        return f"{escape(bang)} ({escape(ali)})" if ali else escape(bang)

    def _fmt_row(r):
        ts = int(r.get("ts", 0))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "¿?"
        dest = r.get("dest") or "¿?"
        dest_alias = r.get("dest_alias") or _alias_of(dest) or ""
        hops = r.get("hops")
        ok = "✅" if r.get("ok") else "❌"
        src = r.get("source") or "?"
        ruta = r.get("route") or []

        head = f"{ok} <b>{escape(dest)}</b>" + (f" ({escape(dest_alias)})" if dest_alias else "")
        hops_s = f"<b>Hops</b>: {int(hops) if hops is not None else '¿?'} • <i>fuente</i>: {escape(src)} • {escape(when)}"
        path_s = "  " + "  →  ".join(_fmt_hop(x) for x in ruta) if ruta else "  (sin detalle de ruta)"
        return f"{head}\n{hops_s}\n{path_s}"

    body = "\n\n".join(_fmt_row(r) for r in rows)
    await _safe_reply_html(update.effective_message, body)
    return ConversationHandler.END


async def traceroute_cmd_CLI(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /traceroute <!id|alias|número>
      - Intenta traceroute por API usando la interfaz persistente del pool (rápido, sin abrir sockets nuevos).
      - Si la API no lo soporta o falla, cae a CLI: `meshtastic --host <host> --traceroute <id>`.
      - Muestra hops y ruta, resolviendo alias cuando sea posible.
      - Registra el resultado en bot_data/broker_traceroute_log.jsonl (best-effort).
    """
    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "traceroute")
    except Exception:
        pass

    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text(
            "Uso: /traceroute <!id|alias|número>\n"
            "Ej.: /traceroute !b03bd52c   o   /traceroute Zgz_Romareda_CL-868"
        )
        return ConversationHandler.END

    raw_target = args[0].strip()

    # Resolver destino a !id usando mapeos existentes
    try:
        mapping = build_nodes_mapping(100)
    except Exception:
        mapping = {}

    def _to_bang_id(token: str) -> str:
        t = token.strip()
        if t.startswith("!"):
            return t
        if mapping:
            m = mapping.get(t.lower()) or mapping.get(t)
            if m:
                return m if str(m).startswith("!") else f"!{m}"
        import re
        if re.fullmatch(r"[0-9a-fA-F]{8}", t):
            return f"!{t.lower()}"
        if t.isdigit():
            try:
                return f"!{int(t):08x}"
            except Exception:
                pass
        return t

    dest = _to_bang_id(raw_target)

    # Intento API
    api_res = None
    try:
        api_res = traceroute_node(dest, timeout=TRACEROUTE_TIMEOUT)
    except Exception:
        api_res = None

    # Fallback CLI
    use_cli = (not api_res) or (hasattr(api_res, "ok") and not getattr(api_res, "ok"))
    cli_res, cli_used = None, False
    if use_cli:
        try:
            with with_broker_paused(max_wait_s=8.0):
                out = run_command(
                    ["--host", MESHTASTIC_HOST, "--traceroute", dest.lstrip("!")],
                    timeout=TRACEROUTE_TIMEOUT
                )
            cli_res = parse_traceroute_output(out)
            cli_used = True
        except Exception:
            cli_res = None

    # Elegir resultado
    class _R:
        def __init__(self, ok, hops, route, raw, source):
            self.ok = ok
            self.hops = hops
            self.route = route
            self.raw = raw
            self.source = source

    if api_res and getattr(api_res, "ok", False):
        best = _R(api_res.ok, api_res.hops, api_res.route, api_res.raw, "API")
    elif cli_res and getattr(cli_res, "ok", False):
        best = _R(cli_res.ok, cli_res.hops, cli_res.route, cli_res.raw, "CLI")
    else:
        await _safe_reply_html(msg, "❌ <b>Traceroute fallido</b>")
        return ConversationHandler.END

    # === Resolver alias ===
    try:
        alias_dict = cargar_aliases_desde_nodes(str(NODES_FILE))
    except Exception:
        alias_dict = {}

    def _alias_of(bang: str) -> str:
        if not isinstance(bang, str):
            return ""
        key = bang if bang.startswith("!") else f"!{bang}"
        return (alias_dict.get(key) or "").strip()

    # Cabecera
    dest_alias = _alias_of(dest)
    header = f"🧭 <b>Traceroute</b> a {escape(dest)}" + (f" ({escape(dest_alias)})" if dest_alias else "")
    hops_line = f"<b>Hops</b>: {best.hops}  •  <i>fuente</i>: {best.source}"

    # Ruta con alias
    def _fmt_hop(bang: str) -> str:
        ali = _alias_of(bang)
        return f"{escape(bang)} ({escape(ali)})" if ali else escape(bang)

    if best.route:
        route_fmt = "  " + "  →  ".join(_fmt_hop(x) for x in best.route)
    else:
        route_fmt = "  (sin detalle de ruta)"

    text_html = f"{header}\n{hops_line}\n\n{route_fmt}"
    await _safe_reply_html(msg, text_html)

    # Log en JSONL
    try:
        rec = {
            "ts": int(time.time()),
            "cmd": "traceroute",
            "dest": dest,
            "dest_alias": dest_alias or None,
            "ok": best.ok,
            "hops": best.hops,
            "route": best.route,
            "route_aliases": [{r: _alias_of(r)} for r in best.route],
            "source": best.source,
            "raw_len": len(best.raw or ""),
            "user": {
                "id": update.effective_user.id if update and update.effective_user else None,
                "username": update.effective_user.username if update and update.effective_user else None
            }
        }
        os.makedirs("bot_data", exist_ok=True)
        with open(os.path.join("bot_data", "broker_traceroute_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return ConversationHandler.END


async def _get_iface_wait_async(_pool, _host, _port, _timeout: float, _interval: float = 0.3):
    """
    Intenta obtener/crear una iface lista desde el pool, sin bloquear el event loop.
    Orden de intentos:
      1) get_iface_wait(timeout=..., interval=...) con kwargs; si falla, intenta POSICIONAL (timeout, interval).
      2) get()/get_or_create() probando firmas: (host,port,timeout=...), (host,port), (host) y luego leer iface.
      3) ensure_connected(host, port, timeout=...) + bucle corto consultando get_iface/get_interface/iface.
    Todo con límites de tiempo duros.
    """
    import time as _time
    end = _time.time() + float(_timeout)

    async def _read_iface():
        try:
            gi = getattr(_pool, "get_iface", None)
            if callable(gi):
                return await _to_thread_timeout(gi, _timeout=min(0.5, _timeout))
            gi2 = getattr(_pool, "get_interface", None)
            if callable(gi2):
                return await _to_thread_timeout(gi2, _timeout=min(0.5, _timeout))
            return getattr(_pool, "iface", None)
        except Exception:
            return None

    # --- 1) get_iface_wait: primero kwargs, luego POSICIONAL si la firma no coincide ---
    giw = getattr(_pool, "get_iface_wait", None)
    if callable(giw):
        # kwargs
        try:
            return await _to_thread_timeout(
                giw,
                _timeout=_timeout,          # timeout del wrapper
                timeout=_timeout,           # kwargs reales
                interval=_interval
            )
        except asyncio.TimeoutError:
            pass
        except TypeError:
            # firma distinta -> probamos posicional: (timeout, interval)
            try:
                return await _to_thread_timeout(
                    giw,
                    _timeout, _interval,
                    _timeout=_timeout
                )
            except Exception:
                pass

    # --- 2) get()/get_or_create() con variaciones de firma ---
    get_fn = getattr(_pool, "get", None) or getattr(_pool, "get_or_create", None)
    if callable(get_fn):
        # a) (host, port, timeout=...)
        try:
            _ = await _to_thread_timeout(get_fn, _host, _port, timeout=_timeout, _timeout=min(0.8, _timeout))
            iface = await _read_iface()
            if iface is not None:
                return iface
        except TypeError:
            # b) (host, port)
            try:
                _ = await _to_thread_timeout(get_fn, _host, _port, _timeout=min(0.8, _timeout))
                iface = await _read_iface()
                if iface is not None:
                    return iface
            except TypeError:
                # c) (host)
                try:
                    _ = await _to_thread_timeout(get_fn, _host, _timeout=min(0.8, _timeout))
                    iface = await _read_iface()
                    if iface is not None:
                        return iface
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass

    # --- 3) ensure_connected + sondeo corto de iface ---
    ensure_fn = getattr(_pool, "ensure_connected", None)
    if callable(ensure_fn):
        try:
            # pasa timeout a la real y al wrapper
            await _to_thread_timeout(ensure_fn, _host, _port, timeout=_timeout, _timeout=min(1.0, _timeout))
        except Exception:
            pass

    while _time.time() < end:
        iface = await _read_iface()
        if iface is not None:
            return iface
        await asyncio.sleep(_interval)

    return None



    async def _ensure_connected_async(_pool, _host, _port, _timeout: float):
        """Llama a ensure_connected en hilo con timeout si existe."""
        ensure_fn = getattr(_pool, "ensure_connected", None)
        if callable(ensure_fn):
            # ✅ pasa timeout a la real y al wrapper
            await _to_thread_timeout(ensure_fn, _host, _port, timeout=_timeout, _timeout=_timeout)

    def _extract_nodes_from_iface(iface) -> list[dict]:
        import time as _time
        now = int(_time.time())

        raw = getattr(iface, "nodes", None)
        if raw and isinstance(raw, dict):
            it = raw.values()
        elif isinstance(raw, list):
            it = raw
        else:
            g = getattr(iface, "getNodes", None)
            it = g() if callable(g) else []
        out = []
        for n in it or []:
            u = n.get("user") or {}
            uid = u.get("id") or n.get("id") or n.get("num") or n.get("nodeId")
            alias = u.get("longName") or u.get("shortName") or n.get("name") or uid or "¿sin_alias?"
            last = n.get("lastHeard") or n.get("last_heard") or n.get("heard")
            last = int(last) if isinstance(last, (int, float)) else 0
            out.append({"id": uid, "alias": alias, "ago": (now - last) if last else None})
        return out

    async def _resolve_dest_async(iface, q: str) -> str | None:
        qn = (q or "").strip()
        nodes = await _to_thread_timeout(_extract_nodes_from_iface, iface, _timeout=min(2.0, timeout))
        if qn.startswith("!"):
            for n in nodes:
                if n["id"] == qn:
                    return qn
            return qn  # permitir !id aunque no esté en tabla
        low = qn.lower()
        for n in nodes:
            if (n["alias"] or "").lower() == low:
                return n["id"]
        for n in nodes:
            if low in (n["alias"] or "").lower():
                return n["id"]
        return None

    def _do_traceroute_with_iface_sync(iface, dest_id: str):
        import re, inspect as _ins

        did = (dest_id or "").strip()
        # Candidatos de método en la API
        candidates = [
            ("traceroute",     {"node_id": did, "timeout": timeout}),
            ("traceroute",     {"dest_id": did, "timeout": timeout}),
            ("traceroute",     {"id": did,      "timeout": timeout}),
            ("sendTraceroute", {"dest_id": did, "timeout": timeout}),
            ("tracerouteNode", {"dest_id": did, "timeout": timeout}),
        ]

        last_err = None
        for name, proposed_kwargs in candidates:
            fn = getattr(iface, name, None)
            if not callable(fn):
                continue
            try:
                kwargs = proposed_kwargs
                try:
                    sig = _ins.signature(fn)
                    accepted = set(sig.parameters.keys())
                    kwargs = {k: v for k, v in proposed_kwargs.items() if k in accepted}
                except Exception:
                    pass

                res = fn(**kwargs) if kwargs else fn(did)

                hops, path = None, None
                if isinstance(res, dict):
                    hops = res.get("hops") or res.get("hopCount")
                    path = res.get("path") or res.get("route") or res.get("nodes")
                elif isinstance(res, (list, tuple)):
                    path = list(res)
                    hops = (len(path) - 1) if path else None
                elif isinstance(res, str):
                    txt = res.strip()
                    ids = re.findall(r"![0-9a-fA-F]{8}", txt)
                    if ids:
                        path = [i.strip() for i in ids]
                        hops = max(0, len(path) - 1)

                if path and isinstance(path, list):
                    path = [str(x) for x in path]

                ok = bool(path and len(path) >= 2)
                return (ok, (int(hops) if hops is not None else None), path, res, None)

            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue

        return (False, None, None, None,
                "La interfaz no expone traceroute por API en esta versión." if last_err is None else last_err)

    # ---------- Ejecución (API-only) ----------
    try:
        await _ensure_connected_async(pool, host, port, _timeout=timeout)  # ✅ _timeout
    except asyncio.TimeoutError:
        await update.effective_message.reply_text(f"⚠️ Timeout conectando a la interfaz ({timeout:.0f}s).")
        return ConversationHandler.END
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Error conectando a la interfaz: {type(e).__name__}: {e}")
        return ConversationHandler.END

    iface = await _get_iface_wait_async(pool, host, port, _timeout=timeout, _interval=0.3)
    if iface is None:
        await update.effective_message.reply_text("⚠️ Interfaz no disponible ahora mismo.")
        return ConversationHandler.END

    dest_id = await _resolve_dest_async(iface, target)
    dest_id = dest_id or target

    # comprobar que hay algún método disponible en la API
    method_names = ("traceroute", "traceRoute", "sendTraceroute", "tracerouteNode", "requestTraceroute", "routeDiscovery")
    if not any(callable(getattr(iface, n, None)) for n in method_names):
        await update.effective_message.reply_text(
            "⚠️ Traceroute no disponible por API: la interfaz no expone un método compatible en esta versión."
        )
        return ConversationHandler.END

    try:
        ok, hops, path, raw, err = await _to_thread_timeout(
            _do_traceroute_with_iface_sync, iface, dest_id, _timeout=timeout
        )
    except asyncio.TimeoutError:
        await update.effective_message.reply_text(f"⚠️ Traceroute por API excedió {timeout:.0f}s.")
        return ConversationHandler.END
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Fallo en traceroute por API: {type(e).__name__}: {e}")
        return ConversationHandler.END

    if ok:
        lines = [f"🛰️ Traceroute a {(dest_id or target)}"]
        if path:
            for i, p in enumerate(path, 1):
                lines.append(f"{i}. {p}")
        if hops is not None:
            lines.append(f"\nSaltos: {hops}")
        await update.effective_message.reply_text("\n".join(lines))
    else:
        await update.effective_message.reply_text(f"⚠️ Traceroute no disponible por API: {err}")
    return ConversationHandler.END

# --- [NUEVO] pseudo "en vivo" desde JSONL para NO abrir sockets ---
def _read_pseudo_live_from_jsonl(jsonl_path: str, max_n: int = 20, window_mins: int = 30) -> list[dict]:
    """
    Devuelve lista compacta 'en vivo' por nodo leyendo el JSONL de telemetría:
      - última entrada por nodo dentro de la ventana 'window_mins'
      - ordenada por recencia
      - Campos devueltos: id, alias(placeholder), ago, battery, voltage, temp, air, chutil
    NOTA: TELEMETRY no porta SNR/RSSI → se pondrá '—'
    """
    import os, json, time
    now = int(time.time())
    cutoff = now - int(window_mins) * 60
    if not jsonl_path or not os.path.exists(jsonl_path):
        return []

    best = {}  # node_id -> (ts, row)
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                ts = obj.get("ts") or obj.get("timestamp") or obj.get("time")
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue

                nid = str(obj.get("from") or obj.get("fromId") or obj.get("from_id") or "").strip()
                if not nid:
                    continue

                dev = obj.get("device") or {}
                env = obj.get("environment") or {}
                row = {
                    "id": nid,
                    "alias": None,  # lo completamos al formatear con nodes.txt
                    "ago": now - int(ts),
                    "snr": None,
                    "rssi": None,
                    "battery": dev.get("batteryLevel"),
                    "voltage": dev.get("voltage"),
                    "temp": (env.get("temperature") if isinstance(env, dict) else None),
                    "air": dev.get("airUtilTx") or dev.get("airutil") or dev.get("airUtil"),
                    "chutil": dev.get("channelUtilization") or dev.get("chanutil") or dev.get("channelUtil"),
                }
                prev = best.get(nid)
                if prev is None or ts > prev[0]:
                    best[nid] = (ts, row)
    except Exception:
        return []

    rows = [r for _, r in best.values()]
    rows.sort(key=lambda r: r["ago"] if r["ago"] is not None else 10**9)
    if max_n and max_n > 0:
        rows = rows[:max_n]
    return rows

async def telemetria_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /telemetria [!id|alias] [mins|max_n] [timeout]
      - Sin destino: listado rápido de métricas "en vivo" (pool persistente), ordenado por recencia.
        * [max_n] (opcional) limita filas. [timeout] (opcional) espera pool.
      - Con destino (!id o alias): métricas "en vivo" + HISTÓRICO desde el broker (FETCH_TELEMETRY).
        * [mins] (opcional) ventana en minutos para el histórico (por defecto 30 min).
        * [timeout] (opcional) espera pool.
      Campos habituales si existen: SNR, RSSI, batería/voltaje, temperatura, airmon, etc.
    """
    bump_stat(update.effective_user.id, update.effective_user.username or "", "telemetria")

    # ---------- Parseo de argumentos (compatible hacia atrás) ----------
    raw_args = context.args or []
    target = None
    max_n = 20          # solo aplica a modo listado (sin destino)
    timeout = 4.0
    hist_mins = 30      # === [NUEVO] ventana por defecto para histórico cuando hay destino

    def _is_number(s: str) -> bool:
        return s.isdigit() or s.replace('.', '', 1).isdigit()

    def _clean_token(s: str) -> str:
        return (s or "").strip().strip(",.;:")

    if raw_args:
        a0 = _clean_token(raw_args[0])
        # Con destino: !id o alias textual
        if a0.startswith("!") or not _is_number(a0):
            target = a0
            # Si hay segundo arg numérico → ahora lo interpretamos como minutos de histórico
            if len(raw_args) >= 2 and _is_number(_clean_token(str(raw_args[1]))):
                try:
                    hist_mins = int(float(_clean_token(str(raw_args[1]))))
                except Exception:
                    hist_mins = 30
            # Si hay tercer arg numérico → timeout
            if len(raw_args) >= 3 and _is_number(_clean_token(str(raw_args[2]))):
                try:
                    timeout = float(_clean_token(str(raw_args[2])))
                except Exception:
                    timeout = 4.0
        else:
            # Sin destino: [max_n] [timeout] como ya tenías
            try:
                max_n = int(a0)
            except Exception:
                max_n = 20
            if len(raw_args) >= 2 and _is_number(_clean_token(str(raw_args[1]))):
                try:
                    timeout = float(_clean_token(str(raw_args[1])))
                except Exception:
                    timeout = 4.0

    # ---------- Acceso a pool/interface persistente ----------
    pool = context.bot_data.get("tcp_pool")
    host = context.bot_data.get("mesh_host")
    port = context.bot_data.get("mesh_port", 4403)
        # --- [NUEVO] bandera dura: NO abrir sockets desde el bot ---
    disable_direct_iface = True

    if not pool or not host:
        await update.effective_message.reply_text("⚠️ Config no inicializada (pool/host).")
        return ConversationHandler.END

    import time, socket, json as _json
    now = int(time.time())

    # ---------- Extracción de métricas desde iface (sin abrir conexiones nuevas) ----------
    def _extract_nodes_with_metrics(iface):
        raw = getattr(iface, "nodes", None)
        if raw and isinstance(raw, dict):
            it = raw.values()
        elif isinstance(raw, list):
            it = raw
        else:
            g = getattr(iface, "getNodes", None)
            it = g() if callable(g) else []

        out = []
        for n in it or []:
            if not isinstance(n, dict):
                continue
            u = n.get("user") or {}
            uid = u.get("id") or n.get("id") or n.get("nodeId") or n.get("num") or ""
            uid_str = str(uid).strip()
            alias = u.get("longName") or u.get("shortName") or n.get("name") or uid_str or "¿sin_alias?"

            metrics = n.get("deviceMetrics") or n.get("metrics") or {}
            snr = metrics.get("snr", n.get("snr"))
            rssi = metrics.get("rssi", n.get("rssi"))
            batt = metrics.get("batteryLevel") or metrics.get("battery") or metrics.get("batt")
            voltage = metrics.get("voltage") or metrics.get("vBatt") or metrics.get("vbatt")
            temp = metrics.get("temperature") or metrics.get("tempC") or metrics.get("temp")
            airmon = metrics.get("airUtilTx") or metrics.get("airtime") or metrics.get("airUtil")
            ch_util = metrics.get("channelUtilization") or metrics.get("chUtil")

            last = n.get("lastHeard") or n.get("last_heard") or n.get("heard")
            last = int(last) if isinstance(last, (int, float)) else 0
            ago = (now - last) if last else None

            out.append({
                "id": uid_str,
                "alias": str(alias),
                "ago": ago,
                "snr": snr,
                "rssi": rssi,
                "battery": batt,
                "voltage": voltage,
                "temp": temp,
                "air": airmon,
                "chutil": ch_util
            })

        if not target:
            out.sort(key=lambda x: (x["ago"] if x["ago"] is not None else 10**9))
            if max_n and max_n > 0:
                out[:] = out[:max_n]
        return out

    metrics_list = []

    # === [CAMBIO CRÍTICO] NO abrir conexiones desde el bot ===
    if not disable_direct_iface:
        # (opcional: aquí podrías dejar SOLO un intento que NO cree socket nuevo.
        # Pero como no sabemos si el pool soporta 'reuse_only', lo simplificamos a 'no abrir'.)
        pass

    # Si no tenemos datos "en vivo" porque no queremos abrir sockets, hacemos pseudo-live
    if not metrics_list:
        import os
        jsonl_path = context.bot_data.get("telemetry_jsonl_path") or os.path.join("bot_data", "telemetry_log.jsonl")
        # Usamos la misma semántica que ya tenías: sin destino → listado compacto
        # Para no cambiar UX, usa hist_mins como ventana por defecto si quieres
        pseudo_window = 30  # min por defecto para pseudo-live
        metrics_list = _read_pseudo_live_from_jsonl(jsonl_path, max_n=max_n, window_mins=pseudo_window)




    # ---------- Filtro por destino (si se indicó !id o alias) ----------
    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    if target:
        tgt = _norm(target)
        def _match(m: dict) -> bool:
            mid = _norm(m.get("id") or "")
            mal = _norm(m.get("alias") or "")
            mid_bare = mid[1:] if mid.startswith("!") else mid
            tgt_bare = tgt[1:] if tgt.startswith("!") else tgt
            return (tgt == mid) or (tgt_bare == mid_bare) or (tgt == mal)

        filtered = [m for m in metrics_list if _match(m)]
        metrics_list = filtered

        if not metrics_list:
            await update.effective_message.reply_text(f"❌ Nodo {target} no encontrado ahora mismo.")
            return ConversationHandler.END

    # === [NUEVO] Histórico desde el broker (FETCH_TELEMETRY) ==================
    hist_lines = []
    if target:
        # Resolver !id canónico si tienes util; si no, usa el id de la foto en vivo:
        node_id = None
        try:
            if 'resolver_alias_o_id' in globals() and callable(globals()['resolver_alias_o_id']):
                _res = resolver_alias_o_id(target)  # puede ser (node_id, alias) o un str
                if isinstance(_res, (tuple, list)):
                    node_id = str(_res[0]) if _res else None
                else:
                    node_id = str(_res) if _res else None
        except Exception:
            node_id = None

            
        if not node_id:
            # toma el primero del vivo
            node_id = metrics_list[0].get("id")

        # === [SUSTITUIR por esta versión] Helper para pedir histórico al broker ===
        async def _fetch_telemetry_broker(seconds: int, node: str, limit: int = 120):
            import time as _t, socket as _s, json as _j
            host_b = context.bot_data.get("backlog_host", "127.0.0.1")
            port_b = int(context.bot_data.get("backlog_port", 8766))  # ← por defecto 8766 en tu broker v5
            now = int(_t.time())

            # 1) Intento A: comando directo FETCH_TELEMETRY (Opción B)
            #    Preferido si lo tienes implementado en _BacklogServer.
            payload_A = {"cmd": "FETCH_TELEMETRY",
                        "params": {"since": float(seconds), "node": node or None, "limit": int(limit)}}
            try:
                # Si tienes helper centralizado:
                if 'fetch_backlog_from_broker' in globals() and callable(globals()['fetch_backlog_from_broker']):
                    res = await fetch_backlog_from_broker("FETCH_TELEMETRY", params=payload_A["params"])
                    if isinstance(res, dict):
                        items = res.get("items") or res.get("data") or []
                        if items:
                            return items
                # Fallback TCP crudo:
                with _s.create_connection((host_b, port_b), timeout=4.0) as s:
                    s.sendall((_j.dumps(payload_A, ensure_ascii=False) + "\n").encode("utf-8"))
                    buf = b""; s.settimeout(6.0)
                    while True:
                        b = s.recv(65536)
                        if not b: break
                        buf += b
                        if b"\n" in b: break
                resp = _j.loads(buf.decode("utf-8", "ignore").strip())
                items = (resp.get("items") or resp.get("data") or []) if isinstance(resp, dict) and resp.get("ok") else []
                if items:
                    return items
            except Exception:
                pass

            # 2) Intento B (fallback): usar FETCH_BACKLOG con portnums=["TELEMETRY_APP"]
            #    y filtrar por nodo en cliente.
            try:
                since_ts = int(now - int(seconds)) if seconds < 1e10 else int(seconds)
                payload_B = {"cmd": "FETCH_BACKLOG",
                            "params": {"since_ts": since_ts, "until_ts": None,
                                        "channel": None, "portnums": ["TELEMETRY_APP"],
                                        "limit": int(limit)}}

                # Helper centralizado si existe
                if 'fetch_backlog_from_broker' in globals() and callable(globals()['fetch_backlog_from_broker']):
                    res = await fetch_backlog_from_broker("FETCH_BACKLOG", params=payload_B["params"])
                    rows = (res.get("data") or res.get("items") or []) if isinstance(res, dict) and res.get("ok", True) else []
                else:
                    with _s.create_connection((host_b, port_b), timeout=4.0) as s:
                        s.sendall((_j.dumps(payload_B, ensure_ascii=False) + "\n").encode("utf-8"))
                        buf = b""; s.settimeout(6.0)
                        while True:
                            b = s.recv(65536)
                            if not b: break
                            buf += b
                            if b"\n" in b: break
                    resp = _j.loads(buf.decode("utf-8", "ignore").strip())
                    rows = (resp.get("data") or resp.get("items") or []) if isinstance(resp, dict) and resp.get("ok", True) else []

                # Normaliza posibles estructuras de telemetría y filtra por nodo si se pasó
                out = []
                node_norm = str(node or "").lower()
                node_bare = node_norm[1:] if node_norm.startswith("!") else node_norm
                for r in rows:
                    fr = (r.get("from") or r.get("fromId") or r.get("from_id") or "")
                    fr_norm = str(fr).lower()
                    fr_bare = fr_norm[1:] if fr_norm.startswith("!") else fr_norm
                    if node and not (fr_norm == node_norm or fr_bare == node_bare):
                        continue

                    # Unifica timestamps
                    ts = r.get("ts") or r.get("rxTime") or r.get("timestamp") or r.get("rx_time")
                    # Unifica payload
                    decoded = r.get("decoded") or {}
                    telem = decoded.get("telemetry") or decoded.get("payload") or {}
                    dev = (r.get("device") if isinstance(r.get("device"), dict) else {}) or telem.get("deviceMetrics") or telem.get("device") or {}
                    env = (r.get("environment") if isinstance(r.get("environment"), dict) else {}) or telem.get("environmentMetrics") or telem.get("environment") or {}

                    if not (dev or env):
                        # si se persistió con otra forma, lo dejamos pasar igualmente; cliente decide
                        pass

                    out.append({"ts": ts, "from": fr, "device": dev or None, "environment": env or None})
                return out
            except Exception:
                return []

        # Ventana en segundos
               # Ventana en segundos
        window_s = int(hist_mins) * 60 if hist_mins and hist_mins > 0 else 1800
        items = await _fetch_telemetry_broker(window_s, node_id, limit=200)

        if items:
            # Formateo compacto de histórico (device + environment si existen) [vía broker]
            for r in items[:60]:
                ts = r.get("ts") or r.get("rxTime") or r.get("timestamp") or 0
                hh = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "??:??:??"
                dev = r.get("device") or {}
                env = r.get("environment") or {}
                s_dev = ", ".join([f"{k}:{v}" for k, v in dev.items()]) if dev else ""
                s_env = ", ".join([f"{k}:{v}" for k, v in env.items()]) if env else ""
                if s_dev or s_env:
                    hist_lines.append(f"⏱ {hh}  DEV[{s_dev}]  ENV[{s_env}]".strip())

        # === [NUEVO] Fallback a JSONL local si el histórico por broker no devolvió nada ===
        if not hist_lines:
            import os, json as _j, time as _t
            jsonl_path = (
                context.bot_data.get("telemetry_jsonl_path")
                or os.path.join("bot_data", "telemetry_log.jsonl")
            )

            def _read_hist_jsonl(path: str, node: str, window_secs: int, limit: int = 200):
                if not path or not os.path.exists(path):
                    return []
                now = int(_t.time())
                since_ts = now - int(window_secs)
                node_norm = str(node or "").lower()
                node_bare = node_norm[1:] if node_norm.startswith("!") else node_norm

                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                except Exception:
                    return []

                out = []
                # Leemos de más reciente a más antiguo
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = _j.loads(line)
                    except Exception:
                        continue

                    ts = obj.get("ts") or obj.get("timestamp") or obj.get("time") or 0
                    if isinstance(ts, (int, float)) and ts < since_ts:
                        # ya es más antiguo que la ventana → como vamos hacia atrás, podemos cortar si quieres
                        # pero por seguridad seguimos sin romper
                        pass

                    fr = (obj.get("from") or obj.get("fromId") or obj.get("from_id") or "")
                    fr_norm = str(fr).lower()
                    fr_bare = fr_norm[1:] if fr_norm.startswith("!") else fr_norm
                    if node and not (fr_norm == node_norm or fr_bare == node_bare):
                        continue

                    # Estructura típica en tu JSONL local:
                    # { "ts": ..., "from": "!id", "device": {...}, "environment": {...}|None }
                    dev = obj.get("device") or {}
                    env = obj.get("environment") or {}

                    out.append({"ts": ts, "device": dev or {}, "environment": env or {}})
                    if len(out) >= limit:
                        break

                return list(reversed(out))  # cronológico

            local_items = _read_hist_jsonl(jsonl_path, node_id, window_s, limit=200)
            for r in local_items[:60]:
                ts = r.get("ts") or 0
                hh = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "??:??:??"
                dev = r.get("device") or {}
                env = r.get("environment") or {}
                s_dev = ", ".join([f"{k}:{v}" for k, v in dev.items()]) if dev else ""
                s_env = ", ".join([f"{k}:{v}" for k, v in env.items()]) if env else ""
                if s_dev or s_env:
                    hist_lines.append(f"⏱ {hh}  DEV[{s_dev}]  ENV[{s_env}]".strip())
        # === [FIN NUEVO] =======================================================

    if not metrics_list and not hist_lines:
        await update.effective_message.reply_text("📊 Telemetría:\n\n(sin datos ahora mismo)")
        return ConversationHandler.END

    # ---------- Formateo de salida ----------
    def fmt_ago(sec):
        if sec is None:
            return "—"
        m, s = divmod(max(0, int(sec)), 60)
        h, m = divmod(m, 60)
        if h: return f"{h}h {m}m"
        if m: return f"{m}m {s}s"
        return f"{s}s"

    def fmt(val, suffix=""):
        if isinstance(val, (int, float)):
            if isinstance(val, float):
                return f"{val:.3f}{suffix}" if suffix.strip().upper() == "V" else f"{val:.1f}{suffix}"
            return f"{val}{suffix}"
        if val is None:
            return "—"
        return str(val)

    lines = []
    for m in metrics_list:
        alias = str((m.get("alias") or m.get("id") or "¿sin_alias?")).strip()
        nid = m.get("id") or "¿id?"
        snr_txt = fmt(m.get("snr"), " dB")
        rssi_txt = fmt(m.get("rssi"), " dBm")
        batt_txt = fmt(m.get("battery"), "%")
        volt = m.get("voltage")
        volt_txt = f"{float(volt):.3f} V" if isinstance(volt, (int, float)) else ("—" if volt is None else f"{volt} V")
        temp_txt = fmt(m.get("temp"), " °C")
        air_txt = fmt(m.get("air"), " %")
        chutil_txt = fmt(m.get("chutil"), " %")
        ago_txt = fmt_ago(m.get("ago"))
        lines.append(
            f"{alias} ({nid}) — visto hace {ago_txt}\n"
            f"  • SNR: {snr_txt} | RSSI: {rssi_txt}\n"
            f"  • Batería: {batt_txt} | Voltaje: {volt_txt} | Temp: {temp_txt}\n"
            f"  • AirUtilTx: {air_txt} | ChannelUtil: {chutil_txt}"
        )

    header = "📊 Telemetría (en vivo):" if not target else f"📊 Telemetría (en vivo) de {target}:"
    txt = header + "\n\n" + "\n\n".join(lines)

    # Adjuntar histórico si lo hay
    if hist_lines:
        txt_hist = f"\n\n🗂 Histórico últimos {hist_mins} min (broker):\n" + "\n".join(hist_lines)
        txt += txt_hist

    # Enviar respetando límite de Telegram
    if len(txt) > 3900:
        # Si tienes un helper para dividir mensajes, úsalo aquí.
        # Si no, cortamos de forma simple:
        await update.effective_message.reply_text(txt[:3900])
        resto = txt[3900:]
        while resto:
            await update.effective_message.reply_text(resto[:3900])
            resto = resto[3900:]
    else:
        await update.effective_message.reply_text(txt)

    return ConversationHandler.END


# ===== NUEVO: comando /canales =====
# ===== NUEVO: comando /canales (robusto con ensure_connected y rutas de fallback) =====
async def canales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /canales — Muestra lista de canales (número + nombre/PSK si existe).
    Intenta reutilizar la interfaz del pool; si no está lista, fuerza ensure_connected
    y recurre a las rutas alternativas del pool (session/run_with_interface/acquire/get).
    """
        
    bump_stat(update.effective_user.id, update.effective_user.username or "", "canales")

    pool = context.bot_data.get("tcp_pool")
    host = context.bot_data.get("mesh_host")
    port = context.bot_data.get("mesh_port", 4403)

    if not pool or not host:
        await update.effective_message.reply_text("⚠️ Pool TCP no inicializado.")
        return

    # --- Helper: extraer lista de canales desde una iface ---
    def _extract_channels_from_iface(iface):
        try:
            chans = getattr(getattr(iface, "localNode", None), "channels", None)
            if not chans:
                return []
            lines = []
            for idx, ch in enumerate(chans):
                if not ch:
                    continue
                # ch.settings puede ser objeto (attrs) o dict
                settings = getattr(ch, "settings", None)
                if settings is None and isinstance(ch, dict):
                    settings = ch.get("settings")
                name = None
                psk = None
                if settings is not None:
                    # nombre
                    name = getattr(settings, "name", None)
                    if name is None and isinstance(settings, dict):
                        name = settings.get("name")
                    # psk (string/bytes/None)
                    psk = getattr(settings, "psk", None)
                    if psk is None and isinstance(settings, dict):
                        psk = settings.get("psk")
                name = name or f"Canal {idx}"
                # normalizar psk (si viniera en bytes)
                try:
                    if isinstance(psk, (bytes, bytearray)):
                        psk = psk.decode("utf-8", errors="ignore")
                except Exception:
                    pass
                line = f"#{idx} — {name}"
                if psk:
                    line += f" (psk={psk})"
                lines.append(line)
            return lines
        except Exception:
            return []

    # 1) Intento directo: get_iface_wait / get_iface
    try:
        iface = None
        if hasattr(pool, "get_iface_wait"):
            iface = pool.get_iface_wait(timeout=3.0)
        elif hasattr(pool, "get_iface"):
            iface = pool.get_iface()
        else:
            iface = getattr(pool, "iface", None)

        if iface is None:
            # 2) Forzar conexión si el pool aún no ha abierto socket
            ensure_fn = getattr(pool, "ensure_connected", None)
            if callable(ensure_fn):
                try:
                    ensure_fn(host, port, timeout=4.0)
                except Exception:
                    pass
            # reintentar obtener iface
            if hasattr(pool, "get_iface_wait"):
                iface = pool.get_iface_wait(timeout=2.5)
            elif hasattr(pool, "get_iface"):
                iface = pool.get_iface()
            else:
                iface = getattr(pool, "iface", None)

        if iface is not None:
            lines = _extract_channels_from_iface(iface)
            if lines:
                text = "📡 <b>Canales configurados:</b>\n" + "\n".join(lines)
                await update.effective_message.reply_text(text, parse_mode="HTML")
                return
    except Exception:
        pass

    # 3) Rutas de fallback del pool (sin romper la interfaz persistente)
    # 3.1) session(...)
    try:
        session_cm = getattr(pool, "session", None)
        if callable(session_cm):
            with pool.session(host, port, timeout=4.0) as iface:
                lines = _extract_channels_from_iface(iface)
                if lines:
                    text = "📡 <b>Canales configurados:</b>\n" + "\n".join(lines)
                    await update.effective_message.reply_text(text, parse_mode="HTML")
                    return
    except Exception:
        pass

    # 3.2) run_with_interface(...)
    try:
        run_with_iface = getattr(pool, "run_with_interface", None)
        if callable(run_with_iface):
            lines = run_with_iface(host, port, 4.0, _extract_channels_from_iface)
            if lines:
                text = "📡 <b>Canales configurados:</b>\n" + "\n".join(lines)
                await update.effective_message.reply_text(text, parse_mode="HTML")
                return
    except Exception:
        pass

    # 3.3) acquire()/release()
    try:
        acquire_fn = getattr(pool, "acquire", None)
        if callable(acquire_fn):
            iface = None
            try:
                iface = pool.acquire(host, port, timeout=4.0)
                lines = _extract_channels_from_iface(iface)
                if lines:
                    text = "📡 <b>Canales configurados:</b>\n" + "\n".join(lines)
                    await update.effective_message.reply_text(text, parse_mode="HTML")
                    return
            finally:
                try:
                    if iface and hasattr(iface, "release"):
                        iface.release()
                except Exception:
                    pass
    except Exception:
        pass

    # 3.4) get()/ensure_connected() (último intento)
    try:
        get_fn = getattr(pool, "get", None) or getattr(pool, "get_or_create", None)
        ensure_fn = getattr(pool, "ensure_connected", None)
        if callable(get_fn):
            iface = get_fn(host, port)
            if callable(ensure_fn):
                try:
                    ensure_fn(host, port, timeout=4.0)
                except Exception:
                    pass
            lines = _extract_channels_from_iface(iface)
            if lines:
                text = "📡 <b>Canales configurados:</b>\n" + "\n".join(lines)
                await update.effective_message.reply_text(text, parse_mode="HTML")
                return
    except Exception:
        pass

    # Si hemos llegado aquí, no conseguimos leer canales
    await update.effective_message.reply_text("⚠️ No se pudo acceder a la interfaz TCP o no hay canales configurados.")

async def cobertura_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /cobertura [!id|alias] [Xh] [entorno]
      - Genera un mapa de cobertura a partir del BacklogServer (sin abrir sockets al nodo).
      - HTML: Heatmap + Círculos (si Folium). KML: polígonos circulares + pines.
      - 'entorno' ∈ {urbano, suburbano, abierto}. Por defecto: urbano.
      - Ejemplos:
        /cobertura
        /cobertura 12h
        /cobertura !9eeb1328 48h suburbano
        /cobertura Quasimodo abierto
    """
        
    bump_stat(update.effective_user.id, update.effective_user.username or "", "cobertura")

    args = context.args or []
    target = None
    hours = 24
    env = "urbano"

    # Parse horas tipo "12h"
    rest = []
    for a in args:
        s = str(a).strip().lower()
        if s.endswith("h") and s[:-1].isdigit():
            hours = int(s[:-1])
        elif s in ("urbano", "suburbano", "abierto"):
            env = s
        else:
            rest.append(a)

    if rest:
        target = " ".join(rest).strip()

    host = (context.bot_data.get("backlog_host") if context.bot_data else None) or "127.0.0.1"
    try:
        port = int((context.bot_data.get("backlog_port") if context.bot_data else None) or 8766)
    except Exception:
        port = 8766

    try:
        # ANTES:
        # out = build_coverage_from_backlog(...)

        # AHORA:
        out = build_coverage_combined(
            hours=hours,
            target_node=target,
            output_dir="bot_data/maps",
            backlog_host=host,
            backlog_port=port,
            env=env,
            make_kml=True,
        )


    except Exception as e:
        await update.effective_message.reply_text(f"❗ No pude generar el mapa de cobertura: {e}")
        return ConversationHandler.END

    # Enviar HTML (si existe) y KML
    sent_any = False
    try:
        if out.get("html") and os.path.exists(out["html"]):
            with open(out["html"], "rb") as f:
                await update.effective_message.reply_document(
                    document=f,
                    filename=os.path.basename(out["html"]),
                    caption=f"Cobertura {'de ' + target if target else '(todos)'} • {hours}h • {env}",
                )
            sent_any = True
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Error enviando HTML: {e}")

    try:
        if out.get("kml") and os.path.exists(out["kml"]):
            with open(out["kml"], "rb") as f:
                await update.effective_message.reply_document(
                    document=f,
                    filename=os.path.basename(out["kml"]),
                    caption=f"KML (círculos + pines) • {hours}h • {env}",
                )
            sent_any = True
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Error enviando KML: {e}")

    if not sent_any:
        await update.effective_message.reply_text("⚠️ No se pudo adjuntar ningún archivo de salida.")

    return ConversationHandler.END


def _append_send_ack_log_row(row: List[Any]) -> None:
    new_file = not SEND_ACK_LOG_CSV.exists()
    try:
        with SEND_ACK_LOG_CSV.open("a", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            if new_file:
                w.writerow(["timestamp","dest","canal","texto","attempts","ack_ok","reason","packet_id"])
            w.writerow(row)
    except Exception as e:
        log(f"⚠️ No se pudo escribir log de envío ACK: {e}")

import re

# --- REEMPLAZA COMPLETO ---
def _extract_ack_params(args: list[str]) -> tuple[int, float, float, list[str]]:
    """
    Extrae reintentos/espera/backoff de una lista de tokens, sin comerse ninguna palabra del texto.
    Formatos aceptados (mezclables, en cualquier orden):
      - reintentos=3
      - espera=12
      - backoff=1.7
    Devuelve: (attempts, wait_s, backoff, rest_tokens)
    """
    attempts = ACK_MAX_ATTEMPTS
    wait_s   = float(ACK_WAIT_SEC)
    backoff  = float(ACK_BACKOFF)

    rest: list[str] = []
    for t in (args or []):
        m = re.match(r"(?i)reintentos\s*=\s*(\d+)$", t)
        if m:
            try: attempts = max(1, int(m.group(1)))
            except Exception: pass
            continue
        m = re.match(r"(?i)espera\s*=\s*(\d+)$", t)
        if m:
            try: wait_s = max(1.0, float(m.group(1)))
            except Exception: pass
            continue
        m = re.match(r"(?i)backoff\s*=\s*([0-9]*\.?[0-9]+)$", t)
        if m:
            try: backoff = max(1.0, float(m.group(1)))
            except Exception: pass
            continue
        # Cualquier otro token se conserva tal cual (no se pierde ninguna palabra del texto)
        rest.append(t)

    return attempts, float(wait_s), float(backoff), rest


# ------- SCHEDULER COMANDOS

# === NUEVO: /programar ===

async def programar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /programar <YYYY-MM-DD HH:MM> <destino[:canal] | canal N> <texto...> [ack]
    Ejemplos:
      /programar 2025-09-02 09:30 canal 0 broadcast Buenos días a todos
      /programar 2025-09-02 21:45 !b03df4cc:1 Aviso crítico ack
    ZH: Europe/Madrid (por defecto). Guarda en bot_data/scheduled_tasks.jsonl.
    """
    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END
    
    bump_stat(update.effective_user.id, update.effective_user.username or "", "programar")

    # --- Límite seguro de payload en la malla (UTF-8) ---
    MAX_BYTES = 180
    def _utf8_len(s: str) -> int:
        return len(s.encode("utf-8"))
    def _validate_len_or_block(texto_norm: str) -> tuple[bool, str]:
        b = _utf8_len(texto_norm)
        if b <= MAX_BYTES:
            return True, ""
        return False, (
            "❌ <b>Mensaje demasiado largo</b>\n"
            f"• Tamaño: <code>{b} bytes</code> (límite: {MAX_BYTES} bytes)\n"
            "• Acórtalo (recorta título, evita comillas tipográficas o usa una URL más corta)."
        )

    toks = [t for t in (context.args or []) if t.strip()]

    # SUBMENÚ si no hay argumentos: muestra atajos /en y /mañana
    if len(toks) == 0:
        kb = ReplyKeyboardMarkup(
            [
                ["/en 10 canal 0 ", "/mañana 09:30 canal 0 "],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
            selective=True,
            input_field_placeholder="Elige un atajo, completa parámetros y envía…"
        )
        await update.effective_message.reply_text(
            "🗂️ <b>Programar envío</b>\n"
            "Toca un atajo y <i>completa los parámetros</i> antes de enviar.\n\n"
            "<b>Ejemplos</b>:\n"
            "• <code>/en 15 canal 0 Buenos días a todos</code>\n"
            "• <code>/mañana 09:30 !a0cb0bc4 Aviso importante</code>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    # 1) Fecha/hora local (se valida en broker_tasks)
    when_local = " ".join(toks[:2])
    rest = toks[2:]

    # 2) Canal y destino/texto SIN consultas a la API (no build_nodes_mapping)
    canal = BROKER_CHANNEL
    user_set_canal = False

    # Permitir "canal N" antes del destino
    if len(rest) >= 2 and rest[0].lower() == "canal":
        try:
            canal = int(rest[1])
            user_set_canal = True
        except Exception:
            pass
        rest = rest[2:]

    if not rest:
        await update.effective_message.reply_text("Falta el destino y el texto.")
        return

    def _plausible_dest(tok: str) -> bool:
        t = tok.strip()
        return (t.lower() == "broadcast") or t.startswith("!") or t.isdigit()

    # --- Caso: usuario puso "canal N" y el siguiente token NO parece destino → broadcast implícito
    if user_set_canal and rest and not _plausible_dest(rest[0]):
        destination = "broadcast"
        texto = " ".join(rest).strip()
        if not texto:
            await update.effective_message.reply_text("Falta el texto a enviar.")
            return

        # ACK al final del texto
        require_ack = False
        if texto.endswith(" ack") or texto.endswith(" ACK"):
            require_ack = True
            texto = texto.rsplit(" ", 1)[0].strip()

        # Normalizar texto (el broker volverá a normalizar; esto es idempotente)
        texto_norm = _norm_mesh(texto)

        # VALIDACIÓN DE LONGITUD: bloquear si excede el límite
        ok_len, err = _validate_len_or_block(texto_norm)
        if not ok_len:
            await update.effective_message.reply_text(err, parse_mode="HTML")
            return

        # Estimar número de partes (orientativo; el broker hace el split real)
        est_parts = len(_split_mesh(texto_norm, max_bytes=MAX_BYTES))

        try:
            res = broker_tasks.schedule_message(
                when_local=when_local,
                channel=int(canal),
                message=texto_norm,
                destination=str(destination),
                require_ack=bool(require_ack),
                meta={
                    "scheduled_by": update.effective_user.username or str(update.effective_user.id),
                    "user_set_canal": user_set_canal,
                    "raw_dest_token": None,
                    "implicit_broadcast": True,
                    "bot_est_parts": est_parts,
                    # NUEVO → para notificación de ejecución:
                    "chat_id": update.effective_chat.id,
                    "reply_to": update.effective_message.message_id,
                },
                max_attempts=3,
            )
            if not res.get("ok"):
                raise RuntimeError(res)
            t = res["task"]
            extra = f"\n• Partes estimadas: {est_parts}" if est_parts > 1 else ""
            await update.effective_message.reply_text(
                "⏰ Tarea programada:\n"
                f"• ID: {t['id']}\n"
                f"• Cuándo (local): {when_local} (Europe/Madrid)\n"
                f"• Canal: {t['channel']}\n"
                f"• Destino: {t['destination']}\n"
                f"• ACK: {'Sí' if t['require_ack'] else 'No'}"
                f"{extra}"
            )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ No se pudo programar: {e}")
        return
    # --- FIN caso broadcast implícito ---

    # 3) Primer token = destino (puede venir con sufijo :canal)
    dest_token = rest[0]
    canal_from_dest = None

    if ":" in dest_token:
        head, tail = dest_token.split(":", 1)
        if tail.isdigit():
            canal_from_dest = int(tail)
            dest_core = head
        else:
            dest_core = dest_token
    else:
        dest_core = dest_token

    # Si destino trae canal y NO pusiste "canal N", prevalece el del destino
    if (canal_from_dest is not None) and (not user_set_canal):
        canal = canal_from_dest

    # Normalizar destino a guardar (sin resolver alias aquí)
    if dest_core.lower() == "broadcast":
        destination = "broadcast"
    else:
        destination = dest_core  # "!id" o alias (se resolverá al enviar)

    # 4) Texto = resto tras el destino
    texto = " ".join(rest[1:]).strip()
    if not texto:
        await update.effective_message.reply_text("Falta el texto a enviar.")
        return

    # 5) Flag ACK (si el texto acaba en ' ack' o ' ACK')
    require_ack = False
    if texto.endswith(" ack") or texto.endswith(" ACK"):
        require_ack = True
        texto = texto.rsplit(" ", 1)[0].strip()

    # 6) Normalizar texto (idempotente respecto al broker)
    texto_norm = _norm_mesh(texto)

    # VALIDACIÓN DE LONGITUD: bloquear si excede el límite
    ok_len, err = _validate_len_or_block(texto_norm)
    if not ok_len:
        await update.effective_message.reply_text(err, parse_mode="HTML")
        return

    est_parts = len(_split_mesh(texto_norm, max_bytes=MAX_BYTES))

    # 7) Programar SIN tocar API ni conexiones ahora
    try:
        res = broker_tasks.schedule_message(
            when_local=when_local,
            channel=int(canal),
            message=texto_norm,
            destination=str(destination),
            require_ack=bool(require_ack),
            meta={
                "scheduled_by": update.effective_user.username or str(update.effective_user.id),
                "user_set_canal": user_set_canal,
                "raw_dest_token": dest_token,
                "bot_est_parts": est_parts,
                # NUEVO → para notificación de ejecución:
                "chat_id": update.effective_chat.id,
                "reply_to": update.effective_message.message_id
            },
            max_attempts=3,
        )
        if not res.get("ok"):
            raise RuntimeError(res)
        t = res["task"]
        extra = f"\n• Partes estimadas: {est_parts}" if est_parts > 1 else ""
        await update.effective_message.reply_text(
            "⏰ Tarea programada:\n"
            f"• ID: {t['id']}\n"
            f"• Cuándo (local): {when_local} (Europe/Madrid)\n"
            f"• Canal: {t['channel']}\n"
            f"• Destino: {t['destination']}\n"
            f"• ACK: {'Sí' if t['require_ack'] else 'No'}"
            f"{extra}"
        )
    except Exception as e:
        await update.effective_message.reply_text(f"❌ No se pudo programar: {e}")

# ===================== NUEVAS FUNCIONES /en y /mañana =====================

# ─────────────────────────────────────────────────────────────────────────────
# Helpers nuevos (colócalos en el mismo módulo donde están en_cmd y aprs_cmd)
# ─────────────────────────────────────────────────────────────────────────────

from typing import List, Tuple

def _parse_minutes_list(spec: str) -> List[int]:
    """
    Convierte '5' o '5,10,25' en [5] o [5,10,25].
    Filtra vacíos, ignora espacios, valida enteros >0.
    """
    parts = [p.strip() for p in spec.split(",")]
    mins = []
    for p in parts:
        if not p:
            continue
        try:
            v = int(p)
            if v <= 0:
                continue
            mins.append(v)
        except ValueError:
            continue
    return mins

def _parse_after_canal(tokens: List[str]) -> Tuple[int, str]:
    """
    Extrae el canal y el mensaje a partir de la palabra 'canal'.
    tokens: lista de argumentos ya tokenizados.
    Devuelve (canal_int, mensaje_str).
    Lanza ValueError si falta canal o mensaje.
    """
    try:
        idx = tokens.index("canal")
    except ValueError:
        raise ValueError("Falta la palabra clave 'canal'.")

    if idx + 1 >= len(tokens):
        raise ValueError("Falta el número de canal tras 'canal'.")

    ch_str = tokens[idx + 1]
    try:
        ch = int(ch_str)
    except ValueError:
        raise ValueError("El canal debe ser numérico.")

    # El mensaje es todo lo que hay después del número de canal, tal cual
    if idx + 2 >= len(tokens):
        raise ValueError("Falta el mensaje a enviar.")
    msg = " ".join(tokens[idx + 2:]).strip()
    if not msg:
        raise ValueError("Falta el mensaje a enviar.")

    return ch, msg

def _schedule_many(using_existing_single_scheduler, *, channel: int, message: str, minutes_list: List[int], mode: str = "mesh") -> List[str]:
    """
    Itera la programación usando la infraestructura existente de una sola programación.
    - using_existing_single_scheduler: función/corrutina ya existente para programar UNA sola tarea.
      Debe aceptar (channel, message, eta_datetime, mode).
    - mode: 'mesh' para /en, 'aprs' para /aprs.

    Devuelve lista de IDs/ack de tareas si el scheduler los retorna; si no, devuelve marcas de tiempo.
    """
    acks = []
    now = datetime.utcnow()
    for m in minutes_list:
        eta = now + timedelta(minutes=m)
        # Llama a TU scheduler actual (no se cambia su firma ni su comportamiento).
        ack = using_existing_single_scheduler(channel, message, eta, mode=mode)
        acks.append(str(ack) if ack is not None else eta.isoformat() + "Z")
    return acks





# ==========================
# /en — Programar en +minutos
# ==========================

from telegram import ReplyKeyboardRemove

from typing import List

async def en_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /en <minutos|m1,m2,...> <destino[:canal] | canal N> <texto…>
    Ejemplos:
      /en 15 canal 0 Buenos días a todos
      /en 5 !b03df4cc:1 Aviso rápido
      /en 5,10,25 canal 0 Mensaje      ← múltiples envíos programados
    """
    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "en")
    except Exception:
        pass

    # --- Límite seguro de payload en la malla (UTF-8) ---
    MAX_BYTES = 180
    def _utf8_len(s: str) -> int:
        return len(s.encode("utf-8"))
    def _validate_len_or_block(texto_norm: str) -> tuple[bool, str]:
        b = _utf8_len(texto_norm)
        if b <= MAX_BYTES:
            return True, ""
        return False, (
            "❌ <b>Mensaje demasiado largo</b>\n"
            f"• Tamaño: <code>{b} bytes</code> (límite: {MAX_BYTES} bytes)\n"
            "• Acórtalo (recorta título, evita comillas tipográficas o usa una URL más corta)."
        )

    args = context.args or []
    if len(args) < 3:
        await _safe_reply_html(
        update.effective_message,
        "Uso: /en <minutos|m1,m2,...> <destino[:canal] | canal N> <texto…>\n"
        "Ej.: /en 10 canal 0 Recordatorio reunión"
    )


    # 1) minutos o lista de minutos
    minutes_spec = args[0]
    minutes_list = _parse_minutes_list(minutes_spec)
    if not minutes_list:
        try:
            mins = int(minutes_spec)
            if mins <= 0:
                raise ValueError
            minutes_list = [mins]
        except Exception:
            await _safe_reply_html(
                update.effective_message,
                "⏱️ El primer argumento debe ser minutos (>0) o lista separada por comas, p. ej. 5,10,25."
            )

            return

    # 2) Parseo de destino/canal/texto SIN API: solo con nodes.txt (igual que antes)
    nodes_map = context.user_data.get("nodes_map")
    if not nodes_map:
        try:
            nodes_map = _build_alias_fallback_from_nodes_file()
            context.user_data["nodes_map"] = nodes_map  # caché ligera
        except Exception:
            nodes_map = {}

    try:
        node_id, canal, texto, _ = parse_dest_channel_and_text(
            args[1:], nodes_map,
            allow_api=False,   # si tu parser soporta este flag
            silent=True        # si tu parser soporta suprimir logs
        )
    except TypeError:
        # si tu parser no tiene esos kwargs
        node_id, canal, texto, _ = parse_dest_channel_and_text(args[1:], nodes_map)

    if not texto:
        await _safe_reply_html(update.effective_message, "❗ Falta el texto del mensaje.")

        return

    if canal is None:
        canal = globals().get("BROKER_CHANNEL", 0)

    # Normalizar texto
    texto_norm = _norm_mesh(texto)
    texto_html = escape(texto_norm)  # ← evita romper parse_mode="HTML"
    dst_html   = escape(node_id or "broadcast")
    
    # Validación de longitud
    ok_len, err = _validate_len_or_block(texto_norm)
    if not ok_len:
        await update.effective_message.reply_text(err, parse_mode="HTML")
        return

    # Estimación de partes (troceo real lo hará broker_task)
    est_parts = len(_split_mesh(texto_norm, max_bytes=MAX_BYTES))

    # 3) Programar N tareas con broker_task.schedule_message (sin cambiar su firma)
    try:
        import broker_task as _bt
    except Exception as e:
       await _safe_reply_html(update.effective_message, f"❌ Error al cargar scheduler: {type(e).__name__}: {e}")
       return

    ids = []
    errors = []
    for mins in minutes_list:
        when_local_dt = datetime.now(TZ_EUROPE_MADRID) + timedelta(minutes=mins)
        when_local_str = when_local_dt.strftime("%Y-%m-%d %H:%M")
        try:
            res = _bt.schedule_message(
                when_local=when_local_str,
                channel=int(canal),
                message=texto_norm,
                destination=(node_id or "broadcast"),
                require_ack=False,
                meta={
                    "scheduled_by": update.effective_user.username or str(update.effective_user.id),
                    "bot_est_parts": est_parts,
                    "via": "/en",
                    # NUEVO → para notificación de ejecución:
                    "chat_id": update.effective_chat.id,
                    "reply_to": update.effective_message.message_id
                }
            )
            if isinstance(res, dict) and res.get("ok"):
                ids.append(res.get("task", {}).get("id", "?"))
            else:
                errors.append(f"{mins}min")
        except Exception as e:
            errors.append(f"{mins}min:{type(e).__name__}")

    # 4) Respuesta
    if ids and not errors:
       extra = f"\n• Partes estimadas: {est_parts}" if est_parts > 1 else ""
       await _safe_reply_html(
            update.effective_message,
            "⏱️ Programados {n} envío(s) → <b>{txt}</b>\nCanal {ch}, destino {dst}\nMinutos: {mins}\nIDs: {ids}{extra}".format(
                n=len(minutes_list),
                txt=texto_html,                     # ← escapado
                ch=canal,
                dst=dst_html,                       # ← escapado
                mins=",".join(str(m) for m in minutes_list),
                ids=", ".join(str(i) for i in ids),
                extra=extra
            )
        )

    elif ids and errors:
       await _safe_reply_html(
            update.effective_message,
            "⚠️ Programados parcialmente. IDs: {ids}. Fallos en: {err}".format(
                ids=", ".join(str(i) for i in ids),
                err=", ".join(errors)
            )
        )

    else:
       await _safe_reply_html(update.effective_message, "❌ No se pudo programar ningún envío.")

# ==========================
# /mañana — Programar al día siguiente HH:MM
# ==========================

async def manana_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /mañana <HH:MM> <destino[:canal] | canal N> <texto…>
    Ejemplos:
      /mañana 09:30 canal 0 Buenos días
      /mañana 21:45 !b03df4cc:1 Aviso crítico
    Programa un mensaje para mañana a la hora indicada.
    """
    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END
    
    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "mañana")
    except Exception:
        pass

    # --- Límite seguro de payload en la malla (UTF-8) ---
    MAX_BYTES = 180
    def _utf8_len(s: str) -> int:
        return len(s.encode("utf-8"))
    def _validate_len_or_block(texto_norm: str) -> tuple[bool, str]:
        b = _utf8_len(texto_norm)
        if b <= MAX_BYTES:
            return True, ""
        return False, (
            "❌ <b>Mensaje demasiado largo</b>\n"
            f"• Tamaño: <code>{b} bytes</code> (límite: {MAX_BYTES} bytes)\n"
            "• Acórtalo (recorta título, evita comillas tipográficas o usa una URL más corta)."
        )

    args = context.args or []
    if len(args) < 3:
        await update.effective_message.reply_text(
            "Uso: /mañana <HH:MM> <destino[:canal] | canal N> <texto…>\n"
            "Ej.: /mañana 09:30 canal 0 Buenos días equipo"
        )
        return

    # Parseo y validación de hora
    hora_token = args[0].strip()
    try:
        hh, mm = map(int, hora_token.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
    except Exception:
        await update.effective_message.reply_text("⏱️ Hora inválida. Usa formato HH:MM (00–23:59).")
        return

    hora_str = f"{hh:02d}:{mm:02d}"

    # Parseo de destino/canal/texto usando tu parser existente
    # Parseo de destino/canal/texto SIN API: solo con nodes.txt
    nodes_map = context.user_data.get("nodes_map")
    if not nodes_map:
        try:
            # Utilidad ya existente en el proyecto que NO usa API
            nodes_map = _build_alias_fallback_from_nodes_file()
            context.user_data["nodes_map"] = nodes_map  # cache ligera por chat/usuario
        except Exception:
            nodes_map = {}

    # Importante: NO permitir que el parser vaya a la API ni que loguee ese intento
    try:
        node_id, canal, texto, _ = parse_dest_channel_and_text(
            args[1:], nodes_map,
            allow_api=False,   # si tu parser soporta este flag
            silent=True        # si tu parser soporta suprimir logs
        )
    except TypeError:
        # Si tu parser no tiene esos argumentos, llamamos sin flags.
        node_id, canal, texto, _ = parse_dest_channel_and_text(args[1:], nodes_map)


    if not texto:
        await update.effective_message.reply_text("❗ Falta el texto del mensaje.")
        return

    if canal is None:
        canal = globals().get("BROKER_CHANNEL", 0)

    # Normalizar texto (idempotente con el broker)
    texto_norm = _norm_mesh(texto)

    # ✅ Validación de longitud (bloquea si excede)
    ok_len, err = _validate_len_or_block(texto_norm)
    if not ok_len:
        await update.effective_message.reply_text(err, parse_mode="HTML")
        return

    # Estimar partes (orientativo; el troceo real lo hace broker_task)
    est_parts = len(_split_mesh(texto_norm, max_bytes=MAX_BYTES))

    # Mañana a esa hora (zona Europe/Madrid)
    now_local = datetime.now(TZ_EUROPE_MADRID)
    when_local_dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
    when_local_str = when_local_dt.strftime("%Y-%m-%d %H:%M")

    # Programación vía broker_task
    try:
        import broker_task as _bt
        res = _bt.schedule_message(
            when_local=when_local_str,
            channel=int(canal),
            message=texto_norm,
            destination=(node_id or "broadcast"),
            require_ack=False,
            meta={
                "scheduled_by": update.effective_user.username or str(update.effective_user.id),
                "bot_est_parts": est_parts,
                "via": "/mañana",
                # NUEVO → para notificación de ejecución:
                "chat_id": update.effective_chat.id,
                "reply_to": update.effective_message.message_id
            }
        )
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error al programar: {type(e).__name__}: {e}")
        return

    if isinstance(res, dict) and res.get("ok"):
        t = res.get("task", {})
        extra = f"\n• Partes estimadas: {est_parts}" if est_parts > 1 else ""
        await update.effective_message.reply_text(
            f"📅 Programado mañana {hora_str} → <b>{texto_norm}</b>\n"
            f"Canal {canal}, destino {node_id or 'broadcast'}{extra}\n"
            f"ID tarea: <code>{t.get('id','?')}</code>",
            parse_mode="HTML",
        )
    else:
        await update.effective_message.reply_text("❌ No se pudo programar el mensaje.")

# ==========================
# /diario — Programar diariamente a una hora un mensaje
# ==========================

async def diario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /diario <HH:MM[,HH:MM,...]> [mesh|aprs|ambos] [grupo <id>]
            <destino[:canal] | canal N | CALL|broadcast> [aprs <CALL|broadcast>:] <texto…>

    Ejemplos:
      /diario 09:00 mesh canal 2 Parte diario Mesh
      /diario 08:00,12:30 ambos grupo fiestas2025 canal 2 aprs EA1ABC: Programa de fiestas
      /diario 18:45 aprs EA1ABC: Aviso para APRS
    """
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    bump_stat(update.effective_user.id, update.effective_user.username or "", "diario")

    args = [a.strip() for a in (context.args or []) if a and a.strip()]
    if not args:
        await update.effective_message.reply_text(
            "Uso:\n"
            "/diario <HH:MM[,HH:MM,...]> [mesh|aprs|ambos] [grupo <id>] "
            "<destino[:canal] | canal N | CALL|broadcast> [aprs <CALL|broadcast>:] <texto…>"
        )
        return

    # 1) Horas (una o varias separadas por coma)
    horas_spec = args[0]
    horas_list = []
    for chunk in horas_spec.split(","):
        try:
            hh, mm = [int(x) for x in chunk.split(":", 1)]
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
            horas_list.append((hh, mm, f"{hh:02d}:{mm:02d}"))
        except Exception:
            pass
    if not horas_list:
        await update.effective_message.reply_text("⏰ Hora inválida. Usa HH:MM[,HH:MM,...] (00–23:59).")
        return

    # 2) Transporte
    transport = "mesh"
    idx = 1
    if len(args) >= 3 and args[1].lower() in ("mesh", "aprs", "ambos", "both"):
        transport = "both" if args[1].lower() == "ambos" else args[1].lower()
        idx = 2

    # 3) Extraer (y quitar) 'grupo <id>' de los tokens, esté donde esté
    group_id: Optional[str] = None
    def _strip_group_tokens(tokens: list[str]) -> tuple[list[str], Optional[str]]:
        gid = None
        t = tokens[:]
        i = 0
        while i < len(t):
            if t[i].lower() in ("grupo", "group", "grupo_id", "group_id") and (i + 1) < len(t):
                raw = t[i + 1].strip()
                # Normaliza slug seguro pero respeta el nombre que has puesto
                gid = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")[:40] or None
                del t[i:i+2]
                continue
            i += 1
        return t, gid

    aprs_dest: Optional[str] = None
    node_id, canal, texto_norm = None, None, None

    if transport in ("mesh", "both"):
        # Mapa de alias rápido (tu helper)
        nodes_map = context.user_data.get("nodes_map")
        if not nodes_map:
            try:
                nodes_map = _build_alias_fallback_from_nodes_file()
                context.user_data["nodes_map"] = nodes_map
            except Exception:
                nodes_map = {}

        tail_mesh = args[idx:]
        if not tail_mesh:
            await update.effective_message.reply_text("❗ Falta el destino y el texto.")
            return

        # Quitar 'grupo <id>' de los tokens ANTES de parsear canal/destino
        tail_mesh, gid = _strip_group_tokens(tail_mesh)
        if gid:
            group_id = gid

        # En modo BOTH, permitir 'aprs <dest>[:]' en los tokens y retirarlo antes del parseo mesh
        if transport == "both":
            t = tail_mesh[:]
            j = 0
            while j < len(t):
                if t[j].lower() == "aprs":
                    aprs_dest = (t[j + 1] if (j + 1) < len(t) else "broadcast")
                    if isinstance(aprs_dest, str) and aprs_dest.endswith(":"):
                        aprs_dest = aprs_dest[:-1]
                    del t[j:j+2]
                    tail_mesh = t
                    break
                j += 1

        if not tail_mesh:
            await update.effective_message.reply_text("❗ Falta el destino y el texto.")
            return

        # Parser estándar destino/canal/texto
        try:
            node_id, canal, texto, _ = parse_dest_channel_and_text(
                tail_mesh, nodes_map, allow_api=False, silent=True
            )
        except TypeError:
            node_id, canal, texto, _ = parse_dest_channel_and_text(tail_mesh, nodes_map)

        if not texto:
            await update.effective_message.reply_text("❗ Falta el texto del mensaje.")
            return

        texto_norm = _norm_mesh(texto)
        ok_len, err = _validate_len_or_block(texto_norm)
        if not ok_len:
            await update.effective_message.reply_text(err, parse_mode="HTML")
            return

        if canal is None:
            canal = globals().get("BROKER_CHANNEL", 0)

    elif transport == "aprs":
        tail = args[idx:]
        if not tail:
            await update.effective_message.reply_text("❗ Falta destino APRS y texto.")
            return

        # También permitimos 'grupo <id>' aquí
        tail, gid = _strip_group_tokens(tail)
        if gid:
            group_id = gid

        joined = " ".join(tail)
        if ":" in joined:
            head, txt = joined.split(":", 1)
            aprs_dest = (head or "").strip().upper() or "BROADCAST"
            texto_norm = (txt or "").strip()
        else:
            aprs_dest = tail[0].strip().upper() if tail else "BROADCAST"
            texto_norm = " ".join(tail[1:]).strip()

        if not texto_norm:
            await update.effective_message.reply_text("❗ Falta el texto del mensaje.")
            return

        # Para compatibilidad con broker_task, usamos el canal de broker aunque sea APRS
        canal = globals().get("BROKER_CHANNEL", 0)
        node_id = "broadcast"

    # 4) Estimar partes (el envío real troceará si hace falta)
    est_parts = len(_split_mesh(texto_norm, max_bytes=MAX_BYTES))

    # 5) Programar todas las horas con repetición diaria
    created = []
    try:
        now_local = datetime.now(TZ_EUROPE_MADRID)
        for hh, mm, hhmm_txt in horas_list:
            first_dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if first_dt <= now_local:
                first_dt = first_dt + timedelta(days=1)
            when_local_str = first_dt.strftime("%Y-%m-%d %H:%M")

            meta = {
                "scheduled_by": update.effective_user.username or str(update.effective_user.id),
                "bot_est_parts": est_parts,
                "via": "/diario",
                "repeat": "daily",
                "daily_time": hhmm_txt,
                "transport": transport,
                "chat_id": update.effective_chat.id,
                "reply_to": update.effective_message.message_id,
            }
            if group_id:
                meta["daily_group_id"] = group_id
            if aprs_dest:
                meta["aprs_dest"] = aprs_dest

            res = broker_tasks.schedule_message(
                when_local=when_local_str,
                channel=int(canal),
                message=texto_norm,
                destination=(node_id or "broadcast"),
                require_ack=False,
                meta=meta
            )
            if not (isinstance(res, dict) and res.get("ok")):
                raise RuntimeError(res)
            created.append(res["task"])

        # 6) Resumen al usuario
        lines = [
            "⏰ Tareas diarias creadas:",
            f"• Grupo: <code>{group_id or '-'}</code>",
            f"• Transporte: {transport.upper()}",
        ]
        if transport in ("mesh", "both"):
            lines.append(f"• MESH → Canal: {created[0]['channel']}  • Destino: {created[0]['destination']}")
        if transport in ("aprs", "both"):
            lines.append(f"• APRS → Destino: {aprs_dest or 'broadcast'}")
        if est_parts > 1:
            lines.append(f"• Partes estimadas: {est_parts}")

        # Listado de horas + IDs (primera ejecución mostrada en hora local)
        for t in created:
            meta_t = t.get("meta") or {}
            wutc = t.get("when_utc") or ""
            dt_utc = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt_utc = datetime.strptime(wutc, fmt).replace(tzinfo=UTC)
                    break
                except Exception:
                    continue
            first_local = dt_utc.astimezone(TZ_EUROPE_MADRID).strftime("%Y-%m-%d %H:%M") if dt_utc else wutc
            lines.append(f"  - {meta_t.get('daily_time','--:--')}  → ID <code>{t['id']}</code>  (primera: {first_local} local)")

        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        await update.effective_message.reply_text(f"❌ No se pudo programar: {type(e).__name__}: {e}")


async def mis_diarios_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /mis_diarios [estado] [grupo <group_id>]
    Lista las tareas que tienen meta.repeat == 'daily'.
    Estados: pending|done|failed|canceled (por defecto: pending)
    Filtro opcional por grupo: daily_group_id
    """
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    bump_stat(update.effective_user.id, update.effective_user.username or "", "mis_diarios")

    # --- Parseo flexible de argumentos: estado + grupo <id> en cualquier orden ---
    args = [a.strip() for a in (context.args or []) if a and a.strip()]
    status = "pending"
    group_id = None
    i = 0
    while i < len(args):
        a = args[i].lower()
        if a in ("pending", "done", "failed", "canceled"):
            status = a
            i += 1
            continue
        if a in ("grupo", "group", "grupo_id", "group_id"):
            if i + 1 < len(args):
                group_id = args[i + 1].strip()
                i += 2
                continue
            else:
                await update.effective_message.reply_text("Uso: /mis_diarios [pending|done|failed|canceled] [grupo <group_id>]")
                return
        # Si no casa con nada, avanzar
        i += 1

    try:
        res = broker_tasks.list_tasks(status=status if status else None)
        if not res.get("ok"):
            raise RuntimeError(res)
        rows = res.get("tasks") or []

        # Filtro por repeat=daily y, si aplica, por group_id
        diarios = []
        for r in rows:
            meta = r.get("meta") or {}
            if (meta.get("repeat") or "").lower() != "daily":
                continue
            if group_id and (meta.get("daily_group_id") or "") != group_id:
                continue
            diarios.append(r)

        if not diarios:
            extra = f" y grupo {group_id}" if group_id else ""
            await update.effective_message.reply_text(f"(No hay tareas diarias con estado {status}{extra}.)")
            return

        # ===== NUEVO FORMATO DE SALIDA =====
        from html import escape

        def _safe(x, default: str = "-") -> str:
            if x is None:
                return default
            s = str(x).strip()
            return s if s else default

        def _parse_when_local(when_utc: str):
            # Parseo robusto with/without seconds → a zona Europe/Madrid
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(when_utc, fmt).replace(tzinfo=UTC).astimezone(TZ_EUROPE_MADRID)
                    break
                except Exception:
                    continue
            return dt or datetime.now(TZ_EUROPE_MADRID)

        def _short_id(task_id: str) -> str:
            if not task_id:
                return "-"
            return (task_id[:8] + "…") if len(task_id) > 9 else task_id

        def _fmt(r: dict) -> str:
            meta = r.get("meta") or {}

            # Fecha/hora próxima en local
            when_utc = r.get("when_utc") or ""
            dt_local = _parse_when_local(when_utc)
            proxima_local = f"{dt_local.strftime('%Y-%m-%d %H:%M')} (local)"

            # Hora diaria declarada
            hora = meta.get("daily_time") or f"{dt_local.hour:02d}:{dt_local.minute:02d}"

            # Estado
            estado = _safe(r.get("status"), "pending").lower()

            # Transporte / canal / destino
            transport = (meta.get("transport") or "mesh").upper()
            canal = r.get("channel")
            canal_txt = str(canal) if isinstance(canal, int) else _safe(canal, "-")
            destino = _safe(r.get("destination"), "broadcast")

            # APRS dest por defecto → 'broadcast' si transport incluye APRS
            aprs_dest = meta.get("aprs_dest")
            if not aprs_dest and transport in ("APRS", "BOTH"):
                aprs_dest = "broadcast"
            aprs_dest = _safe(aprs_dest, "-")

            # Grupo
            grupo = _safe(meta.get("daily_group_id"), "-")

            # Intentos / máximos
            intentos = int(r.get("attempts") or 0)
            max_intentos = int(meta.get("max_retries") or r.get("max_attempts") or 3)

            # Último error (prioriza meta.last_error si existe)
            last_err = _safe(meta.get("last_error") or r.get("last_error") or "-", "-")

            # Mensaje mostrado (limpio y acotado)
            msg_raw = (meta.get("orig_message") or r.get("message") or "").strip()
            msg_show = " ".join(msg_raw.split())
            if len(msg_show) > 240:
                msg_show = msg_show[:240] + "…"

            rid = _safe(r.get("id"), "-")
            rid_short = _short_id(rid)

            # Construcción HTML
            head = (
                f"📩 <b>{escape(hora)}</b> • {escape(estado)} • {escape(proxima_local)} — "
                f"ID <code>{escape(rid_short)}</code>"
            )
            body = escape(msg_show) if msg_show else ""

            bullets = [
                f"Transporte: <code>{escape(transport)}</code>",
                f"Canal: <code>{escape(canal_txt)}</code>",
                f"Destino: <code>{escape(destino)}</code>",
                f"APRS: <code>{escape(aprs_dest)}</code>",
                f"Grupo: <code>{escape(grupo)}</code>",
                f"Intentos: <code>{intentos}/{max_intentos}</code>",
                f"Último error: <code>{escape(last_err)}</code>",
            ]
            bullets_fmt = "\n".join([f"   • {b}" for b in bullets])

            return f"{head}\n{body}\n{bullets_fmt}".strip()

        # Ordenar por hora local (si daily_time existe) para lectura natural
        def _key(r: dict):
            meta = r.get("meta") or {}
            hhmm = meta.get("daily_time") or ""
            try:
                h, m = hhmm.split(":", 1)
                return (int(h), int(m))
            except Exception:
                # fallback: ordenar por when_utc
                wt = _safe(r.get("when_utc"), "")
                try:
                    dt = datetime.strptime(wt[:16], "%Y-%m-%d %H:%M")  # asume mínimo YYYY-MM-DD HH:MM
                    return (dt.hour, dt.minute)
                except Exception:
                    return (99, 99)

        diarios.sort(key=_key)
        bloques = [_fmt(r) for r in diarios[:120]]

        cabecera = "🗓️ <b>Tareas diarias</b> — estado: <code>{}</code>{}\n\n".format(
            escape(status),
            (f"• grupo <code>{escape(group_id)}</code>" if group_id else "")
        )
        salida = cabecera + "\n\n".join(bloques)

        # Enviar chunked con parse_mode=HTML
        for ch in chunk_text(salida):
            await update.effective_message.reply_text(ch, parse_mode="HTML", disable_web_page_preview=True)
        # ===== FIN NUEVO FORMATO =====

    except Exception as e:
        await update.effective_message.reply_text(f"❌ No se pudo listar: {e}")

async def parar_diario_grupo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /parar_diario_grupo <group_id>
    Cancela todas las tareas diarias asociadas a ese grupo.
    """
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    bump_stat(update.effective_user.id, update.effective_user.username or "", "parar_diario_grupo")

    if not context.args:
        await update.effective_message.reply_text("Uso: /parar_diario_grupo <group_id>")
        return

    group_id = context.args[0].strip()
    if not group_id:
        await update.effective_message.reply_text("Uso: /parar_diario_grupo <group_id>")
        return

    try:
        res_all = broker_tasks.list_tasks()  # todas
        if not res_all.get("ok"):
            raise RuntimeError(res_all)
        rows = res_all.get("tasks") or []

        to_cancel = []
        for r in rows:
            meta = r.get("meta") or {}
            if (meta.get("repeat") or "").lower() != "daily":
                continue
            if (meta.get("daily_group_id") or "") != group_id:
                continue
            if r.get("status") in ("canceled",):
                continue
            to_cancel.append(r.get("id"))

        if not to_cancel:
            await update.effective_message.reply_text(f"(No hay tareas diarias activas con grupo {group_id}.)")
            return

        ok_cnt, err_cnt = 0, 0
        for tid in to_cancel:
            try:
                cres = broker_tasks.cancel(tid)
                if cres.get("ok"):
                    ok_cnt += 1
                else:
                    err_cnt += 1
            except Exception:
                err_cnt += 1

        await update.effective_message.reply_text(
            f"🛑 Grupo <code>{escape(group_id)}</code> cancelado: "
            f"{ok_cnt} ok, {err_cnt} errores.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error: {type(e).__name__}: {e}")

# Asegura imports arriba del fichero si no los tienes ya:
# from telegram.ext import ContextTypes, ConversationHandler

async def parar_diario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /parar_diario <task_id>
    Alias de cancelar para tareas diarias (pero sirve para cualquier task ID).
    """
    # 1) Respeto a tu guard de cooldown
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END

    # 2) Telemetría (no romper si no existe)
    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "parar_diario")
    except Exception:
        pass

    # 3) Obtener task_id
    task_id = (context.args[0].strip() if context.args else None)
    if not task_id:
        await update.effective_message.reply_text("Uso: /parar_diario <task_id>")
        return

    # 4) Import robusto del módulo de tareas (singular/plural)
    try:
        import broker_task as _bt
    except Exception:
        try:
            import broker_task as _bt
        except Exception as e:
            await update.effective_message.reply_text(
                f"❌ Error: no se pudo importar el gestor de tareas (broker_tasks/broker_task): {type(e).__name__}: {e}"
            )
            return

    # 5) Lógica principal con manejo de errores claro
    try:
        # [Opcional] informar si no era daily — aislado en su propio try/except para no romper la cancelación
        try:
            res_all = _bt.list_tasks()
            meta = None
            for r in (res_all.get("tasks") or []):
                if r.get("id") == task_id:
                    meta = r.get("meta") or {}
                    break
            if meta:
                repeat_val = meta.get("repeat")
                # Acepta 'daily' o banderas equivalentes
                is_daily = isinstance(repeat_val, str) and repeat_val.lower() == "daily"
                if not is_daily:
                    await update.effective_message.reply_text("⚠️ Aviso: la tarea no es 'diaria'. Se cancelará igualmente.")
        except Exception:
            # Silencioso: si falla la inspección, pasamos a cancelar igualmente
            pass

        # Cancelar la tarea
        res = _bt.cancel(task_id)
        if isinstance(res, dict) and res.get("ok"):
            await update.effective_message.reply_text(f"🛑 Tarea {task_id} cancelada.")
        else:
            await update.effective_message.reply_text(f"❌ No se pudo cancelar {task_id}: {res}")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error: {type(e).__name__}: {e}")



# === NUEVO: /tareas [status] ===
async def tareas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /tareas [pending|done|failed|canceled]
    Lista tareas desde bot_data/scheduled_tasks.jsonl
    """
    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END
    
    bump_stat(update.effective_user.id, update.effective_user.username or "", "tareas")

    status = (context.args[0].strip().lower() if context.args else "pending")
    if status not in (None, "pending", "done", "failed", "canceled"):
        status = "pending"
    try:
        res = broker_tasks.list_tasks(status=status if status else None)
        if not res.get("ok"):
            raise RuntimeError(res)
        rows = res.get("tasks") or []
        if not rows:
            await update.effective_message.reply_text(f"(No hay tareas con estado {status or 'cualquiera'}.)")
            return

        lines = []
        for r in rows[:100]:
            lines.append(
                f"- {r['id']} • {r['status']} • ch={r['channel']} • dest={r['destination']} • ACK={'Sí' if r.get('require_ack') else 'No'}\n"
                f"  cuando_utc={r['when_utc']} • intentos={r['attempts']}/{r['max_attempts']}\n"
                f"  último_error={r.get('last_error') or '-'}"
            )
        for ch in chunk_text("🗂️ Tareas:\n" + "\n".join(lines)):
            await send_pre(update.effective_message, ch)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ No se pudo listar: {e}")

# === NUEVO: /cancelar_tarea <task_id> ===
async def cancelar_tarea_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    # === [NUEVO] bloquear si el broker está en cooldown ===
    if await _abort_if_cooldown(update, context):
        return ConversationHandler.END
    
    bump_stat(update.effective_user.id, update.effective_user.username or "", "cancelar_tarea")
    task_id = (context.args[0].strip() if context.args else "")
    if not task_id:
        await update.effective_message.reply_text("Uso: /cancelar_tarea <task_id>")
        return
    try:
        res = broker_tasks.cancel(task_id)
        if res.get("ok"):
            await update.effective_message.reply_text(f"🛑 Tarea cancelada: {task_id}")
        else:
            await update.effective_message.reply_text(f"❗ No se pudo cancelar (¿id correcto?): {task_id}")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error al cancelar: {e}")



# ---- Diálogo /enviar (Forcereply)

async def on_send_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dest = (update.effective_message.text or "").strip()
    context.user_data["send_dest"] = dest
    await update.effective_message.reply_text("Escribe el texto a enviar:", reply_markup=ForceReply(selective=True))
    return ASK_SEND_TEXT

async def on_send_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = (update.effective_message.text or "").strip()
    dest_token = context.user_data.pop("send_dest", "broadcast")
    nodes_map = context.user_data.get("nodes_map") or build_nodes_mapping()

    node_id, canal, texto_final, forced_flag = parse_dest_channel_and_text([dest_token, texto], nodes_map)

    traceroute_ok = None; hops = 0
    if TRACEROUTE_CHECK and node_id:
        res = traceroute_node(node_id, timeout=min(TRACEROUTE_TIMEOUT, 20))
        traceroute_ok = bool(res.ok); hops = res.hops
        if not traceroute_ok:
            forced_flag = True

    out = send_text_message(node_id, texto_final, canal=canal)
    respuestas = await quick_broker_listen(node_id, canal, SEND_LISTEN_SEC)

    dest_txt = "broadcast" if node_id is None else node_id
    resumen = (
        f"✉️ Envío a {dest_txt} (canal {canal})\n"
        f"Resultado: {out}\n"
        f"Forzado: {'Sí' if forced_flag else 'No'}\n"
        f"Respuestas en {SEND_LISTEN_SEC}s: {respuestas}"
    )
    if traceroute_ok is not None:
        resumen += f"\nTraceroute previo: {'OK' if traceroute_ok else 'Sin ruta'} (hops={hops})"
    for ch in chunk_text(resumen):
        await send_pre(update.effective_message, ch)

    _append_send_log_row([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        dest_txt, canal,
        (texto_final[:200] + "…") if texto_final and len(texto_final) > 200 else (texto_final or texto),
        "1" if forced_flag else "0",
        "" if traceroute_ok is None else ("1" if traceroute_ok else "0"),
        hops, respuestas,
    ])
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# -------------------------
# ESCUCHA BROKER CONTINUA
# -------------------------

class BrokerClient:
    """Cliente asíncrono para un broker TCP de mensajes Meshtastic."""
    def __init__(self, host: str, port: int, channel: Optional[int], on_message_coro):
        self.host = host
        self.port = port
        self.channel = channel
        self.on_message_coro = on_message_coro  # async def(chat_id, text)
        self._task: Optional[asyncio.Task] = None
        self._running = asyncio.Event()
        self._running.clear()
        self._chat_ids: set[int] = set()

    def add_chat(self, chat_id: int) -> None:
        self._chat_ids.add(chat_id)

    def remove_chat(self, chat_id: int) -> None:
        self._chat_ids.discard(chat_id)

    def chats(self) -> List[int]:
        return sorted(self._chat_ids)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running.set()
        self._task = asyncio.create_task(self._run_loop(), name="broker-client-loop")

    async def stop(self) -> None:
        self._running.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        backoff = 1.5
        delay = 1.0
        while self._running.is_set():
            try:
                log(f"🔌 Conectando a broker {self.host}:{self.port}…")
                reader, writer = await asyncio.open_connection(self.host, self.port)
                log("✅ Conectado al broker.")
                delay = 1.0
                while self._running.is_set():
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("Conexión cerrada por el broker.")
                    text = line.decode("utf-8", errors="ignore").strip()
                    if not text:
                        continue
                    for chat_id in list(self._chat_ids):
                        try:
                            await self.on_message_coro(chat_id, text)
                        except Exception as e:
                            log(f"❗ Error enviando mensaje del broker a chat {chat_id}: {e}")
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                log(f"⚠️ Broker desconectado: {e}. Reintentando en {delay:.1f}s…")
                await asyncio.sleep(delay)
                delay = min(delay * backoff, 60.0)

BROKER: Optional[BrokerClient] = None

def _extract_text_from_packet_or_summary(obj: Dict[str, Any]) -> str:
    pkt = obj.get("packet", {}) or {}
    dec = pkt.get("decoded", {}) or {}
    data = dec.get("data", {}) or {}

    txt = data.get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()

    txt = dec.get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()

    summ = obj.get("summary", {}) or {}
    txt = summ.get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()

    return ""

def _extract_from_id(pkt: Dict[str, Any]) -> str:
    dec = pkt.get("decoded", {}) or {}
    hdr = dec.get("header", {}) or {}
    cand = (
        hdr.get("fromId")
        or pkt.get("fromId")
        or hdr.get("from")
        or pkt.get("from")
        or ""
    )
    return cand or ""

# MODIFICADA: _broker_listen_loop — muestra canal en modo /escuchar all y mantiene métricas

#1-09-2025 11:58
async def _broker_listen_loop_OLD(chat_id: int, listen_chan: Optional[int], context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bucle de escucha para un chat concreto. Conecta al broker por TCP y reenvía TEXT_MESSAGE_APP.
    Se detiene cuando context.chat_data['listen_state']['active'] == False o al cancelarse la task.
    Reintenta reconectar de forma simple si se cae la conexión.

    Reutiliza SOLO utilidades existentes:
      - _extract_channel_index_from_packet(pkt)
      - _extract_text_from_packet_or_summary(obj)
      - _extract_from_id(pkt)
      - _build_alias_fallback_from_nodes_file()
      - extract_rssi(pkt), extract_snr(pkt), _fmt_db(valor, unidad)
      - extract_hop_limit(pkt), extract_hop_start(pkt), extract_relay_node(pkt)
    """
    import asyncio, json

    # Utilidad local para saber si sigue activa la escucha
    def is_active() -> bool:
        st = context.chat_data.get("listen_state") or {}
        return bool(st.get("active"))

    backoff = 1.5
    wait = 1.0

    while is_active():
        reader = writer = None
        try:
            # Conexión al broker TCP
            reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
            # Guardamos el writer para poder cerrarlo al parar
            context.chat_data["listen_writer"] = writer

            while is_active():
                # Lee una línea con timeout corto para poder comprobar el flag activo periódicamente
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    # EOF del broker → romper bucle interno para reconectar
                    break

                # Parseo robusto del JSON de broker
                try:
                    obj = json.loads(line.decode("utf-8", errors="ignore"))
                except Exception:
                    continue

                if obj.get("type") != "packet":
                    continue

                pkt = obj.get("packet", {}) or {}
                dec = pkt.get("decoded", {}) or {}
                if dec.get("portnum") != "TEXT_MESSAGE_APP":
                    continue

                # Filtro por canal lógico si procede
                try:
                    ch = _extract_channel_index_from_packet(pkt)
                except Exception:
                    ch = None
                if listen_chan is not None and isinstance(ch, int) and ch != listen_chan:
                    continue

                # Texto decodificado (usa la utilidad que ya tienes en el proyecto)
                try:
                    texto = _extract_text_from_packet_or_summary(obj)
                except Exception:
                    texto = None
                if not texto:
                    continue  # si no hay texto útil, no reenviamos

                # Origen (ID) y alias (prioriza el que venga del broker; si no, fichero de nodos)
                try:
                    origen = _extract_from_id(pkt) or "(desconocido)"
                except Exception:
                    origen = "(desconocido)"

                # 1) Si el broker ya adjunta alias, úsalo
                alias_broker = (obj.get("from_alias")
                                or pkt.get("from_alias")
                                or pkt.get("sender")
                                or "").strip() if isinstance(pkt, dict) else ""

                # 2) Caer al fichero de nodos si no vino alias en el evento
                if alias_broker:
                    alias = alias_broker
                else:
                    try:
                        alias_map = _build_alias_fallback_from_nodes_file() or {}
                        alias = alias_map.get(origen, "")
                    except Exception:
                        alias = ""

                origen_txt = f"{alias} ({origen})" if alias else origen

                # Canal visible en el encabezado
                if listen_chan is None:
                    canal_str = f"{ch}*" if ch is not None else "??*"
                else:
                    canal_str = str(ch) if ch is not None else "??"

                # Métricas de señal y hops (reutilizando tus funciones)
                try:
                    rssi = extract_rssi(pkt)
                except Exception:
                    rssi = None
                try:
                    snr = extract_snr(pkt)
                except Exception:
                    snr = None

                try:
                    rssi_txt = _fmt_db(rssi, "dBm") if rssi is not None else "¿?"
                except Exception:
                    rssi_txt = str(rssi) if rssi is not None else "¿?"
                try:
                    snr_txt = _fmt_db(snr, "dB") if snr is not None else "¿?"
                except Exception:
                    snr_txt = str(snr) if snr is not None else "¿?"

                try:
                    hop_limit = extract_hop_limit(pkt)
                except Exception:
                    hop_limit = None
                try:
                    hop_start = extract_hop_start(pkt)
                except Exception:
                    hop_start = None
                try:
                    relay = extract_relay_node(pkt)
                except Exception:
                    relay = None

                # Hops reales = hop_start - hop_limit (acotado a >= 0)
                if hop_limit is not None and hop_start is not None:
                    try:
                        hops_real = max(0, int(hop_start) - int(hop_limit))
                    except Exception:
                        hops_real = None
                else:
                    hops_real = None

                # Construcción de líneas de métricas (más detalladas)
                hops_real_txt = str(hops_real) if hops_real is not None else "—"
                hl_txt = str(hop_limit) if hop_limit is not None else "¿?"
                hs_txt = str(hop_start) if hop_start is not None else "¿?"
                rn_txt = str(relay) if relay is not None else "¿?"
               
                # NUEVO: calcular calidad de enlace a partir del SNR
                quality = _snr_quality_label(snr)

                # Envío al chat (mismo formato que escuchar_cmd + canal visible)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"📩 {origen_txt} (canal {canal_str}):\n"
                            f"{texto}\n"
                            f"   • RX: RSSI {rssi_txt} | SNR {snr_txt} ({quality})\n"
                            f"   • Hops reales: {hops_real_txt}\n"
                            f"   • hop_limit: {hl_txt} | hop_start: {hs_txt} | relay_node: {rn_txt}"
                        )
                    )
                except Exception as e:
                    log(f"❗ Error enviando mensaje del broker a chat {chat_id}: {e}")

        except asyncio.CancelledError:
            # La task fue cancelada explícitamente
            break
        except Exception as e:
            log(f"⚠️ _broker_listen_loop: {e}")
        finally:
            # Cerramos el writer si está abierto y limpiamos referencia
            try:
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
            except Exception:
                pass
            context.chat_data.pop("listen_writer", None)

        # Si sigue activa, reintenta conectar con backoff
        if is_active():
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break
            wait = min(wait * backoff, 10.0)

    # Fin del bucle

async def _broker_listen_loop(chat_id: int, listen_chan: Optional[int], context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bucle de escucha para un chat concreto. Conecta al broker por TCP y reenvía TEXT_MESSAGE_APP.
    Se detiene cuando context.chat_data['listen_state']['active'] == False o al cancelarse la task.
    Reintenta reconectar de forma simple si se cae la conexión.

    Reutiliza SOLO utilidades existentes:
      - _extract_channel_index_from_packet(pkt)
      - _extract_text_from_packet_or_summary(obj)
      - _extract_from_id(pkt)
      - _build_alias_fallback_from_nodes_file()
      - extract_rssi(pkt), extract_snr(pkt), _fmt_db(valor, unidad)
      - extract_hop_limit(pkt), extract_hop_start(pkt), extract_relay_node(pkt)
    """
    import asyncio, json, os, socket  # [MOD] añadimos os, socket para mejorar logs

    # [NUEVO] Verbose activable por env (opcional)
    TELEGRAM_BROKER_VERBOSE = bool(int(os.getenv("TELEGRAM_BROKER_VERBOSE", "0")))

    # [NUEVO] Mapa explicativo WinError → texto humano
    _WINERR_EXPLAIN = {
        64:    "El nombre de red especificado ya no está disponible (corte remoto / mid-session).",
        1225:  "El equipo remoto rechazó la conexión (servicio no aceptando, firewall o cooldown activo).",
        10053: "Conexión abortada localmente (timeout/cancelación en cliente).",
        10054: "Conexión restablecida por el host remoto (corte duro desde el otro extremo).",
    }

    # [NUEVO] Helpers locales de logging enriquecido
    def _explain_winerror(e: BaseException) -> str:
        try:
            code = getattr(e, "winerror", None) or getattr(e, "errno", None)
            if code in _WINERR_EXPLAIN:
                return f"[WinError {code}] {_WINERR_EXPLAIN[code]}"
            return f"{type(e).__name__}: {e}"
        except Exception:
            return f"{type(e).__name__}: {e}"

    async def _query_broker_status_async(host: str, port: int, timeout: float = 2.5):
        """
        Consulta BROKER_STATUS al BacklogServer para enriquecer el log cuando hay errores.
        Devuelve dict o None. No lanza hacia fuera.
        """
        try:
            req = {"cmd": "BROKER_STATUS"}
            line = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            try:
                w.write(line)
                await w.drain()
                resp = await asyncio.wait_for(r.readline(), timeout=timeout)
            finally:
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass
            obj = json.loads(resp.decode("utf-8", "ignore"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    # Utilidad local para saber si sigue activa la escucha
    def is_active() -> bool:
        st = context.chat_data.get("listen_state") or {}
        return bool(st.get("active"))

    backoff = 1.5
    wait = 1.0

    while is_active():
        reader = writer = None
        try:
            # Conexión al broker TCP
            if TELEGRAM_BROKER_VERBOSE:
                log(f"🔌 Intentando conectar a broker TCP {BROKER_HOST}:{BROKER_PORT} …")
            reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
            # Guardamos el writer para poder cerrarlo al parar
            context.chat_data["listen_writer"] = writer
            wait = 1.0  # [NUEVO] reset backoff tras conectar
            if TELEGRAM_BROKER_VERBOSE:
                log("✅ Conectado al broker TCP.")

            while is_active():
                # Lee una línea con timeout corto para poder comprobar el flag activo periódicamente
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    # EOF del broker → romper bucle interno para reconectar
                    if TELEGRAM_BROKER_VERBOSE:
                        log("ℹ️ Broker cerró la conexión (EOF). Reintentando…")
                    break

                # Parseo robusto del JSON de broker
                try:
                    obj = json.loads(line.decode("utf-8", errors="ignore"))
                except Exception:
                    continue

                if obj.get("type") != "packet":
                    continue

                pkt = obj.get("packet", {}) or {}
                dec = pkt.get("decoded", {}) or {}
                if dec.get("portnum") != "TEXT_MESSAGE_APP":
                    continue

                # Filtro por canal lógico si procede
                try:
                    ch = _extract_channel_index_from_packet(pkt)
                except Exception:
                    ch = None
                if listen_chan is not None and isinstance(ch, int) and ch != listen_chan:
                    continue

                # Texto decodificado (usa la utilidad que ya tienes en el proyecto)
                try:
                    texto = _extract_text_from_packet_or_summary(obj)
                except Exception:
                    texto = None
                if not texto:
                    continue  # si no hay texto útil, no reenviamos

                # Origen (ID) y alias (prioriza el que venga del broker; si no, fichero de nodos)
                try:
                    origen = _extract_from_id(pkt) or "(desconocido)"
                except Exception:
                    origen = "(desconocido)"

                # 1) Si el broker ya adjunta alias, úsalo
                alias_broker = (obj.get("from_alias")
                                or pkt.get("from_alias")
                                or pkt.get("sender")
                                or "").strip() if isinstance(pkt, dict) else ""

                # 2) Caer al fichero de nodos si no vino alias en el evento
                if alias_broker:
                    alias = alias_broker
                else:
                    try:
                        alias_map = _build_alias_fallback_from_nodes_file() or {}
                        alias = alias_map.get(origen, "")
                    except Exception:
                        alias = ""

                origen_txt = f"{alias} ({origen})" if alias else origen

                # Canal visible en el encabezado
                if listen_chan is None:
                    canal_str = f"{ch}*" if ch is not None else "??*"
                else:
                    canal_str = str(ch) if ch is not None else "??"

                # Métricas de señal y hops (reutilizando tus funciones)
                try:
                    rssi = extract_rssi(pkt)
                except Exception:
                    rssi = None
                try:
                    snr = extract_snr(pkt)
                except Exception:
                    snr = None

                try:
                    rssi_txt = _fmt_db(rssi, "dBm") if rssi is not None else "¿?"
                except Exception:
                    rssi_txt = str(rssi) if rssi is not None else "¿?"
                try:
                    snr_txt = _fmt_db(snr, "dB") if snr is not None else "¿?"
                except Exception:
                    snr_txt = str(snr) if snr is not None else "¿?"

                try:
                    hop_limit = extract_hop_limit(pkt)
                except Exception:
                    hop_limit = None
                try:
                    hop_start = extract_hop_start(pkt)
                except Exception:
                    hop_start = None
                try:
                    relay = extract_relay_node(pkt)
                except Exception:
                    relay = None

                # Hops reales = hop_start - hop_limit (acotado a >= 0)
                if hop_limit is not None and hop_start is not None:
                    try:
                        hops_real = max(0, int(hop_start) - int(hop_limit))
                    except Exception:
                        hops_real = None
                else:
                    hops_real = None

                # Construcción de líneas de métricas (más detalladas)
                hops_real_txt = str(hops_real) if hops_real is not None else "—"
                hl_txt = str(hop_limit) if hop_limit is not None else "¿?"
                hs_txt = str(hop_start) if hop_start is not None else "¿?"
                rn_txt = str(relay) if relay is not None else "¿?"

                 # NUEVO: calcular calidad de enlace a partir del SNR
                quality = _snr_quality_label(snr)

                # Envío al chat (mismo formato que escuchar_cmd + canal visible)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"📩 {origen_txt} (canal {canal_str}):\n"
                            f"{texto}\n"
                            f"   • RX: RSSI {rssi_txt} | SNR {snr_txt} ({quality})\n"
                            f"   • Hops reales: {hops_real_txt}\n"
                            f"   • hop_limit: {hl_txt} | hop_start: {hs_txt} | relay_node: {rn_txt}"
                        )
                    )
                except Exception as e:
                    log(f"❗ Error enviando mensaje del broker a chat {chat_id}: {e}")

        except asyncio.CancelledError:
            # La task fue cancelada explícitamente
            break
        except Exception as e:
            # [NUEVO] Diagnóstico enriquecido en errores de red/conexión
            human = _explain_winerror(e)
            log(f"⚠️ _broker_listen_loop: {human}")
            try:
                st = await _query_broker_status_async(BROKER_HOST, (BACKLOG_PORT if 'BACKLOG_PORT' in globals() else (BROKER_PORT + 1)))
                if isinstance(st, dict):
                    log(f"ℹ️ Estado broker → status={st.get('status')}, cooldown_remaining={st.get('cooldown_remaining')}s")
            except Exception:
                pass
        finally:
            # Cerramos el writer si está abierto y limpiamos referencia
            try:
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
            except Exception:
                pass
            context.chat_data.pop("listen_writer", None)

        # Si sigue activa, reintenta conectar con backoff
        if is_active():
            try:
                if TELEGRAM_BROKER_VERBOSE:
                    log(f"⏳ Reintentando conexión al broker en {wait:.1f}s …")
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break
            wait = min(wait * backoff, 10.0)

    # Fin del bucle


# === [NUEVO] Replay de mensajes perdidos desde el OFFLINE_LOG ===
import os, json
from datetime import datetime, timezone



def _safe_int(x, default=None):
    try:
        return int(x)
    except:
        return default

async def replay_offline_messages(update: Update, chat_id: int, listen_chan: int | None, since_epoch: int) -> int:
    """
    Lee el broker_offline_log.jsonl, filtra por canal (si procede) y por ts>=since_epoch,
    ordena por ts ascendente y reenvía al chat con métricas.
    Devuelve cuántos mensajes se reenvían.
    """
    if not os.path.exists(OFFLINE_LOG_PATH):
        return 0

    rows = []
    with open(OFFLINE_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue

            ts = _safe_int(evt.get("ts")) or _safe_int(evt.get("rx_time"))
            if ts is None or ts < since_epoch:
                continue

            # Filtrado de canal si el usuario no pidió 'all'
            ch = evt.get("channel")
            if listen_chan is not None:
                try:
                    if int(ch) != int(listen_chan):
                        continue
                except:
                    continue

            rows.append(evt)

    if not rows:
        return 0

    rows.sort(key=lambda e: _safe_int(e.get("ts")) or 0)

    # Formato de salida: texto + métricas si están
    count = 0
    for evt in rows:
        port = evt.get("portnum") or evt.get("decoded", {}).get("portnum") or "?"
        frm  = evt.get("from") or "?"
        frm_alias  = evt.get("from_alias") or "?"
        to_alias = frm  = evt.get("to_alias") or "?"
        to   = evt.get("to") or "?"
        ch   = evt.get("channel")
        rxr  = evt.get("rx_rssi", None)
        rsn  = evt.get("rx_snr", None)
        hlim = evt.get("hop_limit", None)
        hst  = evt.get("hop_start", None)
        rnod = evt.get("relay_node", None)

        # texto decodificado si vino como TEXT_MESSAGE_APP
        text = None
        # v1: a veces viene 'text' plano; v2: dentro de 'payload' decodificado
        text = evt.get("text") or evt.get("decoded", {}).get("text")
        if not text and isinstance(evt.get("payload"), dict):
            text = evt["payload"].get("text")

        # sello temporal legible
        ts_epoch = _safe_int(evt.get("ts")) or _safe_int(evt.get("rx_time")) or 0
        dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).astimezone()
        when = dt.strftime("%Y-%m-%d %H:%M:%S")

        # Línea “cabecera” con métricas
        head = (f"📩 [Canal {ch} | {port} | {frm_alias} {frm} → {to_alias} {to}\n "
                f" RX: RSSI {rxr if rxr is not None else '?'}\n "    
                f" RX: SNR {rsn if rsn is not None else '?'}\n "
                f" hop_limit {hlim if hlim is not None else '?'} | "
                f" hop_start {hst if hst is not None else '?'} | "
                f" relay {rnod if rnod is not None else '?'}\n"
                f"{when}]")
       

        body = (text if isinstance(text, str) and text.strip()
                else "(no-texto)")

        msg = f"{head}\n{body}"
        try:
            await update.effective_message.reply_text(msg)
            count += 1
        except Exception:
            # No bloqueamos el resto
            pass

    return count



async def parar_escucha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Detiene la escucha activa de este chat.
    - Cancela la task de escucha si existe.
    - Cierra el writer TCP si está abierto.
    - Limpia el flag context.chat_data["listen_state"].
    - Informa del canal que estaba en escucha (o 'todos los canales').
    """
    import asyncio
    global BROKER
    bump_stat(update.effective_user.id, update.effective_user.username or "", "parar_escucha")

    # === [NUEVO] Sellar hora de última parada por chat_id ===
    from datetime import datetime, timezone
    chat_id = update.effective_chat.id
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    context.bot_data[f"escucha_last_stop_{chat_id}"] = now_ts
    await update.effective_message.reply_text("🛑 Escucha detenida. Registraré y reproduciré lo perdido cuando vuelvas a /escuchar.")

    # Estado previo para informar
    prev_state = context.chat_data.get("listen_state") or {}
    prev_chan = prev_state.get("channel", None)
    canal_txt = "todos los canales" if prev_chan is None else f"canal {prev_chan}"
    was_active = bool(prev_state.get("active"))

    # === NUEVO: decrementar contador global si esta escucha estaba contabilizada
    try:
        if prev_state.get("active_was_counted"):
            context.bot_data["listen_active_count"] = max(0, (context.bot_data.get("listen_active_count") or 0) - 1)
    except Exception:
        # No romper el flujo si bot_data no existe aún
        pass

    # 1) Marcar como inactiva (reinicia el estado; se pierde el flag contado, a propósito)
    context.chat_data["listen_state"] = {"active": False, "channel": None, "since": None, "active_was_counted": False}

    # 2) Cancelar task si existe
    task = context.chat_data.pop("listen_task", None)
    if task:
        try:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except Exception as e:
            log(f"⚠️ cancelar listen_task: {e}")

    # 3) Cerrar writer si existe
    w = context.chat_data.pop("listen_writer", None)
    if w:
        try:
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
        except Exception as e:
            log(f"⚠️ cerrar listen_writer: {e}")

    # 4) Si usabas un objeto BROKER con tracking de chats, intenta quitarlo (opcional)
    try:
        if BROKER and hasattr(BROKER, "remove_chat"):
            BROKER.remove_chat(update.effective_chat.id)
    except Exception as e:
        log(f"⚠️ remove_chat: {e}")

    # 5) Mensaje al usuario
    if was_active:
        await update.effective_message.reply_text(
            f"⏹️ Se detuvo la escucha en {canal_txt}."
        )
    else:
        await update.effective_message.reply_text("⏹️ No había una escucha activa para este chat.")

async def escuchar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Suscribe este chat a los mensajes TEXT_MESSAGE_APP del broker.
    Uso: /escuchar [N|all]
      - N   → escuchar solo ese canal lógico
      - all → escuchar todos los canales

    Cambios:
    - Evita escuchas duplicadas por chat.
    - Lanza una task asyncio propia que conecta al broker y reenvía mensajes.
    - Guarda estado y task en context.chat_data para poder parar luego.
    """
    import asyncio, time

    global BROKER
    bump_stat(update.effective_user.id, update.effective_user.username or "", "escuchar")

    if not BROKER_HOST:
        await update.effective_message.reply_text(
            "No hay BROKER_HOST configurado. Define BROKER_HOST/BROKER_PORT."
        )
        return

    # —— Evitar escuchas duplicadas por chat
    chat_key = "listen_state"
    st = context.chat_data.get(chat_key) or {}
    if st.get("active"):
        prev_chan = st.get("channel", None)
        canal_msg_exist = "todos los canales" if prev_chan is None else f"canal {prev_chan}"
        await update.effective_message.reply_text(
            f"👂 Ya hay una escucha activa en {canal_msg_exist}. "
            f"Usa /parar_escucha para detenerla antes de volver a /escuchar."
        )
        return

    # —— Parseo de argumento de canal
    arg = context.args[0].strip() if context.args else None
    if arg and arg.lower() in ("all", "*"):
        listen_chan = None
        canal_msg = "todos los canales"
    else:
        try:
            listen_chan = int(arg) if arg is not None else BROKER_CHANNEL
        except Exception:
            listen_chan = BROKER_CHANNEL
        canal_msg = f"canal {listen_chan}"

    # —— Registrar estado y lanzar task
    context.chat_data[chat_key] = {
        "active": True,
        "channel": listen_chan,     # None = all
        "since": int(time.time()),
        "active_was_counted": False,   # === NUEVO: inicializamos el flag
    }

    # === NUEVO: contador global de escuchas activas (en cualquier chat)
    try:
        if not context.chat_data[chat_key]["active_was_counted"]:
            context.bot_data["listen_active_count"] = (context.bot_data.get("listen_active_count") or 0) + 1
            context.chat_data[chat_key]["active_was_counted"] = True
    except Exception:
        # No romper si bot_data aún no está inicializado
        pass

    # Cerrar cualquier writer previo por seguridad
    try:
        w = context.chat_data.get("listen_writer")
        if w:
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
    except Exception:
        pass

    # === NUEVO: replay backlog desde la última parada (si existe marca temporal)
    chat_id = update.effective_chat.id
    last_stop_ts = context.bot_data.get(f"escucha_last_stop_{chat_id}")
    if last_stop_ts:
        count = await replay_offline_messages(
            update=update,
            chat_id=chat_id,
            listen_chan=listen_chan,
            since_epoch=last_stop_ts,
        )
        if count > 0:
            await update.effective_message.reply_text(
                f"📜 Reproducidos {count} mensajes perdidos desde la última escucha."
            )

    # Lanzar la task del bucle de escucha
    task = asyncio.create_task(_broker_listen_loop(update.effective_chat.id, listen_chan, context))
    context.chat_data["listen_task"] = task

    await update.effective_message.reply_text(
        f"👂 Escuchando {canal_msg}. Enviaré aquí los TEXT_MESSAGE_APP que vayan llegando.\n"
        f"Para detener: /parar_escucha"
    )

# === NUEVO: /refrescar_nodos ================================================
# === /refrescar_nodos (usando SOLO helpers existentes) =======================
async def refrescar_nodos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /refrescar_nodos [auto|api|cli] [max_n=50] [timeout=12]
    - auto: usa ensure_nodes_file_fresh(0, max_n, True) y, si queda pobre, completa por API
    - api:  fuerza API (load_nodes_with_hops)
    - cli:  fuerza CLI (sync_nodes_and_save) — ya pausa internamente con with_broker_paused

    Escribe/actualiza bot_data/nodos.txt y responde con el nº de entradas detectadas.
    """
    user = update.effective_user
    bump_stat(user.id, user.username or "", "refrescar_nodos")

    args = context.args or []
    mode = (args[0].strip().lower() if args else "auto")
    try:
        max_n = int(args[1]) if len(args) >= 2 and str(args[1]).lstrip("-").isdigit() else 50
    except Exception:
        max_n = 50
    # timeout se acepta por compatibilidad pero NO se utiliza porque los helpers no lo soportan
    try:
        timeout = int(args[2]) if len(args) >= 3 and str(args[2]).lstrip("-").isdigit() else 12
    except Exception:
        timeout = 12

    ensure_nodes_path_exists()

    await update.effective_message.reply_text(
        f"🔄 Refrescando nodos ({mode})…\n"
        f"• max={max_n}  • timeout={timeout}s"
    )

    def _count_file_rows() -> int:
        try:
            rows = _parse_nodes_table(NODES_FILE)
            return len(rows or [])
        except Exception:
            return 0

    mode = mode if mode in {"auto", "api", "cli"} else "auto"
    updated_via = []
    total = 0

    if mode == "api":
        try:
            nodes = load_nodes_with_hops(n_max=max_n)
            total = len(nodes or [])
            updated_via.append("API")
        except Exception as e:
            await update.effective_message.reply_text(f"⚠️ API falló: {type(e).__name__}: {e}")

    elif mode == "cli":
        try:
            # Este helper YA pausa el broker internamente con with_broker_paused
            sync_nodes_and_save(max_n)
            total = _count_file_rows()
            updated_via.append("CLI")
        except Exception as e:
            await update.effective_message.reply_text(f"⚠️ CLI falló: {type(e).__name__}: {e}")

    else:  # auto
        try:
            # Fuerza “frescura” del fichero con el máximo pedido
            ensure_nodes_file_fresh(max_age_s=0, max_rows=max_n, force_if_empty=True)
            updated_via.append("CLI")  # La ruta AUTO usa el refresco de fichero por CLI
        except Exception:
            pass

        total = _count_file_rows()
        if total < max(5, max_n // 3):
            try:
                nodes = load_nodes_with_hops(n_max=max_n)
                total = max(total, len(nodes or []))
                updated_via.append("API")
            except Exception:
                pass

    final_total = max(total, _count_file_rows())
    via_txt = " + ".join(updated_via) if updated_via else "—"

    await update.effective_message.reply_text(
        f"✅ Refresco completado.\n"
        f"• Vía: {via_txt}\n"
        f"• Entradas en nodos.txt: {final_total}"
    )





# =========================
# Helpers para /escuchar (JSONL broker)
# =========================
import asyncio, json, time
from typing import Optional, Tuple, Any

# --- Constantes por defecto (solo si no las tienes ya definidas) ---
try:
    BROKER_HOST
except NameError:
    BROKER_HOST = "127.0.0.1"   # ajusta si tu broker JSONL escucha en otra IP

try:
    BROKER_PORT
except NameError:
    BROKER_PORT = 8765          # puerto del broker JSONL (no el 4403 de la radio)

try:
    BROKER_CHANNEL
except NameError:
    BROKER_CHANNEL = 0          # canal lógico por defecto si el usuario no indica


import os as _os_for_pause_mode

# Modo de pausa del broker para operaciones CLI:
#  - "auto"   → Windows: pausa / Linux: no pausa
#  - "always" → siempre pausa
#  - "never"  → nunca pausa
_raw_pause_mode = ""
try:
    _raw_pause_mode = (_os_for_pause_mode.environ.get("BOT_PAUSE_MODE", "") or "").strip().lower()
except Exception:
    _raw_pause_mode = ""

if _raw_pause_mode not in ("auto", "always", "never", ""):
    _raw_pause_mode = "auto"

BOT_PAUSE_MODE = _raw_pause_mode or "auto"


def _get_pause_mode_effective() -> str:
    """
    Devuelve el modo efectivo:
      - auto   → Windows: always, Linux/otros: never
      - always → siempre pausa broker
      - never  → nunca pausa broker
    """
    m = (BOT_PAUSE_MODE or "auto").strip().lower()
    if m not in ("auto", "always", "never"):
        m = "auto"
    if m == "auto":
        # En Windows mantenemos la pausa; en Linux/RPi no.
        return "always" if _os_for_pause_mode.name == "nt" else "never"
    return m



# --- Detección robusta de si un evento es de texto ---
def _evt_is_text(evt: dict) -> bool:
    """
    Considera texto si:
      - portnum_name == 'TEXT_MESSAGE_APP'
      - app == 'TEXT_MESSAGE_APP'
      - portnum == 1 (valor común para texto en Meshtastic)
      - O existe 'text' no vacío
    """
    pnum = evt.get("portnum")
    app  = (evt.get("portnum_name") or evt.get("app") or "").upper()
    txt  = evt.get("text") or evt.get("payload") or ""

    if isinstance(pnum, int) and pnum == 1:
        return True
    if isinstance(app, str) and "TEXT_MESSAGE_APP" in app:
        return True
    if isinstance(txt, str) and txt.strip():
        return True
    return False

def _evt_extract_channels(evt: dict) -> Tuple[Optional[int], Optional[int]]:
    """
    Extrae canal lógico y rfch si existen en el JSONL emitido por el broker.
    Admite varias claves para ser compatible con distintas versiones.
    """
    ch  = evt.get("canal") or evt.get("channel") or evt.get("logical_channel")
    rf  = evt.get("rfch")  or evt.get("rf_channel") or evt.get("rfslot")
    try:
        ch = int(ch) if ch is not None else None
    except Exception:
        ch = None
    try:
        rf = int(rf) if rf is not None else None
    except Exception:
        rf = None
    return ch, rf

async def _broker_listen_loop_jsonl(chat_id: int, listen_chan: Optional[int], context) -> None:
    """
    Abre conexión TCP al broker JSONL, lee 1 JSON por línea y reenvía a Telegram.
    Guarda el writer en context.chat_data['listen_writer'] para que /parar_escucha lo cierre.
    """
    reader = writer = None
    # Pequeño backoff para reconexión si el broker cae
    backoff = [1, 2, 4, 6, 10, 15, 20, 30]
    try:
        i = 0
        while True:
            try:
                reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
                # Guarda writer para que parar_escucha_cmd lo pueda cerrar limpiamente
                context.chat_data["listen_writer"] = writer
                # === NUEVO: mapa de alias (fallback desde nodes.txt)
                try:
                    alias_map = _build_alias_fallback_from_nodes_file() or {}
                except Exception:
                    alias_map = {}

                # Resetea backoff al reconectar
                i = 0

                # Bucle de lectura
                while True:
                    line = await reader.readline()
                    if not line:
                        await asyncio.sleep(0.1)
                        continue
                    try:
                        evt = json.loads(line.decode("utf-8", errors="ignore").strip() or "{}")
                       
                    except Exception:
                        continue

                    # filtrar solo mensajes de texto
                    if not _evt_is_text(evt):
                        continue
                    
                     # ⬇️ NUEVO: filtrar por IDs bloqueados (silencioso)
                    if _filtrar_evento_si_bloqueado(evt):
                        continue

                    ch, rf = _evt_extract_channels(evt)
                    if listen_chan is not None and ch is not None and ch != listen_chan:
                        continue

                    app  = (evt.get("portnum_name") or evt.get("app") or "?")
                    src  = (evt.get("from") or evt.get("src") or evt.get("id") or "?")
                    txt  = (evt.get("text") or evt.get("payload") or "").strip()

                    ts   = time.strftime("%H:%M:%S")
                    header = f"📡 [{ts}] ch{ch if ch is not None else '?'}"
                    if rf is not None:
                        header += f"/rf{rf}"
                    header += f" | {app} | {src}\n"
                    body = f"📝 {txt}" if txt else ""

                    msg = header + body
                    # troceo para evitar límite de Telegram
                    for chunk in (msg[i:i+3800] for i in range(0, len(msg), 3800)):
                        if chunk:
                            await context.bot.send_message(chat_id=chat_id, text=chunk)

            except asyncio.CancelledError:
                # Cancelación normal desde /parar_escucha
                raise
            except Exception as e:
                # Informa del error y reintenta con backoff progresivo
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Escucha: {type(e).__name__}: {e}")
                except Exception:
                    pass

                # Cerrar antes de reintentar
                try:
                    if writer:
                        writer.close()
                        await writer.wait_closed()
                except Exception:
                    pass
                writer = None
                context.chat_data.pop("listen_writer", None)

                # Esperar según backoff y reintentar
                delay = backoff[min(i, len(backoff)-1)]
                i += 1
                await asyncio.sleep(delay)

    finally:
        # Limpieza final si salimos del loop
        try:
            if writer:
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass
        context.chat_data.pop("listen_writer", None)

#29-08-2028 09:10 Nueva funcion
async def on_forcereply_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()

    # --- Traceroute (Forcereply) ---
    if context.user_data.pop("await_traceroute", False):
        node_id = _resolve_node_id(text, context)
        if node_id == text and text.isdigit() and not context.user_data.get("nodes_map"):
            await update.effective_message.reply_text("Primero ejecuta /ver_nodos para usar el número de orden.")
            return
        if not node_id.startswith("!"):
            await update.effective_message.reply_text("No se pudo resolver el destino a un !id.")
            return
        res = traceroute_node(node_id)
        if res.ok:
            ruta = " --> ".join(res.route) if res.route else "(ruta no desglosada)"
            out = f"🧭 Traceroute a {node_id}\nSaltos: {res.hops}\nRuta: {ruta}"
        else:
            out = f"No se encontró ruta hacia {node_id}.\n\nSalida:\n{res.raw}"
        for chunk in chunk_text(out):
            await send_pre(update.effective_message, chunk)
        return

    # --- Telemetría (Forcereply) -> SIEMPRE API + escucha corta TELEMETRY_APP ---
    if context.user_data.pop("await_telemetry", False):
        node_id = _resolve_node_id(text, context)
        if node_id == text and text.isdigit() and not context.user_data.get("nodes_map"):
            await update.effective_message.reply_text("Primero ejecuta /ver_nodos para usar el número de orden.")
            return
        if not node_id.startswith("!"):
            await update.effective_message.reply_text("No se pudo resolver el destino a un !id.")
            return

        # Intento por API (sin CLI) para evitar cierres bruscos de socket y stacktraces
        try:
            #res = api_request_telemetry(MESHTASTIC_HOST, node_id, timeout=TELEMETRY_TIMEOUT)
            res = api_request_telemetry(MESHTASTIC_HOST, node_id, timeout=TELEMETRY_TIMEOUT, allow_cli_fallback=False)

            raw = res.get("raw", "(sin salida)")
        except Exception as e:
            raw = f"(error solicitando telemetría por API: {e})"

        # Ventana corta de escucha de TELEMETRY_APP para confirmar recepción
        # Usamos canal por defecto del bot; si se quiere más fino, se podría permitir pasar canal en el prompt
        canal = BROKER_CHANNEL
        total_tel, by_type = await quick_broker_listen_telemetry(dest_id=node_id,
                                                                 channel=canal,
                                                                 seconds=TELEMETRY_LISTEN_SEC)

        resumen_tel = f"\nRespuestas TELEMETRY_APP en {TELEMETRY_LISTEN_SEC}s: {total_tel}"
        if by_type:
            detalle = ", ".join([f"{k}={v}" for k, v in by_type.items()])
            resumen_tel += f" ({detalle})"

        txt = f"🛰️ Telemetría solicitada a {node_id}\n{raw}{resumen_tel}"
        for chunk in chunk_text(txt):
            await send_pre(update.effective_message, chunk)
        return

    # --- Flujo /enviar (Forcereply) ---
    if context.user_data.get("await_send_dest"):
        context.user_data["send_dest_menu"] = text
        context.user_data.pop("await_send_dest", None)
        await update.effective_message.reply_text(
            "Ahora, escribe el texto a enviar (puedes añadir canal en el destino como !id:2 / alias:5):",
            reply_markup=ForceReply()
        )
        context.user_data["await_send_text"] = True
        return

    if context.user_data.pop("await_send_text", False):
        dest = context.user_data.pop("send_dest_menu", "broadcast")
        nodes_map = context.user_data.get("nodes_map") or build_nodes_mapping()
        node_id, canal, texto_final, forced_flag = parse_dest_channel_and_text([dest, text], nodes_map)

        traceroute_ok = None
        hops = 0
        if TRACEROUTE_CHECK and node_id:
            res = traceroute_node(node_id, timeout=min(TRACEROUTE_TIMEOUT, 20))
            traceroute_ok = bool(res.ok)
            hops = res.hops
            if not traceroute_ok:
                forced_flag = True

        out, pid = send_text_message(node_id, texto_final or text, canal=canal)
        respuestas = await quick_broker_listen(node_id, canal, SEND_LISTEN_SEC)

        dest_txt = "broadcast" if node_id is None else node_id
        ans = (
            f"✉️ Envío a {dest_txt} (canal {canal})\n"
            f"Resultado: {out}\n"
            f"Forzado: {'Sí' if forced_flag else 'No'}\n"
            f"Respuestas en {SEND_LISTEN_SEC}s: {respuestas}"
        )
        if traceroute_ok is not None:
            ans += f"\nTraceroute previo: {'OK' if traceroute_ok else 'Sin ruta'} (hops={hops})"

        for ch in chunk_text(ans):
            await send_pre(update.effective_message, ch)

        _append_send_log_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            dest_txt, canal,
            (texto_final[:200] + "…") if texto_final and len(texto_final) > 200 else (texto_final or text),
            "1" if forced_flag else "0",
            "" if traceroute_ok is None else ("1" if traceroute_ok else "0"),
            hops,
            respuestas,
        ])
        return

    # --- Flujo /enviar_ack (Forcereply) ---
    if context.user_data.pop("await_enviar_ack", False):
        tokens = text.split()
        attempts, wait_s, backoff, rest = _extract_ack_params(tokens)
        nodes_map = context.user_data.get("nodes_map") or build_nodes_mapping()
        node_id, canal, texto_final, _ = parse_dest_channel_and_text(rest, nodes_map)
        if not texto_final:
            await update.effective_message.reply_text("Falta el texto del mensaje.")
            return
        result = await send_with_ack_retry(node_id, texto_final, canal, attempts, wait_s, backoff)

        dest_txt = "broadcast" if node_id is None else node_id
        if result.get("ok"):
            resumen = (
                f"✅ ACK recibido para {dest_txt} (canal {canal})\n"
                f"Intentos: {result['attempts']}  •  packet_id: {result.get('packet_id')}"
            )
        else:
            resumen = (
                f"⚠️ Sin ACK para {dest_txt} (canal {canal})\n"
                f"Intentos: {result['attempts']}  •  Motivo: {result.get('reason','')}\n"
                f"packet_id: {result.get('packet_id')}"
            )
        for ch in chunk_text(resumen):
            await send_pre(update.effective_message, ch)

        _append_send_ack_log_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            dest_txt, canal,
            (texto_final[:200] + "…") if len(texto_final) > 200 else texto_final,
            result.get("attempts"), "1" if result.get("ok") else "0",
            result.get("reason",""), result.get("packet_id",""),
        ])
        return

# -------------------------
# ESTADO / ESTADÍSTICA
# -------------------------
# === REHECHA: /estado — sin duplicar helpers, usando _broker_ctrl ya existente ===
async def estado_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Muestra estado del nodo Meshtastic, del broker TCP y el estado interno del broker
    (vía BacklogServer: BROKER_STATUS). No introduce helpers nuevos.
    Reutiliza: run_command(), _broker_ctrl().
    """
    msg = update.effective_message

    import os, socket, time

    # --- Config de entorno / existentes ---
    mesh_host = os.getenv("MESHTASTIC_HOST", globals().get("MESHTASTIC_HOST", "")).strip() or "127.0.0.1"
    try:
        broker_host = os.getenv("BROKER_HOST", "127.0.0.1").strip()
    except Exception:
        broker_host = "127.0.0.1"
    try:
        broker_port = int(os.getenv("BROKER_PORT", "8765"))
    except Exception:
        broker_port = 8765

    # --- 1) Meshtastic host: usamos el CLI existente (run_command) como ya hacías ---
    host_line = f"- Meshtastic host {mesh_host}: "
    try:
        ok_cli, out = run_command(
            ["--host", mesh_host, "--info"],
            timeout=20
        )
        host_line += "OK" if ok_cli else "KO"
    except Exception:
        host_line += "KO"

    # --- 2) Broker TCP: prueba TCP directa (inline, sin helper nuevo) ---
    broker_line = f"- Broker {broker_host}:{broker_port}: "
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((broker_host, broker_port))
        broker_line += "OK"
    except Exception:
        broker_line += "KO"
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass

    # --- 3) Estado interno vía BacklogServer (_broker_ctrl ya existe) ---
    #     Esperamos que exponga BROKER_STATUS -> {ok, status, cooldown_remaining}
    interno_line = "- Estado interno: "
    try:
        st = _broker_ctrl("BROKER_STATUS", {}, 2.5)
        if isinstance(st, dict) and st.get("ok"):
            status = str(st.get("status") or "unknown")
            cdrem  = st.get("cooldown_remaining")
            if cdrem is not None:
                interno_line += f"{status} (cooldown: {int(cdrem)}s)"
            else:
                interno_line += f"{status}"
        else:
            # Mantener la misma frase de tu salida anterior si no está disponible
            interno_line += "(no disponible por control UDP)"
    except Exception:
        interno_line += "(no disponible por control UDP)"

    text = "Estado:\n" + "\n".join([host_line, broker_line, "", interno_line])
    try:
        await msg.reply_text(text)
    except Exception:
        pass
    return ConversationHandler.END



async def estadistica_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_message.reply_text("Solo disponible para admins.")
        return
    bump_stat(user.id, user.username or "", "estadistica")
    stats = load_stats()
    users = stats.get("users", {})
    counts = stats.get("counts", {})
    parts = ["Estadísticas de uso"]
    if users:
        parts.append("\nUsuarios:")
        for uid, info in users.items():
            uname = info.get("username") or "(sin username)"
            last = info.get("last_used")
            parts.append(f"- {uname} (id {uid}) • última vez: {last}")
    if counts:
        parts.append("\nComandos:")
        for cmd, num in counts.items():
            parts.append(f"- /{cmd}: {num}")
    await update.effective_message.reply_text("\n".join(parts))

# === [NUEVO] Helper genérico para enviar comandos al broker por el puerto de control UDP ===
import os, socket, json

# === [NUEVO] Comando /broker_resume ===

import asyncio

async def broker_resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telemetría opcional sin romper si no existe
    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "broker_resume")
    except Exception:
        pass

    await update.effective_chat.send_action("typing")
    # Enviamos BROKER_RESUME
    resp = await asyncio.to_thread(_send_broker_ctrl, "BROKER_RESUME", None, 2.0)

    # Consultamos estado profundo para devolver algo útil
    deep = await asyncio.to_thread(_query_broker_status_ctrl)

    lines = ["🔄 BROKER_RESUME enviado."]
    if resp and isinstance(resp, dict):
        ok = resp.get("ok")
        msg = resp.get("msg") or ""
        lines.append(f"→ Respuesta broker: {'✅ OK' if ok else '❌ FAIL'} {msg}".rstrip())
    else:
        lines.append("→ Respuesta broker: (sin respuesta)")

    if deep:
        connected = "✅" if deep.get("connected") else "❌"
        paused = "⏸️" if deep.get("mgr_paused") else "▶️"
        txblk = "🛑" if deep.get("tx_blocked") else "🟢"
        cd = deep.get("cooldown_remaining")
        cd_str = (f"{cd}s" if isinstance(cd, (int, float)) and cd is not None else "0s")
        lines += [
            "",
            "Estado actual:",
            f"- Conexión al nodo: {connected}",
            f"- Manager: {paused}  (mgr_paused={deep.get('mgr_paused')})",
            f"- TX guard: {txblk}  (tx_blocked={deep.get('tx_blocked')})",
            f"- Cooldown restante: {cd_str}",
        ]
    else:
        lines.append("\nEstado actual: (no disponible por control UDP)")

    await update.effective_message.reply_text("\n".join(lines))

# === [NUEVO] Comando /force_reconnect ===
async def force_reconnect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "force_reconnect")
    except Exception:
        pass

    await update.effective_chat.send_action("typing")

    # Parámetro opcional: /force_reconnect [grace_s]
    # Si el usuario pasa un número, lo mandamos como 'grace_window_s'
    grace_s = None
    if context.args:
        try:
            grace_s = int(context.args[0])
        except Exception:
            grace_s = None

    extra = {"grace_window_s": grace_s} if grace_s is not None else None
    resp = await asyncio.to_thread(_send_broker_ctrl, "FORCE_RECONNECT", extra, 2.0)

    # Consultamos estado profundo después
    deep = await asyncio.to_thread(_query_broker_status_ctrl)

    lines = ["♻️ FORCE_RECONNECT enviado."]
    if resp and isinstance(resp, dict):
        ok = resp.get("ok")
        msg = resp.get("msg") or ""
        lines.append(f"→ Respuesta broker: {'✅ OK' if ok else '❌ FAIL'} {msg}".rstrip())
    else:
        lines.append("→ Respuesta broker: (sin respuesta)")

    if deep:
        connected = "✅" if deep.get("connected") else "❌"
        paused = "⏸️" if deep.get("mgr_paused") else "▶️"
        txblk = "🛑" if deep.get("tx_blocked") else "🟢"
        cd = deep.get("cooldown_remaining")
        cd_str = (f"{cd}s" if isinstance(cd, (int, float)) and cd is not None else "0s")
        lines += [
            "",
            "Estado actual:",
            f"- Conexión al nodo: {connected}",
            f"- Manager: {paused}  (mgr_paused={deep.get('mgr_paused')})",
            f"- TX guard: {txblk}  (tx_blocked={deep.get('tx_blocked')})",
            f"- Cooldown restante: {cd_str}",
        ]
    else:
        lines.append("\nEstado actual: (no disponible por control UDP)")

    await update.effective_message.reply_text("\n".join(lines))

# === [NUEVO] Comando /broker_status ===

async def broker_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telemetría opcional; no falla si no existe
    try:
        bump_stat(update.effective_user.id, update.effective_user.username or "", "broker_status")
    except Exception:
        pass

    await update.effective_chat.send_action("typing")

    # Parámetro opcional: /broker_status raw|json → muestra la respuesta JSON cruda además del resumen
    want_raw = False
    if context.args:
        arg0 = (context.args[0] or "").strip().lower()
        if arg0 in ("raw", "json"):
            want_raw = True

    deep = await asyncio.to_thread(_query_broker_status_ctrl)

    if not deep:
        await update.effective_message.reply_text(
            "ℹ️ Estado interno: (no disponible por control UDP)\n"
            "— Verifica BROKER_CTRL_HOST/PORT en el bot y que el broker esté respondiendo en 8766."
        )
        return

    connected = "✅" if deep.get("connected") else "❌"
    paused = "⏸️" if deep.get("mgr_paused") else "▶️"
    txblk = "🛑" if deep.get("tx_blocked") else "🟢"
    cd = deep.get("cooldown_remaining")
    cd_str = (f"{cd}s" if isinstance(cd, (int, float)) and cd is not None else "0s")

    ver = deep.get("version")
    node_host = deep.get("node_host")
    node_port = deep.get("node_port")
    node_hint = f"{node_host}:{node_port}" if node_host and node_port else ""
    since = deep.get("since")  # ISO o texto, depende de tu broker

    lines = []
    lines.append("📡 Broker status:")
    lines.append(f"- Conexión al nodo: {connected}")
    lines.append(f"- Manager: {paused}  (mgr_paused={deep.get('mgr_paused')})")
    lines.append(f"- TX guard: {txblk}  (tx_blocked={deep.get('tx_blocked')})")
    lines.append(f"- Cooldown restante: {cd_str}")
    if node_hint:
        lines.append(f"- Nodo objetivo: {node_hint}")
    if ver:
        lines.append(f"- Broker versión: {ver}")
    if since:
        lines.append(f"- Desde: {since}")

    if want_raw:
        # Adjunta JSON crudo formateado para diagnóstico
        try:
            raw = json.dumps(deep, ensure_ascii=False, indent=2)
        except Exception:
            raw = str(deep)
        lines.append("\n```json")
        lines.append(raw)
        lines.append("```")

    await update.effective_message.reply_text("\n".join(lines), disable_web_page_preview=True)



# ====== Retro-compatibilidad con nombres antiguos ======

def _load_nodes_file_lines() -> list[str]:
    """
    Retro-compatibilidad.
    Antes devolvía las líneas de nodos.txt en bruto.
    Ahora reutiliza load_nodes_file_safe() y devuelve alias simples.
    """
    rows = load_nodes_file_safe()
    out = []
    for r in rows:
        try:
            # construye línea estilo "id;alias;mins;hops"
            nid = r.get("id") or r.get("node_id") or "?"
            alias = r.get("alias") or "?"
            mins = r.get("mins")
            hops = r.get("hops")
            out.append(f"{nid};{alias};{mins};{hops}")
        except Exception:
            continue
    return out


def enrich_hops_from_nodes_file(node_map: dict) -> None:
    """
    Retro-compatibilidad.
    Antes enriquecía hops desde nodos.txt.
    Ahora llama a load_nodes_with_hops() y actualiza node_map en sitio.
    """
    try:
        fresh = load_nodes_with_hops(limit=200)
        hops_map = {nid: hops for (nid, alias, mins, hops) in fresh if hops is not None}
        for nid, info in node_map.items():
            if "hops" not in info or info["hops"] is None:
                if nid in hops_map:
                    info["hops"] = hops_map[nid]
    except Exception as e:
        log(f"⚠️ enrich_hops_from_nodes_file (retro) falló: {e}")

# === NUEVO BLOQUE: Gestión de bloqueos de IDs ===
try:
    DATA_DIR
except NameError:
    # Fallback por si DATA_DIR no existiera aún en este fichero
    import os
    DATA_DIR = os.path.join(os.path.dirname(__file__), "bot_data")

BLOQUEADOS_FILE = os.path.join(DATA_DIR, "bloqueados.ids")

def _load_bloqueados() -> set[str]:
    """Carga los IDs bloqueados desde el archivo (uno por línea)."""
    bloqueados = set()
    try:
        with open(BLOQUEADOS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                idv = line.strip()
                if idv:
                    bloqueados.add(idv)
    except FileNotFoundError:
        pass
    return bloqueados

def _save_bloqueados(ids: set[str]):
    """Guarda los IDs bloqueados (uno por línea)."""
    os.makedirs(os.path.dirname(BLOQUEADOS_FILE), exist_ok=True)
    tmp = BLOQUEADOS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for idv in sorted(ids):
            f.write(idv + "\n")
    os.replace(tmp, BLOQUEADOS_FILE)

def _norm_bang_id(tok: str) -> str:
    """
    Normaliza tokens de id:
      - Si empieza por '!' se respeta.
      - Si es hex de 8 chars, se convierte a '!hex'.
      - En otro caso, se devuelve tal cual (para no excluir casos especiales).
    """
    t = (tok or "").strip()
    if not t:
        return t
    if t.startswith("!"):
        return t
    # ¿exactamente 8 hex? -> añade '!'
    import re
    if re.fullmatch(r"[0-9a-fA-F]{8}", t):
        return f"!{t.lower()}"
    return t

async def bloquear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /bloquear <id1,id2,...>     → añade IDs
    /bloquear lista             → lista IDs actuales
    (solo admin)
    """
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Solo administradores pueden usar este comando.")
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Uso:\n"
            "• /bloquear <id1,id2,...>\n"
            "• /bloquear lista"
        )
        return

    # subcomando 'lista'
    if len(args) == 1 and args[0].strip().lower() == "lista":
        bloqueados = sorted(_load_bloqueados())
        if not bloqueados:
            await update.effective_message.reply_text("🧾 Lista de bloqueados vacía.")
            return
        text = "🧾 <b>Bloqueados actuales</b>:\n" + "\n".join(f"• {x}" for x in bloqueados)
        await update.effective_message.reply_text(text, parse_mode="HTML")
        return

    # alta de ids
    raw_ids = " ".join(args)
    parts = [p.strip() for p in raw_ids.replace(";", ",").split(",") if p.strip()]
    if not parts:
        await update.effective_message.reply_text("❌ No se proporcionaron IDs válidos.")
        return

    nuevos = {_norm_bang_id(p) for p in parts}
    bloqueados = _load_bloqueados()
    antes = len(bloqueados)
    bloqueados |= nuevos
    _save_bloqueados(bloqueados)
    añadidos = sorted(bloqueados)[max(0, len(bloqueados)-len(nuevos)):]  # informativo

    await update.effective_message.reply_text(
        "🚫 Bloqueados añadidos:\n"
        f"{', '.join(sorted(nuevos))}\n\n"
        f"Total bloqueados: {len(bloqueados)} (antes {antes})"
    )

async def desbloquear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /desbloquear <id1,id2,...>  (solo admin)
    """
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Solo administradores pueden usar este comando.")
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Uso: /desbloquear <id1,id2,...>")
        return

    raw_ids = " ".join(args)
    parts = [p.strip() for p in raw_ids.replace(";", ",").split(",") if p.strip()]
    if not parts:
        await update.effective_message.reply_text("❌ No se proporcionaron IDs válidos.")
        return

    objetivo = {_norm_bang_id(p) for p in parts}
    bloqueados = _load_bloqueados()
    antes = len(bloqueados)
    eliminados = sorted(list(bloqueados & objetivo))
    bloqueados -= objetivo
    _save_bloqueados(bloqueados)

    await update.effective_message.reply_text(
        f"✅ IDs desbloqueados: {', '.join(eliminados) or 'ninguno'}\n"
        f"Total bloqueados: {len(bloqueados)} (antes {antes})"
    )

def is_id_bloqueado(node_id: str) -> bool:
    """Comprueba si un ID está bloqueado."""
    if not node_id:
        return False
    return _norm_bang_id(node_id) in _load_bloqueados()

def _filtrar_evento_si_bloqueado(evt: dict) -> bool:
    """
    Devuelve True si el evento debe ser filtrado (bloqueado).
    Se usa en la recepción de mensajes desde el broker/backlog.
    """
    from_id = str(evt.get("from") or evt.get("fromId") or "").strip()
    if not from_id:
        return False
    if is_id_bloqueado(from_id):
        print(f"[bloqueado] Ignorado mensaje de {from_id}", flush=True)
        return True
    return False


# -------------------------
# ERRORES / ARRANQUE
# -------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        log(f"❌ Excepción no capturada: {context.error}")
    except Exception:
        pass

def build_application() -> Application:
    if not TOKEN:
        print("❗ Falta TELEGRAM_TOKEN en variables de entorno.", file=sys.stderr)
        sys.exit(2)

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_error_handler(on_error)

    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("canales", canales_cmd))
   
    # Handlers de comandos…
   
# (El resto ya lo tienes: ver_nodos, traceroute, telemetria, enviar, enviar_ack, escuchar, parar_escucha, vecinos, estado, ayuda…)

    app.add_handler(CommandHandler("enviar", enviar_cmd))
    app.add_handler(CommandHandler("enviar_ack", enviar_ack_cmd))
    app.add_handler(CommandHandler("escuchar", escuchar_cmd))
    app.add_handler(CommandHandler("parar_escucha", parar_escucha_cmd))
    app.add_handler(CommandHandler("estado", estado_cmd))
    app.add_handler(CommandHandler("estadistica", estadistica_cmd))

    app.add_handler(CommandHandler("programar", programar_cmd))
    app.add_handler(CommandHandler("diario", diario_cmd))
    app.add_handler(CommandHandler("mis_diarios", mis_diarios_cmd))
    app.add_handler(CommandHandler("parar_diario", parar_diario_cmd))
    app.add_handler(CommandHandler("parar_diario_grupo", parar_diario_grupo_cmd))


    # Handlers de los dos comandos
    app.add_handler(CommandHandler("en", en_cmd))
    app.add_handler(CommandHandler("manana", manana_cmd))  # usa tu función manana_cmd o mañana_cmd según la que pegaste

    app.add_handler(CommandHandler("tareas", tareas_cmd))
    app.add_handler(CommandHandler("traceroute", traceroute_cmd))
    app.add_handler(CommandHandler("rt", traceroute_cmd))                 # alias directo
    app.add_handler(CommandHandler("traceroute_status", traceroute_status_cmd))

    app.add_handler(CommandHandler("telemetria", telemetria_cmd))
    app.add_handler(CommandHandler("lora", lora_cmd))
    app.add_handler(CommandHandler("cancelar_tarea", cancelar_tarea_cmd))

    app.add_handler(CommandHandler("position", position_cmd))
    app.add_handler(CommandHandler("position_mapa", position_mapa_cmd))
    app.add_handler(CommandHandler("cobertura", cobertura_cmd))  # NUEVO
    app.add_handler(CommandHandler("aprs", aprs_cmd))
    app.add_handler(CommandHandler("aprs_on", aprs_on_cmd))
    app.add_handler(CommandHandler("aprs_off", aprs_off_cmd))
    # opcional:
    app.add_handler(CommandHandler("aprs_status", aprs_status_cmd))

    app.add_handler(CommandHandler("ver_nodos", ver_nodos_cmd))
    app.add_handler(CommandHandler("vecinos", vecinos_cmd))
    app.add_handler(CommandHandler("vecinos5", vecinosX_cmd))  # NUEVO
    app.add_handler(CommandHandler("reconectar", reconectar_cmd))
    app.add_handler(CommandHandler("refrescar_nodos", refrescar_nodos_cmd))


    app.add_handler(CommandHandler("bloquear", bloquear_cmd))
    app.add_handler(CommandHandler("desbloquear", desbloquear_cmd))

# === [AÑADIDO] Registro de comandos nuevos ===

    app.add_handler(CommandHandler("broker_resume", broker_resume_cmd))
    app.add_handler(CommandHandler("force_reconnect", force_reconnect_cmd))
    app.add_handler(CommandHandler(["notificaciones", "notify", "notifs"], notificaciones_cmd))
# === [AÑADIDO] Registro de /broker_status ===

    app.add_handler(CommandHandler("broker_status", broker_status_cmd))
    app.add_handler(CommandHandler("auditoria_red", auditoria_red_cmd))
    app.add_handler(CommandHandler("auditoria_integral", auditoria_integral_cmd))

# ...
  
    # Conversación /enviar
    conv = ConversationHandler(
        entry_points=[CommandHandler("enviar", enviar_cmd)],
        states={
            ASK_SEND_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_send_dest)],
            ASK_SEND_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_send_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        name="enviar_conv",
        persistent=False,
    )
    app.add_handler(conv)

    # Menú (callback) y ForceReply del menú
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, on_forcereply_text))
    app.add_handler(MessageHandler(filters.Regex(r"^/vecinos\d+$"), vecinosX_cmd))
  
    return app

# --- ERRORES / ARRANQUE
# === MODIFICADA: post_startup con prefetch API antes del pool ===
async def post_startup(app: Application) -> None:
    # Menú oficial
    await set_bot_menu(app)

    # Config que usan varios comandos
    app.bot_data["mesh_host"] = MESHTASTIC_HOST
    app.bot_data["mesh_port"] = 4403  # puerto TCP de la radio/relay

    # IMPORTANTÍSIMO: guardar el pool persistente (clase con classmethods)
    # Ya tienes el import arriba: from tcpinterface_persistent import TCPInterfacePool
    app.bot_data["tcp_pool"] = TCPInterfacePool

    """
    Se ejecuta tras construir la Application (PTB v20+).
    Añade inicialización del scheduler de tareas, manteniendo lo que ya tuvieses.
    """

    # === [NUEVO] Prefetch inicial por API ANTES de cualquier conexión del pool ===
    # === Prefetch inicial (estilo v5.4, reutilizando helpers existentes) ===
    # === Prefetch inicial (CLI primero; API solo si hace falta) ===
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 📡 Prefetch inicial de nodos…", flush=True)

        # 1) Asegura ruta/fichero
        ensure_nodes_path_exists()

        # 2) Refrescar por CLI si el fichero está vacío o viejo
        #    (pause broker dentro de sync_nodes_and_save)
        ensure_nodes_file_fresh(max_age_s=300, max_rows=50, force_if_empty=True)

        # 3) Si la CLI ya dejó datos suficientes, no llames a la API
        try:
            rows_file = _parse_nodes_table(NODES_FILE)  # ya la tienes
        except Exception:
            rows_file = []

        if rows_file and len(rows_file) >= 5:
            # Ya tenemos un nodos.txt “bonito” o, al menos, utilizable
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 💾 Prefetch listo SOLO con CLI. Entradas: {len(rows_file)} (ver {NODES_FILE}).", flush=True)
        else:
            # 4) Si la CLI no aportó lo suficiente, entonces API (que además guardará en formato bonito)
            nodes = load_nodes_with_hops(n_max=50)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 💾 Prefetch listo tras API. Entradas: {len(nodes)} (ver {NODES_FILE}).", flush=True)

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ Prefetch inicial omitido: {type(e).__name__}: {e}", flush=True)

 # Inicializar broker_tasks → persistencia en ./bot_data
    try:
        broker_tasks.configure_sender(_tasks_send_adapter)
        broker_tasks.configure_reconnect(_tasks_reconnect_adapter)
        DATA_DIR_BROKER = os.path.join(os.path.dirname(__file__), "bot_data")
        os.makedirs(DATA_DIR_BROKER, exist_ok=True)
        broker_tasks.init(data_dir=DATA_DIR_BROKER, tz_name="Europe/Madrid", poll_interval_sec=2.0)
        # broker_tasks.start() # ← DESACTIVADO en el bot: evita duplicidades
        log("[Tasks] Scheduler del bot inicializado.")
    except Exception as e:
        log(f"[Tasks] No se pudo iniciar el scheduler en el bot: {e}")

    log("🤖 Bot arrancado y listo. Menú establecido (pool TCP inicializado).")


def main() -> None:
    # Construye la app
    app = build_application()
    app.post_init = post_startup  # tu post_init async está bien

    # ── REGISTRO DEL JOB: solo si hay JobQueue y está habilitado ───────────
    # Requiere que tengas definidas las globals:
    #   _NOTIFY_JOB_STARTED = False
    #   NOTIFY_DONE_ENABLED = True/False (si la usas; si no, usa True literal)
        # ── REGISTRO DEL JOB: solo si hay JobQueue ─────────────────────────────
    global _NOTIFY_JOB_STARTED
    job_queue = getattr(app, "job_queue", None)

    if job_queue is None:
        logging.warning("[notify_done] JobQueue no disponible; arranco sin notificador.")
    else:
        # Limpia posibles duplicados anteriores si los hubiera (por refuerzo)
        try:
            for j in job_queue.get_jobs_by_name("notify_done"):
                j.schedule_removal()
        except Exception:
            pass

        if (not _NOTIFY_JOB_STARTED) and NOTIFY_DONE_ENABLED:
            job_queue.run_repeating(
                _notify_executed_tasks_job,
                interval=30,      # cada 30s
                first=10,         # arranca a los 10s
                name="notify_done"
            )
            _NOTIFY_JOB_STARTED = True
            logging.info("[notify_done] Job activado (cada 30s)")

    # ───────────────────────────────────────────────────────────────────────

    # Arranca el bot
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
        stop_signals=None,
    )


if __name__ == "__main__":
    import atexit
    from tcpinterface_persistent import TCPInterfacePool
    atexit.register(TCPInterfacePool.shutdown)

    main()
