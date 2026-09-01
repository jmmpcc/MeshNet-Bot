#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# v7.0.30
# v7.0.30: unifica metadatos de versión tras la corrección segura del descubrimiento MeshCore.

from __future__ import annotations
"""
Meshtastic_Broker.py v7.0.30 — Broker MeshNet con servidor BBS y soporte de perfiles de radio.
Modo añadido: Meshcore embebido
19/02/2026 Se añade notificacion de RX MESHCORE en nodo A y Alias de MESHCORE del emisor RX
    [MC:<CANAL_LOGICO>:<ALIAS>] y el alias se resuelve por trama (si llega) y por heurística (si no llega).
--------------------------------
Broker JSONL para Meshtastic (TCPInterface) con salida limpia.

Cambios v3.3:
- Restaurada inferencia de canal lógico=0 para puertos de sistema (marcado con '*').
- Restaurada inferencia de RFch desde localNode.localConfig.lora.frequencySlot (marcado con '*').
- Limpieza de [DEBUG] (solo visibles con --debug-packets).
- Autoreconexión al nodo y heartbeats JSONL periódicos.
- Soporte --bind/--port para exponer el broker a clientes JSONL.

Uso rápido:
  python Meshtastic_Broker_v5.9.py --host 192.168.1.201 --verbose
  python Meshtastic_Broker_v5.9.py --host 192.168.1.201 --bind 127.0.0.1 --port 8765 --verbose --no-heartbeat
  python Meshtastic_Broker_v5.9.py --host 192.168.1.201 --verbose --debug-packets
"""
import argparse
import base64
import binascii
import json
import selectors
import socket
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
import re
from pathlib import Path
import os

from pubsub import pub
from meshtastic.tcp_interface import TCPInterface
from auto_reply import AutoReply


# === [NUEVO] BBS (broker-side) ==============================================
_BBS_IMPORT_ERROR = None
try:
    from bbs_server import BbsServer
except Exception as e:
    BbsServer = None
    _BBS_IMPORT_ERROR = f"{type(e).__name__}: {e}"

BBS = None  # instancia global
# ============================================================================

# === [NUEVO] Malla -> correo electrónico ===================================
from email_to_mesh import handle_mesh_mail_command

def _handle_mesh_mail_command_if_needed(text: str, source: str) -> str | None:
    return handle_mesh_mail_command(str(text or ""), source=source)
# ============================================================================


# --- Pasarela embebida (NUEVO) ---
from bridge_in_broker import (
    bridge_start_in_broker,
    bridge_stop_in_broker,
    bridge_status_in_broker,
    bridge_mirror_outgoing_from_broker,   # ← NUEVO
)

# === [NUEVO] MeshCore embebido en broker (opcional, 24/7) =========================
import asyncio
import hashlib
_MESHCORE_AVAILABLE = False
_MESHCORE_IMPORT_ERROR = None
try:
    from meshcore import MeshCore as _MeshCore  # type: ignore
    from meshcore import EventType as _MCEventType  # type: ignore
    _MESHCORE_AVAILABLE = True
except Exception as _e_mc:
    _MESHCORE_AVAILABLE = False
    _MESHCORE_IMPORT_ERROR = f"{type(_e_mc).__name__}: {_e_mc}"

MESHCORE_ENGINE = None  # instancia global (si se habilita)


def _auto_reply_config_path() -> Path:
    configured = (os.getenv("AUTO_REPLY_CONFIG") or "").strip()
    if configured:
        return Path(configured).expanduser()
    data_dir = (os.getenv("BOT_DATA_DIR") or "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "auto_reply.json"
    container_data = Path("/app/bot_data")
    if container_data.exists():
        return container_data / "auto_reply.json"
    return Path(__file__).resolve().parent.parent / "bot_data/auto_reply.json"


AUTO_REPLY = AutoReply(_auto_reply_config_path())


def _enqueue_meshtastic_auto_reply(channel: int, text: str) -> bool:
    """Encola la respuesta por la única ruta TX propiedad del broker."""
    reply = AUTO_REPLY.reply_for("meshtastic", int(channel), text)
    if reply is None:
        return False
    queue = globals().get("SENDQ")
    if queue is None or not hasattr(queue, "offer"):
        return False
    queue.offer({
        "channel": int(channel),
        "text": reply,
        "destination": None,
        "require_ack": False,
        "type": "text",
        "no_bridge": True,
        "origin": "auto_reply",
        "meta": {"auto_reply": 1},
    }, coalesce=False)
    return True

def _env_truthy(name: str, default: str = "0") -> bool:
    v = (os.getenv(name, default) or default).strip().lower()
    return v in {"1", "true", "on", "si", "sí", "y", "yes"}


# === [NUEVO v7.0.14A] Configuración externa segura del Bridge ===============
def _resolve_bridge_config_path(cli_path: Optional[str] = None) -> Path:
    """
    Resuelve la ruta de bridge_config.json sin modificar el sistema.

    Prioridad:
      1) Parámetro CLI --bridge-config
      2) Variable BRIDGE_CONFIG_PATH
      3) BOT_DATA_DIR/bridge_config.json
      4) bot_data/bridge_config.json relativo al directorio actual

    Uso:
        path = _resolve_bridge_config_path(args.bridge_config)

    Esta función no valida ni lee el JSON. Solo calcula una ruta razonable para
    despliegue Docker/Raspberry y para ejecución local.
    """
    if cli_path:
        return Path(cli_path).expanduser()

    env_path = (os.getenv("BRIDGE_CONFIG_PATH") or "").strip()
    if env_path:
        return Path(env_path).expanduser()

    bot_data_dir = (os.getenv("BOT_DATA_DIR") or "").strip()
    if bot_data_dir:
        return (Path(bot_data_dir).expanduser() / "bridge_config.json")

    # En contenedor el patrón habitual es /app/bot_data. En local, bot_data/.
    if Path("/app/bot_data").exists():
        return Path("/app/bot_data/bridge_config.json")

    return Path("bot_data/bridge_config.json")


def _apply_bridge_config_runtime_once(cli_path: Optional[str] = None, verbose: bool = False) -> dict:
    """
    Aplica bridge_config.json como overlay seguro sobre os.environ.

    Uso:
        _apply_bridge_config_runtime_once(args.bridge_config, verbose=args.verbose)

    Parámetros:
        cli_path:
            Ruta opcional recibida desde --bridge-config.
        verbose:
            Si True, muestra más detalle diagnóstico.

    Funcionalidad:
        - Si el fichero no existe, no hace nada y mantiene .env actual.
        - Si el módulo bridge_config.py no está disponible, no rompe el broker.
        - Si el JSON no valida, no aplica nada y mantiene .env actual.
        - Si valida, aplica las variables runtime resueltas a os.environ.

    Motivo de diseño:
        El broker actual ya decide BRIDGE_ENABLED, MESHCORE_ENABLE, mapas, límites
        y bloqueo BBS leyendo variables de entorno. Aplicar un overlay validado
        permite introducir configuración por JSON sin reescribir funciones que
        ya están probadas 24/7.
    """
    path = _resolve_bridge_config_path(cli_path)
    out = {
        "ok": False,
        "applied": False,
        "path": str(path),
        "profile": None,
        "details": None,
        "warnings": [],
        "errors": [],
    }

    if not path.exists():
        out["ok"] = True
        out["details"] = "missing_config_using_env"
        if verbose or _env_truthy("BRIDGE_CONFIG_VERBOSE", "0"):
            print(f"[bridge-config] no existe {path}; se usa configuración .env actual", flush=True)
        return out

    try:
        from bridge_config import load_bridge_config, validate_bridge_config  # type: ignore
    except Exception as e:
        out["details"] = f"import_error: {type(e).__name__}: {e}"
        print(f"[bridge-config] ⚠️ no se pudo importar bridge_config.py; se usa .env actual: {type(e).__name__}: {e}", flush=True)
        return out

    try:
        cfg = load_bridge_config(path, allow_missing=False)
        result = validate_bridge_config(cfg, env=os.environ)
    except Exception as e:
        out["details"] = f"load_or_validate_exception: {type(e).__name__}: {e}"
        print(f"[bridge-config] ⚠️ no se pudo leer/validar {path}; se usa .env actual: {type(e).__name__}: {e}", flush=True)
        return out

    out["warnings"] = list(getattr(result, "warnings", []) or [])
    out["errors"] = list(getattr(result, "errors", []) or [])

    # RADIO_PROFILE es la autoridad operativa. En meshcore_only no se aplica un
    # JSON que active un bridge mixto, aunque el fichero sea válido. Así se evita
    # contaminar el entorno con hosts/mapas de Meshtastic durante un despliegue
    # exclusivamente MeshCore. El fichero se conserva para futuros cambios de
    # perfil y el broker continúa usando el .env actual.
    try:
        from radio_profile import normalize_radio_profile  # type: ignore
        active_radio_profile = normalize_radio_profile(os.getenv("RADIO_PROFILE"), allow_legacy_empty=True)
    except Exception:
        active_radio_profile = (os.getenv("RADIO_PROFILE") or "").strip().lower().replace("-", "_")

    configured_profile_raw = str((getattr(result, "config", {}) or {}).get("profile") or "").strip()
    try:
        from radio_profile import bridge_profile_matches_radio_profile  # type: ignore
        profiles_match = bridge_profile_matches_radio_profile(
            active_radio_profile,
            configured_profile_raw,
        )
    except Exception:
        configured_normalized = configured_profile_raw.lower().replace("-", "_").replace(" ", "_")
        profiles_match = (
            active_radio_profile in {"", "legacy"}
            or configured_normalized in {"", "off", active_radio_profile}
        )

    # RADIO_PROFILE es autoritativo para todos los perfiles canónicos, no solo
    # para meshcore_only. El JSON puede complementar el perfil activo únicamente
    # cuando ambos describen la misma arquitectura. Si son distintos, no se
    # aplica ningún valor del JSON para evitar que hosts, mapas o flags cambien
    # silenciosamente la topología seleccionada en .env.
    if not profiles_match:
        out["ok"] = True
        out["details"] = "ignored_by_radio_profile_mismatch"
        out["profile"] = configured_profile_raw or None
        out["warnings"].append(
            "bridge_config.json no aplicado porque su perfil "
            f"{configured_profile_raw!r} no coincide con RADIO_PROFILE="
            f"{active_radio_profile!r}."
        )
        print(
            f"[bridge-config] ℹ️ {path} validado pero no aplicado: "
            f"profile JSON={configured_profile_raw or 'off'} distinto de "
            f"RADIO_PROFILE={active_radio_profile}",
            flush=True,
        )
        return out

    if not getattr(result, "ok", False):
        out["details"] = "validation_failed_using_env"
        print(f"[bridge-config] ⚠️ configuración inválida en {path}; se usa .env actual", flush=True)
        for err in out["errors"]:
            print(f"[bridge-config]   ERROR: {err}", flush=True)
        for warn in out["warnings"]:
            print(f"[bridge-config]   AVISO: {warn}", flush=True)
        return out

    runtime = getattr(result, "runtime", {}) or {}
    env_map = runtime.get("env", {}) or {}
    if not isinstance(env_map, dict):
        out["details"] = "runtime_env_missing_using_env"
        print(f"[bridge-config] ⚠️ runtime env no disponible; se usa .env actual", flush=True)
        return out

    for key, value in env_map.items():
        os.environ[str(key)] = str(value)

    out["ok"] = True
    out["applied"] = True
    out["profile"] = runtime.get("profile")
    out["details"] = runtime.get("bridge_kind")

    print(
        f"[bridge-config] ✅ aplicado {path} "
        f"profile={out['profile']} kind={out['details']} peers={runtime.get('peers')}",
        flush=True,
    )
    for warn in out["warnings"]:
        print(f"[bridge-config]   AVISO: {warn}", flush=True)

    return out
# ============================================================================



def _load_dotenv_runtime() -> None:
    """Carga variables de .env antes de resolver perfiles radio/runtime."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return

    candidates = []
    explicit = (os.getenv("ENV_FILE") or os.getenv("DOTENV_PATH") or "").strip()
    if explicit:
        candidates.append(explicit)
    candidates.extend(["/app/.env", str(Path.cwd() / ".env")])

    seen = set()
    for item in candidates:
        try:
            path = Path(item).expanduser()
            key = str(path)
            if key in seen or not path.exists():
                continue
            seen.add(key)
            load_dotenv(dotenv_path=str(path), override=False)
            print(f"[env] .env cargado: {path}", flush=True)
        except Exception as e:
            print(f"[env] ⚠️ no se pudo cargar {item}: {type(e).__name__}: {e}", flush=True)


def _radio_profile() -> str:
    """Devuelve el perfil canónico utilizando el resolvedor común v7.0.20."""
    try:
        from radio_profile import normalize_radio_profile  # type: ignore
        profile = normalize_radio_profile(os.getenv("RADIO_PROFILE"), allow_legacy_empty=True)
        return "" if profile == "legacy" else profile
    except Exception:
        # Fallback conservador para no romper imágenes antiguas donde todavía no
        # estuviese incluido radio_profile.py.
        return (os.getenv("RADIO_PROFILE") or "").strip().lower().replace("-", "_")


def _is_meshcore_only_profile() -> bool:
    return _radio_profile() == "meshcore_only"


def _apply_radio_profile_runtime(verbose: bool = False) -> dict:
    """Aplica el perfil de radio común sin reescribir la lógica ya estable.

    La resolución se delega en :mod:`radio_profile`, que normaliza aliases y
    aplica únicamente los overrides mínimos de cada arquitectura. Si el módulo
    no puede importarse se conserva el comportamiento v7.0.19 para
    ``meshcore_only`` y no se alteran los demás modos.
    """
    try:
        from radio_profile import apply_radio_profile_to_environment  # type: ignore

        caps = apply_radio_profile_to_environment(env=os.environ, strict=False)
        out = caps.to_dict()
        out["applied"] = bool(caps.valid and not caps.legacy_mode)
        out["overrides"] = dict(caps.environment_overrides)

        if not caps.valid:
            for warning in caps.warnings:
                print(f"[radio-profile] ⚠️ {warning}; se conserva el entorno sin aplicar", flush=True)
            return out

        if caps.legacy_mode:
            if verbose:
                print("[radio-profile] RADIO_PROFILE vacío: modo legacy sin overrides", flush=True)
            return out

        print(
            f"[radio-profile] ✅ profile={caps.profile} "
            f"node_a={caps.node_a_transport or '-'} "
            f"node_b={caps.node_b_transport or '-'} "
            f"meshcore={'ON' if caps.meshcore_enabled else 'OFF'} "
            f"meshtastic={'ON' if caps.meshtastic_enabled else 'OFF'}",
            flush=True,
        )
        if caps.alias_used:
            print(
                f"[radio-profile] alias {caps.requested_profile!r} normalizado a {caps.profile!r}",
                flush=True,
            )
        if verbose:
            print(f"[radio-profile] overrides={caps.environment_overrides}", flush=True)
        return out
    except Exception as e:
        profile = (os.getenv("RADIO_PROFILE") or "").strip().lower().replace("-", "_")
        out = {"profile": profile or None, "applied": False, "fallback": True}
        if profile != "meshcore_only":
            print(
                f"[radio-profile] ⚠️ resolvedor común no disponible: {type(e).__name__}: {e}; "
                "se conserva configuración legacy",
                flush=True,
            )
            return out

        overrides = {
            "MESHCORE_ENABLE": "1",
            "BRIDGE_ENABLED": "0",
            "BBS_ENABLED": "0",
            "BBS_ENABLE": "0",
            "MESHCORE_ONLY": "1",
        }
        for key, value in overrides.items():
            os.environ[key] = value
        out["applied"] = True
        out["overrides"] = overrides
        print("[radio-profile] ✅ fallback meshcore_only: Meshtastic/BBS OFF, MeshCore ON", flush=True)
        return out


def _mc_parse_ch_map() -> dict[int, dict]:
    """
    Mapa Meshtastic CH -> destino MeshCore.

    Prioridad:
      1) MESHCORE_CHANNEL_MAP (texto, recomendado):
         - Contacto: "0:AB12CD34:PUBLIC,2:EE99AA00:ZGZ"
         - Canal MeshCore: "0:chan:0:PUBLIC,2:chan:1:ZGZ"
           (forma: <ch_meshtastic>:chan:<channel_idx_meshcore>[:tag])

         TAG es opcional. Si no hay TAG:
           - se usa el nombre del canal si existe, o
           - CHx como fallback.

      2) MESHCORE_CH2CONTACT (compat simple): "0:AB12CD34,2:EE99AA00"
      3) MESHCORE_MAP_CH_TO_CONTACT (JSON):
         {"0":"AB12CD34","2":{"contact":"EE99AA00","tag":"ZGZ"}}

    Devuelve:
      { ch: {"kind":"contact","contact":"<prefix>","tag":"<tag opcional>"} }
      { ch: {"kind":"chan","channel_idx":<int>,"tag":"<tag opcional>"} }
    """
    out: dict[int, dict] = {}

    def _add_contact(ch: int, contact: str, tag: str | None = None):
        c = (contact or "").strip()
        if not c:
            return
        out[int(ch)] = {"kind": "contact", "contact": c, "tag": (tag or "").strip() or None}

    def _add_chan(ch: int, chan_idx: int, tag: str | None = None):
        try:
            ci = int(chan_idx)
        except Exception:
            return
        out[int(ch)] = {"kind": "chan", "channel_idx": int(ci), "tag": (tag or "").strip() or None}

    raw = (os.getenv("MESHCORE_CHANNEL_MAP") or "").strip()
    if raw:
        for part in raw.split(","):
            p = (part or "").strip()
            if not p:
                continue
            # formatos soportados:
            #   ch:contact
            #   ch:contact:tag
            #   ch:chan:idx
            #   ch:chan:idx:tag
            toks = [t.strip() for t in p.split(":")]
            if len(toks) < 2:
                continue
            try:
                ch = int(toks[0])
            except Exception:
                continue

            mode = (toks[1] or "").strip().lower()
            if mode in ("chan", "channel"):
                if len(toks) < 3:
                    continue
                try:
                    idx = int(toks[2])
                except Exception:
                    continue
                tag = toks[3] if len(toks) >= 4 else None
                _add_chan(ch, idx, tag)
            else:
                contact = toks[1]
                tag = toks[2] if len(toks) >= 3 else None
                _add_contact(ch, contact, tag)

    if out:
        return out

    raw2 = (os.getenv("MESHCORE_CH2CONTACT") or "").strip()
    if raw2:
        for part in raw2.split(","):
            p = (part or "").strip()
            if not p:
                continue
            toks = [t.strip() for t in p.split(":")]
            if len(toks) < 2:
                continue
            try:
                ch = int(toks[0])
            except Exception:
                continue
            _add_contact(ch, toks[1], None)

    rawj = (os.getenv("MESHCORE_MAP_CH_TO_CONTACT") or "").strip()
    if rawj:
        try:
            obj = json.loads(rawj)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    try:
                        ch = int(k)
                    except Exception:
                        continue
                    if isinstance(v, str):
                        _add_contact(ch, v, None)
                    elif isinstance(v, dict):
                        _add_contact(ch, str(v.get("contact") or ""), (v.get("tag") or None))
        except Exception:
            pass

    return out
def _mc_parse_contact_to_ch() -> dict[str, int]:
    """
    Mapa MeshCore contacto(prefix) -> Meshtastic CH.

    Soporta:
      - MESHCORE_CONTACT_TO_CH: "ABCDEF:0,112233:2"
      - MESHCORE_CONTACT_DEFAULT_CH: int (fallback)
    """
    out: dict[str, int] = {}
    raw = (os.getenv("MESHCORE_CONTACT_TO_CH") or "").strip()
    if raw:
        for part in raw.split(","):
            p = (part or "").strip()
            if not p:
                continue
            toks = [t.strip() for t in p.split(":")]
            if len(toks) < 2:
                continue
            pref = toks[0]
            try:
                ch = int(toks[1])
            except Exception:
                continue
            if pref:
                out[pref] = int(ch)
    return out

def _embedded_b_uses_meshcore() -> bool:
    """
    Reutiliza el sistema ya existente del broker para saber qué backend embebido
    está activo.

    Regla real:
    - Si MESHCORE_ENABLE=1 -> backend embebido MeshCore.
    - En caso contrario, si BRIDGE_ENABLED=1 -> backend embebido Meshtastic.
    - No se introduce ninguna variable nueva.
    """
    return _env_truthy("MESHCORE_ENABLE", "0")

def _check_and_reconnect_embedded_b(iface_a=None, reason: str = "") -> dict:
    """
    Comprueba si el nodo B embebido sigue sano tras la reconexión del nodo A.
    Si no lo está, rearma únicamente el backend embebido correspondiente.

    Diseño:
    - Reutiliza la lógica REAL ya existente en el broker v7.
    - No introduce variables nuevas de configuración.
    - Evita reinicios innecesarios del bridge embebido en 24/7.

    Uso:
        _check_and_reconnect_embedded_b(iface_a=iface_a, reason="connection.established")

    Parámetros:
    - iface_a: interfaz válida del nodo A ya conectada.
    - reason: texto para diagnóstico/log.

    Devuelve:
    - dict con resumen del resultado.
    """
    why = (reason or "").strip()
    out = {
        "ok": True,
        "reason": why,
        "backend": None,
        "action": "noop",
        "details": None,
    }

    # ---------------------------------------------------------
    # CASO 1: backend embebido activo = MeshCore
    # Regla REAL del broker: MESHCORE_ENABLE=1
    # ---------------------------------------------------------
    if _embedded_b_uses_meshcore():
        out["backend"] = "meshcore"
        try:
            eng = globals().get("MESHCORE_ENGINE")
            if not eng:
                out["ok"] = False
                out["action"] = "skip"
                out["details"] = "meshcore_engine_missing"
                print(f"[broker] Check embedded B ({why}): MeshCore activo pero sin engine", flush=True)
                return out

            is_ok = False
            healthy_fn = getattr(eng, "is_healthy", None)

            if callable(healthy_fn):
                try:
                    is_ok = bool(healthy_fn())
                except Exception:
                    is_ok = False
            else:
                # Fallback mínimo compatible con tu broker actual
                th = getattr(eng, "_thread", None)
                stop_evt = getattr(eng, "_stop", None)
                mc_obj = getattr(eng, "_mc", None)
                connected = bool(getattr(eng, "_connected", False))
                is_ok = bool(
                    th is not None
                    and th.is_alive()
                    and stop_evt is not None
                    and not stop_evt.is_set()
                    and mc_obj is not None
                    and connected
                )

            if is_ok:
                out["action"] = "already_ok"
                out["details"] = "meshcore_healthy"
                print(f"[broker] Check embedded B ({why}): MeshCore OK", flush=True)
                return out

            print(f"[broker] Check embedded B ({why}): MeshCore NO OK -> restart", flush=True)

            try:
                eng.stop()
            except Exception as e:
                print(f"[broker] MeshCore stop warning: {type(e).__name__}: {e}", flush=True)

            time.sleep(1.0)

            try:
                eng.start()
                out["action"] = "restart"
                out["details"] = "meshcore_restarted"
            except Exception as e:
                out["ok"] = False
                out["action"] = "error"
                out["details"] = f"meshcore_start_error: {type(e).__name__}: {e}"
                print(f"[broker] MeshCore start ERROR: {type(e).__name__}: {e}", flush=True)

            return out

        except Exception as e:
            out["ok"] = False
            out["action"] = "error"
            out["details"] = f"meshcore_check_error: {type(e).__name__}: {e}"
            print(f"[broker] Check embedded B ({why}) meshcore ERROR: {type(e).__name__}: {e}", flush=True)
            return out

    # ---------------------------------------------------------
    # CASO 2: backend embebido activo = Meshtastic bridge
    # Regla REAL: si NO es MeshCore, el backend embebido es el bridge
    # ---------------------------------------------------------
    out["backend"] = "bridge"
    try:
        st = bridge_status_in_broker()
        if not isinstance(st, dict):
            st = {}

        running = bool(st.get("running"))
        iface_b_ok = bool(st.get("iface_b"))

        # Si el bridge está operativo y B está vivo, lo dejamos.
        # No forzamos restart por defecto para evitar churn 24/7.
        if running and iface_b_ok:
            out["action"] = "already_ok"
            out["details"] = "bridge_running_iface_b_ok"
            print(f"[broker] Check embedded B ({why}): Bridge Meshtastic OK", flush=True)
            return out

        print(
            f"[broker] Check embedded B ({why}): Bridge Meshtastic NO OK "
            f"(running={running}, iface_b={iface_b_ok}) -> reconnect",
            flush=True
        )

        try:
            bridge_stop_in_broker()
        except Exception as e:
            print(f"[broker] bridge_stop warning: {type(e).__name__}: {e}", flush=True)

        time.sleep(1.0)

        try:
            bridge_start_in_broker(iface_a)
            out["action"] = "restart" if running else "start"
            out["details"] = "bridge_restarted_with_iface_a"
        except Exception as e:
            out["ok"] = False
            out["action"] = "error"
            out["details"] = f"bridge_start_error: {type(e).__name__}: {e}"
            print(f"[broker] bridge_start ERROR: {type(e).__name__}: {e}", flush=True)

        return out

    except Exception as e:
        out["ok"] = False
        out["action"] = "error"
        out["details"] = f"bridge_check_error: {type(e).__name__}: {e}"
        print(f"[broker] Check embedded B ({why}) bridge ERROR: {type(e).__name__}: {e}", flush=True)
        return out


# === [NUEVO v7.0.14] Arranque autónomo de B MeshCore ==========================
def _start_meshcore_embedded_autonomous(reason: str = "startup") -> dict:
    """
    Arranca/verifica MeshCore embebido sin depender de que el nodo A Meshtastic
    haya establecido conexión TCP.

    Uso:
        _start_meshcore_embedded_autonomous("broker.startup")
        _start_meshcore_embedded_autonomous("connection.established")

    Parámetros:
        reason:
            Texto de diagnóstico para log. No afecta al funcionamiento.

    Funcionalidad:
        - Si MESHCORE_ENABLE=0, no hace nada.
        - Si BRIDGE_ENABLED=1 y MESHCORE_ENABLE=1, respeta la prioridad histórica
          del bridge Meshtastic y no arranca MeshCore para evitar doble backend B.
        - Si la librería meshcore no está disponible, informa y sale sin romper
          el broker.
        - Crea MESHCORE_ENGINE si todavía no existe.
        - Si el engine existe pero no está sano, llama a start(). El propio engine
          mantiene su supervisor/reconexión 24/7.
        - No necesita iface_a ni conexión TCP con el nodo A.

    Devuelve:
        dict normalizado con ok/backend/action/details/status.
    """
    why = (reason or "startup").strip()
    out = {
        "ok": True,
        "backend": "meshcore",
        "reason": why,
        "action": "noop",
        "details": None,
        "status": None,
    }

    try:
        meshcore_enabled = _env_truthy("MESHCORE_ENABLE", "0")
        bridge_enabled = _env_truthy("BRIDGE_ENABLED", "0")

        if not meshcore_enabled:
            out["action"] = "skip"
            out["details"] = "MESHCORE_ENABLE=0"
            return out

        if bridge_enabled:
            out["action"] = "skip"
            out["details"] = "BRIDGE_ENABLED=1 tiene prioridad sobre MESHCORE_ENABLE=1"
            print(
                f"[meshcore] arranque autónomo omitido ({why}): BRIDGE_ENABLED=1 tiene prioridad",
                flush=True,
            )
            return out

        if not _MESHCORE_AVAILABLE:
            out["ok"] = False
            out["action"] = "error"
            out["details"] = f"meshcore_not_available: {_MESHCORE_IMPORT_ERROR or 'unknown'}"
            print(
                f"[meshcore] arranque autónomo no disponible ({why}): {out['details']}",
                flush=True,
            )
            return out

        global MESHCORE_ENGINE
        if MESHCORE_ENGINE is None:
            MESHCORE_ENGINE = MeshCoreEmbeddedBridge()
            out["action"] = "created"

        healthy = False
        try:
            healthy = bool(MESHCORE_ENGINE.is_healthy())
        except Exception:
            healthy = False

        if not healthy:
            MESHCORE_ENGINE.start()
            out["action"] = "start" if out["action"] == "noop" else "create_start"
            out["details"] = "meshcore_autonomous_start_requested"
        else:
            out["action"] = "already_ok"
            out["details"] = "meshcore_healthy"

        try:
            out["status"] = MESHCORE_ENGINE.status() if MESHCORE_ENGINE else None
        except Exception as e:
            out["status"] = {"status_error": f"{type(e).__name__}: {e}"}

        print(f"[meshcore] arranque/verificación autónoma ({why}): {out}", flush=True)
        return out

    except Exception as e:
        out["ok"] = False
        out["action"] = "error"
        out["details"] = f"{type(e).__name__}: {e}"
        print(f"[meshcore] arranque autónomo ERROR ({why}): {type(e).__name__}: {e}", flush=True)
        return out

def _mc_parse_chanidx_to_ch() -> dict[int, int]:
    """
    Mapa MeshCore channel_idx -> Meshtastic CH.
    - MESHCORE_CHANIDX_TO_CH: "0:0,1:2"
    """
    out: dict[int, int] = {}
    raw = (os.getenv("MESHCORE_CHANIDX_TO_CH") or "").strip()
    if not raw:
        return out
    for part in raw.split(","):
        p = (part or "").strip()
        if not p:
            continue
        toks = [t.strip() for t in p.split(":")]
        if len(toks) < 2:
            continue
        try:
            a = int(toks[0]); b = int(toks[1])
        except Exception:
            continue
        out[a] = b
    return out

def _mc_parse_chanidx_to_tag() -> dict[int, str]:
    """
    Mapa MeshCore channel_idx -> tag/nombre humano.
    - MESHCORE_CHANIDX_TO_TAG: "2:EMERGENCIAS,3:COORD"
    """
    out: dict[int, str] = {}
    raw = (os.getenv("MESHCORE_CHANIDX_TO_TAG") or "").strip()
    if not raw:
        return out
    for part in raw.split(","):
        p = (part or "").strip()
        if not p:
            continue
        toks = [t.strip() for t in p.split(":", 1)]
        if len(toks) < 2:
            continue
        try:
            idx = int(toks[0])
        except Exception:
            continue
        tag = (toks[1] or "").strip()
        if not tag:
            continue
        out[idx] = tag
    return out

def _safe_meshcore_max_text_bytes() -> int:
    """
    Límite conservador (en BYTES UTF-8) para TX hacia MeshCore.
    Env:
      MESHCORE_MAX_TEXT_BYTES (default 140)

    El log de TX muestra una vista previa recortada, pero el envío real se
    divide con este límite antes de llamar al firmware MeshCore.
    """
    try:
        v = int((os.getenv("MESHCORE_MAX_TEXT_BYTES") or "140").strip())
    except Exception:
        v = 140
    return max(80, min(v, 260))


def _safe_meshcore_part_delay_sec() -> float:
    """
    Demora entre partes de un mismo mensaje MeshCore troceado.
    Env:
      MESHCORE_TX_PART_DELAY_SEC (default 1.0)

    La pausa solo se aplica cuando el texto se divide en varias partes. Ayuda a
    que el firmware y la cola RF procesen cada fragmento sin ráfagas seguidas.
    """
    try:
        v = float((os.getenv("MESHCORE_TX_PART_DELAY_SEC") or "1.0").strip())
    except Exception:
        v = 1.0
    return max(0.0, min(v, 10.0))


def _split_meshcore_send_parts(text: str, max_bytes: int) -> list[str]:
    """Crea partes MeshCore estables y sin pérdida, contando el prefijo ``(i/n)``."""
    value = str(text or "").strip()
    if not value:
        return []
    max_bytes = max(80, int(max_bytes or 140))
    if len(value.encode("utf-8", errors="ignore")) <= max_bytes:
        return [value]

    def _lossless_bodies(body_limit: int) -> list[str]:
        remaining = value
        bodies: list[str] = []
        while remaining:
            if len(remaining.encode("utf-8", errors="ignore")) <= body_limit:
                bodies.append(remaining)
                break
            lo, hi, best = 1, len(remaining), 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if len(remaining[:mid].encode("utf-8", errors="ignore")) <= body_limit:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            cut = best
            # Incluye el separador en la parte anterior para que concatenar los
            # cuerpos reproduzca exactamente el texto normalizado de entrada.
            whitespace = max(
                remaining.rfind(" ", 0, cut + 1),
                remaining.rfind("\n", 0, cut + 1),
                remaining.rfind("\t", 0, cut + 1),
            )
            if whitespace >= max(1, int(cut * 0.6)):
                cut = whitespace + 1
            bodies.append(remaining[:cut])
            remaining = remaining[cut:]
        return bodies

    total_hint = 2
    for _ in range(12):
        prefix_bytes = len(f"({total_hint}/{total_hint}) ".encode("utf-8"))
        bodies = _lossless_bodies(max(1, max_bytes - prefix_bytes))
        actual_total = len(bodies)
        if actual_total == total_hint:
            return [f"({idx}/{actual_total}) {body}" for idx, body in enumerate(bodies, 1)]
        total_hint = actual_total
    raise RuntimeError("meshcore_split_no_converge")


# === [FIX APRS -> MeshCore] Limpieza de payload APRS para MeshCore ============
# Objetivo:
# - Cuando una trama APRS llega a Meshtastic y se refleja a MeshCore por canal,
#   evitar reenviar la cabecera APRS cruda:
#       !4138.43N/00054.20W>000/000/A=000111 ...
# - Mantener únicamente el comentario útil y la URL de Google Maps.
# - Reducir longitud para que MeshCore no parta la URL en la coma de coordenadas.
#
# Ejemplo entrada:
#   !4138.43N/00054.20W>000/000/A=000111 QRV R70-R72 sdr:in91np.ddns.net:8073 Abierto https://maps.google.com/?q=41.640500,-0.903333
#
# Ejemplo salida:
#   QRV R70-R72 sdr:in91np.ddns.net:8073 Abierto https://maps.google.com/?q=41.640500,-0.903333
# ============================================================================

_APRS_UNCOMPRESSED_POS_RE = re.compile(
    r"^\s*!"
    r"(?P<lat_deg>\d{2})(?P<lat_min>\d{2}\.\d+)(?P<lat_hemi>[NS])"
    r"[/\\]"
    r"(?P<lon_deg>\d{3})(?P<lon_min>\d{2}\.\d+)(?P<lon_hemi>[EW])"
    r"(?P<symbol>.?)"
    r"(?P<body>.*)$"
)


def _aprs_coord_to_decimal(deg_s: str, min_s: str, hemi: str) -> float | None:
    """
    Convierte una coordenada APRS no comprimida a decimal.

    Uso:
        lat = _aprs_coord_to_decimal("41", "38.43", "N")

    Parámetros:
        deg_s:
            Grados como texto. Latitud: 2 dígitos. Longitud: 3 dígitos.
        min_s:
            Minutos APRS con decimales.
        hemi:
            Hemisferio: N/S/E/W.

    Funcionalidad:
        - Convierte coordenadas APRS DDMM.mmN / DDDMM.mmW a decimal.
        - Aplica signo negativo para hemisferios S y W.
        - Devuelve None ante cualquier valor corrupto para no romper el broker 24/7.
    """
    try:
        deg = int(str(deg_s))
        minutes = float(str(min_s))
        value = float(deg) + (minutes / 60.0)
        if str(hemi).upper() in ("S", "W"):
            value = -value
        return value
    except Exception:
        return None


def _clean_aprs_position_text_for_meshcore(text: str) -> str:
    """
    Limpia un texto APRS de posición antes de reenviarlo a MeshCore.

    Uso:
        msg = _clean_aprs_position_text_for_meshcore(msg)

    Parámetros:
        text:
            Texto original recibido desde Meshtastic/APRS o generado por una tarea.

    Funcionalidad:
        - Detecta paquetes APRS no comprimidos que empiezan por '!DDMM.mmN/DDDMM.mmW'.
        - Elimina cabecera de posición, curso/velocidad y altitud APRS.
        - Conserva el comentario humano.
        - Si no existe URL de maps.google.com, la genera desde la posición APRS.
        - Si el texto no parece APRS de posición, lo devuelve igual salvo normalización
          ligera de espacios.

    Motivo:
        MeshCore parte mensajes largos por límite de bytes. Si reenviamos la cabecera
        APRS completa, la URL puede caer en una segunda parte o partirse justo en la coma
        de latitud/longitud, quedando el enlace inutilizable.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    # Normalización mínima, sin tocar URLs ni coordenadas.
    raw = re.sub(r"\s+", " ", raw).strip()

    m = _APRS_UNCOMPRESSED_POS_RE.match(raw)
    if not m:
        return raw

    lat = _aprs_coord_to_decimal(
        m.group("lat_deg"),
        m.group("lat_min"),
        m.group("lat_hemi"),
    )
    lon = _aprs_coord_to_decimal(
        m.group("lon_deg"),
        m.group("lon_min"),
        m.group("lon_hemi"),
    )

    body = str(m.group("body") or "").strip()

    # En APRS típico tras el símbolo vienen curso/velocidad:
    #   000/000
    body = re.sub(r"^\s*\d{3}/\d{3}\s*", "", body).strip()

    # Después puede venir altitud:
    #   /A=000111
    #   A=000111
    body = re.sub(r"^\s*/?A=\d{6}\s*", "", body, flags=re.IGNORECASE).strip()

    # Limpieza de separadores residuales.
    body = body.lstrip(" /:-").strip()
    body = re.sub(r"\s+", " ", body).strip()

    # Si ya venía una URL de Google Maps, no la duplicamos.
    has_maps_url = bool(re.search(r"https?://maps\.google\.", body, flags=re.IGNORECASE))

    if not has_maps_url and lat is not None and lon is not None:
        maps_url = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"
        body = f"{body} {maps_url}".strip() if body else maps_url

    return body or raw

class MeshCoreEmbeddedBridge:
    """
    Pasarela MeshCore embebida en el broker.

    - RX Meshtastic -> TX MeshCore (por mapeo CH->contacto).
    - RX MeshCore   -> TX Meshtastic (por mapeo contacto->CH o chan_idx->CH).

    Diseñado para 24/7:
    - Hilo dedicado + asyncio con supervisor de reconexión.
    - Cola de envío hacia MeshCore para no bloquear el broker.
    - Dedup simple para evitar eco/loops cuando reinyectamos a Meshtastic.
    """

    def __init__(self):
        self.enable = _env_truthy("MESHCORE_ENABLE", "0") and _MESHCORE_AVAILABLE
        self.mode = (os.getenv("MESHCORE_MODE", "serial") or "serial").strip().lower()
        self.serial_port = (os.getenv("MESHCORE_SERIAL_PORT") or "").strip() or None
        self.serial_baud = int((os.getenv("MESHCORE_SERIAL_BAUD", "115200") or "115200").strip() or 115200)

        self.tcp_host = (os.getenv("MESHCORE_TCP_HOST") or "").strip() or None
        self.tcp_port = int((os.getenv("MESHCORE_TCP_PORT", "4000") or "4000").strip() or 4000)

        self.ble_addr = (os.getenv("MESHCORE_BLE_ADDR") or "").strip() or None
        self.ble_pin = (os.getenv("MESHCORE_BLE_PIN") or "").strip() or None

        self.default_contact_prefix = (os.getenv("MESHCORE_DEFAULT_CONTACT_PREFIX") or "").strip() or None
        self.default_contact_ch = int((os.getenv("MESHCORE_CONTACT_DEFAULT_CH", "0") or "0").strip() or 0)

        self.ch_map = _mc_parse_ch_map()  # Meshtastic CH -> MeshCore contact
        self.contact_to_ch = _mc_parse_contact_to_ch()  # MeshCore contact -> Meshtastic CH
        self.chanidx_to_ch = _mc_parse_chanidx_to_ch()  # MeshCore channel_idx -> Meshtastic CH

        # Reverse map (MeshCore channel_idx -> tag lógico) para prefijos RX.
        # Fuentes (prioridad):
        #   1) MESHCORE_CHANIDX_TO_TAG (nuevo, independiente de mirroring)
        #   2) tags definidos en MESHCORE_CHANNEL_MAP
        self.chanidx_to_tag: dict[int, str] = {}
        try:
            self.chanidx_to_tag.update(_mc_parse_chanidx_to_tag())
            for _ch, m in (self.ch_map or {}).items():
                if (m or {}).get("kind") == "chan":
                    ci = m.get("channel_idx")
                    tg = (m.get("tag") or "").strip()
                    if ci is not None and tg and int(ci) not in self.chanidx_to_tag:
                        self.chanidx_to_tag[int(ci)] = tg
        except Exception:
            self.chanidx_to_tag = {}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop = None
        self._tx_q = None
        self._mc = None
        # Cola persistente entre sesiones para no perder TX durante reconexiones:
        # - retries explícitos
        # - mensajes pendientes al romper una sesión
        # - mensajes encolados mientras no hay _tx_q activa
        self._retry_spool_lock = threading.Lock()
        self._retry_spool_max = max(
            100,
            int((os.getenv("MESHCORE_RETRY_SPOOL_MAX", "2000") or "2000").strip() or 2000),
        )
        self._retry_spool: deque[tuple] = deque(maxlen=self._retry_spool_max)
        self._retry_spool_drop_count = 0

        # Reintentos reales de TX MeshCore.
        # Solo afecta al backend MeshCore embebido. No toca Meshtastic, APRS, BBS ni bridge.
        self._tx_max_retries = max(
            0,
            int((os.getenv("MESHCORE_TX_MAX_RETRIES", "3") or "3").strip() or 3),
        )
        self._tx_retry_backoff_sec = max(
            0.5,
            float((os.getenv("MESHCORE_TX_RETRY_BACKOFF_SEC", "3") or "3").strip() or 3.0),
        )

        # === Prefijo RX MeshCore -> Meshtastic ===
        # Estilos:
        #   tech    -> [MC:<prefix>] (debug)
        #   alias   -> [MC:<alias>] (usa MESHCORE_CONTACT_ALIASES)
        #   compact -> [MC]
        #   channel -> canales: [MC-<TAG>] ; contactos: tech
        self.rx_prefix_style = (os.getenv("MESHCORE_RX_PREFIX_STYLE", "tech") or "tech").strip().lower()

        # Mapa opcional prefix->alias para contactos MeshCore (solo si rx_prefix_style=alias o quieres fallback)
        # Formato: "6a18cb3d125b:EA2FBO_V4,ab12cd34:EB2EAS-7"
        self.contact_aliases = {}
        raw_alias = (os.getenv("MESHCORE_CONTACT_ALIASES", "") or "").strip()
        if raw_alias:
            for part in raw_alias.split(","):
                part = (part or "").strip()
                if not part or ":" not in part:
                    continue
                k, v = part.split(":", 1)
                k = (k or "").strip()
                v = (v or "").strip()
                if k and v:
                    self.contact_aliases[k] = v

        # Log de encolado TX hacia MeshCore (útil para debug)
        self.log_enqueue = _env_truthy("MESHCORE_LOG_ENQUEUE", "0")


        # anti-eco: fingerprints de mensajes que hemos inyectado a Meshtastic
        self._inject_lock = threading.Lock()
        self._inject_recent: dict[str, float] = {}
        self._inject_ttl = float((os.getenv("MESHCORE_INJECT_DEDUP_SEC", "12") or "12").strip() or 12.0)

        # métricas básicas
        self._last_ok = 0.0
        self._last_err = ""
        self._connected = False

        # Cache ligero de contactos MeshCore para resolver rutas RX:
        # - public_key completo -> nombre/posición
        # - prefijos cortos -> nombre/posición
        # - hashes compactos de ruta -> nombre/posición cuando coinciden con
        #   prefijos únicos de public_key (formato usado por MeshCore)
        self._mc_contacts_cache: dict[str, dict] = {}
        self._mc_path_prefix_cache: dict[str, list[dict]] = {}

    def _meshcore_remember_contact(self, contact: dict | None) -> None:
        if not isinstance(contact, dict):
            return
        public_key = str(contact.get("public_key") or contact.get("pubkey") or contact.get("key") or "").strip().lower()
        name = str(contact.get("name") or contact.get("adv_name") or contact.get("alias") or contact.get("label") or "").strip()
        if not public_key and not name:
            return
        item = dict(contact)
        if public_key:
            item["public_key"] = public_key
        if name:
            item["name"] = name
        for lat_key, lon_key in (("adv_lat", "adv_lon"), ("lat", "lon"), ("latitude", "longitude")):
            try:
                lat = item.get(lat_key)
                lon = item.get(lon_key)
                if lat is not None and lon is not None and float(lat) != 0.0 and float(lon) != 0.0:
                    item["lat"] = float(lat)
                    item["lon"] = float(lon)
                    break
            except Exception:
                pass
        keys = set()
        if public_key:
            keys.add(public_key)
            for n in (6, 8, 10, 12, 16):
                keys.add(public_key[:n])
        for k in (contact.get("pubkey_prefix"), contact.get("key_prefix"), contact.get("prefix"), contact.get("id")):
            if k:
                keys.add(str(k).strip().lower())
        for k in keys:
            if k:
                self._mc_contacts_cache[k] = item
        # MeshCore representa los repetidores de la ruta con los primeros bytes
        # de la public_key del repetidor. Indexamos prefijos por longitud real
        # para resolver nombres solo cuando el prefijo es inequívoco.
        if public_key:
            for hex_len in (2, 4, 6, 8, 16):
                prefix = public_key[:hex_len]
                if not prefix:
                    continue
                bucket = self._mc_path_prefix_cache.setdefault(prefix, [])
                if not any(str(x.get("public_key") or "").lower() == public_key for x in bucket):
                    bucket.append(item)


    def _meshcore_contact_prefix_by_name(self, name: str) -> str:
        """Resuelve de forma inequívoca el prefijo DM conocido para un alias.

        Prioriza los prefijos reales/configurados de ``contact_aliases``. Solo
        si no existe esa asociación reutiliza los contactos cacheados y deja
        ``public_key[:12]`` como compatibilidad legacy.
        """
        wanted = _norm_text(name or "").casefold()
        if not wanted:
            return ""

        alias_matches = []
        for key, alias in (self.contact_aliases or {}).items():
            if _norm_text(alias).casefold() == wanted and str(key).strip():
                alias_matches.append(str(key).strip())
        alias_unique = list(dict.fromkeys(alias_matches))
        if len(alias_unique) == 1:
            return alias_unique[0]
        if len(alias_unique) > 1:
            return ""

        matches = []
        for contact in (self._mc_contacts_cache or {}).values():
            if not isinstance(contact, dict):
                continue
            contact_name = _norm_text(contact.get("name") or contact.get("adv_name") or contact.get("alias") or contact.get("label") or "")
            if contact_name.casefold() != wanted:
                continue
            public_key = str(contact.get("public_key") or contact.get("pubkey") or contact.get("key") or "").strip()
            prefix = str(contact.get("pubkey_prefix") or contact.get("key_prefix") or contact.get("prefix") or "").strip()
            dm_key = prefix or (public_key[:12] if public_key else "")
            if dm_key:
                matches.append(dm_key)
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else ""

    def _meshcore_contact_display(self, contact: dict | None, fallback: str = "") -> str:
        if not isinstance(contact, dict):
            return fallback or "desconocido"
        name = str(contact.get("name") or contact.get("adv_name") or contact.get("alias") or contact.get("label") or "").strip()
        pk = str(contact.get("public_key") or "").strip()
        return name or (pk[:12] if pk else fallback) or "desconocido"

    def _meshcore_enrich_path_info(self, data: dict) -> dict:
        enriched = dict(data or {})
        # Primero recuerda el emisor, si está en contacts/cache.
        pref = str(enriched.get("pubkey_prefix") or "").strip().lower()
        contact = None
        try:
            if self._mc is not None and pref:
                getter = getattr(self._mc, "get_contact_by_key_prefix", None)
                if callable(getter):
                    contact = getter(pref)
        except Exception:
            contact = None
        if isinstance(contact, dict):
            self._meshcore_remember_contact(contact)
        else:
            contact = self._mc_contacts_cache.get(pref)
        if isinstance(contact, dict):
            enriched["from_name"] = self._meshcore_contact_display(contact, pref)
            enriched["from_lat"] = contact.get("lat")
            enriched["from_lon"] = contact.get("lon")

        chunks, _, _ = _meshcore_path_chunks_from_payload(enriched)
        raw_path = enriched.get("path")
        path_items = raw_path if isinstance(raw_path, list) else []
        repeaters = []
        for idx, chunk in enumerate(chunks):
            chunk_s = str(chunk).strip().lower()
            matches = self._mc_path_prefix_cache.get(chunk_s, [])
            c = matches[0] if len(matches) == 1 else None
            snr = None
            try:
                if idx < len(path_items) and isinstance(path_items[idx], dict):
                    snr = path_items[idx].get("snr")
            except Exception:
                snr = None
            entry = {
                "hash": chunk_s,
                "name": self._meshcore_contact_display(c, chunk_s) if c else chunk_s,
                "resolved": bool(c),
                "ambiguous": len(matches) > 1,
                "snr": snr,
                "lat": c.get("lat") if isinstance(c, dict) else None,
                "lon": c.get("lon") if isinstance(c, dict) else None,
            }
            if len(matches) > 1:
                entry["name"] = f"{chunk_s} (prefijo ambiguo: {len(matches)} contactos)"
            repeaters.append(entry)
        if repeaters:
            enriched["meshcore_repeaters"] = repeaters
        return enriched

    def status(self) -> dict:
        return {
            "enabled": bool(self.enable),
            "available": bool(_MESHCORE_AVAILABLE),
            "import_error": _MESHCORE_IMPORT_ERROR if not _MESHCORE_AVAILABLE else None,
            "mode": self.mode,
            "connected": bool(self._connected),
            "last_ok": int(self._last_ok) if self._last_ok else None,
            "last_err": self._last_err or None,
            "ch_map": self.ch_map,
            "default_contact_prefix": self.default_contact_prefix,
            "default_contact_ch": self.default_contact_ch,
        }

    @staticmethod
    def _meshcore_node_type(raw_type) -> tuple[int | None, str, str, bool | None]:
        """Normaliza el tipo anunciado por MeshCore sin inferirlo por el alias.

        Valores del protocolo Companion:
            0 desconocido, 1 companion/chat, 2 repeater, 3 room, 4 sensor.

        Devuelve ``(adv_type, node_type, node_type_label, can_repeat)``. Solo el
        repetidor puro implica ``can_repeat=True`` de forma inequívoca. En room
        o companion la capacidad de repetición depende de features del firmware
        y se mantiene como desconocida si no viene declarada.
        """
        try:
            value = int(raw_type)
        except Exception:
            value = None
        mapping = {
            0: ("unknown", "Desconocido", None),
            1: ("companion", "Companion", None),
            2: ("repeater", "Repetidor", True),
            3: ("room", "Room Server", None),
            4: ("sensor", "Sensor", False),
        }
        node_type, label, can_repeat = mapping.get(value, ("unknown", "Desconocido", None))
        return value, node_type, label, can_repeat

    @staticmethod
    def _meshcore_path_bytes(value) -> bytes:
        """Convierte ``out_path``/PATH_RESPONSE a bytes sin alterar su orden."""
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, (list, tuple)):
            try:
                return bytes(int(x) & 0xFF for x in value)
            except Exception:
                return b""
        if isinstance(value, dict):
            for key in ("path", "out_path", "path_hashes"):
                if key in value:
                    return MeshCoreEmbeddedBridge._meshcore_path_bytes(value.get(key))
            return b""
        text = str(value or "").strip()
        if not text:
            return b""
        compact = re.sub(r"[^0-9a-fA-F]", "", text)
        if compact and len(compact) % 2 == 0:
            try:
                return bytes.fromhex(compact)
            except Exception:
                pass
        try:
            return bytes(int(part.strip(), 0) & 0xFF for part in text.split(",") if part.strip())
        except Exception:
            return b""


    @staticmethod
    def _meshcore_path_geometry(path_value, hash_mode=None, path_len=None) -> tuple[bytes, int, int]:
        """Normaliza una ruta MeshCore y calcula ancho de hash y número de saltos.

        Parámetros:
            path_value:
                Ruta en bytes, hexadecimal, lista o estructura compatible.
            hash_mode:
                Modo MeshCore 0, 1 o 2; equivale a hashes de 1, 2 o 3 bytes.
            path_len:
                Número de saltos declarado por el firmware. Si es válido tiene
                prioridad sobre el cálculo derivado del tamaño en bytes.

        Devuelve:
            ``(path_bytes, hash_width, hop_count)``.

        Funcionalidad:
            - Conserva exactamente los bytes de ruta.
            - Normaliza el ancho a 1..3 bytes.
            - Evita confundir bytes con saltos cuando faltan ``out_path_len``.
        """
        path_bytes = MeshCoreEmbeddedBridge._meshcore_path_bytes(path_value)
        try:
            mode = int(hash_mode) if hash_mode is not None else 0
        except Exception:
            mode = 0
        hash_width = max(1, min(3, mode + 1))

        try:
            declared_len = int(path_len) if path_len is not None else -1
        except Exception:
            declared_len = -1

        if declared_len >= 0:
            hop_count = declared_len
        else:
            hop_count = len(path_bytes) // hash_width if path_bytes else 0

        return path_bytes, hash_width, hop_count

    def _meshcore_resolve_trace_hops(self, path_hashes, path_snrs, hash_width: int = 1) -> list[dict]:
        """Resuelve hashes de ruta contra contactos conocidos sin elegir colisiones."""
        raw_path = self._meshcore_path_bytes(path_hashes)
        try:
            hash_width = max(1, min(3, int(hash_width or 1)))
        except Exception:
            hash_width = 1
        snrs = list(path_snrs or []) if isinstance(path_snrs, (list, tuple, bytes, bytearray)) else []
        chunks = [raw_path[pos:pos + hash_width] for pos in range(0, len(raw_path), hash_width)]
        hops = []
        for idx, chunk_bytes in enumerate(chunks):
            chunk = bytes(chunk_bytes).hex()
            matches = self._mc_path_prefix_cache.get(chunk, [])
            contact = matches[0] if len(matches) == 1 else None
            hop = {
                "index": idx + 1,
                "hash": chunk,
                "resolved": bool(contact),
                "ambiguous": len(matches) > 1,
                "name": self._meshcore_contact_display(contact, chunk) if contact else chunk,
                "public_key": (contact or {}).get("public_key") if isinstance(contact, dict) else None,
                "lat": (contact or {}).get("lat") if isinstance(contact, dict) else None,
                "lon": (contact or {}).get("lon") if isinstance(contact, dict) else None,
                "snr": snrs[idx] if idx < len(snrs) else None,
            }
            if len(matches) > 1:
                hop["name"] = f"{chunk} (prefijo ambiguo: {len(matches)} contactos)"
            hops.append(hop)
        if len(snrs) > len(chunks):
            hops.append({
                "index": len(hops) + 1,
                "hash": "destination",
                "resolved": True,
                "ambiguous": False,
                "name": "Destino",
                "public_key": None,
                "lat": None,
                "lon": None,
                "snr": snrs[len(chunks)],
                "destination": True,
            })
        return hops

    def trace_contact(self, contact_prefix: str, *, discover: bool = False, timeout: float = 20.0) -> dict:
        """Consulta/descubre la ruta y ejecuta TRACE_DATA en el loop MeshCore.

        No abre otra conexión. El control del WebPanel llama a este método desde
        otro hilo y la corrutina se ejecuta mediante ``run_coroutine_threadsafe``.
        """
        prefix = str(contact_prefix or "").strip()
        if not prefix:
            raise ValueError("missing_contact_prefix")
        mc, loop = self._mc, self._loop
        if mc is None or loop is None or not loop.is_running() or not self._connected:
            raise RuntimeError("meshcore_not_connected")
        timeout = max(5.0, min(float(timeout or 20.0), 60.0))

        async def _run_trace():
            contact = None
            getter = getattr(mc, "get_contact_by_key_prefix", None)
            if callable(getter):
                contact = getter(prefix)
            if not isinstance(contact, dict):
                raise RuntimeError("meshcore_contact_not_found")
            self._meshcore_remember_contact(contact)

            commands = getattr(mc, "commands", None)
            if commands is None:
                raise RuntimeError("meshcore_commands_unavailable")

            path_bytes, hash_width, _ = self._meshcore_path_geometry(
                contact.get("out_path"),
                contact.get("out_path_hash_mode"),
                contact.get("out_path_len"),
            )
            discovery_payload = None
            if discover or not path_bytes:
                send_discovery_raw = getattr(commands, "_send_path_discovery_raw", None)
                send_discovery_sync = getattr(commands, "send_path_discovery_sync", None)
                send_discovery_legacy = getattr(commands, "send_path_discovery", None)
                wait_for_event = getattr(mc, "wait_for_event", None)
                event = None

                async def _discover_with_registered_wait(send_callable):
                    """Registra PATH_RESPONSE antes de emitir el descubrimiento.

                    ``meshcore_py.send_path_discovery_sync()`` registra la espera
                    después del envío. En companions rápidos la respuesta puede
                    procesarse durante el retorno del comando y perderse. Este
                    helper conserva el orden seguro espera -> envío -> respuesta,
                    cancela la tarea ante cualquier error y reutiliza el lock de
                    peticiones de la librería cuando está disponible.
                    """
                    if not callable(wait_for_event):
                        raise RuntimeError("meshcore_path_discovery_wait_unavailable")

                    async def _send_and_wait():
                        wait_task = asyncio.create_task(
                            wait_for_event(_MCEventType.PATH_RESPONSE, timeout=timeout)
                        )
                        try:
                            sent = await send_callable(contact)
                            if getattr(sent, "type", None) == _MCEventType.ERROR:
                                raise RuntimeError(
                                    "meshcore_path_discovery_error: "
                                    f"{getattr(sent, 'payload', None)}"
                                )
                            response = await wait_task
                            if response is None:
                                raise RuntimeError("meshcore_path_discovery_no_response")
                            return response
                        except Exception:
                            if not wait_task.done():
                                wait_task.cancel()
                            raise

                    request_lock = getattr(commands, "_mesh_request_lock", None)
                    if request_lock is not None and hasattr(request_lock, "__aenter__"):
                        async with request_lock:
                            return await _send_and_wait()
                    return await _send_and_wait()

                # Camino preferente: método raw sin warning, pero con la espera
                # registrada previamente y con el lock propio de meshcore_py.
                if callable(send_discovery_raw):
                    event = await _discover_with_registered_wait(send_discovery_raw)

                # Fallback para versiones donde solo existe la API síncrona.
                # Se usa únicamente cuando no está disponible el método raw.
                elif callable(send_discovery_sync):
                    event = await send_discovery_sync(
                        contact,
                        timeout=timeout,
                        min_timeout=min(timeout, 5.0),
                    )
                    if event is None:
                        raise RuntimeError("meshcore_path_discovery_no_response")
                    if getattr(event, "type", None) == _MCEventType.ERROR:
                        raise RuntimeError(
                            f"meshcore_path_discovery_error: {getattr(event, 'payload', None)}"
                        )

                # Último fallback para librerías antiguas. Puede emitir el warning
                # deprecado, pero conserva el orden seguro de la espera.
                elif callable(send_discovery_legacy):
                    event = await _discover_with_registered_wait(send_discovery_legacy)
                else:
                    raise RuntimeError("meshcore_path_discovery_api_unavailable")

                if event is not None:
                        discovery_payload = getattr(event, "payload", None)
                        if isinstance(discovery_payload, dict):
                            # ``meshcore_py`` publica PATH_RESPONSE con los
                            # nombres reales del protocolo: ``out_path``,
                            # ``out_path_len`` y ``out_path_hash_len``. Las
                            # versiones anteriores buscaban ``path`` y
                            # ``path_hash_mode``, por lo que descartaban una
                            # ruta descubierta válida y terminaban en
                            # ``meshcore_no_directed_path``.
                            candidate_value = discovery_payload.get("out_path")
                            candidate_len = discovery_payload.get("out_path_len")
                            hash_len_raw = discovery_payload.get("out_path_hash_len")

                            # Compatibilidad defensiva con implementaciones
                            # antiguas o forks que aún devuelvan ``path``.
                            if candidate_value is None:
                                candidate_value = discovery_payload.get("path")
                            if candidate_len is None:
                                candidate_len = discovery_payload.get("path_len")

                            if hash_len_raw is not None:
                                try:
                                    candidate_width = max(1, int(hash_len_raw))
                                except Exception:
                                    candidate_width = 1
                                candidate, _, _ = self._meshcore_path_geometry(
                                    candidate_value,
                                    candidate_width - 1,
                                    candidate_len,
                                )
                            else:
                                candidate, candidate_width, _ = self._meshcore_path_geometry(
                                    candidate_value,
                                    discovery_payload.get("path_hash_mode"),
                                    candidate_len,
                                )
                        else:
                            candidate, candidate_width, _ = self._meshcore_path_geometry(
                                discovery_payload
                            )
                        if candidate:
                            path_bytes = candidate
                            hash_width = candidate_width

            if not path_bytes:
                raise RuntimeError("meshcore_no_directed_path")

            send_trace = getattr(commands, "send_trace", None)
            wait_for_event = getattr(mc, "wait_for_event", None)
            if not callable(send_trace) or not callable(wait_for_event):
                raise RuntimeError("meshcore_trace_api_unavailable")

            tag = int.from_bytes(os.urandom(4), "little", signed=False)
            auth_code = int.from_bytes(os.urandom(4), "little", signed=False)

            # Registrar la espera ANTES del envío evita perder TRACE_DATA si la
            # respuesta se procesa durante el retorno de send_trace().
            wait_task = asyncio.create_task(
                wait_for_event(
                    _MCEventType.TRACE_DATA,
                    attribute_filters={"tag": tag},
                    timeout=timeout,
                )
            )
            # ``meshcore_py.send_trace`` solo admite la ruta como ``str``,
            # ``bytes`` o ``bytearray``. Pasar ``list(path_bytes)`` provoca el
            # error ``unsupported_path_type`` observado desde el WebPanel.
            #
            # Los dos bits inferiores de ``flags`` codifican el ancho de hash
            # usado por TRACE_DATA: 0=1 byte, 1=2 bytes, 2=4 bytes, 3=8 bytes.
            trace_flag_by_width = {1: 0, 2: 1, 4: 2, 8: 3}
            trace_flags = trace_flag_by_width.get(int(hash_width or 1))
            if trace_flags is None:
                if not wait_task.done():
                    wait_task.cancel()
                raise RuntimeError(
                    f"meshcore_trace_unsupported_hash_width: {hash_width}"
                )

            try:
                sent = await send_trace(auth_code, tag, trace_flags, bytes(path_bytes))
                if getattr(sent, "type", None) == _MCEventType.ERROR:
                    raise RuntimeError(
                        f"meshcore_trace_send_error: {getattr(sent, 'payload', None)}"
                    )
                event = await wait_task
            except Exception:
                if not wait_task.done():
                    wait_task.cancel()
                raise
            if event is None:
                raise TimeoutError("meshcore_trace_timeout")
            payload = getattr(event, "payload", None) or {}
            if not isinstance(payload, dict):
                payload = {"raw": str(payload)}
            # ``meshcore_py`` actual entrega TRACE_DATA en ``payload["path"]``
            # como una lista de nodos ``{"hash": ..., "snr": ...}`` y un
            # último elemento que contiene únicamente el SNR del destino. Se
            # conserva compatibilidad con payloads antiguos que expongan
            # ``path_hashes``/``path_snrs``.
            trace_nodes = payload.get("path")
            if isinstance(trace_nodes, list):
                trace_hash_parts = []
                trace_snr_values = []
                for node in trace_nodes:
                    if not isinstance(node, dict):
                        continue
                    node_hash = node.get("hash")
                    if node_hash not in (None, ""):
                        trace_hash_parts.append(str(node_hash))
                    if node.get("snr") is not None:
                        trace_snr_values.append(node.get("snr"))
                path_hashes = "".join(trace_hash_parts) if trace_hash_parts else path_bytes
                path_snrs = trace_snr_values
            else:
                path_hashes = payload.get("path_hashes", path_bytes)
                path_snrs = payload.get("path_snrs") or []
            return {
                "ok": True,
                "contact": {
                    "name": contact.get("adv_name") or contact.get("name"),
                    "public_key": contact.get("public_key"),
                    "prefix": prefix,
                },
                "tag": tag,
                "auth_code": auth_code,
                "path_hex": self._meshcore_path_bytes(path_hashes).hex(),
                "path_len": int(len(self._meshcore_path_bytes(path_hashes)) / max(1, hash_width)),
                "path_hash_width": hash_width,
                "path_snrs": list(path_snrs) if isinstance(path_snrs, (list, tuple, bytes, bytearray)) else [],
                "hops": self._meshcore_resolve_trace_hops(path_hashes, path_snrs, hash_width=hash_width),
                "discovery_used": bool(discover or discovery_payload is not None),
                "payload": payload,
                "ts": int(time.time()),
            }

        future = asyncio.run_coroutine_threadsafe(_run_trace(), loop)
        try:
            return future.result(timeout=timeout + 3.0)
        except Exception as e:
            raise RuntimeError(f"meshcore_trace_failed: {type(e).__name__}: {e}") from e

    def list_contacts(self, limit: int = 80) -> list[dict]:
        """
        Devuelve contactos conocidos por la sesión MeshCore embebida.

        La consulta se ejecuta en el mismo loop asyncio propietario de la
        sesión MeshCore. Esto evita usar desde el hilo del control sockets y
        futures creados en otro loop.
        """
        try:
            max_n = max(1, min(500, int(limit)))
        except Exception:
            max_n = 80

        mc = self._mc
        loop = self._loop
        if mc is None or loop is None or not loop.is_running() or not self._connected:
            raise RuntimeError("meshcore_not_connected")

        async def _fetch_contacts():
            commands = getattr(mc, "commands", None)
            get_contacts = getattr(commands, "get_contacts", None)
            if not callable(get_contacts):
                # Compatibilidad con versiones antiguas que exponían el cache
                # directamente en la instancia principal.
                legacy_get = getattr(mc, "get_contacts", None)
                if callable(legacy_get):
                    result = legacy_get()
                    return await result if asyncio.iscoroutine(result) else result
                return getattr(mc, "contacts", [])

            result = await get_contacts()
            if getattr(result, "type", None) == _MCEventType.ERROR:
                raise RuntimeError(f"meshcore_contacts_error: {getattr(result, 'payload', None)}")
            return getattr(result, "payload", result)

        try:
            future = asyncio.run_coroutine_threadsafe(_fetch_contacts(), loop)
            # El bot espera 8 s al control del broker. Dejamos margen para
            # serializar y devolver un error antes de que venza ese socket.
            items = future.result(timeout=7.0)
        except Exception as e:
            raise RuntimeError(f"meshcore_contacts_query_failed: {type(e).__name__}: {e}") from e

        # La API actual devuelve {public_key: contacto}. Conservamos la clave
        # del diccionario como fallback porque algunas versiones no repiten
        # public_key dentro del contacto.
        if isinstance(items, dict):
            normalized_items = []
            for item_key, item_value in items.items():
                if isinstance(item_value, dict):
                    item = dict(item_value)
                    item.setdefault("public_key", item_key)
                else:
                    item = item_value
                normalized_items.append(item)
            items = normalized_items

        out = []
        seen = set()
        for c in (items or []):
            try:
                if isinstance(c, dict):
                    public_key = c.get("public_key") or c.get("pubkey") or c.get("key")
                    # La API oficial de contactos devuelve public_key completa.
                    # Si alguna versión aporta un prefijo explícito, lo respetamos;
                    # si no, se calcula más abajo con public_key[:12].
                    display_prefix = c.get("pubkey_prefix") or c.get("key_prefix") or c.get("prefix")
                    contact_id = c.get("id")
                    name = c.get("name") or c.get("adv_name") or c.get("alias") or c.get("label")
                    last_seen = c.get("last_seen") or c.get("lastSeen") or c.get("last_advert") or c.get("seen") or c.get("ts")
                    adv_type_raw = c.get("adv_type") if c.get("adv_type") is not None else c.get("type")
                    flags = c.get("flags")
                    last_advert = c.get("last_advert")
                    lastmod = c.get("lastmod")
                    feat1 = c.get("feat1")
                    feat2 = c.get("feat2")
                else:
                    public_key = getattr(c, "public_key", None) or getattr(c, "pubkey", None) or getattr(c, "key", None)
                    display_prefix = getattr(c, "pubkey_prefix", None) or getattr(c, "key_prefix", None) or getattr(c, "prefix", None)
                    contact_id = getattr(c, "id", None)
                    name = getattr(c, "name", None) or getattr(c, "adv_name", None) or getattr(c, "alias", None) or getattr(c, "label", None)
                    last_seen = getattr(c, "last_seen", None) or getattr(c, "lastSeen", None) or getattr(c, "last_advert", None) or getattr(c, "seen", None)
                    adv_type_raw = getattr(c, "adv_type", None)
                    if adv_type_raw is None:
                        adv_type_raw = getattr(c, "type", None)
                    flags = getattr(c, "flags", None)
                    last_advert = getattr(c, "last_advert", None)
                    lastmod = getattr(c, "lastmod", None)
                    feat1 = getattr(c, "feat1", None)
                    feat2 = getattr(c, "feat2", None)

                display_id = (str(display_prefix).strip() if display_prefix is not None else "")
                contact_id = (str(contact_id).strip() if contact_id is not None else "")
                public_key = (str(public_key).strip() if public_key is not None else "")
                # Para DM, el prefix explícito u observado por RF es autoritativo.
                # public_key[:12] se conserva solo como fallback legacy.
                observed_prefix = self._meshcore_contact_prefix_by_name(name)
                dm_key = display_id or observed_prefix or (public_key[:12] if public_key else "") or contact_id
                display_id = display_id or dm_key or contact_id
                if not dm_key or dm_key in seen:
                    continue
                seen.add(dm_key)

                adv_type, node_type, node_type_label, can_repeat = self._meshcore_node_type(adv_type_raw)
                out_path_value = c.get("out_path") if isinstance(c, dict) else getattr(c, "out_path", None)
                out_path_mode = c.get("out_path_hash_mode") if isinstance(c, dict) else getattr(c, "out_path_hash_mode", None)
                out_path_len = c.get("out_path_len") if isinstance(c, dict) else getattr(c, "out_path_len", None)
                path_bytes, path_hash_width, path_hops = self._meshcore_path_geometry(
                    out_path_value,
                    out_path_mode,
                    out_path_len,
                )
                contact_out = {
                    "prefix": display_id,
                    "contact_id": contact_id or None,
                    "dm_key": dm_key,
                    "public_key": public_key or dm_key,
                    "name": (str(name).strip() if name is not None else "") or None,
                    "last_seen": int(last_seen) if isinstance(last_seen, (int, float)) else None,
                    "adv_type": adv_type,
                    "node_type": node_type,
                    "node_type_label": node_type_label,
                    "can_repeat": can_repeat,
                    "flags": flags,
                    "last_advert": last_advert,
                    "lastmod": lastmod,
                    "feat1": feat1,
                    "feat2": feat2,
                    "out_path_hex": path_bytes.hex(),
                    "out_path_hash_mode": out_path_mode,
                    "out_path_hash_width": path_hash_width,
                    "out_path_hops": path_hops,
                    "has_directed_path": bool(path_bytes),
                }
                for key in ("adv_lat", "adv_lon", "lat", "lon", "out_path"):
                    if isinstance(c, dict) and c.get(key) is not None:
                        contact_out[key] = c.get(key)
                self._meshcore_remember_contact(contact_out)
                out.append(contact_out)
                if len(out) >= max_n:
                    break
            except Exception:
                continue

        return out

    def list_channels(self, limit: int = 80) -> list[dict]:
        """
        Devuelve canales MeshCore conocidos por la sesión embebida.

        API real de meshcore>=2.2.28: CommandHandler.get_channel(channel_idx)
        devuelve EventType.CHANNEL_INFO con payload {channel_idx, channel_name,
        channel_secret, channel_hash}. No existe un get_channels() agregado, así
        que se consulta un rango acotado de índices y después se completa con
        MESHCORE_CHANNEL_MAP.
        """
        try:
            max_n = max(1, min(500, int(limit)))
        except Exception:
            max_n = 80
        try:
            scan_max = max(1, min(256, int(os.getenv("MESHCORE_CHANNEL_SCAN_MAX", "40") or "40")))
        except Exception:
            scan_max = 40

        channels_by_idx: dict[int, dict] = {}

        def _add_channel(channel_idx, name=None, role=None, source="api", channel_hash=None):
            try:
                idx = int(channel_idx)
            except Exception:
                return
            if idx < 0 or idx in channels_by_idx:
                return
            channels_by_idx[idx] = {
                "channel_idx": idx,
                "name": (str(name).strip() if name is not None else "") or None,
                "role": (str(role).strip() if role is not None else "") or None,
                "source": source,
                "channel_hash": (str(channel_hash).strip() if channel_hash is not None else "") or None,
            }

        mc = self._mc
        loop = self._loop
        if mc is not None and loop is not None and loop.is_running() and self._connected:
            async def _fetch_channels_by_index():
                commands = getattr(mc, "commands", None)
                get_channel = getattr(commands, "get_channel", None) if commands is not None else None
                if not callable(get_channel):
                    return []

                found = []
                for channel_idx in range(scan_max):
                    result = await get_channel(int(channel_idx))
                    if result is None or getattr(result, "type", None) == _MCEventType.ERROR:
                        continue
                    payload = getattr(result, "payload", None) or {}
                    if isinstance(payload, dict):
                        found.append(payload)
                return found

            try:
                future = asyncio.run_coroutine_threadsafe(_fetch_channels_by_index(), loop)
                items = future.result(timeout=7.0)
            except Exception:
                items = []

            for item in (items or []):
                try:
                    if not isinstance(item, dict):
                        continue
                    _add_channel(
                        item.get("channel_idx"),
                        name=item.get("channel_name"),
                        source="api:get_channel",
                        channel_hash=item.get("channel_hash"),
                    )
                except Exception:
                    continue

        for _ch, mapping in (self.ch_map or {}).items():
            try:
                if (mapping or {}).get("kind") != "chan":
                    continue
                _add_channel(
                    int((mapping or {}).get("channel_idx")),
                    name=(mapping or {}).get("tag"),
                    role=f"Meshtastic CH{int(_ch)}",
                    source="MESHCORE_CHANNEL_MAP",
                )
            except Exception:
                continue

        return list(channels_by_idx.values())[:max_n]

    def start(self) -> None:
        if not self.enable:
            if _env_truthy("MESHCORE_ENABLE", "0") and not _MESHCORE_AVAILABLE:
                print(f"[meshcore] ⚠️ MESHCORE_ENABLE=1 pero falta dependencia: {_MESHCORE_IMPORT_ERROR}", flush=True)
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._runner, name="meshcore-embedded", daemon=True)
        self._thread.start()
        direction_mode = (os.getenv("BRIDGE_DIRECTION_MODE") or "").strip().lower()
        if direction_mode == "meshcore_a_meshtastic_embedded_b":
            print(
                f"[bridge] habilitado A=MeshCore ({self.mode}) -> B=Meshtastic embebido en broker",
                flush=True,
            )
        else:
            print(f"[meshcore] embebido habilitado mode={self.mode}", flush=True)

    def is_healthy(self) -> bool:
        """
        Comprobación ligera de salud del bridge MeshCore embebido.

        Criterio:
        - habilitado
        - hilo supervisor vivo
        - no marcado para stop
        - objeto MeshCore creado
        - conexión marcada como activa

        No valida tráfico profundo; sirve para decidir si conviene reiniciar
        el backend tras recuperar la conexión principal A.
        """
        try:
            th = self._thread
            return bool(
                self.enable
                and th is not None
                and th.is_alive()
                and not self._stop.is_set()
                and self._mc is not None
                and bool(self._connected)
            )
        except Exception:
            return False

    def stop(self) -> None:
        self._stop.set()
        try:
            loop = self._loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(lambda: None)
        except Exception:
            pass
        th = self._thread
        if th and th.is_alive():
            try:
                th.join(timeout=2.0)
            except Exception:
                pass

    def _runner(self) -> None:
        try:
            asyncio.run(self._supervisor())
        except Exception as e:
            self._last_err = f"{type(e).__name__}: {e}"
            print(f"[meshcore] runner fatal: {self._last_err}", flush=True)

    async def _supervisor(self) -> None:
        """
        Supervisor 24/7:
        - Ejecuta sesiones de conexión (_amain_once).
        - Si cae por error/desconexión, reintenta con backoff.
        """
        backoff = [2, 5, 10, 20, 40, 60, 120]
        attempt = 0

        _orphan_q_after_break = None
        while not self._stop.is_set():
            try:
                await self._amain_once()
                attempt = 0  # sesión terminó "limpio"
            except Exception as e:
                self._last_err = f"{type(e).__name__}: {e}"
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                print(f"[meshcore-embedded] supervisor: {self._last_err} (reintento en {delay}s)", flush=True)

                # Sleep cooperativo cancelable
                for _ in range(int(delay * 10)):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(0.1)


    async def _connect(self):
        if self.mode == "tcp":
            if not self.tcp_host:
                raise RuntimeError("MESHCORE_TCP_HOST vacío")
            return await _MeshCore.create_tcp(self.tcp_host, int(self.tcp_port), auto_reconnect=True)  # type: ignore[attr-defined]
        if self.mode == "ble":
            if not self.ble_addr:
                raise RuntimeError("MESHCORE_BLE_ADDR vacío")
            if self.ble_pin:
                return await _MeshCore.create_ble(self.ble_addr, pin=str(self.ble_pin))  # type: ignore[attr-defined]
            return await _MeshCore.create_ble(self.ble_addr)  # type: ignore[attr-defined]
        # serial
        if not self.serial_port:
            raise RuntimeError("MESHCORE_SERIAL_PORT vacío")
        return await _MeshCore.create_serial(self.serial_port, int(self.serial_baud), debug=False)  # type: ignore[attr-defined]

    async def _amain_once(self) -> None:
        """
        Una sesión de conexión MeshCore.
        Si cae la conexión o hay error, esta corrutina termina y el supervisor reintenta.
        """
        import asyncio as _aio

        self._loop = _aio.get_running_loop()
        self._tx_q = _aio.Queue()

        # --- conectar ---
        print(f"[meshcore-embedded] CONNECTING mode={self.mode}", flush=True)
        self._mc = await self._connect()
        self._connected = True
        self._last_ok = time.time()
        self._last_err = ""
        print("[meshcore-embedded] CONNECTED", flush=True)

        mc = self._mc

        # Reinyecta retries/pendientes persistidos SOLO tras conexión exitosa.
        # Si _connect() falla, el spool debe permanecer intacto para próximos intentos.
        try:
            with self._retry_spool_lock:
                pending_retry = list(self._retry_spool)
                self._retry_spool.clear()
            for _item in pending_retry:
                _norm_item = self._normalize_tx_spool_item(_item)
                if _norm_item is not None:
                    self._tx_q.put_nowait(_norm_item)
            if pending_retry:
                print(f"[meshcore-embedded] retries restaurados tras reconexión: {len(pending_retry)}", flush=True)
        except Exception:
            pass

        # Cargar contactos cuanto antes: la API expone nombre y posición en la
        # libreta de contactos; se usa para resolver emisor y repetidores.
        try:
            ensure_contacts = getattr(mc, "ensure_contacts", None)
            if callable(ensure_contacts):
                await ensure_contacts(follow=True)
                for _contact in (getattr(mc, "contacts", {}) or {}).values():
                    self._meshcore_remember_contact(_contact)
        except Exception as e:
            print(f"[meshcore-embedded] contacts preload WARN: {type(e).__name__}: {e}", flush=True)

        async def _on_contact_event(event):
            try:
                payload = getattr(event, "payload", None)
                if isinstance(payload, dict) and all(isinstance(v, dict) for v in payload.values()):
                    for _contact in payload.values():
                        self._meshcore_remember_contact(_contact)
                elif isinstance(payload, dict):
                    self._meshcore_remember_contact(payload)
            except Exception:
                pass

        # --- activar auto-fetch (CRÍTICO para que entren eventos RX) ---
        try:
            await mc.start_auto_message_fetching()  # type: ignore[union-attr]
            print("[meshcore-embedded] auto_message_fetching ON", flush=True)
        except Exception as e:
            print(f"[meshcore-embedded] auto_message_fetching ERROR: {type(e).__name__}: {e}", flush=True)

        async def _on_msg(event):
            try:
                et = getattr(event, "type", None)
                data = dict(getattr(event, "payload", None) or {})
                kind = "contact"
                chan_idx = None
                if et == _MCEventType.CHANNEL_MSG_RECV:  # type: ignore[union-attr]
                    kind = "chan"
                    try:
                        chan_idx = int(data.get("channel_idx"))
                    except Exception:
                        chan_idx = None

                text_msg = str(data.get("text") or "").strip()
                if not text_msg:
                    return

                # Enriquecer con contactos/posiciones conocidos antes de formatear ruta.
                try:
                    data = self._meshcore_enrich_path_info(data)
                except Exception:
                    pass

                # === [LOG RX MeshCore] ===
                try:
                    print(
                        f"[meshcore-embedded RX] "
                        f"type={et} kind={kind} chan_idx={chan_idx} "
                        f"prefix={data.get('pubkey_prefix')} "
                        f"from={data.get('from_name') or ''} "
                        f"text='{text_msg[:120]}' "
                        f"path={_meshcore_format_repeater_path(data)}",
                        flush=True
                    )
                except Exception:
                    pass

                # Decide canal Meshtastic destino
                ch_out = None
                if kind == "chan" and chan_idx is not None:
                    ch_out = self.chanidx_to_ch.get(int(chan_idx))
                if ch_out is None:
                    pref = str(data.get("pubkey_prefix") or "").strip()
                    ch_out = self.contact_to_ch.get(pref)
                if ch_out is None:
                    ch_out = int(self.default_contact_ch)

                # Prefijo corto para identificar origen MeshCore
                pref = str(data.get("pubkey_prefix") or "").strip()

                # Alias: 1) si viene en payload (no siempre), 2) mapping por pubkey_prefix,
                # 3) heurística: si el texto viene como "EA2FBO_V4: ..." usarlo.
                alias = str(data.get("alias") or data.get("name") or "").strip()
                if not alias and pref:
                    alias = str(self.contact_aliases.get(pref) or "").strip()

                # Si el texto lleva "ALIAS: ...", extrae alias y limpia cuerpo (evita duplicar).
                # En algunos CHANNEL_MSG_RECV MeshCore no llega pubkey_prefix; en ese caso
                # resolvemos el alias visual contra contactos/aliases para poder responder por DM.
                try:
                    from bbs_transport import unwrap_meshcore_sender_prefix

                    extracted, rest = unwrap_meshcore_sender_prefix(text_msg)
                    if extracted:
                        if not alias:
                            alias = extracted
                        if not pref:
                            pref = self._meshcore_contact_prefix_by_name(extracted)
                        text_msg = rest
                except Exception:
                    pass

                # Conserva en memoria el prefijo DM observado realmente por RF
                # junto a su alias. No reemplaza configuraciones explícitas.
                if pref and alias and pref not in self.contact_aliases:
                    self.contact_aliases[pref] = alias

                # === [BBS] Comandos recibidos directamente por MeshCore ======
                # El motor BBS es común a ambas radios. Se responde por el mismo
                # transporte de entrada y se conserva la política Meshtastic:
                # DM, canales autorizados, direccionamiento multi-BBS y DM_ONLY.
                if text_msg.upper().startswith("#BBS"):
                    try:
                        from bbs_transport import handle_bbs_transport_command

                        bbs = globals().get("BBS_ENGINE")
                        raw_allowed = (
                            os.getenv("BBS_MESHCORE_CHANNELS")
                            or os.getenv("BBS_CHANNELS")
                            or os.getenv("BBS_CHANNEL")
                            or ""
                        )
                        allowed_channels = set()
                        for item in raw_allowed.split(","):
                            try:
                                allowed_channels.add(int(item.strip()))
                            except (TypeError, ValueError):
                                continue

                        replies = handle_bbs_transport_command(
                            engine=bbs,
                            text=text_msg,
                            source_id=pref,
                            channel=chan_idx,
                            is_direct=(kind == "contact"),
                            bbs_callsign=(
                                os.getenv("BBS_CALLSIGN")
                                or getattr(bbs, "bbs_callsign", "")
                            ),
                            allowed_channels=allowed_channels,
                            dm_channel=int(os.getenv("BBS_DM_CHANNEL", "0") or "0"),
                            dm_only=_env_truthy("BBS_DM_ONLY", "1"),
                            dm_init_hint=_env_truthy("BBS_DM_INIT_HINT", "1"),
                        )
                        for reply in replies or ():
                            if reply.direct:
                                self.enqueue_send_contact(pref, reply.text)
                            else:
                                self.enqueue_send_channel(reply.channel, reply.text)
                        if bbs is None:
                            print(
                                "[BBS] MeshCore: comando consumido; motor BBS desactivado",
                                flush=True,
                            )
                        self._last_ok = time.time()
                        # Nunca dejar que un comando BBS recibido por MeshCore
                        # caiga al flujo normal, que lo inyectaría en Meshtastic.
                        return
                    except Exception as _e_bbs:
                        self._last_err = f"bbs_meshcore: {type(_e_bbs).__name__}: {_e_bbs}"
                        print(f"[BBS] MeshCore WARN: {self._last_err}", flush=True)
                        return

                # === Respuesta automática propiedad del broker ==============
                # Se encola, nunca se transmite directamente desde el callback RX.
                # Así comparte la conexión persistente, backoff y circuit breaker.
                if kind == "chan" and chan_idx is not None:
                    try:
                        reply = AUTO_REPLY.reply_for("meshcore", int(chan_idx), text_msg)
                        if reply is not None:
                            self.enqueue_send_channel(int(chan_idx), reply)
                    except Exception as _e_auto_reply:
                        print(
                            f"[auto-reply] MeshCore ch={chan_idx} ERROR: "
                            f"{type(_e_auto_reply).__name__}: {_e_auto_reply}",
                            flush=True,
                        )

                # === [FARMACIAS] Comando interno MeshCore =======================
                # Los mensajes de contacto y de canal se aceptan; la respuesta se
                # encola siempre como contacto directo al pubkey_prefix de origen.
                try:
                    from farmacias_commands import (
                        FarmaciasCommandContext,
                        handle_farmacias_command,
                        is_allowed_origin,
                        is_farmacias_command,
                    )
                    if is_farmacias_command(text_msg):
                        _ctx_farma = FarmaciasCommandContext(
                            network="meshcore",
                            source_id=pref,
                            text=text_msg,
                            channel=chan_idx,
                            is_direct=(kind == "contact"),
                            packet_id=data.get("id") or data.get("message_id") or data.get("timestamp"),
                        )
                        if is_allowed_origin(_ctx_farma):
                            if pref:
                                def _farma_meshcore_worker():
                                    def _enqueue_dm(_message: str) -> None:
                                        self.enqueue_send_contact(pref, str(_message))
                                    handle_farmacias_command(_ctx_farma, _enqueue_dm)

                                threading.Thread(
                                    target=_farma_meshcore_worker,
                                    name="farmacias-meshcore",
                                    daemon=True,
                                ).start()
                                self._last_ok = time.time()
                                return
                            print(
                                "[farmacias] meshcore WARN: comando de canal sin pubkey_prefix resoluble; "
                                "se deja pasar al listener externo",
                                flush=True,
                            )
                except Exception as _e_farma:
                    print(f"[farmacias] meshcore WARN: {type(_e_farma).__name__}: {_e_farma}", flush=True)

                # === [EMERGENCIAS] Comando interno MeshCore ====================
                # La API local solo consulta su caché; la respuesta vuelve siempre
                # como DM al contacto de origen y nunca abre otra conexión de radio.
                try:
                    from emergencias_commands import (
                        EmergenciasCommandContext,
                        handle_emergencias_command,
                        is_allowed_origin as is_emergencias_allowed_origin,
                        is_emergencias_command,
                    )
                    if is_emergencias_command(text_msg):
                        _ctx_emerg = EmergenciasCommandContext(
                            network="meshcore",
                            source_id=pref,
                            text=text_msg,
                            channel=chan_idx,
                            is_direct=(kind == "contact"),
                            packet_id=data.get("id") or data.get("message_id") or data.get("timestamp"),
                        )
                        if is_emergencias_allowed_origin(_ctx_emerg):
                            if pref:
                                def _emergencias_meshcore_worker():
                                    def _enqueue_dm(_message: str) -> None:
                                        self.enqueue_send_contact(pref, str(_message))
                                    handle_emergencias_command(_ctx_emerg, _enqueue_dm)

                                threading.Thread(
                                    target=_emergencias_meshcore_worker,
                                    name="emergencias-meshcore",
                                    daemon=True,
                                ).start()
                                self._last_ok = time.time()
                                return
                            print(
                                "[emergencias] meshcore WARN: comando de canal sin "
                                "pubkey_prefix resoluble; se deja pasar",
                                flush=True,
                            )
                except Exception as _e_emerg:
                    print(
                        f"[emergencias] meshcore WARN: "
                        f"{type(_e_emerg).__name__}: {_e_emerg}",
                        flush=True,
                    )

                try:
                    mail_reply = _handle_mesh_mail_command_if_needed(text_msg, source=(alias or pref or "meshcore"))
                    if mail_reply is not None:
                        self.enqueue_send_channel(chan_idx if chan_idx is not None else 0, mail_reply)
                        self._last_ok = time.time()
                        return
                except Exception as _e_mail:
                    try:
                        self.enqueue_send_channel(chan_idx if chan_idx is not None else 0, f"Error correo: {_e_mail}")
                    except Exception:
                        pass
                    self._last_err = f"mail: {type(_e_mail).__name__}: {_e_mail}"
                    return

                head = self._meshcore_rx_head(
                    kind=kind,
                    chan_idx=chan_idx,
                    pubkey_prefix=pref,
                    alias=alias
                )

                out_txt = f"{head} {text_msg}"
                mc_chan_tag = None
                if kind == "chan" and chan_idx is not None:
                    try:
                        mc_chan_tag = (self.chanidx_to_tag or {}).get(int(chan_idx))
                    except Exception:
                        mc_chan_tag = None


                # Inyectar a Meshtastic vía cola del broker (SENDQ), salvo en
                # RADIO_PROFILE=meshcore_only, donde Meshtastic está completamente OFF.
                q = globals().get("SENDQ")
                if _is_meshcore_only_profile():
                    emit_meshcore_rx_to_hub_and_log(
                        ch=int(ch_out),
                        text=out_txt,
                        pubkey_prefix=pref,
                        kind=kind,
                        chan_idx=chan_idx,
                        chan_tag=(mc_chan_tag or None),
                        from_alias=(alias or None),
                        path_info=data,
                    )
                    self._last_ok = time.time()
                    return

                if q is not None and hasattr(q, "offer"):
                    self._remember_injected(int(ch_out), out_txt)
                    q.offer(
                        {
                            "channel": int(ch_out),
                            "text": out_txt,
                            "destination": None,
                            "require_ack": False,
                            "type": "text",
                            "no_bridge": True,
                            "origin": "meshcore",
                            "meta": {"meshcore": 1, "pubkey_prefix": pref, "kind": kind, "channel_idx": chan_idx},
                        },
                        coalesce=False,
                    )

                    # MeshCore->BOT: emitir también al bus JSONL/backlog para que el bot lo vea
                    emit_meshcore_rx_to_hub_and_log(
                        ch=int(ch_out),
                        text=out_txt,
                        pubkey_prefix=pref,
                        kind=kind,
                        chan_idx=chan_idx,
                        chan_tag=(mc_chan_tag or None),
                        from_alias=(alias or None),
                        path_info=data,
                    )


                    # === [IMPORTANTE] Replicar también a HOME_NODE_ID (nodo A) si se pide.
                    # Motivo: cuando el bot está conectado, quieres ver igualmente los mensajes inyectados
                    # como DM en tu nodo A (HOME).
                    try:
                        if _env_truthy("MESHCORE_ECHO_TO_HOME", "0") and HOME_NODE_ID:
                            self._remember_injected(int(ch_out), out_txt)
                            q.offer(
                                {
                                    "channel": int(ch_out),
                                    "text": out_txt,
                                    "destination": str(HOME_NODE_ID),
                                    "require_ack": False,
                                    "type": "text",
                                    "no_bridge": True,
                                    "origin": "meshcore",
                                    "meta": {"meshcore": 1, "echo_home": 1, "pubkey_prefix": pref, "kind": kind, "channel_idx": chan_idx},
                                },
                                coalesce=False,
                            )
                    except Exception:
                        pass
                    self._last_ok = time.time()

            except Exception as e:
                self._last_err = f"{type(e).__name__}: {e}"

        # Suscripción RX
        try:
            for _evt in ("CONTACTS", "NEW_CONTACT", "ADVERTISEMENT", "PATH_UPDATE"):
                try:
                    mc.subscribe(getattr(_MCEventType, _evt), _on_contact_event)  # type: ignore[union-attr]
                except Exception:
                    pass
            mc.subscribe(_MCEventType.CONTACT_MSG_RECV, _on_msg)  # type: ignore[union-attr]
            mc.subscribe(_MCEventType.CHANNEL_MSG_RECV, _on_msg)  # type: ignore[union-attr]
        except Exception as e:
            self._last_err = f"subscribe: {type(e).__name__}: {e}"
            print(f"[meshcore-embedded] {self._last_err}", flush=True)

        # --- bucle TX ---
        _orphan_q_after_break = None
        while not self._stop.is_set():
            try:
                item = await _aio.wait_for(self._tx_q.get(), timeout=0.5)
            except _aio.TimeoutError:
                continue

            normalized_item = self._normalize_tx_spool_item(item)
            if normalized_item is None:
                continue
            dst, msg, retry_count, item_max_retries, tx_id, send_parts, next_part = normalized_item
            failed_part = next_part

            try:
                max_b = _safe_meshcore_max_text_bytes()
                # Las partes se calculan una sola vez al encolar y se conservan en
                # el spool junto con el índice pendiente durante las reconexiones.
                send_parts = list(send_parts) if send_parts else _split_meshcore_send_parts(msg, max_b)
                next_part = max(0, min(int(next_part), len(send_parts)))

                # === [LOG TX MeshCore] ===
                # text_preview es solo una vista previa de log; no es el texto completo enviado.
                try:
                    msg_text = str(msg or "")
                    msg_bytes = len(msg_text.encode("utf-8", errors="ignore"))
                    preview_limit = 120
                    preview_truncated = len(msg_text) > preview_limit
                    preview = msg_text[:preview_limit] + ("…" if preview_truncated else "")
                    print(
                        f"[meshcore-embedded TX] tx_id={tx_id} dst={dst} retry={retry_count} "
                        f"chars={len(msg_text)} bytes={msg_bytes} max_part_bytes={max_b} "
                        f"parts={len(send_parts)} next_part={next_part + 1} preview_chars={preview_limit} "
                        f"preview_truncated={preview_truncated} text_preview='{preview}'",
                        flush=True,
                    )
                except Exception:
                    pass

                part_delay_sec = _safe_meshcore_part_delay_sec() if len(send_parts) > 1 else 0.0

                # Enviar secuencialmente, pausando entre partes para evitar ráfagas.
                for part_pos in range(next_part, len(send_parts)):
                    p = send_parts[part_pos]
                    failed_part = part_pos
                    print(
                        f"[meshcore-embedded TX PART] tx_id={tx_id} "
                        f"part={part_pos + 1}/{len(send_parts)} retry={retry_count} "
                        f"bytes={len(p.encode('utf-8', errors='ignore'))}",
                        flush=True,
                    )
                    result = None
                    if isinstance(dst, dict) and str(dst.get("kind") or "").lower() in ("chan", "channel"):
                        chan_idx = int(dst.get("channel_idx"))
                        result = await mc.commands.send_chan_msg(int(chan_idx), p)  # type: ignore[union-attr]
                    else:
                        send_dst = dst
                        if isinstance(send_dst, str):
                            try:
                                c = mc.get_contact_by_key_prefix(send_dst)  # type: ignore[union-attr]
                                if c:
                                    send_dst = c
                            except Exception:
                                pass
                        result = await mc.commands.send_msg(send_dst, p)  # type: ignore[union-attr]

                    # Algunos cortes de enlace dejan una "conexión zombie":
                    # la llamada no lanza excepción pero devuelve ERROR.
                    # Si lo detectamos, forzamos reconexión limpia del engine.
                    try:
                        if getattr(result, "type", None) == _MCEventType.ERROR:  # type: ignore[union-attr]
                            raise RuntimeError(f"meshcore_tx_error: {getattr(result, 'payload', None)}")
                    except Exception:
                        raise
                    next_part = part_pos + 1
                    print(
                        f"[meshcore-embedded TX OK] tx_id={tx_id} "
                        f"part={part_pos + 1}/{len(send_parts)} retry={retry_count}",
                        flush=True,
                    )

                    if part_delay_sec > 0 and part_pos < len(send_parts) - 1:
                        try:
                            await _aio.sleep(part_delay_sec)
                        except Exception:
                            pass
                    else:
                        try:
                            await _aio.sleep(0.15)
                        except Exception:
                            pass

                self._last_ok = time.time()

            except Exception as e:
                self._last_err = f"tx: {type(e).__name__}: {e}"
                # Marcar desconectado ANTES de drenar para que cualquier enqueue
                # concurrente vaya al spool (y no a una _tx_q efímera).
                self._connected = False
                # Despublicar la cola de sesión para que nuevos enqueues no apunten aquí.
                _old_q = self._tx_q
                self._tx_q = None
                if retry_count < item_max_retries:
                    next_retry = retry_count + 1
                    try:
                        self._spool_append(
                            (dst, msg, next_retry, item_max_retries, tx_id, tuple(send_parts), failed_part),
                            why="tx_retry",
                        )
                        print(
                            f"[meshcore-embedded] TX persistido tx_id={tx_id} "
                            f"part={failed_part + 1}/{len(send_parts)} retry={next_retry} "
                            f"tras reconexión max={item_max_retries}",
                            flush=True,
                        )
                    except Exception:
                        pass

                    # Pausa corta antes de romper sesión/reconectar.
                    # Evita bucles agresivos cuando MeshCore devuelve no_event_received repetidamente.
                    try:
                        await _aio.sleep(float(self._tx_retry_backoff_sec))
                    except Exception:
                        pass
                else:
                    try:
                        print(
                            f"[meshcore-embedded] TX descartado tras agotar retries "
                            f"retry={retry_count} max={item_max_retries} "
                            f"error={self._last_err}",
                            flush=True,
                        )
                    except Exception:
                        pass


                # Preservar también el resto de pendientes de la cola actual
                # para evitar pérdida bajo ráfagas + reconexión.
                try:
                    if _old_q is not None:
                        # Drenado "cuasi-atómico" con ventana corta de estabilización:
                        # captura callbacks call_soon_threadsafe ya en vuelo.
                        _idle_rounds = 0
                        _max_rounds = 20  # ~400 ms (20 * 20ms)
                        for _ in range(_max_rounds):
                            _moved = 0
                            while True:
                                try:
                                    _pending = _old_q.get_nowait()
                                except _aio.QueueEmpty:
                                    break
                                _norm_pending = self._normalize_tx_spool_item(_pending)
                                if _norm_pending is not None:
                                    self._spool_append(_norm_pending, why="drain_old_q")
                                    _moved += 1
                            if _moved == 0:
                                _idle_rounds += 1
                            else:
                                _idle_rounds = 0
                            if _idle_rounds >= 2:
                                break
                            await _aio.sleep(0.02)
                except Exception:
                    pass
                _orphan_q_after_break = _old_q
                print(f"[meshcore-embedded] TX ERROR -> reconexión: {self._last_err}", flush=True)
                break

        # --- desconexión ---
        try:
            await mc.disconnect()  # type: ignore[union-attr]
        except Exception:
            pass
        # Último drenado por si entraron callbacks tardíos en la cola huérfana
        # mientras hacíamos disconnect().
        try:
            if _orphan_q_after_break is not None:
                for _ in range(3):
                    moved = 0
                    while True:
                        try:
                            _pending = _orphan_q_after_break.get_nowait()
                        except _aio.QueueEmpty:
                            break
                        _norm_pending = self._normalize_tx_spool_item(_pending)
                        if _norm_pending is not None:
                            self._spool_append(_norm_pending, why="drain_orphan_q")
                            moved += 1
                    if moved == 0:
                        await _aio.sleep(0)
                    else:
                        await _aio.sleep(0.01)
        except Exception:
            pass
        self._connected = False
        print("[meshcore-embedded] DISCONNECTED", flush=True)

    def _meshcore_rx_head(
        self,
        *,
        kind: str,
        chan_idx: Optional[int],
        pubkey_prefix: str,
        alias: str
    ) -> str:
        """
        Prefijo para mensajes RX desde MeshCore.
        Prioriza alias recibido en la trama.
        """

        style = (self.rx_prefix_style or "tech").strip().lower()
        prefix = (pubkey_prefix or "").strip()
        alias = (alias or "").strip()

        # Resolver canal lógico
        logical_tag = None
        if kind == "chan" and chan_idx is not None:
            try:
                logical_tag = (self.chanidx_to_tag or {}).get(int(chan_idx))
            except Exception:
                logical_tag = None

        # Compacto
        if style == "compact":
            return "[MC]"

        # Canal puro
        if style == "channel":
            if logical_tag:
                return f"[MC-{logical_tag}]"
            if kind == "contact":
                return "[MC-DM]"
            return f"[MC-CHAN{chan_idx}]" if chan_idx is not None else "[MC]"

        # Alias estructurado
        if style == "alias":

            # Si MeshCore envía alias, usarlo
            display = alias if alias else (prefix if prefix else "UNKNOWN")

            if logical_tag:
                return f"[MC:{logical_tag}:{display}]"

            if kind == "contact":
                return f"[MC:DM:{display}]"

            return f"[MC:{display}]"

        # Técnico (default)
        if prefix:
            return f"[MC:{prefix}]"
        return "[MC]"

    def _spool_append(self, item: tuple, *, why: str = "") -> None:
        """
        Inserta en spool persistente con límite de tamaño para evitar OOM
        durante desconexiones prolongadas.
        """
        with self._retry_spool_lock:
            was_full = (len(self._retry_spool) >= self._retry_spool_max)
            self._retry_spool.append(item)
            if was_full:
                self._retry_spool_drop_count += 1
                # Log limitado: cada 100 drops para no inundar consola.
                if (self._retry_spool_drop_count % 100) == 1:
                    rsn = f" reason={why}" if why else ""
                    print(
                        f"[meshcore] ⚠️ retry_spool lleno (max={self._retry_spool_max}), "
                        f"drop_oldest total={self._retry_spool_drop_count}{rsn}",
                        flush=True,
                    )

    def _normalize_tx_spool_item(
        self,
        item: object,
        default_max_retries: int | None = None,
    ) -> tuple[object, str, int, int, str, tuple[str, ...], int] | None:
        """Normaliza entradas legacy y conserva partes/posición de los retries."""
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            return None
        try:
            dst = item[0]
            msg = str(item[1] or "")
            retry_count = int(item[2] or 0) if len(item) >= 3 else 0
            if len(item) >= 4 and item[3] is not None:
                max_retries = max(0, int(item[3]))
            elif default_max_retries is not None:
                max_retries = max(0, int(default_max_retries))
            else:
                max_retries = max(0, int(self._tx_max_retries))
            tx_id = str(item[4] or "") if len(item) >= 5 else ""
            if not tx_id:
                tx_id = hashlib.sha1(
                    f"{time.time()}|legacy|{dst}|{msg}".encode("utf-8", errors="ignore")
                ).hexdigest()[:12]
            raw_parts = item[5] if len(item) >= 6 else None
            if isinstance(raw_parts, (tuple, list)) and raw_parts:
                send_parts = tuple(str(part) for part in raw_parts)
            else:
                send_parts = tuple(_split_meshcore_send_parts(msg, _safe_meshcore_max_text_bytes()))
            next_part = int(item[6] or 0) if len(item) >= 7 else 0
            next_part = max(0, min(next_part, len(send_parts)))
            return (dst, msg, retry_count, max_retries, tx_id, send_parts, next_part)
        except Exception:
            return None


    def enqueue_send_contact(self, contact_prefix: str, text: str, tx_id: str | None = None, max_retries: int | None = None) -> str | None:
        """
        Encola un TX MeshCore hacia contacto/DM.

        Uso:
            tx_id = self.enqueue_send_contact(contact_prefix, text)

        Parámetros:
            contact_prefix:
                Prefijo de clave pública/contacto MeshCore.
            text:
                Texto a transmitir.
            tx_id:
                Identificador opcional de trazabilidad. Si no se indica, se genera uno.
            max_retries:
                Reintentos máximos para este TX. Si es None, usa MESHCORE_TX_MAX_RETRIES.

        Funcionalidad:
            - Si MeshCore está conectado, encola en la cola activa.
            - Si MeshCore no está sano o está reconectando, persiste en retry_spool.
            - Devuelve tx_id para que MESHCORE_SEND pueda responder al cliente.
            - No confirma TX físico; solo confirma encolado/persistencia.
        """
        if not self.enable:
            return None

        msg = (text or "").strip()
        if not msg:
            return None

        tx_id = (
            tx_id
            or hashlib.sha1(
                f"{time.time()}|contact|{contact_prefix}|{msg}".encode("utf-8", errors="ignore")
            ).hexdigest()[:12]
        )

        try:
            loop = None
            tx_q = None
            with self._retry_spool_lock:
                healthy = bool(self._connected)
                loop = self._loop
                tx_q = self._tx_q

            item_max_retries = self._tx_max_retries if max_retries is None else max(0, int(max_retries))
            send_parts = tuple(_split_meshcore_send_parts(msg, _safe_meshcore_max_text_bytes()))
            item = (str(contact_prefix), msg, 0, item_max_retries, tx_id, send_parts, 0)

            if (not healthy) or (not loop) or (not tx_q):
                self._spool_append(item, why="enqueue_contact_deferred")
                if self.log_enqueue:
                    print(
                        f"[meshcore] enqueue deferred -> contact={str(contact_prefix)} "
                        f"tx_id={tx_id} (sesión no activa)",
                        flush=True,
                    )
                return tx_id

            if self.log_enqueue:
                try:
                    n = len(msg.encode("utf-8", errors="ignore"))
                except Exception:
                    n = len(msg)
                print(
                    f"[meshcore] enqueue -> contact={str(contact_prefix)} len={n} tx_id={tx_id}",
                    flush=True,
                )

            loop.call_soon_threadsafe(tx_q.put_nowait, item)
            return tx_id

        except Exception:
            try:
                item_max_retries = self._tx_max_retries if max_retries is None else max(0, int(max_retries))
                send_parts = tuple(_split_meshcore_send_parts(msg, _safe_meshcore_max_text_bytes()))
                self._spool_append(
                    (str(contact_prefix), msg, 0, item_max_retries, tx_id, send_parts, 0),
                    why="enqueue_contact_fallback",
                )
                return tx_id
            except Exception:
                return None

   
    def enqueue_send_channel(self, channel_idx: int, text: str, tx_id: str | None = None, max_retries: int | None = None) -> str | None:
        """
        Encola un TX MeshCore hacia un canal MeshCore.

        Uso:
            tx_id = self.enqueue_send_channel(channel_idx, text)

        Parámetros:
            channel_idx:
                Índice real del canal MeshCore.
            text:
                Texto a transmitir.
            tx_id:
                Identificador opcional de trazabilidad. Si no se indica, se genera uno.
            max_retries:
                Reintentos máximos para este TX. Si es None, usa MESHCORE_TX_MAX_RETRIES.

        Funcionalidad:
            - Si MeshCore está conectado, encola en la cola activa.
            - Si MeshCore no está sano o está reconectando, persiste en retry_spool.
            - Devuelve tx_id para que MESHCORE_SEND pueda responder al cliente.
            - No confirma TX físico; solo confirma encolado/persistencia.
        """
        if not self.enable:
            return None

        msg = (text or "").strip()
        if not msg:
            return None

        tx_id = (
            tx_id
            or hashlib.sha1(
                f"{time.time()}|chan|{channel_idx}|{msg}".encode("utf-8", errors="ignore")
            ).hexdigest()[:12]
        )

        try:
            loop = None
            tx_q = None
            with self._retry_spool_lock:
                healthy = bool(self._connected)
                loop = self._loop
                tx_q = self._tx_q

            dst = {"kind": "chan", "channel_idx": int(channel_idx)}
            item_max_retries = self._tx_max_retries if max_retries is None else max(0, int(max_retries))
            send_parts = tuple(_split_meshcore_send_parts(msg, _safe_meshcore_max_text_bytes()))
            item = (dst, msg, 0, item_max_retries, tx_id, send_parts, 0)

            if (not healthy) or (not loop) or (not tx_q):
                self._spool_append(item, why="enqueue_chan_deferred")
                if self.log_enqueue:
                    print(
                        f"[meshcore] enqueue deferred -> chan_idx={int(channel_idx)} "
                        f"tx_id={tx_id} (sesión no activa)",
                        flush=True,
                    )
                return tx_id

            if self.log_enqueue:
                try:
                    n = len(msg.encode("utf-8", errors="ignore"))
                except Exception:
                    n = len(msg)
                print(
                    f"[meshcore] enqueue -> chan_idx={int(channel_idx)} len={n} tx_id={tx_id}",
                    flush=True,
                )

            loop.call_soon_threadsafe(tx_q.put_nowait, item)
            return tx_id

        except Exception:
            try:
                item_max_retries = self._tx_max_retries if max_retries is None else max(0, int(max_retries))
                send_parts = tuple(_split_meshcore_send_parts(msg, _safe_meshcore_max_text_bytes()))
                self._spool_append(
                    (
                        {"kind": "chan", "channel_idx": int(channel_idx)},
                        msg,
                        0,
                        item_max_retries,
                        tx_id,
                        send_parts,
                        0,
                    ),
                    why="enqueue_chan_fallback",
                )
                return tx_id
            except Exception:
                return None

    def _fingerprint(self, ch: int, text: str) -> str:
        return f"{int(ch)}|{hashlib.sha1(text.encode('utf-8', errors='ignore')).hexdigest()}"

    def _remember_injected(self, ch: int, text: str) -> None:
        fp = self._fingerprint(ch, text)
        now = time.time()
        with self._inject_lock:
            self._inject_recent[fp] = now
            # purge opportunista
            for k, ts in list(self._inject_recent.items()):
                if now - ts > self._inject_ttl:
                    self._inject_recent.pop(k, None)

    def was_recently_injected(self, ch: int, text: str) -> bool:
        fp = self._fingerprint(ch, text)
        now = time.time()
        with self._inject_lock:
            ts = self._inject_recent.get(fp)
            return bool(ts) and (now - ts <= self._inject_ttl)

    def forward_from_meshtastic(self, *, ch: int, text: str, from_id: str, from_alias: str | None, channel_name: str | None, hop_real: int | None) -> None:
        """
        Llamar desde MeshReceiver._on_rx para reenviar mensajes Meshtastic -> MeshCore.

        Soporta ruteo por:
        - contacto (pubkey_prefix)
        - canal MeshCore (channel_idx) vía MESHCORE_CHANNEL_MAP con 'chan'

        Cambio quirúrgico:
        - Se mantiene intacta la lógica existente de filtros, anti-bucle y ruteo.
        - Solo se cambia el formato del payload para que salga hacia MeshCore con
        prefijo estructurado [MT:<CANAL_LOGICO>:<ALIAS>], simétrico al [MC:...].
        """
        if not self.enable:
            return
        msg = (text or "").strip()
        if not msg:
            return

        # [FIX APRS -> MeshCore]
        # Si el mensaje procede de APRS y contiene cabecera de posición cruda,
        # se limpia antes de construir el payload [MT:...].
        # Esto evita:
        #   1) mostrar datos APRS técnicos antes del texto útil;
        #   2) alargar innecesariamente el mensaje;
        #   3) que MeshCore parta la URL de Google Maps por la coma de lat/lon.
        msg = _clean_aprs_position_text_for_meshcore(msg)
        if not msg:
            return
        # Evitar bucles: si este texto lo acabamos de inyectar desde MeshCore, no lo reenvíes.
        if self.was_recently_injected(int(ch), msg):
            return

        # filtros: BBS y comandos /aprs (no deben salir a MeshCore)
        up = msg.upper()
        if up.startswith("#BBS"):
            return
        if msg.lstrip().lower().startswith("/aprs"):
            return

        mapping = self.ch_map.get(int(ch)) or {}
        kind = (mapping.get("kind") or "contact").strip().lower()
        tag = mapping.get("tag")

        # Nombre lógico del canal:
        # 1) tag del mapping (si existe)
        # 2) nombre real del canal Meshtastic
        # 3) fallback CHx
        if tag:
            logical_tag = str(tag).strip()
        elif channel_name:
            logical_tag = str(channel_name).strip()
        else:
            logical_tag = f"CH{int(ch)}"

        # Alias legible del emisor; fallback al node_id
        sender = (from_alias or "").strip() or str(from_id).strip() or "UNKNOWN"

        # Nuevo prefijo simétrico con la entrada desde MeshCore
        mt_prefix = f"[MT:{logical_tag}:{sender}]"

        # Conserva hops reales si vienen informados
        if isinstance(hop_real, int) and hop_real >= 0:
            payload = f"{mt_prefix} h{int(hop_real)} {msg}"
        else:
            payload = f"{mt_prefix} {msg}"

        if kind in ("chan", "channel"):
            try:
                chan_idx = int(mapping.get("channel_idx"))
            except Exception:
                chan_idx = None
            if chan_idx is None:
                return
            self.enqueue_send_channel(int(chan_idx), payload)
            return

        # default: contacto (pubkey_prefix)
        contact_prefix = mapping.get("contact") or self.default_contact_prefix
        if not contact_prefix:
            return  # sin destino
        self.enqueue_send_contact(str(contact_prefix), payload)

# ================================================================================



# Directorio base único (igual que en el bot)
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "/app/bot_data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Subdirectorio BBS
BBS_DIR = DATA_DIR / "bbs"
BBS_DIR.mkdir(parents=True, exist_ok=True)

# Resolver rutas BBS: si vienen relativas, se hacen relativas a DATA_DIR
def _resolve_under_data_dir(p: str, default_rel: str) -> Path:
    raw = (os.getenv(p) or default_rel).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = DATA_DIR / path
    return path

BBS_DB_PATH  = _resolve_under_data_dir("BBS_DB_PATH",  "bbs/bbs_data.db")
BBS_KEY_PATH = _resolve_under_data_dir("BBS_KEY_PATH", "bbs/.bbs_key")

# Asegura carpetas padre (por si en .env ponen rutas más profundas)
BBS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BBS_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)


# === NUEVO: host/port runtime para Meshtastic (rellenos en main() desde --host)
RUNTIME_MESH_HOST = None   # se fija en main()
RUNTIME_MESH_PORT = 4403   # puerto TCP del nodo Meshtastic



import hashlib
import time
from collections import OrderedDict

class _DedupTTL:
    def __init__(self, ttl_sec: float = 8.0, max_items: int = 2048):
        self.ttl = float(max(1.0, ttl_sec))
        self.max_items = int(max(128, max_items))
        self._store = OrderedDict()  # key -> ts

    def seen_recent(self, key: str) -> bool:
        now = time.time()

        # purge por TTL
        dead = []
        for k, ts in self._store.items():
            if (now - ts) > self.ttl:
                dead.append(k)
            else:
                break
        for k in dead:
            self._store.pop(k, None)

        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = now
            return True

        self._store[key] = now
        self._store.move_to_end(key)

        while len(self._store) > self.max_items:
            self._store.popitem(last=False)

        return False

CTRL_SEND_DEDUP = _DedupTTL(
    ttl_sec=float(os.getenv("CTRL_SEND_TEXT_DEDUP_SEC", "8")),
    max_items=int(os.getenv("CTRL_SEND_TEXT_DEDUP_MAX", "2048"))
)



# --- Compat shim para Meshtastic TCPInterface (host -> hostname) + pool único ---
try:
    import os

    _transport = (os.getenv("MESH_TRANSPORT", "tcp") or "tcp").strip().lower()

    # SOLO tiene sentido en TCP. En bluetooth/usb NO parcheamos TCPInterface.
    if _transport == "tcp":
        from tcpinterface_persistent import TCPInterfacePool  # reutiliza una sola conexión por (host,port)

        import meshtastic.tcp_interface as _tcp_mod
        _TCP_orig = _tcp_mod.TCPInterface

        def _TCPInterface_Compat(*args, **kwargs):
            """
            Wrapper global del ctor que fuerza el uso del pool.
            Acepta tanto host= como hostname= y normaliza port.
            """
            host = kwargs.get("hostname") or kwargs.get("host")
            port = int(kwargs.get("port", RUNTIME_MESH_PORT))

            # args posicionales (libs antiguas)
            if not host and args:
                host = args[0]
            if not host:
                host = "127.0.0.1"

            return TCPInterfacePool.get(host, port)

        # Reemplaza en el módulo y en el símbolo local importado antes
        _tcp_mod.TCPInterface = _TCPInterface_Compat
        TCPInterface = _TCPInterface_Compat

except Exception as _e:
    print(f"[shim TCPInterface@broker] Aviso: {_e}")


# ===================== Utilidades =====================

import inspect

def _bridge_mirror_safe(channel: int, message: str, dest_id: str = None, require_ack: bool = False) -> None:
    """
    Llama al mirror hook del bridge embebido tolerando cambios de firma.
    - Firma antigua: bridge_mirror_outgoing_from_broker(channel, message)
    - Firma nueva (si existiera): bridge_mirror_outgoing_from_broker(payload=..., direction="A2B")
    Nunca lanza excepción hacia arriba (no rompe TX).
    """
    try:
        fn = globals().get("bridge_mirror_outgoing_from_broker")
        if not callable(fn):
            return

        # Intenta detectar si acepta 'payload' por nombre
        try:
            sig = inspect.signature(fn)
            params = sig.parameters
        except Exception:
            params = {}

        # 1) Si acepta payload, úsalo (compat con tu patch “payload=…”)
        if "payload" in params:
            try:
                fn(
                    payload={
                        "type": "text",
                        "text": message,
                        "channel": int(channel),
                        "destination": (dest_id if dest_id else "broadcast"),
                        "require_ack": bool(require_ack),
                    },
                    direction="A2B",
                )
                return
            except TypeError:
                # cae a la firma posicional
                pass

        # 2) Firma posicional (la que tienes ahora en el repo)
        try:
            fn(int(channel), message)
            return
        except TypeError:
            # 3) Último intento: por si el orden fuese distinto
            try:
                fn(message, int(channel))
                return
            except Exception:
                return

    except Exception as _e:
        print(f"[bridge] mirror hook ERROR: {type(_e).__name__}: {_e}", flush=True)
# === NUEVO: Delay opcional para espejo hacia nodo embebido (A -> B) ===
import random
import threading

# Segundos de retardo antes de espejar al nodo embebido.
# 0 = desactivado (comportamiento actual).
BROKER_EMBEDDED_MIRROR_DELAY_SEC = float(os.getenv("BROKER_EMBEDDED_MIRROR_DELAY_SEC", "0") or "0")

# Jitter opcional para evitar colisiones repetidas (0 = sin jitter).
BROKER_EMBEDDED_MIRROR_JITTER_SEC = float(os.getenv("BROKER_EMBEDDED_MIRROR_JITTER_SEC", "0") or "0")


def _bridge_mirror_delayed(channel: int, message: str, dest_id: str = None, require_ack: bool = False) -> None:
    """
    Programa el espejo hacia el bridge embebido con un retardo opcional.
    - No bloquea el hilo principal del broker.
    - Mantiene el comportamiento anterior si el delay es 0.
    """
    try:
        delay = float(BROKER_EMBEDDED_MIRROR_DELAY_SEC or 0.0)
        jitter = float(BROKER_EMBEDDED_MIRROR_JITTER_SEC or 0.0)
        if jitter > 0:
            delay += random.uniform(0.0, jitter)

        # Sin delay -> comportamiento actual
        if delay <= 0:
            _bridge_mirror_safe(channel=channel, message=message, dest_id=dest_id, require_ack=require_ack)
            return

        t = threading.Timer(
            delay,
            _bridge_mirror_safe,
            kwargs={
                "channel": int(channel),
                "message": str(message),
                "dest_id": (dest_id if dest_id else None),
                "require_ack": bool(require_ack),
            },
        )
        t.daemon = True
        t.start()

    except Exception as _e:
        # Nunca romper TX por el delay
        print(f"[bridge] delayed mirror ERROR: {type(_e).__name__}: {_e}", flush=True)

def _safe_first_int(raw: str, default: int = 0) -> int:
    """
    Devuelve el primer entero válido encontrado en una cadena.
    Soporta CSV tipo '3,4,5' -> 3.
    Si falla, devuelve `default`.

    Uso típico:
        ch = _safe_first_int(os.getenv("BBS_CHANNELS") or os.getenv("BBS_CHANNEL", "5"), default=5)
    """
    s = (raw or "").strip()
    if not s:
        return default

    # Si viene en CSV, tomamos el primer token
    first = s.split(",")[0].strip()
    if not first:
        return default

    try:
        return int(first)
    except Exception:
        return default





# === NUEVO: lock de instancia única para Meshtastic TCPInterface ===
import os, sys, time, tempfile
from contextlib import contextmanager

_IS_WIN = os.name == "nt"
if _IS_WIN:
    import msvcrt
else:
    import fcntl


# APRS UTILIZDADES 
# [Meshtastic_Broker_v5.4.py] — cerca del código del servidor JSONL
CLIENTS = {}  # {sock: {"buf": bytearray(), "last_ok": time.time()}}
MAX_CLIENT_BUF = 256 * 1024  # 256 KB por cliente antes de cortar
SLOW_CLIENT_GRACE = 6.0      # segundos de gracia de lentitud

# --- Throttle de logs de espera TX ---
_TX_WAIT_LOG_TS = 0.0


def _setup_client_sock(s):
    try:
        s.setblocking(False)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    CLIENTS[s] = {"buf": bytearray(), "last_ok": time.time()}

def _drop_client(s, reason=""):
    try:
        CLIENTS.pop(s, None)
        s.close()
    except Exception:
        pass
    if reason:
        print(f"[-] Cliente desconectado por: {reason}", flush=True)

def _broadcast_jsonl(line: str):
    """
    Envía 'line' (una línea JSONL con '\n' final) a todos los clientes.
    Clientes lentos: se les acumula en buf; si excede el tope o pasan demasiados
    segundos sin vaciar, se desconectan para no bloquear a los demás.
    """
    if not line.endswith("\n"):
        line = line + "\n"
    data = line.encode("utf-8", "ignore")

    now = time.time()
    for s, st in list(CLIENTS.items()):
        buf = st["buf"]
        # Si no hay cola pendiente y el socket parece “desahogado”, intento envío directo:
        try:
            if not buf:
                sent = s.send(data)
                if sent == len(data):
                    st["last_ok"] = now
                    continue
                else:
                    # resto pendiente
                    buf.extend(data[sent:])
            else:
                # ya había cola pendiente
                buf.extend(data)
        except (BlockingIOError, InterruptedError):
            # no cabe ahora → a la cola
            buf.extend(data)
        except Exception as e:
            _drop_client(s, f"send_error: {e}")
            continue

        # recortes de seguridad
        if len(buf) > MAX_CLIENT_BUF:
            _drop_client(s, "buffer_overflow")
            continue
        if (now - st.get("last_ok", now)) > SLOW_CLIENT_GRACE and len(buf) > 0:
            _drop_client(s, "slow_client")
            continue

def _flush_client_queues():
    """
    Intenta vaciar colas pendientes (llámala de vez en cuando en tu loop principal).
    """
    now = time.time()
    for s, st in list(CLIENTS.items()):
        buf = st["buf"]
        if not buf:
            continue
        try:
            sent = s.send(buf)
            if sent > 0:
                del buf[:sent]
                if not buf:
                    st["last_ok"] = now
        except (BlockingIOError, InterruptedError):
            # aún no se pudo vaciar
            pass
        except Exception as e:
            _drop_client(s, f"flush_error: {e}")



class SingleInstanceLock:
    """
    Candado de instancia única por nombre (host:port).
    En Windows usa msvcrt.locking, en *nix fcntl.flock.
    """
    def __init__(self, name: str):
        safe = "".join(c if c.isalnum() or c in "._-@" else "_" for c in name)
        self.path = os.path.join(tempfile.gettempdir(), f"meshtastic_tcp_{safe}.lock")
        self._fh = None

    def acquire(self, timeout_s: float = 0.0) -> bool:
        t0 = time.time()
        while True:
            try:
                self._fh = open(self.path, "a+b")
                if _IS_WIN:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # guardar info
                self._fh.seek(0)
                self._fh.truncate(0)
                self._fh.write(f"pid={os.getpid()} exe={sys.argv[0]}".encode("utf-8", "ignore"))
                self._fh.flush()
                return True
            except Exception:
                if self._fh:
                    try: self._fh.close()
                    except Exception: pass
                    self._fh = None
                if timeout_s is None or timeout_s <= 0:
                    return False
                if time.time() - t0 >= timeout_s:
                    return False
                time.sleep(0.25)

    def release(self):
        try:
            if self._fh:
                try:
                    if _IS_WIN:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try: self._fh.close()
                except Exception: pass
                self._fh = None
        finally:
            # No borramos el archivo: evita race en Windows.
            pass

# =============== NUEVO: Persistencia offline + servidor backlog ===============
import os, json, time, threading, socket, traceback

# === Control de verbosidad de frames RX (líneas de paquetes) ===
# Por defecto NO se muestran para no saturar la consola.

SHOW_FRAMES = os.getenv("BROKER_SHOW_FRAMES", "0").lower() in {"1","true","yes","on"}

try:
    _POST_CONN_ALLOW_SECS = float(os.getenv("BROKER_POST_CONN_ALLOW", "8.0"))
except Exception:
    _POST_CONN_ALLOW_SECS = 8.0

# Si ejecutas con --no-heartbeat, enviaremos un heartbeat MINIMO cada N segundos para no perder sesión
try:
    _HEARTBEAT_MIN_SECS = float(os.getenv("BROKER_HEARTBEAT_MIN_SECS", "25.0"))
except Exception:
    _HEARTBEAT_MIN_SECS = 25.0

# Marcadores runtime
_LAST_HEARTBEAT_TS = 0.0          # sello de último heartbeat real enviado
_POST_CONNECT_ALLOW_UNTIL = 0.0   # se fija en _on_connection

# apps de control/handshake que dejamos pasar aunque haya cooldown
HANDSHAKE_APPS_ALLOW = {"admin", "control", "nodeinfo", "traceroute"}

# === Owner de la conexión activa para evitar duplicados ===
_CON_OWNER_ID = None   # id() de la interface aceptada
_CON_OWNER_TS = 0.0    # sello temporal de cuándo se aceptó
_DUP_CLOSE_GRACE = 3.0 # ventana en la que cerramos duplicadas (seg)


# Carpeta y fichero donde guardaremos el backlog offline
OFFLINE_DIR = os.getenv("BOT_DATA_DIR", "/app/bot_data")
OFFLINE_LOG_PATH = os.path.join(OFFLINE_DIR, "broker_offline_log.jsonl")

# === [NUEVO] Persistencia de traceroute en backlog ===
TRACEROUTE_LOG_PATH = os.path.join(OFFLINE_DIR, "broker_traceroute_log.jsonl")
_TRACEROUTE_LOCK = threading.Lock()

# === [WEBPANEL/TRACEROUTE v7.0.2] Contexto mínimo de traceroutes lanzados ===
# Objetivo:
#   - No cambiar la forma de enviar traceroute.
#   - No bloquear el broker con waitForTraceRoute().
#   - Guardar contexto para que el WebPanel pueda relacionar:
#       RUN_TRACEROUTE -> TRACEROUTE_APP/ROUTING_APP recibido.
#   - Mantener intacto BBS, APRS, MeshCore, bridge y envío RF.
_TRACEROUTE_PENDING: dict[int, dict] = {}
_TRACEROUTE_PENDING_LOCK = threading.Lock()


def _traceroute_nodeid_from_num(node_num) -> str | None:
    """
    Convierte un nodeNum decimal Meshtastic a formato '!xxxxxxxx'.

    Uso:
        node_id = _traceroute_nodeid_from_num(1378889282)

    Parámetros:
        node_num:
            Número decimal del nodo destino.

    Devuelve:
        Cadena '!xxxxxxxx' en minúsculas o None si no puede convertir.
    """
    try:
        if node_num is None:
            return None
        return f"!{int(node_num) & 0xFFFFFFFF:08x}"
    except Exception:
        return None


def _traceroute_remember_start(
    *,
    target_requested: str,
    dest_node_num: int,
    hop_limit: int,
    ch_index: int,
) -> dict:
    """
    Registra en memoria un traceroute lanzado desde RUN_TRACEROUTE.

    Uso:
        ctx = _traceroute_remember_start(
            target_requested=raw_target,
            dest_node_num=node_num,
            hop_limit=hop_limit,
            ch_index=ch_index,
        )

    No transmite RF.
    No escribe disco.
    Solo conserva contexto temporal para correlación posterior.
    """
    now = int(time.time())
    target_norm = _traceroute_nodeid_from_num(dest_node_num) or str(target_requested or "").strip()

    ctx = {
        "started_ts": now,
        "target_requested": str(target_requested or "").strip(),
        "target_norm": target_norm,
        "dest_node_num": int(dest_node_num),
        "hop_limit": int(hop_limit),
        "ch_index": int(ch_index),
    }

    try:
        ttl = int(os.getenv("BROKER_TRACEROUTE_PENDING_TTL_SEC", "300") or "300")
    except Exception:
        ttl = 300

    try:
        with _TRACEROUTE_PENDING_LOCK:
            _TRACEROUTE_PENDING[int(dest_node_num)] = dict(ctx)

            # Limpieza oportunista para 24/7.
            cutoff = now - max(60, ttl)
            for k, v in list(_TRACEROUTE_PENDING.items()):
                try:
                    if int(v.get("started_ts") or 0) < cutoff:
                        _TRACEROUTE_PENDING.pop(k, None)
                except Exception:
                    _TRACEROUTE_PENDING.pop(k, None)
    except Exception:
        pass

    return ctx


def _traceroute_match_pending(pkt: dict, decoded: dict) -> dict | None:
    """
    Intenta asociar una respuesta RX de traceroute con un RUN_TRACEROUTE reciente.

    La librería Meshtastic puede exponer campos distintos según versión.
    Por eso se buscan varias claves posibles en pkt y decoded.

    Devuelve:
        Contexto pendiente o None.
    """
    candidates: list[int] = []

    def _add_candidate(v):
        try:
            if v is None:
                return

            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return

                if s.startswith("!"):
                    candidates.append(int(s[1:], 16))
                    return

                if s.isdigit():
                    candidates.append(int(s))
                    return

                # Si llega hex sin '!'
                if re.fullmatch(r"[0-9a-fA-F]{8}", s):
                    candidates.append(int(s, 16))
                    return

            elif isinstance(v, (int, float)):
                candidates.append(int(v))
        except Exception:
            return

    try:
        if not isinstance(pkt, dict):
            pkt = {}
        if not isinstance(decoded, dict):
            decoded = {}

        route = decoded.get("route") if isinstance(decoded.get("route"), dict) else {}
        traceroute = decoded.get("traceroute") if isinstance(decoded.get("traceroute"), dict) else {}

        _add_candidate(pkt.get("to"))
        _add_candidate(pkt.get("toId"))
        _add_candidate(pkt.get("to_id"))
        _add_candidate(decoded.get("to"))
        _add_candidate(decoded.get("toId"))
        _add_candidate(decoded.get("to_id"))

        for obj in (route, traceroute):
            _add_candidate(obj.get("dest"))
            _add_candidate(obj.get("to"))
            _add_candidate(obj.get("node"))
            _add_candidate(obj.get("nodeNum"))
            _add_candidate(obj.get("node_num"))

    except Exception:
        pass

    try:
        with _TRACEROUTE_PENDING_LOCK:
            for c in candidates:
                if c in _TRACEROUTE_PENDING:
                    return dict(_TRACEROUTE_PENDING.get(c) or {})
    except Exception:
        pass

    # Fallback seguro:
    # Si solo hay un traceroute reciente, se asocia como best-effort.
    try:
        now = int(time.time())
        ttl = int(os.getenv("BROKER_TRACEROUTE_PENDING_TTL_SEC", "300") or "300")
        with _TRACEROUTE_PENDING_LOCK:
            recent = [
                dict(v)
                for v in _TRACEROUTE_PENDING.values()
                if now - int(v.get("started_ts") or 0) <= max(60, ttl)
            ]
        if len(recent) == 1:
            return recent[0]
    except Exception:
        pass

    return None

def _traceroute_get_single_recent_pending() -> dict | None:
    """
    Devuelve el único traceroute pendiente reciente si solo hay uno.

    Uso:
        ctx = _traceroute_get_single_recent_pending()

    Objetivo:
        - Resolver respuestas ROUTING_APP que llegan sin campo destino claro.
        - Evitar que el WebAdmin quede en espera cuando el broker sí ha recibido
          una respuesta real de traceroute.
        - Mantener seguridad: solo se asocia si hay exactamente un traceroute
          pendiente dentro del TTL.

    No transmite RF.
    No modifica colas.
    No borra estado.
    No toca APRS/BBS/MeshCore/bridge.
    """
    try:
        now = int(time.time())
        ttl = int(os.getenv("BROKER_TRACEROUTE_PENDING_TTL_SEC", "300") or "300")

        with _TRACEROUTE_PENDING_LOCK:
            recent = [
                dict(v)
                for v in _TRACEROUTE_PENDING.values()
                if now - int(v.get("started_ts") or 0) <= max(60, ttl)
            ]

        if len(recent) == 1:
            return recent[0]

    except Exception:
        pass

    return None


def _traceroute_compact_text(pkt: dict, decoded: dict, rec: dict | None = None) -> str:
    """
    Construye texto compacto para guardar en broker_offline_log.jsonl.

    No intenta depender de un único formato interno del SDK.
    Prioriza route_text/text si ya existen y cae a un resumen básico.
    """
    try:
        if isinstance(rec, dict):
            for k in ("route_text", "text", "info"):
                v = rec.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()

        if not isinstance(pkt, dict):
            pkt = {}
        if not isinstance(decoded, dict):
            decoded = {}

        for k in ("text", "message", "route_text"):
            v = decoded.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

        src = (
            pkt.get("fromId")
            or pkt.get("from")
            or decoded.get("fromId")
            or decoded.get("from")
            or "?"
        )
        dst = (
            pkt.get("toId")
            or pkt.get("to")
            or decoded.get("toId")
            or decoded.get("to")
            or "?"
        )

        route = decoded.get("route")
        traceroute = decoded.get("traceroute")

        if isinstance(route, dict) and route:
            return f"route {src} -> {dst} {route}"

        if isinstance(traceroute, dict) and traceroute:
            return f"traceroute {src} -> {dst} {traceroute}"

        return f"traceroute {src} -> {dst}"
    except Exception:
        return "traceroute"

# === [WEBPANEL/TRACEROUTE v7.0.3] Extracción enriquecida de rutas =============
# Objetivo:
#   - No cambiar la ejecución RF de traceroute.
#   - No cambiar RUN_TRACEROUTE.
#   - No tocar BBS/APRS/MeshCore/bridge.
#   - Enriquecer únicamente lo que se persiste en broker_offline_log.jsonl y
#     broker_traceroute_log.jsonl para que el WebPanel pueda dibujar/diagnosticar
#     mejor el resultado.
#
# Uso:
#   enriched = _traceroute_extract_enriched_route(pkt, decoded, rec)
#
# Devuelve:
#   dict con route_nodes, route_back_nodes, route_snr, route_back_snr,
#   route_text_enriched, traceroute_payload y diagnóstico defensivo.
# ==============================================================================

def _traceroute_node_id_any(value) -> str | None:
    """
    Convierte distintos formatos de nodo a '!xxxxxxxx'.

    Parámetros:
      value:
        - int decimal nodeNum
        - str decimal
        - str '!xxxxxxxx'
        - str hex de 8 caracteres

    Devuelve:
      '!xxxxxxxx' en minúsculas o None.

    Seguridad:
      - No transmite RF.
      - No modifica estado global.
      - No lanza excepción.
    """
    try:
        if value is None:
            return None

        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None

            if s.startswith("!"):
                h = s[1:].strip().lower()
                if re.fullmatch(r"[0-9a-f]{8}", h):
                    return "!" + h
                return s.lower()

            if re.fullmatch(r"[0-9a-fA-F]{8}", s):
                return "!" + s.lower()

            if s.isdigit():
                return f"!{int(s) & 0xFFFFFFFF:08x}"

            return None

        if isinstance(value, (int, float)):
            return f"!{int(value) & 0xFFFFFFFF:08x}"

    except Exception:
        return None

    return None


def _traceroute_safe_jsonable(value, max_depth: int = 5):
    """
    Convierte objetos potencialmente raros del SDK Meshtastic a estructuras JSON.

    Uso:
      clean = _traceroute_safe_jsonable(decoded)

    Funcionalidad:
      - Soporta dict/list/tuple/set/bytes/objetos con __dict__.
      - Limita profundidad para evitar crecimiento inesperado.
      - No modifica el objeto original.
      - No lanza excepción.
    """
    try:
        if max_depth <= 0:
            return str(value)[:300]

        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, bytes):
            return {
                "_type": "bytes",
                "len": len(value),
                "hex": value[:256].hex(),
            }

        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                try:
                    ks = str(k)
                    out[ks] = _traceroute_safe_jsonable(v, max_depth - 1)
                except Exception:
                    continue
            return out

        if isinstance(value, (list, tuple, set)):
            return [_traceroute_safe_jsonable(v, max_depth - 1) for v in list(value)[:200]]

        # Protobuf / objetos generados por la librería Meshtastic.
        # Algunas respuestas ROUTING_APP/TRACEROUTE_APP no llegan como dict, sino
        # como objetos con ListFields(). Si no se tratan aquí, route/routeBack/SNR
        # quedan ocultos y el WebPanel solo ve un string compacto.
        list_fields = getattr(value, "ListFields", None)
        if callable(list_fields):
            out = {}
            try:
                for field_desc, field_value in list_fields():
                    name = getattr(field_desc, "name", "") or getattr(field_desc, "json_name", "") or str(field_desc)
                    if name:
                        out[str(name)] = _traceroute_safe_jsonable(field_value, max_depth - 1)
                if out:
                    return out
            except Exception:
                pass

        # Objetos SDK ligeros que no tienen __dict__, pero sí atributos públicos.
        # Se limita a nombres conocidos para no serializar media interfaz radio.
        known_attrs = (
            "route", "routes", "routeBack", "route_back",
            "snrTowards", "snr_towards", "snrBack", "snr_back",
            "routeBackSnr", "route_back_snr", "payload", "routing",
            "errorReason", "error_reason"
        )
        attr_out = {}
        for attr in known_attrs:
            try:
                if hasattr(value, attr):
                    av = getattr(value, attr)
                    if av not in (None, "", [], {}):
                        attr_out[attr] = _traceroute_safe_jsonable(av, max_depth - 1)
            except Exception:
                pass
        if attr_out:
            return attr_out

        if hasattr(value, "__dict__"):
            return _traceroute_safe_jsonable(vars(value), max_depth - 1)

        return str(value)[:500]

    except Exception:
        try:
            return str(value)[:300]
        except Exception:
            return None


def _traceroute_walk_dicts(*roots) -> list[dict]:
    """
    Recorre de forma acotada estructuras anidadas y devuelve diccionarios.

    Uso:
      dicts = _traceroute_walk_dicts(pkt, decoded, rec)

    Funcionalidad:
      - Ayuda a encontrar route/routeBack/snrTowards aunque el SDK los coloque
        en decoded, decoded.payload, routing, traceroute, etc.
      - Evita ciclos por id().
      - Profundidad máxima fija para uso 24/7.
    """
    out: list[dict] = []
    seen: set[int] = set()

    def _walk(obj, depth: int):
        if depth > 7:
            return

        if isinstance(obj, dict):
            oid = id(obj)
            if oid in seen:
                return
            seen.add(oid)
            out.append(obj)
            for v in obj.values():
                if isinstance(v, (dict, list, tuple)):
                    _walk(v, depth + 1)

        elif isinstance(obj, (list, tuple)):
            for v in obj[:200]:
                if isinstance(v, (dict, list, tuple)):
                    _walk(v, depth + 1)

    for r in roots:
        _walk(r, 0)

    return out


def _traceroute_first_value(dicts: list[dict], keys: tuple[str, ...]):
    """
    Devuelve el primer valor no vacío encontrado para una lista de claves.

    Uso:
      route = _traceroute_first_value(dicts, ("route", "routes"))

    No lanza excepción.
    """
    try:
        for d in dicts:
            if not isinstance(d, dict):
                continue
            for k in keys:
                if k in d and d.get(k) not in (None, "", [], {}):
                    return d.get(k)
    except Exception:
        pass
    return None


def _traceroute_normalize_node_list(value) -> list[str]:
    """
    Normaliza una ruta a lista de nodos '!xxxxxxxx'.

    Soporta:
      - [123, 456]
      - ["!db5de158", "!849a5b24"]
      - [{"node": 123}, {"nodeNum": 456}]
      - {"route": [...]}
      - {"nodes": [...]}

    Devuelve:
      lista de nodos sin duplicar consecutivos.
    """
    nodes: list[str] = []

    def _append(v):
        nid = _traceroute_node_id_any(v)
        if nid:
            if not nodes or nodes[-1] != nid:
                nodes.append(nid)

    try:
        if value is None:
            return nodes

        if isinstance(value, dict):
            for key in ("route", "routes", "nodes", "hops", "hop", "routeBack", "route_back", "routeNodes", "route_nodes", "routeBackNodes", "route_back_nodes", "forwardPath", "forward_path", "returnPath", "return_path"):
                if key in value:
                    return _traceroute_normalize_node_list(value.get(key))

            for key in ("node", "nodeNum", "node_num", "id", "from", "to"):
                if key in value:
                    _append(value.get(key))

            return nodes

        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    sub = _traceroute_normalize_node_list(item)
                    for n in sub:
                        if not nodes or nodes[-1] != n:
                            nodes.append(n)
                else:
                    _append(item)

            return nodes

        _append(value)

    except Exception:
        return nodes

    return nodes


def _traceroute_normalize_snr_list(value) -> list:
    """
    Normaliza listas de SNR de traceroute.

    Soporta:
      - [3, -8, None]
      - ["?dB", "4dB"]
      - {"snrTowards": [...]}

    Devuelve lista simple para JSON.
    """
    out: list = []

    try:
        if value is None:
            return out

        if isinstance(value, dict):
            for key in ("snrTowards", "snr_towards", "routeSnr", "route_snr", "routeBackSnr", "route_back_snr", "snrBack", "snr_back", "returnSnr", "return_snr", "snr", "snrs"):
                if key in value:
                    return _traceroute_normalize_snr_list(value.get(key))
            return out

        if isinstance(value, (list, tuple)):
            for item in value[:200]:
                if item is None:
                    out.append(None)
                elif isinstance(item, (int, float)):
                    out.append(item)
                else:
                    out.append(str(item))
            return out

        if isinstance(value, (int, float)):
            return [value]

        if isinstance(value, str) and value.strip():
            return [value.strip()]

    except Exception:
        return out

    return out



def _traceroute_payload_bytes_candidates(*roots) -> list[bytes]:
    """
    Extrae candidatos binarios de payload para intentar decodificar RouteDiscovery.

    Uso interno:
        candidates = _traceroute_payload_bytes_candidates(pkt, decoded, rec)

    Parámetros:
        *roots:
            Paquete RX, decoded y registro plano preliminar.

    Funcionalidad:
        - Busca payload en campos habituales del SDK Meshtastic.
        - Soporta bytes reales, bytearray, listas de enteros, hex y base64.
        - Acota tamaños para evitar consumo anómalo en funcionamiento 24/7.
        - No transmite RF, no modifica estado global y no lanza excepción.
    """
    out: list[bytes] = []
    seen: set[bytes] = set()

    def _add(raw) -> None:
        try:
            if raw is None:
                return
            b = None
            if isinstance(raw, bytes):
                b = raw
            elif isinstance(raw, bytearray):
                b = bytes(raw)
            elif isinstance(raw, (list, tuple)) and raw and all(isinstance(x, int) and 0 <= x <= 255 for x in raw[:512]):
                b = bytes(raw[:512])
            elif isinstance(raw, dict):
                for key in ("bytes", "data", "payload", "raw", "hex", "base64", "b64"):
                    if key in raw:
                        _add(raw.get(key))
                return
            elif isinstance(raw, str):
                text = raw.strip()
                if not text:
                    return
                # Hex puro o con separadores sencillos.
                hx = re.sub(r"[^0-9a-fA-F]", "", text)
                if len(hx) >= 4 and len(hx) % 2 == 0 and len(hx) <= 4096:
                    try:
                        b = binascii.unhexlify(hx)
                    except Exception:
                        b = None
                # Base64 como segundo intento.
                if b is None and len(text) <= 4096:
                    try:
                        b = base64.b64decode(text, validate=True)
                    except Exception:
                        b = None

            if b is None or not b:
                return
            if len(b) > 4096:
                b = b[:4096]
            if b not in seen:
                seen.add(b)
                out.append(b)
        except Exception:
            return

    def _walk(obj, depth: int = 0) -> None:
        if depth > 6:
            return
        try:
            if isinstance(obj, dict):
                for key in (
                    "payload", "raw_payload", "payload_raw", "payloadBytes", "payload_bytes",
                    "payloadHex", "payload_hex", "data", "raw", "bytes", "encrypted", "decodedPayload"
                ):
                    if key in obj:
                        _add(obj.get(key))
                for v in obj.values():
                    if isinstance(v, (dict, list, tuple)):
                        _walk(v, depth + 1)
            elif isinstance(obj, (list, tuple)):
                # Una lista simple de enteros puede ser el payload.
                _add(obj)
                for v in list(obj)[:80]:
                    if isinstance(v, (dict, list, tuple)):
                        _walk(v, depth + 1)
            else:
                for attr in ("payload", "raw_payload", "payloadBytes", "data"):
                    try:
                        if hasattr(obj, attr):
                            _add(getattr(obj, attr))
                    except Exception:
                        pass
        except Exception:
            return

    for root in roots:
        _walk(root, 0)

    return out[:12]


def _traceroute_decode_route_discovery_payload(*roots) -> dict:
    """
    Decodifica protobuf Mesh RouteDiscovery desde payload binario si está disponible.

    Uso interno:
        decoded_rd = _traceroute_decode_route_discovery_payload(pkt, decoded, rec)

    Funcionalidad:
        - Replica la parte esencial de MeshView: obtener route, snr_towards,
          route_back y snr_back desde el payload RouteDiscovery real.
        - Usa meshtastic.protobuf.mesh_pb2.RouteDiscovery si está disponible.
        - Devuelve {} si no hay librería, no hay payload o el payload no es RouteDiscovery.
        - No altera TX/RX, colas, BBS, APRS, MeshCore ni bridge.
    """
    try:
        try:
            from meshtastic.protobuf import mesh_pb2  # type: ignore
        except Exception:
            import meshtastic.protobuf.mesh_pb2 as mesh_pb2  # type: ignore

        route_cls = getattr(mesh_pb2, "RouteDiscovery", None)
        if route_cls is None:
            return {}

        for payload in _traceroute_payload_bytes_candidates(*roots):
            try:
                msg = route_cls()
                msg.ParseFromString(payload)
                safe = _traceroute_safe_jsonable(msg, max_depth=6)
                if not isinstance(safe, dict) or not safe:
                    continue

                # Validación mínima: debe contener alguna señal propia de RouteDiscovery.
                keys_l = {str(k).lower() for k in safe.keys()}
                if not any(k in keys_l for k in ("route", "snrtowards", "snr_towards", "routeback", "route_back", "snrback", "snr_back")):
                    continue

                safe["_decoded_from_payload"] = True
                safe["_decoder"] = "mesh_pb2.RouteDiscovery"
                safe["_payload_len"] = len(payload)
                return safe
            except Exception:
                continue
    except Exception:
        return {}

    return {}


def _traceroute_route_text_from_nodes(
    route_nodes: list[str],
    route_snr: list | None = None,
    fallback: str = "",
) -> str:
    """
    Construye texto legible de ruta a partir de nodos y SNR.

    Ejemplo:
      !a -> !b -> !c
      !a --(3dB)--> !b --(?dB)--> !c

    Si no hay nodos, devuelve fallback.
    """
    try:
        if not route_nodes:
            return fallback or ""

        snrs = route_snr or []
        parts = []

        for i, node in enumerate(route_nodes):
            if i == 0:
                parts.append(str(node))
                continue

            snr_txt = ""
            try:
                snr = snrs[i - 1] if i - 1 < len(snrs) else None
                if snr is not None:
                    snr_txt = f" --({snr}dB)--> "
            except Exception:
                snr_txt = ""

            parts.append(snr_txt if snr_txt else " -> ")
            parts.append(str(node))

        return "".join(parts)

    except Exception:
        return fallback or ""


def _traceroute_extract_enriched_route(pkt: dict, decoded: dict, rec: dict | None = None) -> dict:
    """
    Extrae información enriquecida de ROUTING_APP/TRACEROUTE_APP.

    Parámetros:
      pkt:
        paquete RX normalizado del broker.
      decoded:
        decoded del paquete Meshtastic.
      rec:
        registro plano que se va a persistir, si existe.

    Devuelve:
      {
        "route_nodes": [...],
        "route_snr": [...],
        "route_back_nodes": [...],
        "route_back_snr": [...],
        "route_text_enriched": "...",
        "traceroute_payload": {...},
        "traceroute_payload_keys": [...],
        "route_quality_hint": "..."
      }

    Seguridad:
      - Solo lectura.
      - No transmite RF.
      - No modifica colas.
      - No borra ni rota ficheros.
      - No altera la correlación existente por _TRACEROUTE_PENDING.
    """
    if not isinstance(pkt, dict):
        pkt = {}
    if not isinstance(decoded, dict):
        decoded = {}
    if not isinstance(rec, dict):
        rec = {}

    # Los registros sintéticos traceroute_started no son una ruta RF real.
    # Antes se extraía el target del texto y aparecía como si fuese un único salto,
    # contaminando el WebPanel con falsas rutas. Se marca explícitamente como inicio.
    trace_event = str(rec.get("trace_event") or rec.get("event_type") or "").strip().lower()
    if trace_event == "traceroute_started":
        return {
            "route_nodes": [],
            "route_snr": [],
            "route_back_nodes": [],
            "route_back_snr": [],
            "route_text_enriched": str(rec.get("text") or rec.get("route_text") or "").strip(),
            "route_back_text_enriched": "",
            "route_data": {
                "route_nodes": [],
                "snr_towards": [],
                "route_back": [],
                "snr_back": [],
                "forward_path": [],
                "return_path": [],
                "actual_rf_path": [],
            },
            "traceroute_payload": {},
            "traceroute_payload_keys": [],
            "route_quality_hint": "start_marker_no_rf_route",
            "routing_error_reason": None,
            "route_raw_present": False,
            "route_back_raw_present": False,
            "snr_raw_present": False,
            "snr_back_raw_present": False,
        }

    routing_error_reason = _traceroute_get_routing_error_reason(rec, decoded, pkt)
    routing_error_norm = str(routing_error_reason or "").strip().upper()

    # Además de las estructuras dict originales, se recorren copias JSON-safe.
    # Esto permite ver campos ocultos dentro de objetos protobuf/SDK: route,
    # route_back, snr_towards y snr_back.
    safe_pkt = _traceroute_safe_jsonable(pkt, max_depth=7)
    safe_decoded = _traceroute_safe_jsonable(decoded, max_depth=7)
    safe_rec = _traceroute_safe_jsonable(rec, max_depth=7)
    dicts = _traceroute_walk_dicts(pkt, decoded, rec, safe_pkt, safe_decoded, safe_rec)

    # Fase v7.0.10: intento explícito de decodificación protobuf RouteDiscovery,
    # equivalente funcional al flujo de MeshView, pero alimentado por el broker local.
    route_discovery_decoded = _traceroute_decode_route_discovery_payload(pkt, decoded, rec)
    if isinstance(route_discovery_decoded, dict) and route_discovery_decoded:
        dicts.insert(0, route_discovery_decoded)

    route_raw = _traceroute_first_value(
        dicts,
        (
            "route",
            "routes",
            "hops",
            "hop",
            "routeTowards",
            "route_towards",
            "routeNodes",
            "route_nodes",
            "forwardPath",
            "forward_path",
        ),
    )

    route_back_raw = _traceroute_first_value(
        dicts,
        (
            "routeBack",
            "route_back",
            "backRoute",
            "back_route",
            "returnRoute",
            "return_route",
            "returnPath",
            "return_path",
            "backPath",
            "back_path",
            "routeBackNodes",
            "route_back_nodes",
        ),
    )

    snr_raw = _traceroute_first_value(
        dicts,
        (
            "snrTowards",
            "snr_towards",
            "routeSnr",
            "route_snr",
            "snr",
            "snrs",
            "routeSnrs",
            "route_snrs",
        ),
    )

    snr_back_raw = _traceroute_first_value(
        dicts,
        (
            "routeBackSnr",
            "route_back_snr",
            "snrBack",
            "snr_back",
            "returnSnr",
            "return_snr",
            "returnSnrs",
            "return_snrs",
        ),
    )

    route_nodes = _traceroute_normalize_node_list(route_raw)
    route_back_nodes = _traceroute_normalize_node_list(route_back_raw)
    route_snr = _traceroute_normalize_snr_list(snr_raw)
    route_back_snr = _traceroute_normalize_snr_list(snr_back_raw)

    # Fallback textual SOLO si no estamos ante ROUTING_APP de error/ACK sin ruta.
    # Ejemplo real observado: payload 1805 = MAX_RETRANSMIT y texto
    # "traceroute !local -> !local". Eso no es una ruta: es un estado de routing.
    existing_text = (
        rec.get("route_text")
        or rec.get("text")
        or decoded.get("text")
        or decoded.get("route_text")
        or ""
    )
    may_use_text_fallback = True
    if routing_error_norm and routing_error_norm not in {"NONE", "0", "NO_ERROR"}:
        may_use_text_fallback = False
    if str(rec.get("portnum") or decoded.get("portnum") or pkt.get("portnum") or "").upper() == "ROUTING_APP" and not route_raw and not route_back_raw:
        may_use_text_fallback = False

    if may_use_text_fallback and not route_nodes and isinstance(existing_text, str):
        found = re.findall(r"![0-9a-fA-F]{8}", existing_text)
        for n in found:
            nid = n.lower()
            if not route_nodes or route_nodes[-1] != nid:
                route_nodes.append(nid)

    fallback_text = _traceroute_compact_text(pkt, decoded, rec)
    route_text_enriched = _traceroute_route_text_from_nodes(
        route_nodes,
        route_snr,
        fallback=fallback_text,
    )

    # Payload útil, acotado. No guardamos objetos arbitrarios sin límite.
    payload_candidates = {}
    for key in (
        "routing",
        "traceroute",
        "route",
        "routes",
        "routeNodes",
        "route_nodes",
        "routeBack",
        "route_back",
        "routeBackNodes",
        "route_back_nodes",
        "routeBackSnr",
        "route_back_snr",
        "snrTowards",
        "snr_towards",
        "snrBack",
        "snr_back",
        "payload",
        "raw_payload",
        "payload_hex",
    ):
        try:
            if key in decoded and decoded.get(key) not in (None, "", [], {}):
                payload_candidates[key] = decoded.get(key)
            elif key in pkt and pkt.get(key) not in (None, "", [], {}):
                payload_candidates[key] = pkt.get(key)
            elif key in rec and rec.get(key) not in (None, "", [], {}):
                payload_candidates[key] = rec.get(key)
        except Exception:
            pass

    if isinstance(route_discovery_decoded, dict) and route_discovery_decoded:
        payload_candidates["routeDiscoveryDecoded"] = route_discovery_decoded

    traceroute_payload = _traceroute_safe_jsonable(payload_candidates, max_depth=5)

    # Límite defensivo de tamaño en JSON embebido.
    try:
        max_chars = int(os.getenv("BROKER_TRACEROUTE_PAYLOAD_MAX_CHARS", "12000") or "12000")
    except Exception:
        max_chars = 12000

    try:
        payload_json = json.dumps(traceroute_payload, ensure_ascii=False, default=str)
        if len(payload_json) > max_chars:
            traceroute_payload = {
                "_truncated": True,
                "_max_chars": max_chars,
                "_preview": payload_json[:max_chars],
            }
    except Exception:
        traceroute_payload = {"_error": "payload_not_json_serializable"}

    route_back_text_enriched = _traceroute_route_text_from_nodes(
        route_back_nodes,
        route_back_snr,
        fallback="",
    )

    # Estructura tipo MeshView: conserva ida, vuelta y camino RF combinado.
    # actual_rf_path prioriza ida si existe; si solo hay vuelta, usa vuelta invertida
    # para poder pintar algo coherente sin inventar saltos.
    actual_rf_path = list(route_nodes or [])
    if not actual_rf_path and route_back_nodes:
        actual_rf_path = list(reversed(route_back_nodes))

    route_data = {
        "route_nodes": route_nodes,
        "snr_towards": route_snr,
        "route_back": route_back_nodes,
        "snr_back": route_back_snr,
        "forward_path": route_nodes,
        "return_path": route_back_nodes,
        "actual_rf_path": actual_rf_path,
    }

    quality_hint = "empty_route"
    if routing_error_norm and routing_error_norm not in {"NONE", "0", "NO_ERROR"}:
        quality_hint = f"routing_error_{routing_error_norm}"
    elif routing_error_norm in {"NONE", "0", "NO_ERROR"} and not route_nodes and not route_back_nodes:
        quality_hint = "routing_ack_without_route"
    elif route_nodes:
        if len(route_nodes) >= 2 and route_nodes[0] == route_nodes[-1]:
            quality_hint = "self_loop"
        else:
            quality_hint = "route_nodes_present"
    elif route_back_nodes:
        quality_hint = "route_back_only"
    elif payload_candidates:
        quality_hint = "payload_present_without_normalized_nodes"

    return {
        "route_nodes": route_nodes,
        "route_snr": route_snr,
        "route_back_nodes": route_back_nodes,
        "route_back_snr": route_back_snr,
        "route_text_enriched": route_text_enriched,
        "route_back_text_enriched": route_back_text_enriched,
        "route_data": route_data,
        "traceroute_payload": traceroute_payload,
        "traceroute_payload_keys": sorted(list(payload_candidates.keys())),
        "route_quality_hint": quality_hint,
        "routing_error_reason": routing_error_reason,
        "route_discovery_decoded": bool(route_discovery_decoded),
        "route_raw_present": route_raw is not None,
        "route_back_raw_present": route_back_raw is not None,
        "snr_raw_present": snr_raw is not None,
        "snr_back_raw_present": snr_back_raw is not None,
    }


# === NUEVO: referencia global al gestor de interfaz del broker ===
BROKER_IFACE_MGR = None  # se rellena en main()

# Puerto TCP del servidor de backlog (no interfiere con el puerto principal del broker)
# Puedes ajustarlo si quieres; por defecto usa el puerto del broker (+1) si existe la constante BROKER_PORT,
# si no, fija 8766.
try:
    BACKLOG_PORT = int(BROKER_PORT) + 1  # si ya tienes BROKER_PORT
except Exception:
    BACKLOG_PORT = 8766

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def _log_ex(context: str, exc: Exception) -> None:
    """
    Log defensivo de excepciones del broker.

    Uso:
        _log_ex("append_offline_log failed", e)

    Parámetros:
        context:
            Texto corto indicando dónde se produjo el fallo.
        exc:
            Excepción capturada.

    Funcionalidad:
        - Evita que una ruta de error provoque otro NameError.
        - No transmite RF.
        - No modifica colas.
        - No toca BBS/APRS/MeshCore/bridge.
        - Solo imprime un aviso controlado en consola/log Docker.
    """
    try:
        print(
            f"⚠️ {context}: {type(exc).__name__}: {str(exc)[:300]}",
            flush=True,
        )
    except Exception:
        pass


# === [WEBPANEL/TRACEROUTE v7.0.7b] Captura cruda segura de traceroute ==========
# Objetivo:
#   - Diagnosticar si la ruta completa que se ve en MeshView llega realmente al
#     broker local antes de la normalización.
#   - Mantener intacta la ejecución RF, RUN_TRACEROUTE, BBS, APRS, MeshCore,
#     bridge, SENDQ, reconexión, cooldown y telemetría.
#   - No activar captura masiva salvo que se configure expresamente.
#
# Variables:
#   BROKER_TRACEROUTE_RAW_DEBUG=1
#       Captura todos los TRACEROUTE_APP/ROUTING_APP.
#
#   BROKER_TRACEROUTE_RAW_DEBUG_AUTO_ON_ERROR=1
#       Captura automáticamente solo casos útiles para diagnóstico:
#       errorReason != NONE o paquetes sin route/routeBack normalizable.
#
#   BROKER_TRACEROUTE_RAW_DEBUG_MAX_BYTES=5242880
#       Tamaño máximo del fichero antes de rotar a .1.
#
#   BROKER_TRACEROUTE_RAW_DEBUG_MAX_STR=5000
#       Tamaño máximo de repr/str guardado por campo.
# ============================================================================== 
TRACEROUTE_RAW_DEBUG_PATH = os.path.join(OFFLINE_DIR, "broker_traceroute_raw_debug.jsonl")
_TRACEROUTE_RAW_DEBUG_LOCK = threading.Lock()


def _traceroute_env_bool(name: str, default: str = "0") -> bool:
    """
    Lee una variable booleana de entorno para la captura diagnóstica.

    Uso:
        if _traceroute_env_bool("BROKER_TRACEROUTE_RAW_DEBUG"):
            ...

    No lanza excepción y no altera estado global.
    """
    try:
        v = (os.getenv(name, default) or default).strip().lower()
        return v in {"1", "true", "yes", "on", "si", "sí", "y"}
    except Exception:
        return False


def _traceroute_raw_debug_max_str() -> int:
    """
    Devuelve el límite de caracteres para repr/str en raw debug.
    Protege el broker 24/7 frente a objetos SDK grandes.
    """
    try:
        return max(500, min(int(os.getenv("BROKER_TRACEROUTE_RAW_DEBUG_MAX_STR", "5000") or "5000"), 50000))
    except Exception:
        return 5000


def _traceroute_raw_debug_limited_repr(value) -> str:
    """
    Convierte un objeto a repr() acotado.

    Uso:
        s = _traceroute_raw_debug_limited_repr(pkt)

    Funcionalidad:
        - Permite ver objetos protobuf/SDK sin romper json.dumps().
        - Limita tamaño para no inflar el JSONL.
        - No lanza excepción.
    """
    try:
        limit = _traceroute_raw_debug_max_str()
        s = repr(value)
        if len(s) > limit:
            return s[:limit] + "…[truncated]"
        return s
    except Exception as e:
        return f"<repr_error {type(e).__name__}: {str(e)[:200]}>"


def _traceroute_get_routing_error_reason(*objs) -> str | None:
    """
    Extrae errorReason/error_reason de routing desde pkt/decoded/rec.

    Devuelve:
        'MAX_RETRANSMIT', 'NONE', etc., o None si no existe.
    """
    try:
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            routing = obj.get("routing")
            if isinstance(routing, dict):
                val = routing.get("errorReason") or routing.get("error_reason") or routing.get("error")
                if val is not None:
                    return str(val)
            elif routing is not None:
                val = getattr(routing, "errorReason", None)
                if val is None:
                    val = getattr(routing, "error_reason", None)
                if val is not None:
                    return str(val)
    except Exception:
        pass
    return None


def _traceroute_raw_debug_reason(pkt: dict, decoded: dict, rec: dict) -> str | None:
    """
    Decide si debe capturarse raw debug y devuelve el motivo.

    Reglas:
        - BROKER_TRACEROUTE_RAW_DEBUG=1 => captura siempre.
        - BROKER_TRACEROUTE_RAW_DEBUG_AUTO_ON_ERROR=1 => captura si:
            * routing.errorReason existe y no es NONE.
            * no hay route/routeBack/SNR estructurados.
            * route_nodes queda vacío o con un único nodo.

    No escribe disco. No transmite RF. No modifica rec.
    """
    try:
        if _traceroute_env_bool("BROKER_TRACEROUTE_RAW_DEBUG", "0"):
            return "forced_all"

        if not _traceroute_env_bool("BROKER_TRACEROUTE_RAW_DEBUG_AUTO_ON_ERROR", "1"):
            return None

        err = _traceroute_get_routing_error_reason(rec, decoded, pkt)
        if err and err.upper() not in {"NONE", "0", "NO_ERROR"}:
            return f"routing_error:{err}"

        enriched = _traceroute_extract_enriched_route(pkt, decoded, rec)
        route_nodes = enriched.get("route_nodes") or []
        route_back_nodes = enriched.get("route_back_nodes") or []
        has_raw = bool(enriched.get("route_raw_present") or enriched.get("route_back_raw_present"))
        has_snr = bool(enriched.get("snr_raw_present") or enriched.get("snr_back_raw_present"))

        if not has_raw and not has_snr:
            return "no_structured_route_fields"

        if len(route_nodes) <= 1 and len(route_back_nodes) <= 1:
            return "incomplete_normalized_route"

    except Exception as e:
        return f"raw_debug_reason_error:{type(e).__name__}"

    return None


def _traceroute_append_raw_debug(pkt: dict, decoded: dict, rec: dict, *, reason: str) -> None:
    """
    Guarda una captura cruda acotada de TRACEROUTE_APP/ROUTING_APP.

    Uso:
        reason = _traceroute_raw_debug_reason(pkt, decoded, rec)
        if reason:
            _traceroute_append_raw_debug(pkt, decoded, rec, reason=reason)

    Parámetros:
        pkt:
            Paquete RX tal como lo ve el broker.
        decoded:
            decoded del paquete.
        rec:
            Registro plano preliminar antes de append_offline_log().
        reason:
            Motivo de captura.

    Funcionalidad:
        - Escribe en broker_traceroute_raw_debug.jsonl.
        - Rota a .1 al superar BROKER_TRACEROUTE_RAW_DEBUG_MAX_BYTES.
        - Convierte objetos SDK/protobuf a JSON seguro.
        - Guarda repr() acotado para ver campos que __dict__ no exponga.

    Seguridad:
        - Best-effort: cualquier fallo queda aislado.
        - No bloquea RF salvo el tiempo mínimo de escritura local.
        - No toca BBS/APRS/MeshCore/bridge/SENDQ.
    """
    try:
        path = TRACEROUTE_RAW_DEBUG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)

        try:
            max_bytes = int(os.getenv("BROKER_TRACEROUTE_RAW_DEBUG_MAX_BYTES", "5242880") or "5242880")
        except Exception:
            max_bytes = 5242880
        max_bytes = max(262144, min(max_bytes, 52428800))

        with _TRACEROUTE_RAW_DEBUG_LOCK:
            try:
                if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                    bak = f"{path}.1"
                    try:
                        if os.path.exists(bak):
                            os.remove(bak)
                    except Exception:
                        pass
                    try:
                        os.replace(path, bak)
                    except Exception:
                        pass
            except Exception:
                pass

            row = {
                "ts": int(time.time()),
                "reason": str(reason or "unknown"),
                "portnum": rec.get("portnum") if isinstance(rec, dict) else None,
                "from": rec.get("from") if isinstance(rec, dict) else None,
                "to": rec.get("to") if isinstance(rec, dict) else None,
                "target_requested": rec.get("target_requested") if isinstance(rec, dict) else None,
                "target_norm": rec.get("target_norm") if isinstance(rec, dict) else None,
                "event_type": rec.get("event_type") if isinstance(rec, dict) else None,
                "trace_event": rec.get("trace_event") if isinstance(rec, dict) else None,
                "pkt_keys": sorted(list(pkt.keys())) if isinstance(pkt, dict) else [],
                "decoded_keys": sorted(list(decoded.keys())) if isinstance(decoded, dict) else [],
                "rec_keys": sorted(list(rec.keys())) if isinstance(rec, dict) else [],
                "routing_error_reason": _traceroute_get_routing_error_reason(rec, decoded, pkt),
                "pkt": _traceroute_safe_jsonable(pkt, max_depth=7),
                "decoded": _traceroute_safe_jsonable(decoded, max_depth=7),
                "rec": _traceroute_safe_jsonable(rec, max_depth=7),
                "repr_pkt": _traceroute_raw_debug_limited_repr(pkt),
                "repr_decoded": _traceroute_raw_debug_limited_repr(decoded),
                "repr_routing": _traceroute_raw_debug_limited_repr(
                    (rec.get("routing") if isinstance(rec, dict) else None)
                    or (decoded.get("routing") if isinstance(decoded, dict) else None)
                    or (pkt.get("routing") if isinstance(pkt, dict) else None)
                ),
                "repr_payload": _traceroute_raw_debug_limited_repr(
                    (rec.get("payload") if isinstance(rec, dict) else None)
                    or (decoded.get("payload") if isinstance(decoded, dict) else None)
                    or (pkt.get("payload") if isinstance(pkt, dict) else None)
                ),
            }

            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    except Exception as e:
        try:
            print(f"⚠️ traceroute raw debug append error: {type(e).__name__}: {str(e)[:250]}", flush=True)
        except Exception:
            pass

_append_lock = threading.Lock()

_pool_reconnected_recently = False

# === [NUEVO] Marcas de conexión para backoff por caída temprana ===
_LAST_CONNECT_TS = 0.0     # se fija en _on_connection

import os
try:
    BASE_COOLDOWN_SECS = int(os.getenv("BROKER_BASE_COOLDOWN_SECS", "90"))
except Exception:
    BASE_COOLDOWN_SECS = 90

# Permite configurar por variable de entorno
try:
    _EARLY_DROP_WINDOW = float(os.getenv("BROKER_EARLY_DROP_WINDOW", "20.0"))
except Exception:
    _EARLY_DROP_WINDOW = 20.0

# CUÁNTAS caídas tempranas suprimimos tras /reconectar (por defecto 2)
try:
    _SUPPRESS_EARLY_ESC_DEFAULT_REMAIN = int(os.getenv("BROKER_SUPPRESS_EARLY_ESC_REMAIN", "4"))
except Exception:
    _SUPPRESS_EARLY_ESC_DEFAULT_REMAIN = 4

# Objetivo de escalado cuando ya no hay gracia (por defecto 180)
try:
    _EARLY_ESC_TARGET = int(os.getenv("BROKER_EARLY_ESC_TARGET", "60"))
except Exception:
    _EARLY_ESC_TARGET = 60

import threading

# === [NUEVO] Coordinación de reconexiones ===
_RECONNECT_TIMER = None          # último Timer programado (para cancelarlo si hay otro)
_CONNECT_LOCK = threading.Lock() # serialize resume()
_CONNECTING = False              # flag: hay un resume() en curso

# === [NUEVO] estado de conectividad (para /reconectar del bot)
_IS_CONNECTED = False

# === NUEVO: scheduler de tareas en el broker ===
import broker_task as broker_tasks

# === [NUEVO] Control de heartbeats ===
import logging

# --- [NUEVO] Control de impresión de heartbeat ---
HEARTBEAT_INTERVAL_SECS = 15   # Mantiene tu comportamiento actual si no pasas flags
HEARTBEAT_SILENT = False       # Si True, no imprime ningún heartbeat

# === [NUEVO] Cooldown broker tras caída de conexión ===
COOLDOWN_SECS = int(os.getenv("BROKER_COOLDOWN_SECS", "90"))

# === BLOQUEO FORZADO BBS (Bridge / Triple-Bridge) ===
# Si TRIPLE_BLOCK_BBS_FORCE=1:
#  - Cualquier tráfico originado por la BBS NO cruzará el bridge.
#  - Se fuerza no_bridge=True aunque el texto no empiece por '#BBS'.
#
# Activar en .env:
#   TRIPLE_BLOCK_BBS_FORCE=1

TRIPLE_BLOCK_BBS_FORCE = _env_truthy("TRIPLE_BLOCK_BBS_FORCE", "0")


def _is_bbs_origin(kwargs: dict) -> bool:
    """
    Detecta si el envío proviene del motor BBS.
    Se basa en flags explícitos añadidos al payload.
    """
    try:
        if not isinstance(kwargs, dict):
            return False

        origin = (kwargs.get("origin") or "").strip().lower()
        if origin in {"bbs", "bbs_engine", "bbs_local"}:
            return True

        meta = kwargs.get("meta")
        if isinstance(meta, dict) and bool(meta.get("bbs")):
            return True

    except Exception:
        return False

    return False


# === Nodo B del bridge (usado por /ver_nodos_b y /vecinos_b) ===
B_HOST = (
    os.getenv("BRIDGE_B_HOST", "").strip()
    or os.getenv("B_HOST", "").strip()
)
try:
    B_PORT = int(os.getenv("BRIDGE_B_PORT", os.getenv("B_PORT", "4403")))
except Exception:
    B_PORT = 4403


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

# === [FIX 24/7] Anti-duplicado para SEND_TEXT recibido por CTRL =================
import hashlib

_CTRL_SENDTEXT_DEDUP: dict[str, float] = {}

def _ctrl_sendtext_fingerprint(ch: int, dest: str | None, text: str) -> str:
    """
    Huella estable del comando SEND_TEXT (CTRL) para suprimir reintentos idénticos.
    """
    base = f"{int(ch)}|{dest or 'broadcast'}|{text}"
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()

def _ctrl_sendtext_should_suppress(fp: str, now_ts: float, window_sec: int) -> bool:
    """
    Devuelve True si ya vimos esa huella dentro de la ventana.
    Limpia entradas antiguas para evitar crecimiento infinito.
    """
    last = _CTRL_SENDTEXT_DEDUP.get(fp)
    if last is not None and (now_ts - float(last)) <= float(window_sec):
        return True

    # registra y GC básico
    _CTRL_SENDTEXT_DEDUP[fp] = float(now_ts)

    # GC: purga entradas fuera de ventana * 2 (margen)
    try:
        cutoff = now_ts - float(window_sec) * 2.0
        dead = [k for k, v in _CTRL_SENDTEXT_DEDUP.items() if float(v) < cutoff]
        for k in dead:
            _CTRL_SENDTEXT_DEDUP.pop(k, None)
    except Exception:
        pass

    return False
# ===============================================================================



# === Throttle de logs de guards y barrera TX ===
import threading, time

_guard_last_log = {"sendToRadio": 0.0, "sendHeartbeat": 0.0}

def _guard_log(kind: str, msg: str, interval: float = 5.0):
    now = time.time()
    last = _guard_last_log.get(kind, 0.0)
    if now - last >= interval:
        print(msg, flush=True)
        _guard_last_log[kind] = now

# === Throttle de logs de ctrl SEND_TEXT (evita floods) ===
# BROKER_CTRL_VERBOSE:
#   0 -> throttle (modo recomendado 24/7)
#   1 -> más verboso, pero AÚN amortiguado (intervalo mínimo)
#   2 -> sin throttle (solo debugging puntual)
_ctrl_verbose_raw = os.getenv("BROKER_CTRL_VERBOSE", "0").strip().lower()
if _ctrl_verbose_raw in {"true","yes","on"}:
    CTRL_VERBOSE_LEVEL = 1
else:
    try:
        CTRL_VERBOSE_LEVEL = int(_ctrl_verbose_raw or "0")
    except Exception:
        CTRL_VERBOSE_LEVEL = 0

# Intervalo mínimo en modo verbose=1 (evita floods incluso cuando está activo)
CTRL_VERBOSE_MIN_INTERVAL = float(os.getenv("BROKER_CTRL_VERBOSE_MIN_INTERVAL", "0.5") or "0.5")

_ctrl_last_log = {}

def _ctrl_log(kind: str, msg: str, interval: float = 5.0):
    """
    Log de control amortiguado por 'kind'.

    - BROKER_CTRL_VERBOSE=0: imprime como máximo 1 vez cada 'interval' segundos por 'kind'.
    - BROKER_CTRL_VERBOSE=1: imprime más a menudo, pero sigue amortiguado (intervalo mínimo).
    - BROKER_CTRL_VERBOSE>=2: imprime siempre (sin throttle).
    """
    try:
        lvl = int(CTRL_VERBOSE_LEVEL)
    except Exception:
        lvl = 0

    if lvl >= 2:
        print(msg, flush=True)
        return

    # En verbose=1, nunca permitir intervalos por debajo del mínimo configurado
    eff_interval = float(interval)
    if lvl == 1:
        eff_interval = max(eff_interval, CTRL_VERBOSE_MIN_INTERVAL)

    now = time.time()
    last = float(_ctrl_last_log.get(kind, 0.0))
    if (now - last) >= eff_interval:
        print(msg, flush=True)
        _ctrl_last_log[kind] = now


def _print_frame_line(*values, sep=" ", end="\n", file=None, flush=False):
    """
    Imprime una línea de trama SOLO si SHOW_FRAMES=True.
    Emula la firma de print() para ser un drop-in replacement.
    """
    if not SHOW_FRAMES:
        return
    if file is None:
        file = sys.stdout
    try:
        print(*values, sep=sep, end=end, file=file, flush=flush)
    except Exception:
        try:
            s = sep.join(map(str, values))
            print(s, end=end, file=file, flush=flush)
        except Exception:
            pass

def _parse_channel_names_env(raw: str) -> dict[int, str]:
    """
    Convierte '0:ZARAGOZA,1:GENERAL,2:MADRID' en {0:'ZARAGOZA',1:'GENERAL',2:'MADRID'}.
    Acepta separadores ',' ';' y asignación ':' '='.
    Ignora entradas inválidas sin romper.
    """
    out: dict[int, str] = {}
    if not raw:
        return out

    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return out

    # Normalizar separadores
    for part in s.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, v = part.split(":", 1)
        elif "=" in part:
            k, v = part.split("=", 1)
        else:
            continue

        try:
            idx = int(str(k).strip())
        except Exception:
            continue

        name = str(v).strip().strip('"').strip("'")
        if name:
            out[idx] = name

    return out


# NUEVO: mapa de nombres de canal por índice local (no viene por tramas)
CHANNEL_NAME_BY_INDEX = _parse_channel_names_env(
    os.getenv("BROKER_CHANNEL_NAMES", "")
    or os.getenv("MESH_CHANNEL_NAMES", "")
    or os.getenv("CHANNEL_NAMES", "")
)


def _get_channel_name_from_iface(iface, ch_index: int) -> str | None:
    """
    Intenta obtener el nombre del canal (settings.name) desde la interfaz Meshtastic.
    Soporta estructuras dict u objetos (según versión de librería).
    Devuelve None si no se puede resolver.
    """
    try:
        if iface is None:
            return None

        local = getattr(iface, "localNode", None)
        if local is None:
            return None

        chs = getattr(local, "channels", None)
        if not isinstance(chs, (list, tuple)):
            return None

        if ch_index is None:
            return None
        ch_index = int(ch_index)
        if ch_index < 0 or ch_index >= len(chs):
            return None

        ch = chs[ch_index]

        # Caso dict
        if isinstance(ch, dict):
            settings = ch.get("settings") or {}
            if isinstance(settings, dict):
                name = settings.get("name")
                name = (str(name).strip() if name is not None else "")
                return name or None
            return None

        # Caso objeto (protobuf-ish)
        settings = getattr(ch, "settings", None)
        name = getattr(settings, "name", None) if settings is not None else None
        name = (str(name).strip() if name is not None else "")
        return name or None

    except Exception:
        return None


# Barrera de TX: mientras esté activa, no se intenta ningún envío
TX_BLOCKED = threading.Event()


class _CooldownCtrl:
    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0  # epoch seconds hasta que termina el cooldown

    def enter(self, seconds: int = COOLDOWN_SECS):
        """
        Activa el cooldown durante 'seconds' segundos.
        Solo actualiza la ventana temporal; la lógica de pausar la interfaz
        y bloquear TX se hace en _on_disconnect/_delayed_resume o en
        comandos de control explícitos (BROKER_PAUSE, BROKER_DISCONNECT, etc.).
        """
        with self._lock:
            self._until = int(time.time()) + max(1, int(seconds))

    def is_active(self) -> bool:
        with self._lock:
            return int(time.time()) < self._until

    def remaining(self) -> int:
        with self._lock:
            return max(0, self._until - int(time.time()))

    def clear(self):
        with self._lock:
            self._until = 0
        try:
            TX_BLOCKED.clear()  # [NUEVO] liberar TX al terminar cooldown
        except Exception:
            pass    

COOLDOWN = _CooldownCtrl()

# --- [NUEVO] Próximo cooldown forzado (se consume una sola vez) ---
COOLDOWN_FORCE_NEXT = None
COOLDOWN_FORCE_LOCK = threading.Lock()

# === HOME_NODE_ID (DM estricto) ===
def _norm_node_id(v: str) -> str:
    """
    Normaliza node_id a formato '!xxxxxxxx' en minúsculas.
    Acepta '9ef0c2cc' o '!9ef0c2cc'.
    """
    v = (v or "").strip().lower()
    if not v:
        return ""
    if not v.startswith("!"):
        v = "!" + v
    return v

HOME_NODE_ID = _norm_node_id(os.getenv("HOME_NODE_ID", ""))



# --- [NUEVO] Inicialización global (arriba del fichero, con otros singletons) ---
try:
    from positions_store import TelemetryStore
    TELE_STORE = TelemetryStore(jsonl_path="bot_data/telemetry_log.jsonl")
except Exception:
    TELE_STORE = None


# === [NUEVO] Helper robusto para extraer IDs de from/to en paquetes ===
def _extract_ids_from_packet(pkt: dict, decoded: dict) -> tuple[str, str]:
    """
    Devuelve (who_from, who_to) siempre definidos.
    Intenta varias claves habituales que pueden aparecer en diferentes versiones/estructuras.
    """
    who_from = (
        pkt.get("fromId")
        or decoded.get("fromId")
        or pkt.get("from")
        or decoded.get("from")
        or "?"
    )
    who_to = (
        pkt.get("toId")
        or decoded.get("toId")
        or pkt.get("to")
        or decoded.get("to")
        or "^all"
    )
    return str(who_from), str(who_to)

# Helpers reutilizables (colócalos junto a otros helpers de logging):
def _cooldown_total_secs():
    try:
        cd = globals().get("COOLDOWN")
        if not cd:
            return None
        # soporta dict con 'total' o un objeto con atributo 'total'
        total = cd.get("total") if isinstance(cd, dict) else getattr(cd, "total", None)
        return int(total) if total else None
    except Exception:
        return None

def _cooldown_remaining_secs():
    import time
    try:
        cd = globals().get("COOLDOWN")
        if not cd:
            return None
        until = cd.get("until") if isinstance(cd, dict) else getattr(cd, "until", None)
        if not until:
            return None
        rem = int(max(0, until - time.time()))
        return rem
    except Exception:
        return None

def _fmt_secs(n):
    try:
        return f"{int(n)}s"
    except Exception:
        return "?"

class _NoHeartbeatLogs(logging.Filter):
    """
    Filtro de logging para ocultar trazas relacionadas con heartbeats
    cuando SHOW_HEARTBEATS es False.
    """
    HB_MARKERS = (
        "sendHeartbeat",         # meshtastic.mesh_interface
        "Heartbeat",             # genérico
        "HEARTBEAT_APP",         # nombre de port
        "portnum: HEARTBEAT",    # dumps de paquetes
        "Reprogramada (daily)",   # ← añadido
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        # Ocultar siempre la reprogramación daily
        if "Reprogramada (daily)" in msg:
            return False

        if SHOW_HEARTBEATS:
            return True

        return not any(k in msg for k in self.HB_MARKERS)

# === [NUEVO] Guardas anti-10053/10054 en hilos internos de meshtastic ===
def install_meshtastic_send_guards(verbose: bool = False):
    """
    Envuelve sendHeartbeat() y _sendToRadio() para:
      - Cortar envíos si el broker está en pausa/cooldown/barrera TX (short-circuit).
      - Evitar spam de logs (throttle).
      - Activar cooldown ante errores 10053/10054/OSError.
      - Publicar 'meshtastic.connection.lost' cuando procede.

    Parámetros:
      verbose (bool): si True, imprime trazas adicionales durante la instalación.
    """
    try:
        from meshtastic.mesh_interface import MeshInterface
    except Exception:
        print("[guard] MeshInterface no disponible; no se instalan guards", flush=True)
        return

    if verbose:
        print("[guard] Instalando guards de sendHeartbeat/_sendToRadio...", flush=True)

    # ======== Guard: sendHeartbeat() ========
    if hasattr(MeshInterface, "sendHeartbeat"):
        _orig_sendHeartbeat = MeshInterface.sendHeartbeat

        def _safe_sendHeartbeat(self, *args, **kwargs):
            try:
                now = time.time()
                post_until = float(globals().get("_POST_CONNECT_ALLOW_UNTIL") or 0.0)
                min_int = float(globals().get("_HEARTBEAT_MIN_SECS", 25.0) or 25.0)
                last_hb = float(globals().get("_LAST_HEARTBEAT_TS") or 0.0)
                no_hb = bool(globals().get("NO_HEARTBEAT_MODE", False))

                # === 1) SIEMPRE permitir durante la ventana post-conexión ===
                if now < post_until:
                    ret = _orig_sendHeartbeat(self, *args, **kwargs)
                    globals()["_LAST_HEARTBEAT_TS"] = now
                    return ret

                # === 2) Si NO estamos en modo --no-heartbeat → permitir normal ===
                if not no_hb:
                    ret = _orig_sendHeartbeat(self, *args, **kwargs)
                    globals()["_LAST_HEARTBEAT_TS"] = now
                    return ret

                # === 3) MODO --no-heartbeat → estrangular: permitir sólo 1 cada min_int seg ===
                if (now - last_hb) >= min_int:
                    ret = _orig_sendHeartbeat(self, *args, **kwargs)
                    globals()["_LAST_HEARTBEAT_TS"] = now
                    return ret

                # Demás casos: suprimir silenciosamente (no rompemos la sesión con floods)
                return None

            except Exception:
                # En caso de duda, mejor dejar pasar que matar la sesión
                try:
                    ret = _orig_sendHeartbeat(self, *args, **kwargs)
                    globals()["_LAST_HEARTBEAT_TS"] = time.time()
                    return ret
                except Exception:
                    return None



        MeshInterface.sendHeartbeat = _safe_sendHeartbeat

    # ======== Guard: _sendToRadio() ========
    if hasattr(MeshInterface, "_sendToRadio"):
        _orig__sendToRadio = MeshInterface._sendToRadio

        def _safe__sendToRadio(self, *args, **kwargs):

            # === [NUEVO] Permitir handshake/frames de control aunque haya cooldown ===
            try:
                now = time.time()
                post_until = float(globals().get("_POST_CONNECT_ALLOW_UNTIL") or 0.0)
                
                # Si aún NO marcamos conexión estable → deja pasar (hello/ping/negociación)
                if not bool(globals().get("_IS_CONNECTED", False)):
                    return _orig__sendToRadio(self, *args, **kwargs)

                # Si estamos dentro de la gracia post-conexión → dejar pasar TODO
                if now < post_until:
                    return _orig__sendToRadio(self, *args, **kwargs)
    
                # Si el paquete es de app de control/administración → deja pasar
                pkt = kwargs.get("packet") if ("packet" in kwargs) else (args[1] if len(args) > 1 else None)
                app = None
                if pkt is not None:
                    # atributo estilo objeto
                    app = getattr(pkt, "app", None)
                    # o dict
                    if app is None and isinstance(pkt, dict):
                        app = pkt.get("app") or pkt.get("app_name") or pkt.get("type")
                allow = globals().get("HANDSHAKE_APPS_ALLOW") or {"admin", "control", "nodeinfo", "traceroute"}
                if app in allow:
                    return _orig__sendToRadio(self, *args, **kwargs)
            except Exception:
                pass
            # === [FIN NUEVO] ===

            try:
                mgr = globals().get("BROKER_IFACE_MGR", None)
                c   = globals().get("COOLDOWN", None)
                if (TX_BLOCKED.is_set()
                    or (c and c.is_active())
                    or (mgr and hasattr(mgr, "is_paused") and mgr.is_paused())):
                    try:
                        reasons = []
                        if TX_BLOCKED.is_set(): reasons.append("TX_BLOCKED")
                        if c and c.is_active(): reasons.append(f"COOLDOWN({c.remaining()}s)")
                        if mgr and hasattr(mgr, "is_paused") and mgr.is_paused(): reasons.append("MGR_PAUSED")
                        _guard_log("sendToRadio", "[guard] _sendToRadio(): short-circuit → " + ",".join(reasons), 5.0)
                    except Exception:
                        pass
                    time.sleep(0.02)
                    return None
            except Exception:
                pass

            try:
                return _orig__sendToRadio(self, *args, **kwargs)
            except (ConnectionResetError, OSError) as e:
                code = getattr(e, "winerror", None)
                is10053 = (code == 10053)
                is10054 = (code == 10054)
                _guard_log("sendToRadio",
                           f"[guard] _sendToRadio() atrapó {type(e).__name__} (10053:{is10053} 10054:{is10054}) → marcando desconexión",
                           5.0)
                try:
                    if hasattr(self, "close"): self.close()
                except Exception:
                    pass
                try:
                    _pub = globals().get("_pub") or globals().get("pub")
                    if _pub and hasattr(_pub, "sendMessage"):
                        _pub.sendMessage("meshtastic.connection.lost", interface=self)
                except Exception:
                    pass
                try:
                    c = globals().get("COOLDOWN", None)
                    if c: c.enter(COOLDOWN_SECS)
                    # Anti-chatter: evita imprimir el “Activado tras _sendToRadio()” más de 1 vez/0.8s
                    try:
                        import time as _t
                        last = float(globals().get("_LAST_CD_MARK", 0.0))
                        now  = _t.time()
                        if (now - last) < 0.8:
                            return None
                        globals()["_LAST_CD_MARK"] = now
                    except Exception:
                        pass

                    print(f"[cooldown] Activado tras _sendToRadio() → {COOLDOWN_SECS}s", flush=True)
                except Exception:
                    pass
                return None
            except Exception as e:
                _guard_log("sendToRadio",
                           f"[guard] _sendToRadio() excepción no prevista: {type(e).__name__}: {e}",
                           5.0)
                raise

        MeshInterface._sendToRadio = _safe__sendToRadio

    print("[guard] Guards de sendHeartbeat/_sendToRadio instalados", flush=True)


def install_heartbeat_log_filter() -> None:
    """
    Aplica el filtro a los loggers de meshtastic y, de forma conservadora,
    también al root (solo filtrado por mensaje).
    """
    f = _NoHeartbeatLogs()
    logging.getLogger("meshtastic").addFilter(f)
    logging.getLogger("meshtastic.mesh_interface").addFilter(f)
    logging.getLogger("broker.tasks").addFilter(f)   # ← importante
    logging.getLogger().addFilter(f)

# === [NUEVO] Modo sin heartbeat: anula el envío del SDK de meshtastic ===
def install_no_heartbeat_mode(verbose: bool = False) -> bool:
    """
    Modo sin-heartbeat (seguro):

    Antes: reemplazaba MeshInterface.sendHeartbeat() por NO-OP → TCP queda mudo → el enlace cae.
    Ahora: NO toca el SDK; solo activa una bandera global para que el guard de sendHeartbeat
          (install_meshtastic_send_guards) estrangule (rate-limit) el heartbeat, pero NO lo elimina.

    Si alguien quiere el comportamiento antiguo (bloqueo total), puede activar:
      NO_HEARTBEAT_STRICT=1

    Variables:
      - MESH_HEARTBEAT_MIN_INTERVAL: segundos mínimos entre heartbeats (default 30)
      - NO_HEARTBEAT_STRICT: 1 para volver al NO-OP antiguo (no recomendado)
    """
    try:
        import os
        import logging
        from meshtastic import mesh_interface as _mi

        # Configurar intervalo mínimo de heartbeat para el guard (por defecto 30s)
        try:
            min_int = float(os.getenv("MESH_HEARTBEAT_MIN_INTERVAL", "30") or "30")
        except Exception:
            min_int = 30.0

        # Guard usa estas globals (ya existen en el fichero)
        globals()["_HEARTBEAT_MIN_SECS"] = max(5.0, min_int)

        strict = (os.getenv("NO_HEARTBEAT_STRICT", "0").strip().lower() in {"1", "true", "on", "si", "sí", "y", "yes"})
        if strict:
            # --- MODO ANTIGUO (NO recomendado): NO-OP total ---
            if not hasattr(_mi, "MeshInterface"):
                if verbose:
                    logging.warning("[no-heartbeat] No se encontró MeshInterface en meshtastic.mesh_interface")
                return False

            def _noop(self, *args, **kwargs):
                return None

            _mi.MeshInterface.sendHeartbeat = _noop
            if verbose:
                logging.warning("[no-heartbeat] STRICT activo: sendHeartbeat=NO-OP")
            return True

        # --- MODO SEGURO: no parcheamos el SDK; lo gestiona el guard ---
        if verbose:
            logging.info("[no-heartbeat] Modo seguro activo (rate-limit %.0fs)", max(5.0, min_int))
        return True

    except Exception as e:
        try:
            import logging
            logging.warning(f"[no-heartbeat] No se pudo activar: {e}")
        except Exception:
            print(f"[no-heartbeat] No se pudo activar: {e}", flush=True)
        return False


def is_heartbeat_packet(pkt: dict) -> bool:
    """
    Devuelve True si el paquete parece ser un heartbeat.
    Se intenta ser robusto ante diferentes formas (dict/strings).
    """
    try:
        d = pkt.get("decoded") or {}
        # Algunas implementaciones exponen el nombre del puerto como str
        port = (
            d.get("portnum")
            or d.get("portnum_name")
            or d.get("portnum_str")
            or d.get("portnumText")   # por si acaso
        )
        if isinstance(port, str) and "HEARTBEAT" in port.upper():
            return True

        # Otras veces solo tenemos un volcado de texto
        s = str(pkt)
        if "HEARTBEAT" in s:
            return True
    except Exception:
        pass
    return False


def _is_heartbeat_from_decoded_or_pkt(portnum, decoded, pkt) -> bool:
    """
    Devuelve True si el paquete parece un heartbeat.
    - Cubre casos típicos: "HEARTBEAT_APP", "HEARTBEAT".
    - Tolera diferentes formas de exposición (strings/volcados).
    """
    try:
        # 1) portnum ya viene como string en muchos casos
        if isinstance(portnum, str) and "HEARTBEAT" in portnum.upper():
            return True

        # 2) por si viene con otros nombres
        port_text = (
            decoded.get("portnum_name")
            or decoded.get("portnum_str")
            or decoded.get("portnumText")
        )
        if isinstance(port_text, str) and "HEARTBEAT" in port_text.upper():
            return True

        # 3) fallback por texto
        if "HEARTBEAT" in str(decoded).upper():
            return True
        if "HEARTBEAT" in str(pkt).upper():
            return True
    except Exception:
        pass
    return False

# === [NUEVO] sender seguro para la cola

# === Worker para drenar la cola SENDQ ===
import threading, time

class _BacklogWorker(threading.Thread):
    daemon = True
    def run(self):
        print("[ctrl] BacklogWorker iniciado", flush=True)
        while True:
            try:
                q = globals().get("SENDQ")
                item = None
                if hasattr(q, "take"):
                    item = q.take(timeout=1.0)
                elif hasattr(q, "get"):
                    item = q.get(block=True, timeout=1.0)
                if not item:
                    continue
                _safe_send_to_radio_via_iface_or_fallback(item)
            except Exception as e:
                print(f"[ctrl] BacklogWorker error: {type(e).__name__}: {e}", flush=True)
                time.sleep(0.5)

_worker_instance = None

def start_backlog_worker():
    """
    Compatibilidad histórica.

    Uso:
        start_backlog_worker()

    Funcionalidad:
        - No arranca un segundo worker sobre SENDQ.
        - SENDQ ya tiene su propio hilo interno mediante SENDQ.start().
        - Evita un bucle inútil porque SendQueue no expone métodos take() ni get().
        - Mantiene la llamada existente en main() sin romper arranque ni estructura.

    Motivo:
        En la arquitectura actual, la cola real es broker_resilience.SendQueue.
        Esa cola procesa internamente sus mensajes con su propio hilo _run().
        Mantener _BacklogWorker activo no aporta funcionalidad y puede consumir CPU.
    """
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = "disabled_sendq_internal_worker_already_running"
    return


def _iface_ready_reason() -> tuple[bool, str]:
    """
    Devuelve (ready, reason): False si la TX hacia Meshtastic no debe ejecutarse.

    Ajuste quirúrgico v7.0.12:
    - Se recupera el criterio funcional de v7.0.10-fix2.
    - La existencia de la interfaz principal del broker vuelve a ser el criterio real
      de disponibilidad de TX, siempre que no haya pausa ni cooldown.
    - _IS_CONNECTED deja de ser una barrera dura porque puede quedar desincronizado
      cuando el evento pubsub "meshtastic.connection.established" se descarta como
      interfaz secundaria, aunque mgr.iface ya sea válido.

    Motivo:
    - El fallo observado era:
        SEND_TEXT recv -> SEND_TEXT dequeued -> TX en espera — not_connected
    - Eso ocurre porque mgr.iface existe, pero _IS_CONNECTED permanece False.
    - En la versión que funcionaba no existía ese bloqueo por _IS_CONNECTED.

    No afecta:
    - Pausa manual del broker.
    - Cooldown tras caída real.
    - MeshCore-only.
    - APRS.
    - BBS.
    - Bridge embebido.
    """
    try:
        mgr = globals().get("BROKER_IFACE_MGR")
        c = globals().get("COOLDOWN")

        if mgr is None:
            return False, "manager_missing"

        if hasattr(mgr, "is_paused") and mgr.is_paused():
            return False, "paused"

        if c and hasattr(c, "is_active") and c.is_active():
            return False, "cooldown"

        # Criterio recuperado de la versión funcional:
        # si existe interfaz principal, la cola puede intentar transmitir.
        iface = getattr(mgr, "iface", None)

        if iface is None and hasattr(mgr, "get_iface"):
            try:
                iface = mgr.get_iface()
            except Exception:
                iface = None

        if iface is None:
            return False, "disconnected"

        return True, ""

    except Exception:
        return False, "unknown"

def _safe_send_to_radio_via_iface_or_fallback(msg: dict) -> bool:
    """
    Envía un mensaje de SENDQ hacia Meshtastic usando la ruta existente
    _tasks_send_adapter().

    Uso:
        _safe_send_to_radio_via_iface_or_fallback(msg)

    Parámetros esperados en msg:
        {
            "channel": int,
            "text": str,
            "destination": None | "broadcast" | "!nodeid",
            "require_ack": bool,
            "type": "text",
            "no_bridge": bool opcional,
            "origin": str opcional,
            "meta": dict opcional
        }

    Funcionalidad:
        - Si Meshtastic está listo, envía igual que antes.
        - Si está desconectado, en cooldown o pausado, reencola con backoff progresivo.
        - Evita bucle rápido dequeue/requeue durante caídas TCP.
        - Mantiene intactas las protecciones de BBS y bridge mediante no_bridge/origin/meta.
    """
    if not isinstance(msg, dict):
        return False

    now = time.time()

    # ---------------------------------------------------------
    # 0) Backoff diferido por mensaje.
    # Si el mensaje fue reencolado con _next_try_ts, no volver a intentarlo
    # inmediatamente. Esto evita el bucle:
    #   dequeue -> disconnected -> requeue -> dequeue -> disconnected...
    # ---------------------------------------------------------
    try:
        next_try = float(msg.get("_next_try_ts", 0.0) or 0.0)
    except Exception:
        next_try = 0.0

    if next_try > now:
        try:
            SENDQ.offer(msg, coalesce=False)
        except Exception:
            pass

        try:
            time.sleep(min(1.5, max(0.2, next_try - now)))
        except Exception:
            pass

        return False

    # ---------------------------------------------------------
    # 1) Log de desencolado
    # ---------------------------------------------------------
    try:
        _ch = int(msg.get("channel", 0) or 0)
        _dest = msg.get("destination") or "broadcast"
        _txt = str(msg.get("text") or "")
        _ctrl_log(
            "send_text_dequeued",
            f"[ctrl] SEND_TEXT dequeued ch={_ch} dest={_dest} len={len(_txt.encode('utf-8'))}",
            interval=5.0,
        )
    except Exception as _e:
        _ctrl_log(
            "send_text_deq_err",
            f"[ctrl] SEND_TEXT dequeue log error: {type(_e).__name__}: {_e}",
            interval=5.0,
        )

    # ---------------------------------------------------------
    # 2) CircuitBreaker abierto
    # ---------------------------------------------------------
    if not CIRCUIT_BREAKER.can_attempt():
        try:
            wait_count = int(msg.get("_wait_retry", 0) or 0) + 1
            msg["_wait_retry"] = wait_count
            msg["_next_try_ts"] = time.time() + min(30.0, 2.0 + wait_count)
            SENDQ.offer(msg, coalesce=False)
        except Exception:
            pass

        _ctrl_log(
            "circuit_open",
            "[ctrl] CircuitBreaker abierto. TX pausada temporalmente; reintentará con backoff.",
            interval=5.0,
        )

        try:
            time.sleep(1.0)
        except Exception:
            pass

        return False

    # ---------------------------------------------------------
    # 3) Interfaz no lista
    # ---------------------------------------------------------
    ready, reason = _iface_ready_reason()
    if not ready:
        try:
            wait_count = int(msg.get("_wait_retry", 0) or 0) + 1
            msg["_wait_retry"] = wait_count

            # Backoff moderado:
            # 1º: 4s, 2º: 6s, 3º: 8s... máximo 30s.
            delay = min(30.0, 2.0 + (wait_count * 2.0))
            msg["_next_try_ts"] = time.time() + delay

            SENDQ.offer(msg, coalesce=False)
        except Exception:
            pass

        _ctrl_log(
            "tx_wait",
            f"[ctrl] TX en espera — {reason}. Reintentará con backoff.",
            interval=5.0,
        )

        try:
            time.sleep(1.0)
        except Exception:
            pass

        return False

    # ---------------------------------------------------------
    # 4) Interfaz lista: limpiar marcas internas y enviar
    # ---------------------------------------------------------
    try:
        msg.pop("_wait_retry", None)
        msg.pop("_next_try_ts", None)
    except Exception:
        pass

    try:
        r = _tasks_send_adapter(
            ch=int(msg.get("channel", 0) or 0),
            text=str(msg.get("text") or ""),
            dest=msg.get("destination") or "broadcast",
            require_ack=bool(msg.get("require_ack")),
            no_bridge=bool(msg.get("no_bridge", False)),
            origin=(msg.get("origin") or ""),
            meta=(msg.get("meta") if isinstance(msg.get("meta"), dict) else None),
            timeout_s=None,
        )

        ok = bool(r.get("ok")) if isinstance(r, dict) else bool(r)
        if not ok:
            raise RuntimeError(r.get("error") if isinstance(r, dict) else "tx_failed")

        try:
            CIRCUIT_BREAKER.record_success()
        except Exception:
            pass

        return True

    except Exception as e:
        try:
            CIRCUIT_BREAKER.record_error()
        except Exception:
            pass

        try:
            wait_count = int(msg.get("_wait_retry", 0) or 0) + 1
            msg["_wait_retry"] = wait_count
            msg["_next_try_ts"] = time.time() + min(45.0, 5.0 + (wait_count * 3.0))
            SENDQ.offer(msg, coalesce=False)
        except Exception:
            pass

        _ctrl_log(
            "tx_fail",
            f"[ctrl] TX fallo: {type(e).__name__}: {e}. Reencolado con backoff.",
            interval=5.0,
        )

        try:
            time.sleep(1.0)
        except Exception:
            pass

        return False


def _tasks_send_adapter(
    channel: int | None = None,
    message: str | None = None,
    destination: str | None = None,
    require_ack: bool = False,
    **kwargs
) -> dict:
    """
    Adapter de envío usado por:
      - el scheduler (broker_tasks) -> firma histórica (channel, message, destination, require_ack)
      - la cola SENDQ/_safe_send_to_radio_via_iface_or_fallback -> firma por keywords (ch/text/dest/require_ack/timeout_s)

    Objetivo 24/7:
      1) Intentar enviar por la MISMA conexión TCP del broker (iface_mgr) para no abrir 2 sesiones al nodo.
      2) Si no es posible (no iniciado / no conectado / error), caer al adapter resiliente (pool).
    Devuelve: {ok: bool, packet_id?: int, error?: str}
    """
    # --- Compatibilidad con llamadas por keyword (legacy/flex) ---
    # _safe_send_to_radio_via_iface_or_fallback llama así:
    #   _tasks_send_adapter(ch=..., text=..., dest=..., require_ack=..., timeout_s=None)
    if channel is None and "ch" in kwargs:
        try:
            channel = int(kwargs.get("ch") or 0)
        except Exception:
            channel = 0

    if message is None and "text" in kwargs:
        message = str(kwargs.get("text") or "")

    if destination is None and "dest" in kwargs:
        destination = kwargs.get("dest")

    if "require_ack" in kwargs:
        require_ack = bool(kwargs.get("require_ack"))

    # timeout_s (si viene) se aplica solo a la espera de ACK (si procede)
    timeout_s = kwargs.get("timeout_s", None)
    try:
        timeout_s = float(timeout_s) if timeout_s is not None else None
    except Exception:
        timeout_s = None


    # [NUEVO] Flag para impedir espejo hacia bridge (BBS / control interno).
    # Se propaga desde SENDQ como no_bridge=True.
    no_bridge = bool(kwargs.get("no_bridge", False))
    origin = str(kwargs.get("origin") or "").strip().lower()
    scheduled_task = bool(kwargs.get("scheduled_task", False))

    # Seguridad BBS: bloqueo de tráfico hacia bridge
    try:
        # 1) Comandos directos '#BBS'
        if (message is not None) and str(message).lstrip().upper().startswith("#BBS"):
            no_bridge = True

        # 2) Respuestas generadas por la BBS (aunque no empiecen por '#BBS')
        if (not no_bridge) and TRIPLE_BLOCK_BBS_FORCE and _is_bbs_origin(kwargs):
            no_bridge = True

    except Exception:
        pass


    # Normalización final
    try:
        channel_i = int(channel or 0)
    except Exception:
        channel_i = 0
    message_s = "" if message is None else str(message)
    destination_s = None if destination is None else str(destination)

    dest_id = None if (not destination_s or destination_s.lower() == "broadcast") else destination_s

    # 1) Preferente: usar la interfaz activa del broker
    try:
        mgr = globals().get("BROKER_IFACE_MGR")
        if mgr is not None:
            # Obtener iface con tolerancia a distintas implementaciones
            iface = None
            for attr in ("get_iface", "get_interface"):
                if hasattr(mgr, attr):
                    iface = getattr(mgr, attr)()
                    break
            if iface is None:
                iface = getattr(mgr, "iface", None)
            if iface is None:
                raise RuntimeError("iface no disponible (todavía no conectado)")

            pkt = iface.sendText(
                message_s,
                destinationId=(dest_id if dest_id else "^all"),  # broadcast explícito
                wantAck=bool(require_ack),                       # ACK solo tiene sentido en unicast
                wantResponse=False,
                channelIndex=int(channel_i),
            )

            # [NUEVO] Persistir también el TX del broker en OFFLINE_LOG para que bridgehub (hub_mode=broker)
            # lo vea vía FETCH_BACKLOG y lo reenvíe igual que si fuera RX del nodo embebido.
            try:
                _ts = int(time.time())
                rec_tx = {
                    "ts": _ts,
                    "rx_time": _ts,                 # IMPORTANTE: FETCH_BACKLOG filtra por rx_time
                    "channel": int(channel_i),
                    "portnum": "TEXT_MESSAGE_APP",
                    "from": "BROKER",               # origen lógico
                    "to": (dest_id if dest_id else "^all"),
                    "from_alias": "broker",
                    "to_alias": None,
                    "text": message_s,
                    # metadatos para evitar ambigüedad aguas abajo
                    "direction": "tx",
                    "origin": "broker_local",
                    "no_bridge": bool(no_bridge),
                }
                append_offline_log(rec_tx)
            except Exception as _e:
                print(f"⚠️ offline_log TX mirror failed: {type(_e).__name__}: {_e}", flush=True)

            # [NUEVO] espejo hacia B si la pasarela embebida está activa
            # IMPORTANTE: usar wrapper tolerante a firma para no romper 24/7 si cambia el hook
            # Incorporamos DELAYED entre envios: cambio de _bridge_mirror_safe a _bridge_mirror_delayed
            if not no_bridge:
                _bridge_mirror_delayed(
                    channel=int(channel_i),
                    message=message_s,
                    dest_id=(dest_id if dest_id else None),
                    require_ack=bool(require_ack),
                )

                # [FIX] Cuando el embebido activo es MeshCore (nodo B), los TX locales
                # del broker (incluidas tareas programadas) también deben reenviarse.
                # Se limita a broadcast/canal para mantener semántica actual de mapeo CH->B.
                try:
                    mc = globals().get("MESHCORE_ENGINE")
                    if (
                        mc
                        and getattr(mc, "enable", False)
                        and (dest_id is None)
                        and (not origin.startswith("bot"))
                        and str(message_s).strip()
                    ):
                        ch_name = None
                        try:
                            ch_name = (globals().get("CHANNEL_NAME_BY_INDEX") or {}).get(int(channel_i))
                        except Exception:
                            ch_name = None

                        if scheduled_task:
                            # Mantener paridad con forward_from_meshtastic:
                            # los comandos APRS no deben reflejarse hacia MeshCore.
                            if str(message_s).lstrip().lower().startswith("/aprs"):
                                pass
                            else:
                                _m = (getattr(mc, "ch_map", None) or {}).get(int(channel_i)) or {}
                                _k = str(_m.get("kind") or "contact").strip().lower()
                                # [FIX v7.0.10-fix1] Mantener el mismo saneamiento APRS
                                # también en tareas programadas, que no pasan por forward_from_meshtastic().
                                try:
                                    _mc_message = _clean_aprs_position_text_for_meshcore(str(message_s))
                                except Exception:
                                    _mc_message = str(message_s)

                                if _k in ("chan", "channel"):
                                    _mc_ch = _m.get("channel_idx")
                                    if _mc_ch is not None:
                                        mc.enqueue_send_channel(int(_mc_ch), _mc_message)
                                else:
                                    _contact = _m.get("contact") or getattr(mc, "default_contact_prefix", None)
                                    if _contact:
                                        mc.enqueue_send_contact(str(_contact), _mc_message)
                        else:
                            mc.forward_from_meshtastic(
                                ch=int(channel_i),
                                text=str(message_s),
                                from_id="BROKER",
                                from_alias="BROKER",
                                channel_name=(ch_name or None),
                                hop_real=None,
                            )
                except Exception as _e_mc_tx:
                    print(f"⚠️ meshcore→fw(tx): {_e_mc_tx}", flush=True)


            print(
                f"[tx] broker sendText ch={int(channel_i)} dest={dest_id or 'broadcast'} "
                f"len={len(message_s.encode('utf-8'))}",
                flush=True
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

            # Si se pide ACK (solo unicast) e iface lo soporta, esperar
            if require_ack and dest_id and pid is not None and hasattr(iface, "waitForAck"):
                try:
                    _to = 15.0 if timeout_s is None else max(1.0, float(timeout_s))
                    ok_ack = bool(iface.waitForAck(pid, timeout=_to))
                except Exception:
                    ok_ack = False
                return {"ok": ok_ack, "packet_id": pid, "error": (None if ok_ack else "NO_APP_ACK")}

            # Broadcast o sin ACK → OK con el envío
            return {"ok": True, "packet_id": pid, "error": None}
    except Exception:
        # seguimos al fallback
        pass

    # 2) Fallback: usar el adapter resiliente (pool) como ya hacías
    try:
        try:
            from meshtastic_api_adapter import send_text_simple_with_retry_resilient as _send
        except Exception:
            from meshtastic_api_adapter import send_text_simple_with_retry as _send  # fallback

        host = globals().get("RUNTIME_MESH_HOST") or "127.0.0.1"
        port = globals().get("RUNTIME_MESH_PORT") or 4403

        res = _send(
            host=host,
            port=port,
            text=message_s,
            dest_id=dest_id,
            channel_index=int(channel_i),
            want_ack=bool(require_ack),
        )
        ok = bool(res.get("ok"))
        pid = res.get("packet_id")
        return {"ok": ok, "packet_id": pid, "error": (None if ok else res.get("error"))}
    except Exception as e:
        return {"ok": False, "packet_id": None, "error": f"{type(e).__name__}: {e}"}
def _tasks_reconnect_adapter() -> bool:
    """
    Preferente: pedir al broker (iface_mgr) que se reconecte él.
    Fallback: usar mesh_reconnect() del adapter/pool si existiera.
    """
    try:
        mgr = globals().get("BROKER_IFACE_MGR")
        if mgr is not None and hasattr(mgr, "signal_disconnect"):
            mgr.signal_disconnect()   # fuerza ciclo de reconexión del propio broker
            return True
    except Exception:
        pass

    # Fallback: reconexión del pool (si existiera el helper)
    try:
        from meshtastic_api_adapter import mesh_reconnect
        host = globals().get("RUNTIME_MESH_HOST") or "127.0.0.1"
        port = globals().get("RUNTIME_MESH_PORT") or 4403
        return bool(mesh_reconnect(host=host, port=port))
    except Exception:
        return False

# === [NUEVO] resiliencia
from broker_resilience import CircuitBreaker, Watchdog, SendQueue

# Instancias globales
CIRCUIT_BREAKER = CircuitBreaker(max_errors=5, window_secs=30, open_secs=90, halfopen_successes=3)

def _on_starvation():
    """Watchdog: si no hay tráfico N segundos, pedimos reapertura suave."""
    try:
        mgr = globals().get("BROKER_IFACE_MGR")
        if mgr and hasattr(mgr, "signal_disconnect"):  # v5 ya tiene pause/resume y signal_disconnect
            mgr.signal_disconnect()
    except Exception:
        pass

WATCHDOG = Watchdog(idle_secs=120, on_starvation=_on_starvation)
WATCHDOG.start()

SENDQ = SendQueue(maxsize=200, coalesce_keys=("destination","channel","type"))
# === [NUEVO | MÓDULO] Activar cola y hooks
SENDQ.set_sender(_safe_send_to_radio_via_iface_or_fallback)
SENDQ.on_error(lambda e: CIRCUIT_BREAKER.record_error())
SENDQ.on_success(lambda: CIRCUIT_BREAKER.record_success())
SENDQ.start()



def init_broker_tasks():
    try:
        broker_tasks.configure_sender(
            lambda ch, msg, dst, ack: _tasks_send_adapter(
                channel=ch,
                message=msg,
                destination=dst,
                require_ack=ack,
                scheduled_task=True,
            )
        )
        broker_tasks.configure_reconnect(_tasks_reconnect_adapter)
        # Guarda en ./broker_data/scheduled_tasks.jsonl (separado del bot)
       
         # Guarda en ./bot_data al lado del broker (no CWD):
        _tasks_dir = os.getenv("BOT_DATA_DIR", "/app/bot_data")
        broker_tasks.init(data_dir=_tasks_dir, tz_name="Europe/Madrid", poll_interval_sec=2.0)
        broker_tasks.start()
        
        print("[Tasks@broker] Scheduler iniciado.")
    except Exception as e:
        print(f"[Tasks@broker] No se pudo iniciar: {e}")

def backlog_append(row: dict) -> None:
    """
    Guarda un registro de traceroute en broker_traceroute_log.jsonl.

    Cambio quirúrgico v7.0.4:
      - Convierte el registro a JSON seguro antes de escribirlo.
      - Evita fallos con objetos SDK/protobuf como Routing.
      - Mantiene el fichero broker_traceroute_log.jsonl.
      - No cambia RUN_TRACEROUTE.
      - No cambia TX/RX RF.
      - No toca APRS/BBS/MeshCore/bridge.
    """
    try:
        os.makedirs(os.path.dirname(TRACEROUTE_LOG_PATH), exist_ok=True)

        safe_row = _traceroute_safe_jsonable(row, max_depth=6)

        line = json.dumps(
            safe_row,
            ensure_ascii=False,
            default=str,
        ) + "\n"

        with _TRACEROUTE_LOCK:
            with open(TRACEROUTE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)

    except Exception as e:
        print(f"⚠️ backlog_append error: {type(e).__name__}: {str(e)[:300]}", flush=True)

# === Meshtastic_Broker.py ===
# Sustituye COMPLETA la función append_offline_log por esta versión:

def append_offline_log(rec: dict):
    """
    Persistencia JSONL compatible con panel + retrocompatible con v6 (paquete plano).
    Admite DOS formatos de entrada:
      1) rec = {"packet": {..., "decoded": {...}}}    (formato "nuevo")
      2) rec = {..., "portnum": "...", "text": "..."} (formato "plano" v6)

    Graba:
      - TEXT_MESSAGE_APP  -> text (+payload_hex si existe)
      - POSITION_APP      -> position (+lat/lon/alt atajos)
      - TELEMETRY_APP     -> telemetry (+battery/voltage/channelUtilization/airUtilTx atajos)
      - NODEINFO_APP      -> user (+nodeinfo_* atajos)
      - (Opcional) TRACEROUTE_APP / ROUTING_APP / NEIGHBORINFO_APP (si las añades al set ALLOWED)
    """
    try:
        import os, json, time as _t

        # --- Normalización de orígenes ---
        # v6 pasaba el paquete "plano"; la versión nueva lo envuelve en rec["packet"].
        pkt = (rec or {}).get("packet") or rec or {}
        dec = pkt.get("decoded") or (rec.get("decoded") if isinstance(rec, dict) else {}) or {}

        # --- Detección de puerto (robusta) ---
        port = (
            dec.get("portnum")
            or dec.get("port")
            or pkt.get("portnum")
            or rec.get("portnum")
            or ""
        )
        port = str(port).upper().strip()
        if not port:
            return

        # Tipos permitidos para broker_offline_log.jsonl.
        # Cambio quirúrgico v7.0.2:
        #   - Se añaden TRACEROUTE_APP/ROUTING_APP para que el WebPanel pueda
        #     ver las respuestas reales por FETCH_BACKLOG.
        #   - Se añade NEIGHBORINFO_APP porque ya existe handler RX para ello.
        #   - No se retira ningún tipo existente.
        ALLOWED = {
            "TEXT_MESSAGE_APP",
            "POSITION_APP",
            "TELEMETRY_APP",
            "NODEINFO_APP",
            "NEIGHBORINFO_APP",
            "TRACEROUTE_APP",
            "ROUTING_APP",
            "ADMIN_APP:TRACEROUTE",
            "ADMIN_TRACEROUTE",
        }

        if port not in ALLOWED:
            return

        # --- Campos base (manteniendo nombres históricos) ---
        rx_time_val = (
            pkt.get("rx_time")
            or pkt.get("rxTime")
            or rec.get("rx_time")
            or rec.get("ts")
            or int(_t.time())
        )

        user_dec = dec.get("user") or {}
        from_alias_val = (
            rec.get("from_alias")
            or pkt.get("from_alias")
            or user_dec.get("longName")
            or user_dec.get("shortName")
        )

        TYPE_MAP = {
            "TEXT_MESSAGE_APP": "text",
            "POSITION_APP": "position",
            "TELEMETRY_APP": "telemetry",
            "NODEINFO_APP": "nodeinfo",
            "NEIGHBORINFO_APP": "neighborinfo",
            "TRACEROUTE_APP": "traceroute",
            "ROUTING_APP": "routing",
            "ADMIN_APP:TRACEROUTE": "traceroute",
            "ADMIN_TRACEROUTE": "traceroute",
        }


        row_type = TYPE_MAP.get(port)
        if not row_type:
            return

        obj = {
            "id": pkt.get("id") or rec.get("id"),
            "rx_time": rx_time_val,
            "ts": rec.get("ts") or rx_time_val,
            "channel": (
                pkt.get("channel")
                or (pkt.get("meta") or {}).get("channelIndex")
                or rec.get("channel")
                or (rec.get("summary") or {}).get("canal")
            ),
            "portnum": port,
            "type": row_type,  # usado por el panel
            "from": rec.get("from") or pkt.get("from") or pkt.get("fromId"),
            "to": rec.get("to") or pkt.get("to") or pkt.get("toId"),
            "from_alias": from_alias_val,
            "to_alias": rec.get("to_alias") or pkt.get("to_alias"),
            "rx_rssi": (
                rec.get("rx_rssi")
                if rec.get("rx_rssi") is not None else
                pkt.get("rx_rssi")
                if pkt.get("rx_rssi") is not None else
                pkt.get("rxRssi")
                if pkt.get("rxRssi") is not None else
                (rec.get("summary") or {}).get("rssi")
            ),
            "rx_snr": (
                rec.get("rx_snr")
                if rec.get("rx_snr") is not None else
                pkt.get("rx_snr")
                if pkt.get("rx_snr") is not None else
                pkt.get("rxSnr")
                if pkt.get("rxSnr") is not None else
                (rec.get("summary") or {}).get("snr")
            ),
            "hop_limit": pkt.get("hop_limit") or pkt.get("hopLimit") or rec.get("hop_limit"),
            "hop_start": pkt.get("hop_start") or pkt.get("hopStart") or rec.get("hop_start"),
            "relay_node": pkt.get("relay_node") or pkt.get("relayNode") or rec.get("relay_node"),
        }

        # --- Por tipo ---
        useful = False

        if port == "TEXT_MESSAGE_APP":
            # Texto: mira decoded.text, rec.text, pkt.text o summary.text; payload (bytes/str) como último recurso
            text_val = (
                (dec.get("text") if isinstance(dec, dict) else None)
                or rec.get("text")
                or pkt.get("text")
                or (rec.get("summary") or {}).get("text")
            )
            if text_val is None:
                payload = pkt.get("payload") or rec.get("payload")
                if isinstance(payload, bytes):
                    try:
                        text_val = payload.decode("utf-8", "ignore")
                    except Exception:
                        text_val = None
                elif isinstance(payload, str):
                    text_val = payload

            if text_val:
                obj["text"] = text_val
                useful = True

            payload_hex = (rec.get("summary") or {}).get("payload_hex")
            if payload_hex:
                obj["payload_hex"] = payload_hex

        elif port == "POSITION_APP":
            pos = dec.get("position") or pkt.get("position") or rec.get("position") or {}
            if pos:
                obj["position"] = pos
                if "latitude" in pos and "longitude" in pos:
                    obj["lat"] = pos["latitude"]
                    obj["lon"] = pos["longitude"]
                if "altitude" in pos:
                    obj["alt"] = pos["altitude"]
                useful = True

        elif port == "TELEMETRY_APP":
            tel = dec.get("telemetry") or pkt.get("telemetry") or rec.get("telemetry") or {}
            if tel:
                obj["telemetry"] = tel
                dm = tel.get("deviceMetrics") or {}
                if "batteryLevel" in dm:       obj["battery"]  = dm["batteryLevel"]
                if "voltage" in dm:            obj["voltage"]  = dm["voltage"]
                if "channelUtilization" in dm: obj["ch_util"]  = dm["channelUtilization"]
                if "airUtilTx" in dm:          obj["air_tx"]   = dm["airUtilTx"]
                useful = True

        elif port == "NODEINFO_APP":
            usr = dec.get("user") or pkt.get("user") or rec.get("user") or {}
            if usr:
                obj["user"] = usr
                node_id = usr.get("id") or pkt.get("fromId") or rec.get("fromId")
                if node_id:               obj["nodeinfo_id"] = node_id
                if usr.get("longName"):   obj["nodeinfo_longName"] = usr["longName"]
                if usr.get("shortName"):  obj["nodeinfo_shortName"] = usr["shortName"]
                if usr.get("hwModel"):    obj["nodeinfo_hwModel"] = usr["hwModel"]
                if usr.get("macaddr"):    obj["nodeinfo_macaddr"] = usr["macaddr"]
                if usr.get("publicKey"):  obj["nodeinfo_publicKey"] = usr["publicKey"]
                if "isUnmessagable" in usr:
                    obj["nodeinfo_isUnmessagable"] = bool(usr["isUnmessagable"])
                if not obj.get("from_alias"):
                    alias = usr.get("longName") or usr.get("shortName")
                    if alias:
                        obj["from_alias"] = alias
                useful = True

        elif port == "NEIGHBORINFO_APP":
            # Persistencia ligera de vecinos/saltos.
            # No interpreta la trama en profundidad: conserva campos útiles
            # para topología, históricos y diagnóstico.
            neighborinfo = (
                dec.get("neighborinfo")
                or dec.get("neighbors")
                or pkt.get("neighborinfo")
                or rec.get("neighborinfo")
                or {}
            )

            if neighborinfo:
                obj["neighborinfo"] = neighborinfo

            if rec.get("hops") is not None:
                obj["hops"] = rec.get("hops")
            elif dec.get("hops") is not None:
                obj["hops"] = dec.get("hops")

            if rec.get("via") is not None:
                obj["via"] = rec.get("via")
            elif dec.get("via") is not None:
                obj["via"] = dec.get("via")

            useful = True

        elif port in {"TRACEROUTE_APP", "ROUTING_APP", "ADMIN_APP:TRACEROUTE", "ADMIN_TRACEROUTE"}:
            # Resultado de traceroute/routing recibido por RX.
            #
            # Cambio quirúrgico v7.0.3:
            #   - Conserva la persistencia anterior: text, route_text, target_norm, etc.
            #   - Añade extracción enriquecida para WebPanel:
            #       route_nodes
            #       route_snr
            #       route_back_nodes
            #       route_back_snr
            #       route_display
            #       route_quality_hint
            #       traceroute_payload
            #   - No cambia RUN_TRACEROUTE.
            #   - No transmite RF.
            #   - No toca BBS/APRS/MeshCore/bridge.
            #   - No depende de logs Docker.
            route = dec.get("route") if isinstance(dec, dict) else None
            traceroute = dec.get("traceroute") if isinstance(dec, dict) else None

            pending_ctx = rec.get("pending_ctx") if isinstance(rec.get("pending_ctx"), dict) else None

            if not pending_ctx:
                pending_ctx = _traceroute_match_pending(pkt, dec)

            # Fallback quirúrgico v7.0.5:
            # Algunas respuestas ROUTING_APP llegan como from=local/to=local y no traen
            # el destino solicitado en el paquete RX. Si solo existe un traceroute pendiente
            # reciente, se asocia de forma segura a ese contexto.
            if not pending_ctx:
                pending_ctx = _traceroute_get_single_recent_pending()

            event_type = (
                rec.get("event_type")
                or rec.get("trace_event")
                or "traceroute_result"
            )

            obj["event_type"] = event_type
            obj["trace_event"] = event_type

            if pending_ctx:
                obj["target_requested"] = pending_ctx.get("target_requested")
                obj["target_norm"] = pending_ctx.get("target_norm")
                obj["dest_node_num"] = pending_ctx.get("dest_node_num")
                obj["trace_hop_limit"] = pending_ctx.get("hop_limit")
                obj["trace_ch_index"] = pending_ctx.get("ch_index")
                obj["trace_started_ts"] = pending_ctx.get("started_ts")

            # Si rec trae contexto explícito, prevalece sobre la correlación.
            for k in (
                "target_requested",
                "target_norm",
                "dest_node_num",
                "trace_hop_limit",
                "trace_ch_index",
                "trace_started_ts",
            ):
                if rec.get(k) is not None:
                    obj[k] = rec.get(k)

            # Texto compacto anterior: se conserva.
            route_text = (
                rec.get("route_text")
                or rec.get("text")
                or _traceroute_compact_text(pkt, dec, rec)
            )

            obj["text"] = route_text
            obj["route_text"] = route_text

            if isinstance(route, dict):
                obj["route"] = route

            if isinstance(traceroute, dict):
                obj["traceroute"] = traceroute

            # Campos defensivos habituales según versión del SDK.
            for k in (
                "hop",
                "via",
                "routes",
                "routeNodes",
                "route_nodes",
                "snrTowards",
                "snr_towards",
                "routeBack",
                "route_back",
                "routeBackNodes",
                "route_back_nodes",
                "routeBackSnr",
                "route_back_snr",
                "snrBack",
                "snr_back",
                "routing",
                "payload",
                "raw_payload",
                "payload_hex",
            ):
                try:
                    if rec.get(k) is not None:
                        obj[k] = rec.get(k)
                    elif isinstance(dec, dict) and dec.get(k) is not None:
                        obj[k] = dec.get(k)
                    elif isinstance(pkt, dict) and pkt.get(k) is not None:
                        obj[k] = pkt.get(k)
                except Exception:
                    pass

            # Enriquecimiento no invasivo para WebPanel.
            try:
                enriched = _traceroute_extract_enriched_route(pkt, dec, rec)

                obj["route_nodes"] = enriched.get("route_nodes") or []
                obj["route_snr"] = enriched.get("route_snr") or []
                obj["route_back_nodes"] = enriched.get("route_back_nodes") or []
                obj["route_back_snr"] = enriched.get("route_back_snr") or []
                obj["snr_towards"] = enriched.get("route_snr") or []
                obj["snr_back"] = enriched.get("route_back_snr") or []
                obj["route_data"] = enriched.get("route_data") or {}
                obj["forward_path"] = (enriched.get("route_data") or {}).get("forward_path") or []
                obj["return_path"] = (enriched.get("route_data") or {}).get("return_path") or []
                obj["actual_rf_path"] = (enriched.get("route_data") or {}).get("actual_rf_path") or []
                obj["route_quality_hint"] = enriched.get("route_quality_hint")
                obj["routing_error_reason"] = enriched.get("routing_error_reason")
                obj["traceroute_payload_keys"] = enriched.get("traceroute_payload_keys") or []
                obj["route_discovery_decoded"] = bool(enriched.get("route_discovery_decoded"))
                obj["route_raw_present"] = bool(enriched.get("route_raw_present"))
                obj["route_back_raw_present"] = bool(enriched.get("route_back_raw_present"))
                obj["snr_raw_present"] = bool(enriched.get("snr_raw_present"))
                obj["snr_back_raw_present"] = bool(enriched.get("snr_back_raw_present"))

                # route_display será el texto preferente para el WebPanel.
                qh = str(enriched.get("route_quality_hint") or "")
                if qh.startswith("routing_error_"):
                    route_display = f"Sin ruta RF: {qh.replace('routing_error_', '')}"
                elif qh == "routing_ack_without_route":
                    route_display = "Respuesta ROUTING_APP sin RouteDiscovery"
                elif qh == "start_marker_no_rf_route":
                    route_display = str(route_text or "traceroute iniciado")
                else:
                    route_display = (
                        enriched.get("route_text_enriched")
                        or route_text
                        or ""
                    )
                obj["route_back_display"] = enriched.get("route_back_text_enriched") or ""
                obj["route_display"] = route_display

                # Si el enriquecimiento ha generado una ruta mejor que el texto anterior,
                # no destruimos route_text original; añadimos route_text_enriched.
                obj["route_text_enriched"] = route_display

                payload = enriched.get("traceroute_payload")
                if payload:
                    obj["traceroute_payload"] = payload

            except Exception as e_enrich:
                obj["route_display"] = route_text
                obj["route_quality_hint"] = "enrich_error"
                obj["traceroute_enrich_error"] = f"{type(e_enrich).__name__}: {str(e_enrich)[:200]}"

            # Persistencia adicional específica de traceroute.
            # broker_traceroute_log.jsonl queda como histórico especializado.
            try:
                backlog_append(dict(obj))
            except Exception:
                pass

            useful = True

        if not useful:
            return

        # --- Persistencia + rotación (igual que ya tenías) ---
        path = OFFLINE_LOG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)

        try:
            max_bytes = int(os.getenv("OFFLINE_LOG_MAX_BYTES", "52428800"))  # 50 MiB
        except Exception:
            max_bytes = 52428800

        try:
            if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                bak = f"{path}.1"
                try:
                    if os.path.exists(bak):
                        os.remove(bak)
                except Exception:
                    pass
                try:
                    os.replace(path, bak)
                except Exception:
                    pass
        except Exception:
            pass

        # Escritura final JSON-safe.
        # Importante para ROUTING_APP/TRACEROUTE_APP:
        # algunos campos del SDK pueden ser objetos protobuf no serializables.
        safe_obj = _traceroute_safe_jsonable(obj, max_depth=6)

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(safe_obj, ensure_ascii=False, default=str) + "\n")

    except Exception as e:
        _log_ex("append_offline_log failed", e)


def _meshcore_path_chunks_from_payload(data: dict) -> tuple[list[str], int | None, int | None]:
    """
    Extrae la ruta MeshCore anunciada por la librería oficial.

    La API expone `path_len` como número de saltos/repetidores y, cuando el
    log RF pudo correlacionarse, `path` contiene los hashes compactos de esos
    repetidores. `path_hash_size` o `path_hash_mode` indican el tamaño de cada
    hash. Si solo tenemos `path_len`, devolvemos la cuenta sin inventar nodos.
    """
    if not isinstance(data, dict):
        return [], None, None
    try:
        plen = int(data.get("path_len")) if data.get("path_len") is not None else None
    except Exception:
        plen = None
    if plen == 255:
        plen = 0

    hsize = None
    try:
        if data.get("path_hash_size") is not None:
            hsize = int(data.get("path_hash_size"))
        elif data.get("path_hash_mode") is not None:
            mode = int(data.get("path_hash_mode"))
            hsize = mode + 1 if mode >= 0 else None
    except Exception:
        hsize = None
    if not hsize or hsize <= 0:
        hsize = 1

    chunks: list[str] = []
    raw_path = data.get("path")
    if isinstance(raw_path, list):
        for item in raw_path:
            if isinstance(item, dict):
                h = str(item.get("hash") or "").strip().lower()
            else:
                h = str(item or "").strip().lower()
            h = h.replace(":", "").replace(",", "")
            if h:
                chunks.append(h)
        if plen is None:
            plen = len(chunks)
        elif plen >= 0:
            chunks = chunks[:plen]
    else:
        raw = str(raw_path or "").strip().lower().replace(":", "").replace(",", "")
        if raw:
            step = max(2, int(hsize) * 2)
            chunks = [raw[i:i + step] for i in range(0, len(raw), step) if raw[i:i + step]]
            if plen is None:
                plen = len(chunks)
            elif plen >= 0:
                chunks = chunks[:plen]

    return chunks, plen, hsize

def _meshcore_format_repeater_path(data: dict) -> str:
    chunks, plen, hsize = _meshcore_path_chunks_from_payload(data)
    repeaters = data.get("meshcore_repeaters") if isinstance(data, dict) else None
    if isinstance(repeaters, list) and repeaters:
        parts = []
        for idx, repeater in enumerate(repeaters, 1):
            if not isinstance(repeater, dict):
                continue
            name = str(repeater.get("name") or repeater.get("hash") or f"repetidor {idx}").strip()
            snr = repeater.get("snr")
            snr_txt = ""
            try:
                if snr is not None:
                    snr_txt = f" SNR {float(snr):.1f} dB"
            except Exception:
                pass
            pos_txt = ""
            try:
                lat = repeater.get("lat")
                lon = repeater.get("lon")
                if lat is not None and lon is not None and not (float(lat) == 0.0 and float(lon) == 0.0):
                    pos_txt = f" pos {float(lat):.6f},{float(lon):.6f}"
            except Exception:
                pass
            parts.append(f"{name}{snr_txt}{pos_txt}")
        if parts:
            return " -> ".join(parts)
    if chunks:
        return " -> ".join(chunks)
    if plen and plen > 0:
        return f"{plen} repetidor(es), nombres no disponibles"
    if plen == 0:
        return "directo"
    return "desconocida"

def emit_meshcore_rx_to_hub_and_log(
    *,
    ch: int,
    text: str,
    pubkey_prefix: str = "",
    kind: str = "contact",
    chan_idx: int | None = None,
    chan_tag: str | None = None,
    from_alias: str | None = None,
    path_info: dict | None = None,
) -> None:
    """
    Emite un evento al JsonLineHub (para que el BOT lo vea en vivo)
    y lo persiste en OFFLINE_LOG (para FETCH_BACKLOG / replay).

    Motivo:
    - MeshCore->Meshtastic se inyecta por SENDQ (TX interno).
    - El BOT normalmente "ve" lo que entra por el bus JSONL (hub/backlog).
    """

    # Ruta MeshCore: path_len=N repetidores; path contiene hashes si la API/log RF los aporta.
    path_info = path_info if isinstance(path_info, dict) else {}
    mc_path_chunks, mc_path_len, mc_path_hash_size = _meshcore_path_chunks_from_payload(path_info)
    mc_path_text = _meshcore_format_repeater_path(path_info)

    # Canal / nombre de canal
    try:
        ch_i = int(ch)
    except Exception:
        ch_i = 0

    channel_name = None
    try:
        channel_name = CHANNEL_NAME_BY_INDEX.get(int(ch_i))
    except Exception:
        channel_name = None

    # 1) Emitir en vivo al HUB (BOT)
    try:
        hub = globals().get("BROKER_HUB")
        if hub is not None and hasattr(hub, "broadcast_line"):
            ev = {
                "type": "packet",
                "packet": {
                    "fromId": (f"meshcore:{(pubkey_prefix or '').strip()}" if (pubkey_prefix or '').strip() else "meshcore"),
                    "toId": "^all",
                    "rxTime": int(_now_s()),
                    "decoded": {
                        "portnum": "TEXT_MESSAGE_APP",
                        "text": text,
                        # cabecera compatible para extractores que miran header/fromId
                        "header": {
                            "fromId": (f"meshcore:{(pubkey_prefix or '').strip()}" if (pubkey_prefix or '').strip() else "meshcore"),
                        },
                    },
                    # extras útiles (no rompen a quien no los use)
                    "channel": int(ch_i),
                    "channel_name": channel_name,
                    "from_alias": (from_alias or None),
                    "meshcore": 1,
                    "meshcore_kind": kind,
                    "meshcore_chan_idx": chan_idx,
                    "meshcore_chan_tag": ((chan_tag or "").strip() or None),
                    "meshcore_pubkey_prefix": (pubkey_prefix or "").strip() or None,
                    "meshcore_path_len": mc_path_len,
                    "meshcore_path_hash_size": mc_path_hash_size,
                    "meshcore_path": mc_path_chunks,
                    "meshcore_path_text": mc_path_text,
                    "meshcore_repeaters": path_info.get("meshcore_repeaters") if isinstance(path_info.get("meshcore_repeaters"), list) else None,
                    "meshcore_from_name": path_info.get("from_name"),
                    "meshcore_from_lat": path_info.get("from_lat"),
                    "meshcore_from_lon": path_info.get("from_lon"),
                },
                "ts": _now_s(),
            }
            hub.broadcast_line(_json_dumps(ev) + "\n")
    except Exception:
        pass

    # 2) Persistir en OFFLINE_LOG para backlog
    try:
        append_offline_log(
            {
                "ts": int(_now_s()),
                "channel": ch_i,
                "channel_name": channel_name,
                "portnum": "TEXT_MESSAGE_APP",
                "from": (f"meshcore:{(pubkey_prefix or '').strip()}" if (pubkey_prefix or '').strip() else "meshcore"),
                "to": "broadcast",
                "from_alias": (from_alias or None),
                "to_alias": None,
                "text": text,
                "rx_rssi": None,
                "rx_snr": None,
                "meshcore": 1,
                "meshcore_kind": kind,
                "meshcore_chan_idx": chan_idx,
                "meshcore_chan_tag": ((chan_tag or "").strip() or None),
                "meshcore_pubkey_prefix": (pubkey_prefix or "").strip() or None,
                "meshcore_path_len": mc_path_len,
                "meshcore_path_hash_size": mc_path_hash_size,
                "meshcore_path": mc_path_chunks,
                "meshcore_path_text": mc_path_text,
                "meshcore_repeaters": path_info.get("meshcore_repeaters") if isinstance(path_info.get("meshcore_repeaters"), list) else None,
                "meshcore_from_name": path_info.get("from_name"),
                "meshcore_from_lat": path_info.get("from_lat"),
                "meshcore_from_lon": path_info.get("from_lon"),
            }
        )
    except Exception:
        pass

def _iter_backlog_jsonl(since_ts: int | None, until_ts: int | None, channel: int | None, portnums: list[str] | None, limit: int | None):
    """
    NUEVO: iterador que lee el JSONL y filtra. Devuelve dicts ya cargados.
    """
    if not os.path.isfile(OFFLINE_LOG_PATH):
        return
    sent = 0
    with open(OFFLINE_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # filtros
            if since_ts is not None:
                try:
                    if int(obj.get("rx_time") or 0) < int(since_ts):
                        continue
                except Exception:
                    pass
            if until_ts is not None:
                try:
                    if int(obj.get("rx_time") or 0) > int(until_ts):
                        continue
                except Exception:
                    pass
            if channel is not None and obj.get("channel") != channel:
                continue
            if portnums:
                if str(obj.get("portnum")) not in [str(p) for p in portnums]:
                    continue
            yield obj
            sent += 1
            if limit and sent >= limit:
                return




# === [v7.0.20] Observabilidad operativa consciente de RADIO_PROFILE ===========
def _ops_safe_queue_size(obj) -> int | None:
    """Obtiene el tamaño de una cola sin consumir elementos ni alterar su estado.

    Uso:
        pending = _ops_safe_queue_size(SENDQ)

    Parámetros:
        obj:
            Cola o contenedor a inspeccionar. Admite las implementaciones actuales
            de SendQueue, asyncio.Queue, deque y variantes compatibles.

    Funcionalidad:
        - Consulta métodos públicos cuando existen.
        - Usa atributos internos únicamente como fallback de solo lectura.
        - Devuelve None si el tamaño no puede determinarse de forma segura.
    """
    if obj is None:
        return None
    for name in ("qsize", "size", "pending_count"):
        try:
            value = getattr(obj, name, None)
            value = value() if callable(value) else value
            if value is not None:
                return max(0, int(value))
        except Exception:
            pass
    for name in ("_q", "queue", "_queue", "items", "_items"):
        try:
            value = getattr(obj, name, None)
            if value is not None:
                return max(0, len(value))
        except Exception:
            pass
    try:
        return max(0, len(obj))
    except Exception:
        return None


def _ops_safe_queue_capacity(obj) -> int | None:
    """Obtiene la capacidad configurada de una cola sin modificarla.

    Devuelve None para colas sin límite o cuando la implementación no expone
    una capacidad interpretable.
    """
    if obj is None:
        return None
    for name in ("maxsize", "max_size", "capacity", "_maxsize", "_max_size", "max_items"):
        try:
            value = getattr(obj, name, None)
            value = value() if callable(value) else value
            if value is not None:
                ivalue = int(value)
                return ivalue if ivalue > 0 else None
        except Exception:
            pass
    return None


def _ops_profile_runtime_snapshot() -> dict:
    """Construye el estado real del perfil y de sus backends, solo en lectura.

    Fuente de verdad:
        1. RADIO_PROFILE_RUNTIME generado por radio_profile.py durante el arranque.
        2. RADIO_PROFILE normalizado por el resolvedor común.
        3. Variables históricas únicamente como fallback defensivo.

    Perfiles cubiertos:
        - meshcore_only: nodo A exclusivamente MeshCore.
        - meshtastic_only: nodo A exclusivamente Meshtastic.
        - meshtastic_a_meshcore_embedded_b / meshtastic_meshcore_embedded:
          nodo A Meshtastic y nodo B MeshCore embebido.
        - meshcore_a_meshtastic_embedded_b: nodo A MeshCore y nodo B
          Meshtastic embebido.
        - meshtastic_embedded: compatibilidad con doble Meshtastic histórico.
        - legacy/auto: conserva las reglas históricas.

    Esta función no aplica perfiles, no abre conexiones y no toca las colas.
    """
    runtime = globals().get("RADIO_PROFILE_RUNTIME")
    runtime = dict(runtime) if isinstance(runtime, dict) else {}
    raw_profile = str(os.getenv("RADIO_PROFILE") or runtime.get("requested_profile") or "").strip()
    profile = str(runtime.get("profile") or _radio_profile() or "legacy").strip().lower().replace("-", "_")
    if not profile:
        profile = "legacy"

    aliases = {
        "auto": "legacy",
        "meshcore": "meshcore_only",
        "mc_only": "meshcore_only",
        "meshtastic": "meshtastic_only",
        "mt_only": "meshtastic_only",
        "meshtastic_meshcore_embedded": "meshtastic_a_meshcore_embedded_b",
        "meshcore_embedded": "meshtastic_a_meshcore_embedded_b",
        "mixed": "meshtastic_a_meshcore_embedded_b",
        "hybrid": "meshtastic_a_meshcore_embedded_b",
        "dual": "meshtastic_a_meshcore_embedded_b",
        "dual_meshtastic": "meshtastic_embedded",
        "meshtastic_dual": "meshtastic_embedded",
        "": "legacy",
    }
    profile = aliases.get(profile, profile)

    meshcore_enabled = bool(runtime.get("meshcore_enabled")) if "meshcore_enabled" in runtime else _env_truthy("MESHCORE_ENABLE", "0")
    meshtastic_enabled = bool(runtime.get("meshtastic_enabled")) if "meshtastic_enabled" in runtime else (profile != "meshcore_only")
    bridge_enabled = _env_truthy("BRIDGE_ENABLED", "0")
    direction_mode = str(os.getenv("BRIDGE_DIRECTION_MODE") or runtime.get("bridge_direction_mode") or "").strip().lower()

    node_a_transport = str(runtime.get("node_a_transport") or "").strip().lower()
    node_b_transport = str(runtime.get("node_b_transport") or "").strip().lower()
    if not node_a_transport:
        if profile in {"meshcore_only", "meshcore_a_meshtastic_embedded_b"}:
            node_a_transport = "meshcore"
        elif profile != "legacy":
            node_a_transport = "meshtastic"
    if not node_b_transport:
        if profile == "meshtastic_a_meshcore_embedded_b":
            node_b_transport = "meshcore"
        elif profile in {"meshcore_a_meshtastic_embedded_b", "meshtastic_embedded"}:
            node_b_transport = "meshtastic"

    mgr = globals().get("BROKER_IFACE_MGR")
    meshtastic_connected = bool(globals().get("_IS_CONNECTED", False))
    try:
        if mgr is not None:
            iface = getattr(mgr, "iface", None)
            if iface is None and hasattr(mgr, "get_iface"):
                iface = mgr.get_iface()
            meshtastic_connected = bool(iface is not None)
    except Exception:
        pass

    engine = globals().get("MESHCORE_ENGINE")
    try:
        meshcore_status = engine.status() if engine is not None and hasattr(engine, "status") else {}
    except Exception as exc:
        meshcore_status = {"connected": False, "last_err": f"{type(exc).__name__}: {exc}"}
    if not isinstance(meshcore_status, dict):
        meshcore_status = {}

    try:
        bridge_status = bridge_status_in_broker() or {}
    except Exception as exc:
        bridge_status = {"running": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(bridge_status, dict):
        bridge_status = {}

    expects = {
        "meshcore_primary": node_a_transport == "meshcore",
        "meshtastic_primary": node_a_transport == "meshtastic",
        "meshcore_embedded": node_b_transport == "meshcore",
        "meshtastic_embedded": node_b_transport == "meshtastic",
    }
    if profile == "legacy":
        expects = {
            "meshcore_primary": False,
            "meshtastic_primary": not _is_meshcore_only_profile(),
            "meshcore_embedded": bool(meshcore_enabled and not bridge_enabled),
            "meshtastic_embedded": bool(bridge_enabled),
        }

    inconsistencies: list[str] = []
    for warning in runtime.get("warnings") or []:
        if str(warning).strip():
            inconsistencies.append(str(warning).strip())
    if runtime.get("valid") is False:
        inconsistencies.append("radio_profile_runtime_invalid")
    if profile == "meshcore_only" and meshtastic_connected:
        inconsistencies.append("meshcore_only_but_meshtastic_connected")
    if expects["meshcore_embedded"] and not meshcore_enabled:
        inconsistencies.append("meshcore_embedded_required_but_disabled")
    if expects["meshtastic_embedded"] and not (bridge_enabled or profile == "meshcore_a_meshtastic_embedded_b"):
        inconsistencies.append("meshtastic_embedded_required_but_bridge_disabled")
    if profile == "meshtastic_only" and meshcore_enabled:
        inconsistencies.append("meshtastic_only_with_meshcore_enabled")
    if profile == "meshcore_only" and bridge_enabled:
        inconsistencies.append("meshcore_only_with_bridge_enabled")
    if meshcore_enabled and bridge_enabled and profile not in {"meshcore_a_meshtastic_embedded_b"}:
        inconsistencies.append("both_embedded_backends_enabled")
    inconsistencies = list(dict.fromkeys(inconsistencies))

    return {
        "profile_raw": raw_profile or "legacy",
        "profile": profile,
        "runtime_source": "RADIO_PROFILE_RUNTIME" if runtime else "environment_fallback",
        "runtime": runtime,
        "node_a_transport": node_a_transport or None,
        "node_b_transport": node_b_transport or None,
        "direction_mode": direction_mode or None,
        "expects": expects,
        "meshtastic": {
            "required": bool(expects["meshtastic_primary"] or expects["meshtastic_embedded"]),
            "role": "primary" if expects["meshtastic_primary"] else ("embedded" if expects["meshtastic_embedded"] else "not_applicable"),
            "configured": bool(meshtastic_enabled or bridge_enabled),
            # Para el perfil inverso A=MeshCore/B=Meshtastic, la conexión real
            # pertenece al bridge embebido (running + iface_b), no a iface_mgr.
            # En perfiles con Meshtastic principal se conserva el criterio
            # histórico de BROKER_IFACE_MGR.
            "connected": bool(
                meshtastic_connected
                if expects["meshtastic_primary"]
                else (bridge_status.get("running") and bridge_status.get("iface_b"))
                if expects["meshtastic_embedded"]
                else False
            ),
            "host": (
                globals().get("RUNTIME_MESH_HOST") or os.getenv("MESHTASTIC_HOST")
                if expects["meshtastic_primary"]
                else os.getenv("BRIDGE_B_HOST") or os.getenv("B_HOST")
            ),
            "port": (
                globals().get("RUNTIME_MESH_PORT") or os.getenv("MESHTASTIC_PORT")
                if expects["meshtastic_primary"]
                else os.getenv("BRIDGE_B_PORT") or os.getenv("B_PORT")
            ),
            "paused": bool(getattr(mgr, "is_paused", lambda: False)()) if expects["meshtastic_primary"] and mgr is not None and hasattr(mgr, "is_paused") else False,
        },
        "meshcore": {
            "required": bool(expects["meshcore_primary"] or expects["meshcore_embedded"]),
            "role": "primary" if expects["meshcore_primary"] else ("embedded" if expects["meshcore_embedded"] else "not_applicable"),
            "configured": bool(meshcore_enabled),
            **meshcore_status,
        },
        "embedded_meshtastic": {
            "required": bool(expects["meshtastic_embedded"]),
            "configured": bool(bridge_enabled or profile == "meshcore_a_meshtastic_embedded_b"),
            **bridge_status,
        },
        "cooldown": {
            "active": bool(globals().get("COOLDOWN") and globals()["COOLDOWN"].is_active()),
            "remaining": int(globals()["COOLDOWN"].remaining()) if globals().get("COOLDOWN") else 0,
            "tx_blocked": bool(globals().get("TX_BLOCKED") and globals()["TX_BLOCKED"].is_set()),
        },
        "coherent": not inconsistencies,
        "inconsistencies": inconsistencies,
    }


def _ops_queue_runtime_snapshot() -> dict:
    """Resume las colas reales Meshtastic y MeshCore sin extraer mensajes."""
    sendq = globals().get("SENDQ")
    sendq_pending = _ops_safe_queue_size(sendq)
    sendq_capacity = _ops_safe_queue_capacity(sendq)

    engine = globals().get("MESHCORE_ENGINE")
    spool_pending = None
    spool_capacity = None
    spool_drops = 0
    session_pending = None
    try:
        if engine is not None:
            lock = getattr(engine, "_retry_spool_lock", None)
            if lock is not None:
                with lock:
                    spool = getattr(engine, "_retry_spool", None)
                    spool_pending = len(spool) if spool is not None else 0
                    spool_capacity = int(getattr(engine, "_retry_spool_max", 0) or 0) or None
                    spool_drops = int(getattr(engine, "_retry_spool_drop_count", 0) or 0)
            else:
                spool = getattr(engine, "_retry_spool", None)
                spool_pending = len(spool) if spool is not None else 0
                spool_capacity = int(getattr(engine, "_retry_spool_max", 0) or 0) or None
                spool_drops = int(getattr(engine, "_retry_spool_drop_count", 0) or 0)
            session_pending = _ops_safe_queue_size(getattr(engine, "_tx_q", None))
    except Exception:
        pass

    return {
        "meshtastic_sendq": {
            "pending": sendq_pending,
            "capacity": sendq_capacity,
            "utilization": round(sendq_pending / sendq_capacity, 4) if sendq_pending is not None and sendq_capacity else None,
        },
        "meshcore_retry_spool": {
            "pending": spool_pending,
            "capacity": spool_capacity,
            "drops": spool_drops,
            "session_pending": session_pending,
            "utilization": round(spool_pending / spool_capacity, 4) if spool_pending is not None and spool_capacity else None,
        },
    }


def _broker_operations_snapshot() -> dict:
    """Devuelve el estado consolidado usado por el Centro operativo v7.0.20.

    El comando es estrictamente de observación: no inicia radios, no fuerza
    reconexiones, no consume colas y no escribe datos persistentes.
    """
    profile = _ops_profile_runtime_snapshot()
    queues = _ops_queue_runtime_snapshot()
    incidents: list[dict] = []

    def add(code: str, level: str, component: str, title: str, detail: str) -> None:
        incidents.append({
            "code": code,
            "level": level,
            "component": component,
            "title": title,
            "detail": str(detail or "")[:500],
        })

    if not profile.get("coherent", True):
        add("profile_inconsistent", "critical", "radio_profile", "Perfil de radio incoherente", ", ".join(profile.get("inconsistencies") or []))

    mt = profile.get("meshtastic") or {}
    mc = profile.get("meshcore") or {}
    emb = profile.get("embedded_meshtastic") or {}
    expects = profile.get("expects") or {}

    if expects.get("meshtastic_primary") and not mt.get("connected"):
        add("meshtastic_primary_disconnected", "critical", "meshtastic", "Meshtastic principal sin conexión", "El perfil activo exige el nodo Meshtastic principal.")
    if expects.get("meshtastic_embedded") and not (emb.get("running") and emb.get("iface_b")):
        add("meshtastic_embedded_unavailable", "critical", "bridge", "Meshtastic embebido no operativo", emb.get("error") or "bridge/iface B no disponible")
    if (expects.get("meshcore_primary") or expects.get("meshcore_embedded")) and not mc.get("connected"):
        add("meshcore_required_disconnected", "critical", "meshcore", "MeshCore requerido sin conexión", mc.get("last_err") or "sin conexión activa")

    cooldown = profile.get("cooldown") or {}
    if cooldown.get("active"):
        add("meshtastic_cooldown", "warning", "meshtastic", "Broker Meshtastic en cooldown", f"quedan {cooldown.get('remaining', 0)} s")
    if cooldown.get("tx_blocked"):
        add("meshtastic_tx_blocked", "warning", "meshtastic", "Transmisión Meshtastic bloqueada", "TX_BLOCKED está activo")

    for key, queue_data in queues.items():
        util = queue_data.get("utilization")
        if util is not None and util >= 0.90:
            add(f"{key}_critical", "critical", "queue", "Cola casi llena", f"{key}: {queue_data.get('pending')}/{queue_data.get('capacity')}")
        elif util is not None and util >= 0.70:
            add(f"{key}_warning", "warning", "queue", "Presión de cola elevada", f"{key}: {queue_data.get('pending')}/{queue_data.get('capacity')}")
        if int(queue_data.get("drops") or 0) > 0:
            add(f"{key}_drops", "warning", "queue", "Mensajes descartados en cola", f"{key}: descartados={queue_data.get('drops')}")

    order = {"critical": 0, "warning": 1, "info": 2}
    incidents.sort(key=lambda item: (order.get(item.get("level"), 9), item.get("component", ""), item.get("code", "")))
    status = "critical" if any(item["level"] == "critical" for item in incidents) else ("warning" if incidents else "ok")
    return {
        "ok": status != "critical",
        "status": status,
        "generated_at": int(time.time()),
        "profile": profile,
        "queues": queues,
        "incidents": incidents,
        "counts": {
            "total": len(incidents),
            "critical": sum(1 for item in incidents if item["level"] == "critical"),
            "warning": sum(1 for item in incidents if item["level"] == "warning"),
        },
    }
# ============================================================================


class _BacklogServer(threading.Thread):
    """
    Servidor TCP ligero para dos propósitos:
      1) FETCH_BACKLOG: devolver eventos persistidos (JSONL) con filtros.
         Petición:
           {"cmd":"FETCH_BACKLOG","params":{"since_ts":..., "until_ts":..., "channel":..., "portnums":["TEXT_MESSAGE_APP"], "limit":1000}}
         Respuesta:
           {"ok":true, "data":[ ... mensajes ... ]}

      2) Control del broker (conexión al nodo):
         - {"cmd":"BROKER_PAUSE"}   → pausa la conexión persistente (cierra iface y no reconecta hasta resume)
         - {"cmd":"BROKER_RESUME"}  → reanuda conexión persistente (vuelve a conectar)
         - {"cmd":"BROKER_STATUS"}  → {"ok":true, "status":"paused"|"running"}
    """
    def __init__(self, host: str = "127.0.0.1", port: int = BACKLOG_PORT):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self._sock = None
        self._stop = threading.Event()

        # === [FIX 24/7] Protección contra agotamiento de hilos ==================
        # Problema observado:
        #   BacklogServer creaba un hilo nuevo por cada conexión entrante. Si el
        #   WebPanel/Bot/APRS/scheduler lanzaban muchas consultas o alguna quedaba
        #   viva hasta timeout, el proceso podía llegar a:
        #       RuntimeError: can't start new thread
        #
        # Solución:
        #   - limitar handlers simultáneos con BoundedSemaphore;
        #   - rechazar conexiones de control de forma explícita si está saturado;
        #   - cerrar siempre el socket aceptado;
        #   - liberar siempre el slot del semáforo aunque _handle_client falle.
        #
        # Variables opcionales .env:
        #   BACKLOG_MAX_CLIENT_THREADS=24      número máximo de handlers concurrentes
        #   BACKLOG_CLIENT_TIMEOUT_SEC=10      timeout por cliente aceptado
        #   BACKLOG_LISTEN_BACKLOG=32          cola kernel listen()
        try:
            self._max_client_threads = max(4, int(os.getenv("BACKLOG_MAX_CLIENT_THREADS", "24") or "24"))
        except Exception:
            self._max_client_threads = 24

        try:
            self._client_timeout_sec = max(3.0, float(os.getenv("BACKLOG_CLIENT_TIMEOUT_SEC", "10") or "10"))
        except Exception:
            self._client_timeout_sec = 10.0

        try:
            self._listen_backlog = max(16, int(os.getenv("BACKLOG_LISTEN_BACKLOG", "32") or "32"))
        except Exception:
            self._listen_backlog = 32

        self._client_sem = threading.BoundedSemaphore(self._max_client_threads)
        self._active_clients = 0
        self._active_lock = threading.Lock()
        self._busy_drop_count = 0
        self._thread_start_error_count = 0

        # === [FIX 24/7 WEBPANEL] Protección específica para consultas FETCH_BACKLOG ===
        # El WebPanel puede lanzar varias consultas simultáneas/continuas contra el puerto
        # de control. Si alguna consulta FETCH_BACKLOG pide demasiado histórico o usa el
        # formato antiguo con parámetros en raíz, cada handler queda leyendo JSONL más tiempo
        # del necesario. Estos límites impiden que una vista web degrade el broker.
        #
        # Variables opcionales .env:
        #   BACKLOG_FETCH_MAX_LIMIT=500        límite duro de filas por FETCH_BACKLOG
        #   BACKLOG_FETCH_DEFAULT_LIMIT=200    límite si el cliente no envía limit válido
        #   BACKLOG_FETCH_DEFAULT_WINDOW_SEC=900 ventana si no llega since_ts/until_ts
        try:
            self._fetch_max_limit = max(50, int(os.getenv("BACKLOG_FETCH_MAX_LIMIT", "500") or "500"))
        except Exception:
            self._fetch_max_limit = 500

        try:
            self._fetch_default_limit = max(20, int(os.getenv("BACKLOG_FETCH_DEFAULT_LIMIT", "200") or "200"))
        except Exception:
            self._fetch_default_limit = 200

        try:
            self._fetch_default_window_sec = max(0, int(os.getenv("BACKLOG_FETCH_DEFAULT_WINDOW_SEC", "900") or "900"))
        except Exception:
            self._fetch_default_window_sec = 900

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(self._listen_backlog)
            print(
                f"ℹ️ BacklogServer escuchando en {self.host}:{self.port} "
                f"max_clients={self._max_client_threads} timeout={self._client_timeout_sec}s "
                f"backlog={self._listen_backlog} fetch_max={self._fetch_max_limit} "
                f"fetch_default_limit={self._fetch_default_limit} fetch_default_window={self._fetch_default_window_sec}s",
                flush=True,
            )
            while not self._stop.is_set():
                conn = None
                addr = None
                acquired = False
                try:
                    self._sock.settimeout(1.0)
                    try:
                        conn, addr = self._sock.accept()
                    except socket.timeout:
                        continue

                    # Si todos los handlers están ocupados, no intentamos crear
                    # otro hilo. Respondemos error controlado y cerramos.
                    acquired = self._client_sem.acquire(blocking=False)
                    if not acquired:
                        self._reject_busy(conn, addr)
                        conn = None
                        continue

                    th = threading.Thread(
                        target=self._handle_client_guarded,
                        args=(conn, addr),
                        daemon=True,
                        name="backlog-client",
                    )
                    th.start()
                    conn = None  # queda bajo responsabilidad del handler

                except OSError as e:
                    # Cierre normal durante stop(): no ensuciar log.
                    if self._stop.is_set():
                        break
                    if acquired:
                        self._release_client_slot()
                    self._safe_close_conn(conn)
                    print(f"⚠️ BacklogServer accept/socket error: {type(e).__name__}: {e}", flush=True)

                except RuntimeError as e:
                    # Caso crítico observado: can't start new thread.
                    if acquired:
                        self._release_client_slot()
                    self._safe_close_conn(conn)
                    self._thread_start_error_count += 1
                    print(
                        f"⚠️ BacklogServer thread start error: {type(e).__name__}: {e} "
                        f"active={self._get_active_clients()} max={self._max_client_threads} "
                        f"thread_errors={self._thread_start_error_count}",
                        flush=True,
                    )

                except Exception as e:
                    if acquired:
                        self._release_client_slot()
                    self._safe_close_conn(conn)
                    print(f"⚠️ BacklogServer accept error: {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"⚠️ BacklogServer run error: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

    def _handle_client_guarded(self, conn: socket.socket, addr):
        """
        Wrapper 24/7 alrededor de _handle_client().

        Uso interno:
            threading.Thread(target=self._handle_client_guarded, args=(conn, addr), daemon=True).start()

        Funcionalidad:
            - Incrementa/decrementa contador de clientes activos.
            - Ejecuta la lógica existente sin modificar comandos.
            - Libera SIEMPRE el slot del semáforo.
            - Cierra SIEMPRE el socket aunque _handle_client ya lo haya cerrado.
        """
        with self._active_lock:
            self._active_clients += 1
        try:
            try:
                conn.settimeout(float(self._client_timeout_sec))
            except Exception:
                pass
            self._handle_client(conn, addr)
        finally:
            self._safe_close_conn(conn)
            with self._active_lock:
                self._active_clients = max(0, self._active_clients - 1)
            self._release_client_slot()

    def _release_client_slot(self) -> None:
        """Libera un slot de cliente del BacklogServer de forma tolerante a errores."""
        try:
            self._client_sem.release()
        except ValueError:
            # Ya estaba liberado; no debe romper producción.
            pass
        except Exception:
            pass

    def _get_active_clients(self) -> int:
        """Devuelve el número aproximado de handlers activos."""
        try:
            with self._active_lock:
                return int(self._active_clients)
        except Exception:
            return -1

    def _safe_close_conn(self, conn) -> None:
        """Cierre defensivo de sockets aceptados."""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def _reject_busy(self, conn: socket.socket, addr) -> None:
        """
        Rechaza una conexión cuando el BacklogServer está saturado.

        Devuelve JSON para que el cliente pueda registrar 'backlog_busy' en vez de
        quedarse colgado hasta timeout. No crea hilos nuevos.
        """
        self._busy_drop_count += 1
        try:
            resp = {
                "ok": False,
                "error": "backlog_busy",
                "active_clients": self._get_active_clients(),
                "max_clients": int(self._max_client_threads),
                "busy_drops": int(self._busy_drop_count),
            }
            conn.settimeout(1.0)
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass
        finally:
            self._safe_close_conn(conn)
            try:
                if self._busy_drop_count == 1 or self._busy_drop_count % 25 == 0:
                    print(
                        f"⚠️ BacklogServer BUSY: rechazo controlado "
                        f"active={self._get_active_clients()} max={self._max_client_threads} drops={self._busy_drop_count}",
                        flush=True,
                    )
            except Exception:
                pass

    def _handle_client(self, conn: socket.socket, addr):
        try:
            # No fijar 15s rígidos: se usa el timeout configurable del BacklogServer.
            # Esto evita que consultas del WebPanel mantengan handlers ocupados demasiado tiempo.
            conn.settimeout(float(getattr(self, "_client_timeout_sec", 10.0)))
            data = b""
            while True:
                b = conn.recv(65536)
                if not b:
                    break
                data += b
                if b"\n" in b:
                    break

            line = data.decode("utf-8", "ignore").strip()
            try:
                req = json.loads(line)
            except Exception:
                resp = {"ok": False, "error": "invalid json"}
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            cmd = (req.get("cmd") or "").upper()
            params = req.get("params") or {}

            # --- v7.0.20: estado operativo y perfil de radio (solo lectura) ---
            if cmd in {"OPS_STATUS", "RADIO_PROFILE_STATUS"}:
                resp = _broker_operations_snapshot()
                conn.sendall((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                return

            # --- FETCH_BACKLOG (blindado para WebPanel / clientes antiguos) ---
            if cmd == "FETCH_BACKLOG":
                # Compatibilidad: algunos clientes antiguos enviaban since_ts/limit/portnums
                # en la raíz del JSON en vez de dentro de params. Antes eso se ignoraba,
                # provocando lecturas más grandes del histórico. Se aceptan ambos formatos.
                p = params if isinstance(params, dict) else {}

                def _first_param(name: str, default=None):
                    if name in p:
                        return p.get(name)
                    return req.get(name, default)

                since_ts = _first_param("since_ts", None)
                until_ts = _first_param("until_ts", None)
                channel  = _first_param("channel", None)
                portnums = _first_param("portnums", None) or ["TEXT_MESSAGE_APP"]

                # Límite defensivo: evita peticiones masivas desde el WebPanel.
                try:
                    raw_limit = int(_first_param("limit", self._fetch_default_limit) or self._fetch_default_limit)
                except Exception:
                    raw_limit = int(self._fetch_default_limit)
                limit = max(1, min(int(raw_limit), int(self._fetch_max_limit)))

                # Si el cliente no manda ventana temporal, se aplica una ventana reciente
                # para no leer todo el JSONL histórico en cada refresco del WebPanel.
                # Poner BACKLOG_FETCH_DEFAULT_WINDOW_SEC=0 restaura el comportamiento antiguo.
                if since_ts is None and until_ts is None and int(self._fetch_default_window_sec) > 0:
                    try:
                        since_ts = int(time.time()) - int(self._fetch_default_window_sec)
                    except Exception:
                        since_ts = None

                out = list(_iter_backlog_jsonl(since_ts, until_ts, channel, portnums, limit))
                resp = {
                    "ok": True,
                    "data": out,
                    "count": len(out),
                    "limit": int(limit),
                    "since_ts": since_ts,
                    "until_ts": until_ts,
                    "truncated": bool(raw_limit > limit),
                }
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return
            
            elif cmd == "FETCH_TELEMETRY":
                 # params: {"since": <segundos o epoch>, "node": "!id" (opcional), "limit": 200}
                try:
                    since_raw = params.get("since", 0)
                    since = float(since_raw)
                    now = time.time()
                    since_ts = (now - since) if since < 1e10 else since

                    node = params.get("node")
                    limit = int(params.get("limit", 200))

                    rows = TELE_STORE.query_since(since_ts, node_id=node, limit=limit) if TELE_STORE else []
                    resp = {"ok": True, "count": len(rows), "items": rows}
                except Exception as e:
                    resp = {"ok": False, "error": f"telemetry_error: {e}"}


                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return
        
            # --- NUEVO: envío de texto vía la iface persistente del broker ---
            elif cmd == "SEND_TEXT":
                
                # Defensa de perfil: SEND_TEXT pertenece a la ruta Meshtastic.
                # En meshcore_only debe rechazarse antes de encolar para evitar
                # falsos OK y búsquedas posteriores de un manejador inexistente.
                # Los perfiles combinados y el modo legacy conservan su flujo.
                if _is_meshcore_only_profile():
                    resp = {
                        "ok": False,
                        "error": "meshtastic_disabled_by_radio_profile",
                        "profile": "meshcore_only",
                    }
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

                # === Veto por pausa/cooldown/barrera TX ===
                try:
                    mgr = globals().get("BROKER_IFACE_MGR", None)
                    c   = globals().get("COOLDOWN", None)
                    paused = TX_BLOCKED.is_set() or (c and c.is_active()) \
                            or (mgr and hasattr(mgr, "is_paused") and mgr.is_paused())
                    rem = int(c.remaining()) if (c and c.is_active()) else 0
                except Exception:
                    paused, rem = False, 0

                if paused:
                    resp = {"ok": False, "error": "cooldown_active", "cooldown_remaining": rem}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return
        
                text = params.get("text") or ""
                if not isinstance(text, str) or not text:
                    resp = {"ok": False, "error": "missing text"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); return
                # === [FIX APRS -> Meshtastic/MeshCore] Limpieza temprana de cabecera APRS ===
                # Punto crítico:
                #   SEND_TEXT es la entrada común usada por la pasarela APRS para inyectar
                #   mensajes en la red Meshtastic.
                #
                # Antes:
                #   El broker reenviaba a Meshtastic el paquete APRS completo:
                #       !4138.43N/00054.20W>000/000/A=000111 QRV ...
                #
                # Después:
                #   Si el texto es una posición APRS no comprimida, se elimina la cabecera
                #   técnica y se conserva solamente el comentario útil + Google Maps:
                #       QRV R70-R72 sdr:in91np.ddns.net:8073 Abierto https://maps.google.com/?q=...
                #
                # Ventaja:
                #   - Meshtastic ya no recibe cabecera cruda APRS.
                #   - MeshCore tampoco la recibe si el canal se refleja después.
                #   - Se reduce longitud y se evita partir la URL por la coma de coordenadas.
                try:
                    text_clean = _clean_aprs_position_text_for_meshcore(text)
                    if isinstance(text_clean, str) and text_clean.strip():
                        text = text_clean.strip()
                except Exception as e:
                    # Seguridad 24/7: si el limpiador falla, se mantiene el texto original.
                    try:
                        _ctrl_log(
                            "send_text_aprs_clean_error",
                            f"[ctrl] SEND_TEXT APRS clean error: {type(e).__name__}: {e}",
                            interval=10.0
                        )
                    except Exception:
                        pass

                try:
                    ch = int(params.get("ch") if params.get("ch") is not None else 0)
                except Exception:
                    resp = {"ok": False, "error": "invalid channel"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); return

                raw_dest = params.get("dest")
                dest = None
                if isinstance(raw_dest, str) and raw_dest.strip() and raw_dest.strip().lower() != "broadcast":
                    dest = raw_dest.strip()

                ack_flag = bool(params.get("ack")) and bool(dest)

                # === [FIX 24/7] Suprimir reintentos idénticos por CTRL (típico en respuestas BBS largas) ===
                try:
                    dedup_window = int(os.getenv("CTRL_SENDTEXT_DEDUP_SEC", "20"))
                except Exception:
                    dedup_window = 20

                try:
                    dedup_minlen = int(os.getenv("CTRL_SENDTEXT_DEDUP_MINLEN", "200"))
                except Exception:
                    dedup_minlen = 200

                # Solo aplicamos a UNICAST largos (DM) para minimizar falsos positivos
                if dest and isinstance(text, str) and len(text.encode("utf-8", errors="ignore")) >= dedup_minlen:
                    now_ts = time.time()
                    fp = _ctrl_sendtext_fingerprint(ch=int(ch), dest=str(dest), text=text)
                    if _ctrl_sendtext_should_suppress(fp, now_ts, window_sec=dedup_window):
                        # Respondemos OK para que el cliente deje de reintentar, pero NO encolamos.
                        try:
                            _ctrl_log(
                                "send_text_dedup",
                                f"[ctrl] SEND_TEXT dedup SUPPRESS ch={int(ch)} dest={dest} len={len(text.encode('utf-8'))}",
                                interval=5.0
                            )
                        except Exception:
                            pass
                        resp = {"ok": True, "queued": False, "duplicate_suppressed": True}
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        return


                # === [LOG] controlar recepción (antes de encolar) ===
                try:
                    msg = f"[ctrl] SEND_TEXT recv ch={int(ch)} dest={dest or 'broadcast'} len={len(text.encode('utf-8'))}"
                    _ctrl_log("send_text_recv", msg, interval=5.0)
                except Exception as _e:
                    _ctrl_log("send_text_recv_err", f"[ctrl] SEND_TEXT recv log error: {type(_e).__name__}: {_e}", interval=5.0)
                # === [NUEVO] Encolar (no coalesce para textos de usuario)
                try:

                    
                    # === [FIX] Permitir metadatos en SEND_TEXT (no_bridge/origin) para BBS/privado ===
                    params = req.get("params") or {}

                    no_bridge_flag = bool(params.get("no_bridge", False))
                    origin = (params.get("origin") or params.get("source") or "").strip().lower() or None
                    meta = params.get("meta")
                    if meta is not None and not isinstance(meta, dict):
                        meta = None

                    payload = {
                        "channel": ch,
                        "text": text,
                        "destination": dest,
                        "require_ack": ack_flag,
                        "type": "text",
                    }

                    # Propaga flags si existen (no rompe nada si no se usan)
                    if no_bridge_flag:
                        payload["no_bridge"] = True
                    if origin:
                        payload["origin"] = origin
                    if meta:
                        payload["meta"] = meta

                    SENDQ.offer(payload, coalesce=False)
                    resp = {"ok": True, "queued": True, "path": "broker-queue"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

                    resp = {"ok": True, "queued": True, "path": "broker-queue"}
                except Exception as e:
                    resp = {"ok": False, "error": f"queue_error: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); return

            # --- NUEVO: estado del bridge embebido ---
            elif cmd == "BRIDGE_STATUS":
                try:
                    st = bridge_status_in_broker()  # dict con info del bridge
                    # Normalizamos por si el helper devuelve None
                    if not isinstance(st, dict):
                        st = {"enabled": False}
                    resp = {"ok": True, **st}
                except Exception as e:
                    resp = {"ok": False, "error": f"bridge_status_failed: {type(e).__name__}: {e}"}
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); 
                return            # --- NUEVO: estado MeshCore embebido ---
            elif cmd == "MESHCORE_STATUS":
                try:
                    mc = globals().get("MESHCORE_ENGINE")
                    st = mc.status() if mc else {"enabled": False, "available": bool(_MESHCORE_AVAILABLE)}
                    resp = {"ok": True, **st}
                except Exception as e:
                    resp = {"ok": False, "error": f"meshcore_status_failed: {type(e).__name__}: {e}"}
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"));
                return
            
            # --- NUEVO: enviar a MeshCore (canal_idx) desde clientes (BOT) ---
            # --- NUEVO: enviar a MeshCore desde clientes (BOT) ---
            elif cmd == "MESHCORE_SEND":
                params = req.get("params") or {}
                text = (params.get("text") or "").strip()
                try:
                    text = _clean_aprs_position_text_for_meshcore(text)
                except Exception:
                    text = (params.get("text") or "").strip()

                # Compat: kind opcional. Si no viene, inferimos por campos presentes.
                kind = str(params.get("kind") or "").strip().lower()

                # Aceptamos channel_idx (preferido) y también "ch" por compat/atajo.
                ch_raw = params.get("channel_idx", params.get("ch", None))
                contact_prefix = (params.get("contact_prefix") or params.get("prefix") or "").strip()
                max_retries = params.get("max_retries", None)
                try:
                    max_retries = None if max_retries is None else max(0, int(max_retries))
                except Exception:
                    max_retries = None

                # Inferencia si no viene kind
                if not kind:
                    if contact_prefix and ch_raw is None:
                        kind = "contact"
                    else:
                        kind = "chan"

                # Validación común
                if not text:
                    resp = {"ok": False, "error": "missing text"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

                try:
                    mc = globals().get("MESHCORE_ENGINE")
                    if not mc or not getattr(mc, "enable", False):
                        resp = {"ok": False, "error": "meshcore_not_enabled"}
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        return

                    if kind in ("chan", "channel"):
                        try:
                            channel_idx = int(ch_raw)
                        except Exception:
                            channel_idx = None

                        if channel_idx is None:
                            resp = {"ok": False, "error": "missing channel_idx"}
                            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                            return

                        tx_id = mc.enqueue_send_channel(int(channel_idx), text, max_retries=max_retries)
                        resp = {
                            "ok": True,
                            "queued": True,
                            "path": "meshcore-queue",
                            "kind": "chan",
                            "channel_idx": int(channel_idx),
                            "tx_id": tx_id,
                        }

                    else:
                        # DM/contacto
                        if not contact_prefix:
                            resp = {"ok": False, "error": "missing contact_prefix"}
                            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                            return

                        tx_id = mc.enqueue_send_contact(contact_prefix, text, max_retries=max_retries)
                        resp = {
                            "ok": True,
                            "queued": True,
                            "path": "meshcore-queue",
                            "kind": "contact",
                            "contact_prefix": contact_prefix,
                            "tx_id": tx_id,
                        }

                except Exception as e:
                    resp = {"ok": False, "error": f"meshcore_send_failed: {type(e).__name__}: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            elif cmd == "MESHCORE_TRACE_PATH":
                try:
                    params = req.get("params") or {}
                    contact_prefix = str(params.get("contact_prefix") or params.get("prefix") or "").strip()
                    discover = bool(params.get("discover", False))
                    timeout = float(params.get("timeout") or 20.0)
                    eng = globals().get("MESHCORE_ENGINE")
                    if not eng or not hasattr(eng, "trace_contact"):
                        resp = {"ok": False, "error": "meshcore_trace_unavailable"}
                    else:
                        resp = eng.trace_contact(contact_prefix, discover=discover, timeout=timeout)
                except Exception as e:
                    resp = {"ok": False, "error": f"meshcore_trace_failed: {type(e).__name__}: {e}"}
                conn.sendall((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                return

            elif cmd == "MESHCORE_CONTACTS":
                # params: { "limit": 80 }
                try:
                    params = params or {}
                    limit = int(params.get("limit") or 80)
                except Exception:
                    limit = 80

                eng = globals().get("MESHCORE_ENGINE")  # (en tu broker ya existe esta global)
                if not eng:
                    resp = {"ok": False, "error": "meshcore_disabled"}
                else:
                    try:
                        # Si tu engine ya tiene un método, úsalo.
                        if hasattr(eng, "list_contacts") and callable(getattr(eng, "list_contacts")):
                            contacts = eng.list_contacts(limit=limit)
                        else:
                            # Fallback best-effort: inspección del objeto meshcore conectado si existe.
                            mc = getattr(eng, "_meshcore", None) or getattr(eng, "_mc", None) or getattr(eng, "mc", None)
                            contacts = []
                            if mc is not None:
                                try:
                                    items = mc.get_contacts() if hasattr(mc, "get_contacts") else getattr(mc, "contacts", [])
                                except Exception:
                                    items = []
                                if isinstance(items, dict):
                                    normalized_items = []
                                    for item_key, item_value in items.items():
                                        if isinstance(item_value, dict):
                                            item = dict(item_value)
                                            item.setdefault("public_key", item_key)
                                        else:
                                            item = item_value
                                        normalized_items.append(item)
                                    items = normalized_items
                                for c in (items or []):
                                    try:
                                        if isinstance(c, dict):
                                            public_key = c.get("public_key") or c.get("pubkey") or c.get("key")
                                            display_prefix = c.get("pubkey_prefix") or c.get("key_prefix")
                                            contact_id = c.get("id") or c.get("prefix")
                                            name = c.get("name") or c.get("alias") or c.get("label")
                                            last_seen = c.get("last_seen") or c.get("lastSeen") or c.get("seen") or c.get("ts")
                                        else:
                                            public_key = getattr(c, "public_key", None) or getattr(c, "pubkey", None) or getattr(c, "key", None)
                                            display_prefix = getattr(c, "pubkey_prefix", None) or getattr(c, "key_prefix", None)
                                            contact_id = getattr(c, "id", None) or getattr(c, "prefix", None)
                                            name = getattr(c, "name", None) or getattr(c, "alias", None) or getattr(c, "label", None)
                                            last_seen = getattr(c, "last_seen", None) or getattr(c, "lastSeen", None) or getattr(c, "seen", None)

                                        display_id = (str(display_prefix).strip() if display_prefix is not None else "")
                                        contact_id = (str(contact_id).strip() if contact_id is not None else "")
                                        public_key = (str(public_key).strip() if public_key is not None else "")
                                        # send_msg resuelve contactos por prefijo de public_key mediante
                                        # get_contact_by_key_prefix(); no mostramos la clave completa como DM.
                                        dm_key = display_id or (public_key[:12] if public_key else "") or contact_id
                                        display_id = display_id or dm_key or contact_id
                                        if not dm_key:
                                            continue

                                        contacts.append({
                                            "prefix": display_id,
                                            "contact_id": contact_id or None,
                                            "dm_key": dm_key,
                                            "public_key": public_key or dm_key,
                                            "name": (str(name).strip() if name is not None else "") or None,
                                            "last_seen": int(last_seen) if isinstance(last_seen, (int, float)) else None,
                                        })
                                        if len(contacts) >= limit:
                                            break
                                    except Exception:
                                        continue

                        # Dedup
                        seen = set()
                        uniq = []
                        for d in contacts:
                            key = d.get("dm_key") or d.get("public_key") or d.get("prefix")
                            if not key or key in seen:
                                continue
                            seen.add(key)
                            uniq.append(d)

                        resp = {"ok": True, "count": len(uniq), "contacts": uniq}
                    except Exception as e:
                        resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            elif cmd == "MESHCORE_CHANNELS":
                # params: { "limit": 80 }
                try:
                    params = params or {}
                    limit = int(params.get("limit") or 80)
                except Exception:
                    limit = 80

                eng = globals().get("MESHCORE_ENGINE")
                if not eng:
                    resp = {"ok": False, "error": "meshcore_disabled"}
                else:
                    try:
                        if hasattr(eng, "list_channels") and callable(getattr(eng, "list_channels")):
                            channels = eng.list_channels(limit=limit)
                        else:
                            channels = []
                        resp = {"ok": True, "count": len(channels), "channels": channels}
                    except Exception as e:
                        resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            # --- NUEVO: envío de texto vía lado B del bridge ---
            elif cmd == "SEND_TEXT_VIA":
                params = req.get("params") or {}
                side = (params.get("side") or "B").upper()
                text = (params.get("text") or "").strip()
                try:
                    ch = int(params.get("ch") if params.get("ch") is not None else 0)
                except Exception:
                    ch = 0

                if side != "B":
                    resp = {"ok": False, "error": "only_side_B_supported"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); 
                    return
                if not text:
                    resp = {"ok": False, "error": "missing text"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); 
                    return

                try:
                    # Este helper se encarga de reflejar el paquete hacia el lado B
                    mirrored = bool(bridge_mirror_outgoing_from_broker(int(ch), text))
                    if mirrored:
                        resp = {"ok": True, "mirrored": True, "via": "B"}
                    else:
                        resp = {"ok": False, "error": "bridge_not_running_or_mirror_rejected"}
                except Exception as e:
                    resp = {"ok": False, "error": f"bridge_send_failed: {type(e).__name__}: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); 
                return

            # --- control del broker (pausa/reanuda/estado/desconexion) ---
            elif cmd in {"BROKER_PAUSE", "BROKER_RESUME", "BROKER_STATUS", "BACKLOG_STATUS", "BRIDGE_STATUS", "BROKER_DISCONNECT", "FORCE_RECONNECT", "BROKER_QUIT"}:

                mgr = globals().get("BROKER_IFACE_MGR")
                if not mgr:
                    resp = {"ok": False, "error": "iface manager not ready"}
                else:
                    try:
                        if cmd == "BACKLOG_STATUS":
                            resp = {
                                "ok": True,
                                "status": "running",
                                "active_clients": self._get_active_clients(),
                                "max_clients": int(self._max_client_threads),
                                "busy_drops": int(self._busy_drop_count),
                                "thread_start_errors": int(self._thread_start_error_count),
                                "client_timeout_sec": float(self._client_timeout_sec),
                                "listen_backlog": int(self._listen_backlog),
                                "fetch_max_limit": int(self._fetch_max_limit),
                                "fetch_default_limit": int(self._fetch_default_limit),
                                "fetch_default_window_sec": int(self._fetch_default_window_sec),
                            }

                        elif cmd == "BROKER_PAUSE":
                            # Cierra iface y bloquea reconexión hasta resume()
                            mgr.pause()
                            resp = {"ok": True, "status": "paused"}
                        elif cmd == "BROKER_RESUME":
                            COOLDOWN.clear()
                            mgr.resume()
                            resp = {"ok": True, "status": "running"}

                        # --- [NUEVO] Forzar desconexión con cooldown programado ---
                        elif cmd == "BROKER_DISCONNECT":
                            secs = int(params.get("seconds") or 3)
                            strict = bool(params.get("strict")) if "strict" in params else False

                            def _async_disconnect_and_resume(mgr, secs, strict):
                                # 1) Forzar que el siguiente _on_disconnect use 'secs' en vez de 90
                                try:
                                    with COOLDOWN_FORCE_LOCK:
                                        globals()["COOLDOWN_FORCE_NEXT"] = int(secs)
                                except Exception:
                                    globals()["COOLDOWN_FORCE_NEXT"] = int(secs)

                                # 2) Pausa + señal de desconexión suave (como ya hacías)
                                try:
                                    if hasattr(mgr, "pause"):
                                        mgr.pause()
                                except Exception:
                                    pass
                                try:
                                    if hasattr(mgr, "signal_disconnect"):
                                        mgr.signal_disconnect()
                                except Exception:
                                    pass

                                # 3) Espera 'secs' y reanuda
                                try:
                                    time.sleep(max(1, int(secs)))
                                except Exception:
                                    pass
                                try:
                                    if hasattr(mgr, "resume"):
                                        mgr.resume()
                                except Exception:
                                    pass
                                print(f"[ctrl] BROKER_DISCONNECT → ciclo completo con cooldown={secs}s", flush=True)

                            mgr = globals().get("BROKER_IFACE_MGR")
                            if not mgr:
                                resp = {"ok": False, "error": "iface manager not ready"}
                            else:
                                threading.Thread(target=_async_disconnect_and_resume, args=(mgr, secs, strict), daemon=True).start()
                                # 💡 respuesta inmediata: el bot NO se queda esperando
                                resp = {"ok": True, "scheduled": True, "seconds": int(secs)}

                        elif cmd == "FORCE_RECONNECT":
                            # Reset limpio + preparar ventana de gracia anti-escalado
                            try:
                                import time as _t
                                from tcpinterface_persistent import TCPInterfacePool
                            except Exception:
                                pass

                            try:
                                # === 0) Cooldown corto para el siguiente _on_disconnect ===
                                # (se aplica una sola vez; NO tocar COOLDOWN_SECS base)
                                try:
                                    with COOLDOWN_FORCE_LOCK:
                                        globals()["COOLDOWN_FORCE_NEXT"] = 3   # 3s
                                except Exception:
                                    globals()["COOLDOWN_FORCE_NEXT"] = 3

                                # === 1) Ventana de gracia anti-escalado tras el reset ===
                                #   - Tiempo: 45s
                                #   - Contador: permitir suprimir hasta 2 "caídas tempranas"
                                try:
                                    now = _t.time()
                                    globals()["_SUPPRESS_EARLY_ESC_UNTIL"]  = now + 45.0
                                    globals()["_SUPPRESS_EARLY_ESC_REMAIN"] = int(globals().get("_SUPPRESS_EARLY_ESC_DEFAULT_REMAIN", 2))

                                except Exception:
                                    pass

                                # === 2) Limpieza de estados globales mínimos (sin romper) ===
                                try:
                                    x = globals().get("TX_BLOCKED")
                                    if x:
                                        x.clear()
                                except Exception:
                                    pass
                                try:
                                    cd = globals().get("COOLDOWN")
                                    if cd:
                                        cd.clear()
                                except Exception:
                                    pass

                                # === 3) Reset de la sesión del pool TCP (cierra y reabrirá perezoso) ===
                                try:
                                    TCPInterfacePool.reset(
                                        globals().get("RUNTIME_MESH_HOST") or "",
                                        int(globals().get("RUNTIME_MESH_PORT") or 4403)
                                    )
                                    print("[ctrl] FORCE_RECONNECT → TCPInterfacePool.reset() aplicado.", flush=True)
                                except Exception as e:
                                    print(f"[ctrl] FORCE_RECONNECT → aviso: no se pudo resetear pool: {type(e).__name__}: {e}", flush=True)

                                # === 4) Señal suave al manager: desconecta y reanuda (garantiza no-paused) ===
                                try:
                                    mgr = globals().get("BROKER_IFACE_MGR") or self.iface_mgr
                                except Exception:
                                    mgr = None

                                try:
                                    if mgr and hasattr(mgr, "signal_disconnect"):
                                        mgr.signal_disconnect()
                                except Exception:
                                    pass
                              
                                try:
                                    if mgr and hasattr(mgr, "resume"):
                                        mgr.resume()   # estado no-pausado
                                except Exception:
                                    pass
                              
                                try:
                                    iface_a = None
                                    if mgr and hasattr(mgr, "get_iface"):
                                        iface_a = mgr.get_iface()
                                    elif mgr:
                                        iface_a = getattr(mgr, "iface", None)

                                    def _delayed_check():
                                        try:
                                            time.sleep(2.0)
                                            _check_and_reconnect_embedded_b(iface_a=iface_a, reason="FORCE_RECONNECT")
                                        except Exception as e:
                                            print(f"[broker] delayed FORCE_RECONNECT check ERROR: {type(e).__name__}: {e}", flush=True)

                                    threading.Thread(target=_delayed_check, daemon=True).start()
                                except Exception as e:
                                    print(f"[broker] FORCE_RECONNECT embedded check schedule ERROR: {type(e).__name__}: {e}", flush=True)

                                resp = {"ok": True, "status": "running", "action": "force_reconnect"}
                            except Exception as e:
                                resp = {"ok": False, "error": f"force_reconnect_failed: {type(e).__name__}: {e}"}

                        elif cmd == "BRIDGE_STATUS":
                            try:
                                st = bridge_status_in_broker()
                                resp = {"ok": True, "bridge": st}
                            except Exception as e:
                                resp = {"ok": False, "error": f"bridge_status_failed: {type(e).__name__}: {e}"}

                        else:  # BROKER_STATUS
                            # --- [FIJO] usar SIEMPRE el mismo singleton de cooldown desde globals() ---
                            c = globals().get("COOLDOWN", None)
                            mgr = globals().get("BROKER_IFACE_MGR", None)

                            is_cd = bool(c.is_active()) if c else False
                            rem   = int(c.remaining())  if c else 0
                            is_paused = False
                            try:
                                is_paused = bool(mgr.is_paused()) if mgr and hasattr(mgr, "is_paused") else False
                            except Exception:
                                is_paused = False

                            resp = {
                                "ok": True,
                                "status": ("paused" if (is_paused or is_cd) else "running"),
                                "cooldown_remaining": (rem if is_cd else 0),
                                # --- [NUEVO]
                                "connected": bool(globals().get("_IS_CONNECTED", False)),
                                # --- [NUEVO] contexto útil para el bot/UI ---
                                "node_host": str(globals().get("RUNTIME_MESH_HOST") or ""),
                                "node_port": int(globals().get("RUNTIME_MESH_PORT") or 4403),
                                # opcionales de diagnóstico (si los tienes a mano):
                                "mgr_paused": bool(is_paused),
                                "tx_blocked": bool(TX_BLOCKED.is_set()) if 'TX_BLOCKED' in globals() else False,
                                # Diagnóstico del BacklogServer para detectar presión del WebPanel/Bot.
                                "backlog_active_clients": self._get_active_clients(),
                                "backlog_max_clients": int(self._max_client_threads),
                                "backlog_busy_drops": int(self._busy_drop_count),
                                "backlog_thread_errors": int(self._thread_start_error_count),
                            }

                            # [TRAZA extra (debug de referencia)]:
                            try:
                                print(f"[ctrl] BROKER_STATUS → status={resp['status']} rem={resp['cooldown_remaining']}  "
                                    f"(id(COOLDOWN)={id(c) if c else None})", flush=True)
                            except Exception:
                                pass

                    except Exception as e:
                        resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            # --- NUEVO: lista de nodos actuales desde la iface persistente ---
            elif cmd == "LIST_NODES":
                try:
                    limit = int(params.get("limit") or 0)
                except Exception:
                    limit = 0

                mgr = globals().get("BROKER_IFACE_MGR") or globals().get("IFACE_POOL") or globals().get("POOL")
                iface = None
                try:
                    if mgr is not None:
                        if hasattr(mgr, "get_iface"):
                            iface = mgr.get_iface()
                        elif hasattr(mgr, "get_interface"):
                            iface = mgr.get_interface()
                        else:
                            iface = getattr(mgr, "iface", None)
                except Exception:
                    iface = None

                if iface is None:
                    resp = {"ok": False, "error": "iface_unavailable"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

                import time as _t
                now = int(_t.time())

                def _iter_nodes(_iface):
                    raw_nodes = getattr(_iface, "nodes", None)
                    if raw_nodes and isinstance(raw_nodes, dict):
                        it = raw_nodes.values()
                    elif isinstance(raw_nodes, list):
                        it = raw_nodes
                    else:
                        getnodes = getattr(_iface, "getNodes", None)
                        it = getnodes() if callable(getnodes) else []

                    out = []
                    for n in (it or []):
                        usr = (n.get("user") or {}) if isinstance(n, dict) else {}
                        uid = (usr.get("id")
                               or (n.get("id") if isinstance(n, dict) else None)
                               or (n.get("num") if isinstance(n, dict) else None)
                               or (n.get("nodeId") if isinstance(n, dict) else None)
                               or "")
                        alias = (usr.get("longName") or usr.get("shortName")
                                 or (n.get("name") if isinstance(n, dict) else None)
                                 or uid or "")
                        metrics = (n.get("deviceMetrics") or n.get("metrics") or {}) if isinstance(n, dict) else {}
                        snr = metrics.get("snr", (n.get("snr") if isinstance(n, dict) else None))
                        last_heard = None
                        try:
                            last_heard = int(n.get("lastHeard") or n.get("last_heard") or n.get("heard") or 0)
                        except Exception:
                            last_heard = 0
                        hops = n.get("hops") if isinstance(n, dict) else None
                        if hops is None:
                            hops = 0
                        out.append({
                            "id": uid, "alias": alias, "snr": snr,
                            "lastHeard": last_heard, "ago": (now - last_heard) if last_heard else None,
                            "hops": int(hops) if isinstance(hops, int) else 0
                        })
                    # ordenar por recencia (ago None al final)
                    out.sort(key=lambda x: (x["ago"] if x["ago"] is not None else 10**9))
                    return out

                try:
                    data = _iter_nodes(iface)
                    if limit and limit > 0:
                        data = data[:limit]
                    resp = {"ok": True, "data": data}
                except Exception as e:
                    resp = {"ok": False, "error": f"nodes_error: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            # --- NUEVO: tabla de vecinos (neighbor info) desde la iface persistente ---
            elif cmd == "NEIGHBORS":
                mgr = globals().get("BROKER_IFACE_MGR") or globals().get("IFACE_POOL") or globals().get("POOL")
                iface = None
                try:
                    if mgr is not None:
                        if hasattr(mgr, "get_iface"):
                            iface = mgr.get_iface()
                        elif hasattr(mgr, "get_interface"):
                            iface = mgr.get_interface()
                        else:
                            iface = getattr(mgr, "iface", None)
                except Exception:
                    iface = None

                if iface is None:
                    resp = {"ok": False, "error": "iface_unavailable"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

                neighbors = None
                err = None
                try:
                    if hasattr(iface, "getNeighbors") and callable(getattr(iface, "getNeighbors")):
                        neighbors = iface.getNeighbors()
                    elif hasattr(getattr(iface, "radio", None), "getNeighborInfo"):
                        neighbors = iface.radio.getNeighborInfo()
                    elif hasattr(iface, "neighbors"):
                        neighbors = getattr(iface, "neighbors")
                except Exception as e:
                    err = f"neighbors_call: {e}"

                if neighbors is None:
                    resp = {"ok": False, "error": err or "neighbors_unavailable"}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

                # Normaliza a lista de dicts {id, rssi, snr, hops, via, lastHeard}
                out = []
                try:
                    if isinstance(neighbors, dict):
                        it = neighbors.values()
                    else:
                        it = neighbors
                    for n in (it or []):
                        if not isinstance(n, dict):
                            continue
                        nid = (n.get("id") or n.get("num") or n.get("nodeId") or n.get("fromId") or "")
                        rssi = n.get("rssi")
                        snr = n.get("snr")
                        hops = n.get("hops")
                        via = n.get("via") or n.get("next_hop")
                        try:
                            last_heard = int(n.get("lastHeard") or n.get("heard") or 0)
                        except Exception:
                            last_heard = 0
                        out.append({
                            "id": nid, "rssi": rssi, "snr": snr,
                            "hops": int(hops) if isinstance(hops, int) else None,
                            "via": via, "lastHeard": last_heard
                        })
                    resp = {"ok": True, "data": out}
                except Exception as e:
                    resp = {"ok": False, "error": f"neighbors_parse: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

            # --- [NUEVO] comando: RUN_TRACEROUTE -----------------------------------------
           
            elif cmd == "RUN_TRACEROUTE":
                    # ------------------------------------------------------------
                    # RUN_TRACEROUTE (API Meshtastic) - NO PAUSA el broker
                    #
                    # Acepta target en 2 formatos:
                    #   - "!2744ee88" (hex Meshtastic)  -> se convierte a decimal (nodeNum)
                    #   - "1623194643" (decimal nodeNum) -> se usa tal cual
                    #
                    # Params:
                    #   - target | node : str
                    #   - hop_limit     : int (default 20, 1..50)
                    #   - ch_index      : int (default 0, 0..7)
                    #
                    # Acción:
                    #   - Lanza traceroute por API (sendTraceRoute) de forma NO bloqueante
                    #   - La respuesta llega por el RX normal como TRACEROUTE_APP/ROUTING_APP
                    # ------------------------------------------------------------
                    raw_target = str(params.get("target") or params.get("node") or "").strip()
                    if not raw_target:
                        resp = {"ok": False, "error": "missing target"}
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        return

                    def _parse_dest_to_node_num(v: str) -> int:
                        """
                        Convierte target a nodeNum (decimal) para sendTraceRoute().
                        Acepta '!xxxxxxxx' hex o decimal en texto.
                        """
                        v = v.strip()
                        if v.startswith("!"):
                            hx = v[1:]
                            return int(hx, 16)
                        # decimal
                        return int(v)

                    # hop_limit (Meshtastic traceroute: valores realistas)
                    try:
                        hop_limit = int(params.get("hop_limit") or params.get("hopLimit") or 5)
                    except Exception:
                        hop_limit = 5
                    hop_limit = max(1, min(hop_limit, 7))


                    # ch_index
                    try:
                        ch_index = int(params.get("ch_index") or params.get("channel_index") or params.get("channelIndex") or 0)
                    except Exception:
                        ch_index = 0
                    ch_index = max(0, min(ch_index, 7))

                    ok = False
                    err = None
                    node_num = None

                    try:
                        # Convierte a nodeNum decimal (forma canónica para sendTraceRoute)
                        node_num = _parse_dest_to_node_num(raw_target)

                        # === [WEBPANEL/TRACEROUTE v7.0.6] Registrar contexto ANTES de enviar RF ===
                        # Motivo:
                        #   Algunas respuestas ROUTING_APP pueden llegar muy rápido, incluso antes de
                        #   que el bloque posterior registre traceroute_started. Si el contexto no está
                        #   ya en _TRACEROUTE_PENDING, el RX puede persistir target=None y el WebAdmin
                        #   queda esperando.
                        #
                        # Seguridad:
                        #   - No transmite RF.
                        #   - No modifica sendTraceRoute.
                        #   - No toca APRS/BBS/MeshCore/bridge.
                        #   - Solo registra contexto temporal en memoria.
                        ctx = _traceroute_remember_start(
                            target_requested=raw_target,
                            dest_node_num=int(node_num),
                            hop_limit=int(hop_limit),
                            ch_index=int(ch_index),
                        )

                        # Host/port REALES del nodo (los fijaste en main())
                        mesh_host = globals().get("RUNTIME_MESH_HOST")
                        mesh_port = int(globals().get("RUNTIME_MESH_PORT") or 4403)

                        mgr = globals().get("BROKER_IFACE_MGR")
                        if not (mgr and hasattr(mgr, "get_iface") and callable(mgr.get_iface)):
                            raise RuntimeError("BROKER_IFACE_MGR no disponible (sin get_iface)")

                        iface = mgr.get_iface()
                        if not iface:
                            raise RuntimeError("Sin iface activa (aún no conectado)")

                        # API moderna (prioridad)
                        fn = getattr(iface, "sendTraceRoute", None)
                        if not callable(fn):
                            raise RuntimeError("sendTraceRoute no disponible en esta versión")

                        # Probamos firma completa; si la librería no acepta kwargs, reducimos
                        try:
                            fn(node_num, hop_limit, channelIndex=ch_index)
                        except TypeError:
                            try:
                                fn(node_num, hop_limit, ch_index)
                            except TypeError:
                                fn(node_num, hop_limit)

                        ok = True

                        # === [WEBPANEL/TRACEROUTE v7.0.2] Registrar inicio en OFFLINE_LOG ===
                        # Este registro no es una respuesta RF.
                        # Es una marca local para que el WebPanel vea que el broker lanzó
                        # correctamente el traceroute y pueda correlacionar después la respuesta RX.
                        try:
                            trace_text = (
                                f"traceroute started target={raw_target} "
                                f"node_num={int(node_num)} "
                                f"hop_limit={int(hop_limit)} "
                                f"ch_index={int(ch_index)}"
                            )

                            append_offline_log(
                                {
                                    "ts": int(time.time()),
                                    "rx_time": int(time.time()),
                                    "channel": int(ch_index),
                                    "portnum": "TRACEROUTE_APP",
                                    "from": "BROKER",
                                    "to": ctx.get("target_norm") or raw_target,
                                    "from_alias": "broker",
                                    "to_alias": None,
                                    "event_type": "traceroute_started",
                                    "trace_event": "traceroute_started",
                                    "target_requested": raw_target,
                                    "target_norm": ctx.get("target_norm"),
                                    "dest_node_num": int(node_num),
                                    "trace_hop_limit": int(hop_limit),
                                    "trace_ch_index": int(ch_index),
                                    "trace_started_ts": ctx.get("started_ts"),
                                    "route_text": trace_text,
                                    "text": trace_text,
                                }
                            )


                        except Exception as _e_trace_start:
                            try:
                                print(
                                    f"⚠️ traceroute_started offline_log failed: "
                                    f"{type(_e_trace_start).__name__}: {_e_trace_start}",
                                    flush=True,
                                )
                            except Exception:
                                pass

                    except Exception as e:
                        ok = False
                       
                        err = f"{type(e).__name__}: {e}"

                    resp = {
                        "ok": bool(ok),
                        "started": bool(ok),
                        "target": raw_target,
                        "dest_node_num": node_num,
                        "hop_limit": hop_limit,
                        "ch_index": ch_index,
                    }
                    if not ok:
                        resp["error"] = err or "traceroute start failed"

                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    return

          
            else:
                resp = {"ok": False, "error": "unknown cmd"}
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                return

        except Exception as e:
            try:
                resp = {"ok": False, "error": str(e)}
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

# Enganche automático para persistir sin tocar tus funciones:
def _wrap_emitter_for_persistence():
    """
    Busca funciones emisoras conocidas y las envuelve para llamar a append_offline_log(packet)
    antes de emitir. Si no las encuentra, no rompe nada.
    """
    candidates = [
        "notify_listeners",
        "broadcast_to_subscribers",
        "emit_to_clients",
        "forward_to_clients",
        "publish_to_listeners",
    ]
    wrapped = False
    for name in candidates:
        fn = globals().get(name)
        if callable(fn):
            def _make_wrapper(orig):
                def _wrapped(packet, *args, **kwargs):
                    # Persistencia best-effort: nunca debe romper el envío real
                    try:
                        append_offline_log(packet)
                    except Exception:
                        pass
                    return orig(packet, *args, **kwargs)

                _wrapped.__name__ = orig.__name__
                _wrapped.__doc__  = orig.__doc__
                return _wrapped
                  
            globals()[name] = _make_wrapper(fn)
            print(f"ℹ️ Persistencia activada en '{name}'", flush=True)
            wrapped = True
            break
    if not wrapped:
        print("ℹ️ Persistencia: no se encontró función emisora conocida; no se hace wrap (no es un error).", flush=True)
    return wrapped

_backlog_server_instance = None

def start_backlog_server(bind_host: str = "127.0.0.1", port: int = BACKLOG_PORT):
    """
    NUEVO: arranca el servidor TCP de backlog en un hilo y activa el envoltorio de persistencia.
    Llama a esta función durante el arranque del broker.
    """
    global _backlog_server_instance
    _ensure_dir(OFFLINE_LOG_PATH)
    _wrap_emitter_for_persistence()
    if _backlog_server_instance is None:
        _backlog_server_instance = _BacklogServer(bind_host, port)
        _backlog_server_instance.start()
        print(f"ℹ️ BacklogServer iniciado en {bind_host}:{port}", flush=True)


def _get(d, path, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def _json_default(o):
    if isinstance(o, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(o)).decode("ascii")
    if isinstance(o, set):
        return list(o)
    return str(o)

def _json_dumps(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)

def _now_s() -> int:
    return int(time.time())

def _maybe_hex_to_bytes(s: str) -> Optional[bytes]:
    try:
        s2 = s.strip().replace(" ", "")
        if not s2 or len(s2) % 2:
            return None
        return binascii.unhexlify(s2)
    except Exception:
        return None

def _maybe_b64_to_bytes(s: str) -> Optional[bytes]:
    try:
        s2 = s.strip()
        missing = len(s2) % 4
        if missing:
            s2 += "=" * (4 - missing)
        return base64.b64decode(s2, validate=False)
    except Exception:
        return None

def _looks_text(b: bytes) -> bool:
    try:
        s = b.decode("utf-8")
    except Exception:
        return False
    printable = sum(1 for c in s if (c.isprintable() or c in "\r\n\t"))
    return (printable / max(1, len(s))) > 0.9

# ===================== Debug opcional =====================

# === [NUEVO] Almacén offline de mensajes para replay ===
import json
import threading
from datetime import datetime, timezone

_offline_lock = threading.Lock()

def debug_packet_structure(pkt: dict, enabled: bool):
    """Imprime la estructura del paquete (solo si enabled=True)."""
    if not enabled:
        return
    def print_dict_structure(d, indent=0):
        prefix = "  " * indent
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, dict):
                    print(f"{prefix}{k}: {{")
                    print_dict_structure(v, indent + 1)
                    print(f"{prefix}}}")
                elif isinstance(v, (list, tuple)):
                    print(f"{prefix}{k}: [{type(v).__name__} len={len(v)}]")
                    if len(v) > 0 and isinstance(v[0], dict):
                        print_dict_structure(v[0], indent + 1)
                else:
                    vs = repr(v)
                    if len(vs) > 120:
                        vs = vs[:120] + "…"
                    print(f"{prefix}{k}: {type(v).__name__} = {vs}")
    print("=== ESTRUCTURA DEL PAQUETE ===")
    print_dict_structure(pkt)
    print("==============================")

# ===================== Extracción de texto =====================

def decode_payload_to_text(payload, debug_packets: bool = False) -> Optional[str]:
    """Intenta decodificar un payload (bytes, hex, base64, lista de ints) a UTF-8."""
    if isinstance(payload, str):
        b64 = _maybe_b64_to_bytes(payload)
        if b64 and _looks_text(b64):
            try:
                return b64.decode("utf-8", errors="replace").strip() or None
            except Exception:
                pass
        hx = _maybe_hex_to_bytes(payload)
        if hx and _looks_text(hx):
            try:
                return hx.decode("utf-8", errors="replace").strip() or None
            except Exception:
                pass

    elif isinstance(payload, (bytes, bytearray, memoryview)):
        bb = bytes(payload)
        if _looks_text(bb):
            try:
                return bb.decode("utf-8", errors="replace").strip() or None
            except Exception:
                pass

    elif isinstance(payload, (list, tuple)) and all(isinstance(x, int) for x in payload):
        try:
            bb = bytes(int(x) & 0xFF for x in payload)
            if _looks_text(bb):
                return bb.decode("utf-8", errors="replace").strip() or None
        except Exception:
            pass

    return None

def extract_text_from_packet(pkt: dict, debug_packets: bool = False) -> Optional[str]:
    """Extrae texto desde campos usuales o decodificando payloads si es posible."""
    # Campos típicos:
    text = _get(pkt, "decoded.data.text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    for path in ("decoded.text", "data.text", "text", "decoded.data.message", "decoded.message"):
        text = _get(pkt, path)
        if isinstance(text, str) and text.strip():
            return text.strip()

    # Intentar payloads:
    for path in ("decoded.data.payload", "decoded.payload", "payload", "data.payload", "raw.payload"):
        payload = _get(pkt, path)
        if payload is None:
            continue
        decoded_text = decode_payload_to_text(payload, debug_packets=debug_packets)
        if decoded_text:
            return decoded_text

    return None

def _decode_payload_text(decoded: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Devuelve (text, payload_hex) si procede."""
    if not isinstance(decoded, dict):
        return (None, None)
    data = decoded.get("data") or {}
    text = data.get("text") if isinstance(data, dict) else None
    if not (isinstance(text, str) and text.strip()):
        text = None

    payload_hex = None
    payload = (data.get("payload") if isinstance(data, dict) else None) or decoded.get("payload")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        payload_hex = bytes(payload).hex()
    elif isinstance(payload, str):
        raw = _maybe_b64_to_bytes(payload) or _maybe_hex_to_bytes(payload)
        if raw:
            payload_hex = raw.hex()
    return (text, payload_hex)

def try_fill_text_inplace(pkt: dict, debug_packets: bool = False) -> None:
    """Si falta decoded.data.text lo intenta rellenar a partir de otros campos/payloads."""
    dec = pkt.setdefault("decoded", {}) or {}
    data = dec.setdefault("data", {}) or {}
    if isinstance(data.get("text"), str) and data["text"].strip():
        return
    extracted = extract_text_from_packet(pkt, debug_packets=debug_packets)
    if extracted:
        data["text"] = extracted

# ===================== Canales / métricas =====================

def extract_logical_channel(pkt: dict):
    for path in (
        "meta.channelIndex",
        "channel",
        "rxMetadata.channel",
        "decoded.channel",
        "decoded.data.channel",
        "decoded.header.channelIndex",
        "decoded.header.channel",
    ):
        v = _get(pkt, path)
        if isinstance(v, int): return v
        if isinstance(v, str) and v.strip().isdigit(): return int(v.strip())
    return None

def extract_rf_channel(pkt: dict):
    for path in (
        "meta.rfChannel",
        "rxMetadata.rfChannel",
        "raw.rx_channel",
        "rxChannel",
        "rxMetadata.channel",  # algunas builds lo ponen aquí
    ):
        v = _get(pkt, path)
        if isinstance(v, int): return v
        if isinstance(v, str) and v.strip().isdigit(): return int(v.strip())
    return None

def extract_rssi(pkt: dict) -> Optional[float]:
    v = _get(pkt, "meta.rxRssi")
    if isinstance(v, (int, float)): return float(v)
    for key in ("rssi","rxRssi","rx_rssi"):
        v = pkt.get(key)
        if isinstance(v,(int,float)): return float(v)
    v = _get(pkt,"raw.rx_rssi")
    if isinstance(v,(int,float)): return float(v)
    v = _get(pkt,"rxMetadata.rssi")
    if isinstance(v,(int,float)): return float(v)
    return None

def extract_snr(pkt: dict) -> Optional[float]:
    v = _get(pkt, "meta.rxSnr")
    if isinstance(v, (int, float)): return float(v)
    for key in ("snr","rxSnr","rx_snr"):
        v = pkt.get(key)
        if isinstance(v,(int,float)): return float(v)
    v = _get(pkt,"raw.rx_snr")
    if isinstance(v,(int,float)): return float(v)
    v = _get(pkt,"rxMetadata.snr")
    if isinstance(v,(int,float)): return float(v)
    return None

def stamp_channels(pkt: dict, canal: Optional[int], rfch: Optional[int]) -> None:
    meta = pkt.setdefault("meta", {})
    if canal is not None:
        meta["channelIndex"] = canal
    if rfch is not None:
        meta["rfChannel"] = rfch

# ===================== Inferencias =====================

_SYSTEM_PORTS = {"POSITION_APP","TELEMETRY_APP","NODEINFO_APP","NEIGHBORINFO_APP","ROUTING_APP"}

# === [NUEVO] Regex: /aprs canal N DEST: texto (orden privado) ===
# Se usa para permitir que un DM actúe como orden: enviar por APRS (RF/IS lo decide la pasarela)
# y, además, reinyectar SOLO el texto limpio en un canal Mesh indicado.
_APRS_CANAL_CMD_RE = re.compile(
    r"^\s*/aprs(?:\s+(?:canal|ch)\s+(\d{1,2}))\s+([A-Za-z0-9\-]+)\s*:\s*(.+)\s*$",
    re.IGNORECASE
)


def infer_logical_channel(portnum: Optional[str], enable: bool) -> Tuple[Optional[int], bool]:
    if not enable:
        return (None, False)
    if portnum in _SYSTEM_PORTS:
        return (0, True)
    return (None, False)

def _read_local_frequency_slot(interface) -> Optional[int]:
    try:
        ln = getattr(interface, "localNode", None)
        if ln is not None:
            lc = getattr(ln, "localConfig", None) if not isinstance(ln, dict) else ln.get("localConfig")
            lora = getattr(lc, "lora", None) if lc is not None and not isinstance(lc, dict) else (lc or {}).get("lora")
            if lora is not None:
                slot = getattr(lora, "frequencySlot", None) if not isinstance(lora, dict) else lora.get("frequencySlot", lora.get("frequency_slot"))
                if isinstance(slot, int): return slot
                if isinstance(slot, str) and slot.strip().isdigit(): return int(slot.strip())

        lc = getattr(interface, "localConfig", None)
        if lc is not None:
            lora = getattr(lc, "lora", None) if not isinstance(lc, dict) else lc.get("lora")
            if lora is not None:
                slot = getattr(lora, "frequencySlot", None) if not isinstance(lora, dict) else lora.get("frequencySlot", lora.get("frequency_slot"))
                if isinstance(slot, int): return slot
                if isinstance(slot, str) and slot.strip().isdigit(): return int(slot.strip())

        nodes = getattr(interface, "nodes", None)
        if isinstance(nodes, dict) and "^local" in nodes:
            maybe = nodes["^local"]
            if isinstance(maybe, dict):
                lc = maybe.get("localConfig")
                if isinstance(lc, dict):
                    lora = lc.get("lora")
                    if isinstance(lora, dict):
                        slot = lora.get("frequencySlot", lora.get("frequency_slot"))
                        if isinstance(slot, int): return slot
                        if isinstance(slot, str) and slot.strip().isdigit(): return int(slot.strip())
    except Exception:
        pass
    return None

def infer_rf_channel(interface, enable: bool) -> Tuple[Optional[int], bool]:
    if not enable:
        return (None, False)
    rf = _read_local_frequency_slot(interface)
    if rf is not None:
        return (rf, True)
    return (None, False)

# ===================== Estadísticas =====================

@dataclass
class BrokerStats:
    total: int = 0
    by_port: Dict[str, int] = field(default_factory=dict)
    by_channel: Dict[str, int] = field(default_factory=dict)

    def bump(self, port: Optional[str], canal: Optional[int]):
        self.total += 1
        if port:
            self.by_port[port] = self.by_port.get(port, 0) + 1
        ch_key = "??" if canal is None else str(canal)
        self.by_channel[ch_key] = self.by_channel.get(ch_key, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "by_port": self.by_port, "by_channel": self.by_channel}

# ===================== Hub JSONL =====================

class JsonLineHub:
    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def add_client(self, sock: socket.socket):
        sock.setblocking(False)
        with self._lock:
            self._clients.add(sock)

    def remove_client(self, sock: socket.socket):
        with self._lock:
            self._clients.discard(sock)
        try: sock.close()
        except Exception: pass

    def broadcast_line(self, line: str):
        data = line.encode("utf-8", errors="replace")
        dead = []
        with self._lock:
            for s in list(self._clients):
                try:
                    s.sendall(data)
                except Exception:
                    dead.append(s)
            for s in dead:
                self._clients.discard(s)
                try: s.close()
                except Exception: pass

# ===================== Autoreconexión al nodo =====================

# === [NUEVO] Selector de transporte (tcp/bluetooth/usb) para el nodo del broker ===
import os

def _mesh_transport() -> str:
    """
    Lee el transporte desde .env.
      - tcp        : WiFi/TCP (modo actual)
      - bluetooth  : BLE (emergencia local)
      - usb        : Serial por USB (emergencia más robusta)
    """
    return (os.getenv("MESH_TRANSPORT", "tcp") or "tcp").strip().lower()

def _mesh_transport_id(host: str, port: int) -> str:
    """
    Identificador estable para el candado de instancia única según transporte.
    Evita que el lock sea host:port cuando estamos en BLE/USB.
    """
    t = _mesh_transport()
    if t == "bluetooth":
        return f"ble:{(os.getenv('MESH_BT_ADDR','') or '').strip()}"
    if t == "usb":
        return f"usb:{(os.getenv('MESH_USB_PORT','') or '').strip()}"
    return f"tcp:{host}:{port}"

def _create_meshtastic_interface(host: str, port: int, verbose: bool = False):
    """
    Fábrica única de interface Meshtastic para el broker.
    Mantiene TCP como está (pool/shim si aplica) y añade BLE/USB sin romper nada.

    Devuelve:
      - TCPInterface(...) para tcp
      - BLEInterface(...) para bluetooth
      - SerialInterface(...) para usb
    """
    t = _mesh_transport()

    if t == "bluetooth":
        bt = (os.getenv("MESH_BT_ADDR", "") or "").strip()
        if not bt:
            raise RuntimeError("MESH_TRANSPORT=bluetooth pero falta MESH_BT_ADDR (MAC BLE).")
        try:
            from meshtastic.ble_interface import BLEInterface
        except Exception as e:
            raise RuntimeError(f"No se pudo importar BLEInterface: {e}")
        if verbose:
            print(f"[receiver] Transporte BLE → {bt}", flush=True)
        return BLEInterface(bt)

    if t == "usb":
        dev = (os.getenv("MESH_USB_PORT", "") or "").strip()
        if not dev:
            raise RuntimeError("MESH_TRANSPORT=usb pero falta MESH_USB_PORT (ej. /dev/ttyUSB0).")

        try:
            from meshtastic.serial_interface import SerialInterface
        except Exception as e:
            raise RuntimeError(f"No se pudo importar SerialInterface: {e}")

        if verbose:
            print(f"[receiver] Transporte USB/Serial → {dev}", flush=True)

        iface = None
        try:
            # API correcta de Meshtastic
            iface = SerialInterface(devPath=dev)

            # esperar inicialización del nodo
            for _ in range(20):
                try:
                    if iface.localNode:
                        break
                except Exception:
                    pass
                time.sleep(0.2)

            return iface

        except Exception:
            try:
                if iface is not None:
                    iface.close()
            except Exception:
                pass
            raise

    # Default: tcp
    if verbose:
        print(f"[receiver] Transporte TCP → {host}:{port}", flush=True)

    # Ruta preferente:
    # - En este broker, TCPInterface suele estar parcheado por el shim hacia TCPInterfacePool.
    # - El shim acepta hostname y port, evitando crear strings tipo "host:port".
    try:
        return TCPInterface(hostname=str(host), port=int(port or 4403))
    except TypeError:
        # Compatibilidad con SDKs antiguos que no acepten 'port'.
        host_for_iface = f"{host}:{port}" if port and int(port) != 4403 else str(host)
        return TCPInterface(hostname=host_for_iface)

class InterfaceManager:
    """
    Mantiene la conexión TCPInterface al nodo con reintentos/backoff.
    """
    def __init__(self, host: str, verbose: bool, enable_reconnect: bool):
        self.host = host
        self.verbose = verbose
        self.enable_reconnect = enable_reconnect
        self.iface = None
        self._want_run = True
        self._reconnect_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="iface-reconnector", daemon=True)
        self._lock = threading.Lock()
        self._paused = threading.Event()   # ← NUEVO

    def pause(self):
        """Pone en pausa la conexión: cierra la iface y no reconecta hasta resume()."""
        self._paused.set()
        self._reconnect_event.set()
        try:
            with self._lock:
                if self.iface:
                    try: self.iface.close()
                    except Exception: pass
                    self.iface = None
        except Exception:
            pass

    def resume(self):
        """Quita la pausa y dispara un ciclo de reconexión."""
        if self._paused.is_set():
            self._paused.clear()
            self._reconnect_event.set()

    def is_paused(self) -> bool:
        return self._paused.is_set()
    def start(self):
        self._thread.start()
        self._reconnect_event.set()

    def stop(self):
        self._want_run = False
        self._reconnect_event.set()
        try:
            with self._lock:
                if self.iface:
                    self.iface.close()
        except Exception:
            pass

    def signal_disconnect(self):
        if self.enable_reconnect:
            self._reconnect_event.set()

    def get_iface(self):
        with self._lock:
            return self.iface

    def _loop_OLD(self):
        backoff = [2, 4, 8, 12, 20, 30, 45, 60]
        idx = 0

        while self._want_run:

            self._reconnect_event.wait(timeout=5.0)
            if not self._reconnect_event.is_set():
                continue
            
            self._reconnect_event.clear()
            # === [NUEVO] si está en pausa, no conectar
            # === [TRAZA] si está en pausa, mostrar remaining (si hay cooldown)
            if self._paused.is_set():
                try:
                    if COOLDOWN.is_active():
                        rem = COOLDOWN.remaining()
                        # imprime cada ~1s (ajustable); evita spam si quieres subiendo el paso
                        print(f"[cooldown] ⏳ Pausado. Reintento cuando expire: quedan {rem}s", flush=True)
                        time.sleep(5.0)
                    else:
                        time.sleep(0.2)
                except Exception:
                    time.sleep(0.2)
                continue

            
            if not self.enable_reconnect and self.iface is not None:
                continue

            try:
                with self._lock:
                    if self.iface:
                        try: self.iface.close()
                        except Exception: pass
                        self.iface = None
            except Exception:
                pass

            while self._want_run:
                
                # === [NUEVO] Si ya tenemos conexión activa, no abras otra ===
              
                try:

                    try:
                        if bool(globals().get("_IS_CONNECTED", False)) and (self.iface is not None):
                            # ya estamos conectados; no crear una nueva interfaz
                            idx = 0
                            time.sleep(0.2)
                            break
                    except Exception:
                        pass

                    # === [NUEVO] Gate del Circuit Breaker ANTES de abrir socket
                    if not CIRCUIT_BREAKER.can_attempt():
                        time.sleep(1.0)
                        continue

                     # === [SUSTITUIR] Gate COOLDOWN/pausa por el snippet propuesto ===
                    # respeta cooldown/pausa antes de intentar conectar
                    try:
                        c = globals().get("COOLDOWN")
                        paused = bool(globals().get("MGR_PAUSED") and globals()["MGR_PAUSED"].is_set())
                        if (c and hasattr(c, "is_active") and c.is_active()) or paused:
                            # (opcional) log si quieres
                            if self.verbose and c and hasattr(c, "remaining"):
                                print(f"[cooldown] ⏳ Pausado. Reintento cuando expire: quedan {c.remaining()}s", flush=True)
                            time.sleep(0.5)
                            continue
                    except Exception:
                        pass
                    
                    # --- REEMPLAZA SOLO ESTA PARTE donde hoy tienes: new_iface = TCPInterface(hostname=self.host) ---
                    try:
                        # Si tu CLI permite puerto runtime, úsalo (ya lo guardas en globals en main)
                        port = int(globals().get("RUNTIME_MESH_PORT") or 4403)
                    except Exception:
                        port = 4403

                    # === [NUEVO] Serializar la construcción de la interfaz para evitar dobles sockets ===
                    with globals()["_CONNECT_LOCK"]:
                        if globals().get("_CONNECTING"):
                            # ya hay otro hilo intentando conectar; espera y reintenta el loop exterior
                            time.sleep(0.3)
                            continue
                        globals()["_CONNECTING"] = True
                        try:
                            # (opcional) reset duro del pool antes de abrir, si vienes de un error/timeout
                            try:
                                from tcpinterface_persistent import TCPInterfacePool
                                TCPInterfacePool.reset(globals().get("RUNTIME_MESH_HOST") or "", int(globals().get("RUNTIME_MESH_PORT") or 4403))
                                time.sleep(0.1)
                            except Exception:
                                pass

                            new_iface = TCPInterface(hostname=self.host)
                            with self._lock:
                                self.iface = new_iface
                            idx = 0
                        finally:
                            globals()["_CONNECTING"] = False

               
                    CIRCUIT_BREAKER.record_success()

                    # [NUEVO] Gracia post-conexión: deja respirar 1.5–2s antes de tráfico y de limpiar cooldown
                    time.sleep(2)      
                                   

                    # ✅ conexión OK → limpiar cooldown si seguía armado
                    try:
                        if COOLDOWN.is_active():
                            COOLDOWN.clear()
                            if self.verbose:
                                # NUEVO (mínimo): tras conectar, vuelve a cooldown base
                                globals()["COOLDOWN_SECS"] = 90

                                print("[cooldown] Limpio tras reconexión exitosa.", flush=True)
                    except Exception:
                        pass

                    break

                # === [NUEVO] Captura específica de errores de socket (WinError 10054, etc.) ===
                except OSError as e:
                    # Diferenciamos 10054 para un log más claro (peer cerró la conexión)

                    try:
                        CIRCUIT_BREAKER.record_error()
                    except Exception:
                        pass

                    winerr = getattr(e, "winerror", None)
                    is_10054 = (winerr == 10054) or ("10054" in str(e))
                    delay = backoff[min(idx, len(backoff) - 1)]
                    if self.verbose:
                        if is_10054:
                            print(f"[receiver] ⚠️ OSError 10054 conectando a {self.host}: "
                                f"el host remoto cerró la conexión (reintento en {delay}s)",
                                flush=True)
                        else:
                            print(f"[receiver] ⚠️ OSError conectando a {self.host}: {e} "
                                f"(reintento en {delay}s)",
                                flush=True)
                    time.sleep(delay)
                    idx += 1
                    continue

                # === EXISTENTE (no lo quites): captura genérica de cualquier otra excepción ===
                except Exception as e:

                    try:
                        CIRCUIT_BREAKER.record_error()
                    except Exception:
                        pass

                    delay = backoff[min(idx, len(backoff) - 1)]
                    if self.verbose:
                        print(f"[receiver] Fallo conectando a {self.host}: {e} (reintento en {delay}s)", flush=True)
                    time.sleep(delay)
                    idx += 1

# === MODIFICADA: bucle del pool con anti-reentradas + lock interproceso (sin depender de self.port) ===
    def _loop(self):
        """
        Bucle principal de conexión/reconexión de la interfaz Meshtastic.

        Soporta:
        - TCP  (WiFi/LAN)
        - BLE  (Bluetooth)
        - USB  (SerialInterface)

        Comportamiento 24/7:
        - Un único intento de conexión en vuelo.
        - Lock interproceso por transporte para evitar dobles aperturas.
        - Limpieza segura de iface previa antes de reconectar.
        - Recuperación específica USB cuando el propio proceso deja el puerto bloqueado
            tras un timeout o una apertura parcial.
        """
        import threading
        import time
        import os

        backoff = [2, 4, 8, 12, 20, 30, 45, 60]
        idx = 0

        if not hasattr(self, "_connecting"):
            self._connecting = threading.Event()

        host = getattr(self, "host", None) or str(globals().get("RUNTIME_MESH_HOST") or "127.0.0.1")
        try:
            port = int(globals().get("RUNTIME_MESH_PORT") or 4403)
        except Exception:
            port = 4403

        lock_name = _mesh_transport_id(host, port)
        if not hasattr(self, "_ip_lock"):
            self._ip_lock = SingleInstanceLock(lock_name)

        while self._want_run:
            self._reconnect_event.wait(timeout=1.0)
            if not self._reconnect_event.is_set():
                continue

            # Si está pausado, no conectar
            if hasattr(self, "_paused") and self._paused.is_set():
                time.sleep(0.2)
                continue

            # Si ya hay un connect en curso, esperar
            if self._connecting.is_set():
                time.sleep(0.1)
                continue

            # Consumimos la señal
            self._reconnect_event.clear()

            # Cierre limpio de iface previa
            try:
                with self._lock:
                    if getattr(self, "iface", None):
                        try:
                            self.iface.close()
                        except Exception:
                            pass
                        self.iface = None
            except Exception:
                pass

            # Respeta cooldown si existe
            try:
                if getattr(self, "_cooldown_until", 0) > time.time():
                    time.sleep(min(1.0, self._cooldown_until - time.time()))
            except Exception:
                pass

            self._connecting.set()
            got_lock = False
            new_iface = None

            try:
                # Lock interproceso
                got_lock = self._ip_lock.acquire(timeout_s=2.0)
                if not got_lock:
                    if getattr(self, "verbose", False):
                        print(f"[lock] Otro proceso posee {lock_name}. Esperando…", flush=True)
                    time.sleep(1.0)
                    self._reconnect_event.set()
                    continue

                # Log correcto del intento
                if getattr(self, "verbose", False):
                    print(f"[receiver] Conectando a Meshtastic en {host}:{port}…", flush=True)

                # Crear interfaz según transporte
                new_iface = _create_meshtastic_interface(
                    host=str(host),
                    port=int(port),
                    verbose=getattr(self, "verbose", False),
                )

                # Conexión OK
                idx = 0

                with self._lock:
                    self.iface = new_iface
                    try:
                        self._attach_handlers_locked()
                    except Exception:
                        pass

                if getattr(self, "verbose", False):
                    print("ℹ️ Broker: conectado al nodo Meshtastic", flush=True)

                try:
                    if hasattr(self, "_on_connect_ok"):
                        self._on_connect_ok()
                except Exception:
                    pass

                # Mantener la conexión mientras siga viva
                while (
                    self._want_run
                    and getattr(self, "iface", None) is not None
                    and not getattr(self, "_paused", threading.Event()).is_set()
                ):
                    time.sleep(0.25)

            except Exception as e:
                if getattr(self, "verbose", False):
                    print(f"[receiver] Fallo al crear interface ({_mesh_transport()}): {e}", flush=True)

                # Recuperación específica USB cuando el puerto queda bloqueado
                try:
                    if _mesh_transport() == "usb":
                        dev = (os.getenv("MESH_USB_PORT", "") or "").strip()
                        emsg = (str(e) or "").lower()

                        if dev and ("exclusively lock port" in emsg or "resource temporarily unavailable" in emsg):
                            who = _debug_who_holds_serial(dev)
                            if who:
                                print(f"[receiver] USB ocupado: {who}", flush=True)

                                # Si el puerto lo está reteniendo ESTE proceso, liberamos iface limpia
                                try:
                                    mypid = os.getpid()
                                    if f"pid={mypid} " in (who or ""):
                                        print(f"[receiver] USB ocupado por ESTE proceso (pid={mypid}). Liberando iface de forma segura...", flush=True)

                                        try:
                                            with self._lock:
                                                old_iface = getattr(self, "iface", None)
                                                self.iface = None

                                            if old_iface is not None:
                                                try:
                                                    old_iface.close()
                                                except Exception:
                                                    pass
                                        except Exception:
                                            pass

                                        # Dar tiempo a pyserial/hilo lector a soltar el dispositivo
                                        time.sleep(0.8)
                                except Exception:
                                    pass
                except Exception:
                    pass

                # Cerrar iface parcial si llegó a construirse
                try:
                    if new_iface is not None:
                        try:
                            new_iface.close()
                        except Exception:
                            pass
                except Exception:
                    pass

                time.sleep(backoff[min(idx, len(backoff) - 1)])
                idx += 1
                self._reconnect_event.set()

            finally:
                self._connecting.clear()
                try:
                    if got_lock:
                        self._ip_lock.release()
                except Exception:
                    pass

            # Si salimos del bucle de mantenimiento, cerrar iface actual y notificar desconexión
            try:
                with self._lock:
                    if getattr(self, "iface", None):
                        try:
                            self.iface.close()
                        except Exception:
                            pass
                        self.iface = None
            except Exception:
                pass

            try:
                if hasattr(self, "_on_disconnect"):
                    self._on_disconnect()
            except Exception:
                pass

            self._reconnect_event.set()

    # ===================== Receptor PubSub =====================

class MeshReceiver:
    def __init__(self, hub: JsonLineHub, stats: BrokerStats, verbose: bool,
                 assume_primary: bool, assume_rfslot: bool,
                 iface_mgr: InterfaceManager, debug_packets: bool = False, text_only: bool = False):
        self.hub = hub
        self.stats = stats
        self.verbose = verbose
        self.assume_primary = assume_primary
        self.assume_rfslot = assume_rfslot
        self.iface_mgr = iface_mgr
        self.debug_packets = debug_packets
        self.text_only = text_only
        self.assume_user_primary: bool = True  # canal 0 por defecto en TEXT_MESSAGE_APP

        self._rf_slot_default: Optional[int] = None
        self._alias_cache: dict[str, tuple[str, int]] = {}
        self._alias_cache_ttl = 900  # 15 min


# MODIFICADA: función completa con persistencia offline
    def _on_rx(self, packet=None, interface=None, **kwargs):
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded", {}) or {}
            portnum = decoded.get("portnum")

            # === [FILTRO] Heartbeats (si no queremos mostrarlos, salimos pronto) ===
            if not SHOW_HEARTBEATS and _is_heartbeat_from_decoded_or_pkt(portnum, decoded, pkt):
                return  # no ensuciamos consola ni broadcast_line

            # === Debug opcional de estructura ===
            debug_packet_structure(pkt, self.debug_packets)

            # === Completar texto si podemos (sin ruido en consola) ===
            try_fill_text_inplace(pkt, debug_packets=self.debug_packets)

            # === Canales presentes en el paquete (siempre inicializados) ===
            canal = extract_logical_channel(pkt)
            rfch  = extract_rf_channel(pkt)

            # Inferencia de canal lógico para puertos de sistema si viene vacío
            # (POSITION_APP, TELEMETRY_APP, NODEINFO_APP, NEIGHBORINFO_APP, ROUTING_APP)
            if canal is None:
                canal_infer, did = infer_logical_channel(
                    portnum=str(portnum) if portnum else None,
                    enable=True  # o self.assume_primary si ya lo manejas así
                )
                if did and canal_infer is not None:
                    canal = canal_infer

            # Texto/payload resumidos (útil para TEXT_MESSAGE_APP)
            text, payload_hex = _decode_payload_text(decoded)
            if not text:
                text = extract_text_from_packet(pkt, debug_packets=self.debug_packets)

            # Valor por defecto de RF slot leído del nodo (si lo tenemos)
            rf_assumed = False
            if rfch is None:
                # si tienes self._rf_slot_default como cache:
                if getattr(self, "_rf_slot_default", None) is not None:
                    try:
                        rfch = int(self._rf_slot_default)
                        rf_assumed = True
                    except Exception:
                        rfch = None
                if rfch is None:
                    # inferir vía interfaz si tienes helper infer_rf_channel()
                    try:
                        iface = interface or (self.iface_mgr.get_iface() if hasattr(self, "iface_mgr") else None)
                    except Exception:
                        iface = None
                    try:
                        infer_rf, rf_assumed = infer_rf_channel(iface, getattr(self, "assume_rfslot", True))
                        if infer_rf is not None:
                            rfch = infer_rf
                    except Exception:
                        pass

            # TEXT_MESSAGE_APP sin canal → asumir 0 si está activado
            canal_assumed = False
            if canal is None and str(portnum) == "TEXT_MESSAGE_APP" and getattr(self, "assume_user_primary", True):
                canal = 0
                canal_assumed = True

            # Fallback final seguro
            if canal is None:
                canal = 0

            # === Métricas ===
            rssi = extract_rssi(pkt)
            snr  = extract_snr(pkt)

            # === Sellar metadatos para clientes ===
            stamp_channels(pkt, canal, rfch)

            # === IDs origen/destino (robusto) ===
            who_from, who_to = _extract_ids_from_packet(pkt, decoded)

            # === Resolve alias (directo de trama → cache → iface) ===
            from_alias = None
            to_alias = None

            # 1) Si la trama trae user.longName/shortName (p.ej. NODEINFO_APP)
            user_obj = (decoded.get("user") or {})
            from_alias = (user_obj.get("longName") or user_obj.get("shortName") or "").strip() or None

            # 2) Usar mini-cache / iface como fallback
            iface_now = interface or getattr(self, "iface", None)
            if not from_alias:
                from_alias = self._alias_cache_get(who_from) or self._alias_from_iface(iface_now, who_from)
                if from_alias:
                    self._alias_cache_put(who_from, from_alias)

            if who_to and who_to not in ("^all", "?"):


                to_alias = self._alias_cache_get(who_to) or self._alias_from_iface(iface_now, who_to)
                if to_alias:
                    self._alias_cache_put(who_to, to_alias)

            # === Respuesta automática propiedad del broker ==================
            # SENDQ conserva una sola conexión al nodo y aplica su resiliencia.
            try:
                if str(portnum) == "TEXT_MESSAGE_APP" and isinstance(text, str):
                    _enqueue_meshtastic_auto_reply(int(canal), text)
            except Exception as _e_auto_reply:
                if self.verbose:
                    print(
                        f"[auto-reply] Meshtastic ch={canal} ERROR: "
                        f"{type(_e_auto_reply).__name__}: {_e_auto_reply}",
                        flush=True,
                    )


            # === Salida consola (fundamental) si --verbose y no text-only ===
            if self.verbose and not self.text_only:
                canal_s = f"{canal}*" if canal_assumed else (str(canal) if canal is not None else "??")
                rfch_s  = f"{rfch}*" if rf_assumed else (str(rfch) if rfch is not None else "??")
                rssi_s  = "?" if rssi is None else f"{rssi:.0f} dBm"
                snr_s   = "?" if snr  is None else f"{snr:.1f} dB"
                text_s  = text if text else "(no-texto)"
                #print(f"[Canal {canal_s} | RFch {rfch_s} | {portnum or 'UNKNOWN'} | {who_from} → {who_to} | RSSI {rssi_s} | SNR {snr_s}] {text_s}", flush=True)
                from_txt = f"{(from_alias or '').strip()} ({who_from})" if (from_alias or "").strip() else str(who_from)
                to_txt   = f"{(to_alias or '').strip()} ({who_to})"     if (to_alias or "").strip()   else str(who_to)
                _print_frame_line(f"[Canal {canal_s} | RFch {rfch_s} | {portnum or 'UNKNOWN'} | {from_txt} → {to_txt} | RSSI {rssi_s} | SNR {snr_s}] {text_s}", flush=True)
            
            channel_name = None
            try:
                if canal is not None:
                    channel_name = CHANNEL_NAME_BY_INDEX.get(int(canal))
            except Exception:
                channel_name = None

            # === [FARMACIAS] Comando interno procesado por el broker ============
            # Se intercepta antes de emitir al hub, BBS, APRS o bridge. La consulta
            # se ejecuta en un hilo corto para no bloquear el receptor PubSub.
            try:
                if str(portnum) == "TEXT_MESSAGE_APP" and isinstance(text, str):
                    from farmacias_commands import (
                        FarmaciasCommandContext,
                        handle_farmacias_command,
                        is_farmacias_command,
                    )
                    if is_farmacias_command(text):
                        _to_norm_farma = _norm_node_id(who_to)
                        _is_dm_farma = bool(_to_norm_farma) and _to_norm_farma not in {"^all", "broadcast", "?"}
                        _ctx_farma = FarmaciasCommandContext(
                            network="meshtastic",
                            source_id=str(who_from),
                            text=str(text),
                            channel=int(canal) if canal is not None else None,
                            is_direct=bool(_is_dm_farma),
                            packet_id=pkt.get("id") or pkt.get("packetId") or pkt.get("packet_id"),
                        )

                        def _farma_meshtastic_worker():
                            def _enqueue_dm(_message: str) -> None:
                                _q = globals().get("SENDQ")
                                if _q is None or not hasattr(_q, "offer"):
                                    raise RuntimeError("SENDQ no disponible")
                                _q.offer({
                                    "channel": int(canal) if canal is not None else 0,
                                    "text": str(_message),
                                    "destination": str(who_from),
                                    "require_ack": False,
                                    "type": "text",
                                    "no_bridge": True,
                                    "origin": "farmacias",
                                    "meta": {"farmacias": 1, "reply_dm": 1},
                                }, coalesce=False)
                            handle_farmacias_command(_ctx_farma, _enqueue_dm)

                        # Si el comando no procede de DM ni del canal FARMACIA, el
                        # manejador devuelve False y el paquete sigue su flujo normal.
                        from farmacias_commands import is_allowed_origin
                        if is_allowed_origin(_ctx_farma):
                            threading.Thread(
                                target=_farma_meshtastic_worker,
                                name="farmacias-meshtastic",
                                daemon=True,
                            ).start()
                            return
            except Exception as _e_farma:
                if self.verbose:
                    print(f"⚠️ farmacias meshtastic: {_e_farma}", flush=True)

            # === [EMERGENCIAS] Comando interno procesado por el broker =========
            try:
                if str(portnum) == "TEXT_MESSAGE_APP" and isinstance(text, str):
                    from emergencias_commands import (
                        EmergenciasCommandContext,
                        handle_emergencias_command,
                        is_allowed_origin as is_emergencias_allowed_origin,
                        is_emergencias_command,
                    )
                    if is_emergencias_command(text):
                        _to_norm_emerg = _norm_node_id(who_to)
                        _is_dm_emerg = (
                            bool(_to_norm_emerg)
                            and _to_norm_emerg not in {"^all", "broadcast", "?"}
                        )
                        _ctx_emerg = EmergenciasCommandContext(
                            network="meshtastic",
                            source_id=str(who_from),
                            text=str(text),
                            channel=int(canal) if canal is not None else None,
                            is_direct=bool(_is_dm_emerg),
                            packet_id=pkt.get("id") or pkt.get("packetId") or pkt.get("packet_id"),
                        )

                        def _emergencias_meshtastic_worker():
                            def _enqueue_dm(_message: str) -> None:
                                _q = globals().get("SENDQ")
                                if _q is None or not hasattr(_q, "offer"):
                                    raise RuntimeError("SENDQ no disponible")
                                _q.offer({
                                    "channel": int(canal) if canal is not None else 0,
                                    "text": str(_message),
                                    "destination": str(who_from),
                                    "require_ack": False,
                                    "type": "text",
                                    "no_bridge": True,
                                    "origin": "emergencias",
                                    "meta": {"emergencias": 1, "reply_dm": 1},
                                }, coalesce=False)
                            handle_emergencias_command(_ctx_emerg, _enqueue_dm)

                        if is_emergencias_allowed_origin(_ctx_emerg):
                            threading.Thread(
                                target=_emergencias_meshtastic_worker,
                                name="emergencias-meshtastic",
                                daemon=True,
                            ).start()
                            return
            except Exception as _e_emerg:
                if self.verbose:
                    print(f"⚠️ emergencias meshtastic: {_e_emerg}", flush=True)


            # === Emitir JSONL a clientes ===
            event = {
                "type": "packet",
                "packet": pkt,
                 # === NUEVO: IDs y alias en plano para que el bot los lea fácil ===
                "from": who_from,
                "to":   who_to,
                "from_alias": from_alias or None,
                "to_alias":   to_alias   or None,
                "channel_name": channel_name,
                "summary": {
                    "portnum": portnum,
                    "text": text,
                    "payload_hex": payload_hex,
                    "canal": canal,
                    # NUEVO
                    "channel_name": channel_name,
                    
                    "rfch": rfch,
                    "rssi": rssi,
                    "snr": snr,
                },
                "assumptions": {
                    "canal_assumed": bool(canal_assumed),
                    "rfch_assumed": bool(rf_assumed),
                },
                "ts": _now_s(),
            }
            # --- [NUEVO] No ecoar al bot/listeners los comandos de la BBS ---
            # Motivo: /escuchar all no debería mostrar tráfico de control (#BBS ...).
            # Esto NO afecta al procesamiento interno de la BBS: el intercept sigue ejecutándose.
            try:
                t0 = (text or "").strip()
                hide_bbs = (os.getenv("BBS_HIDE_ECHO", "1").strip().lower() in {"1", "true", "on", "si", "sí", "y", "yes"})
                if hide_bbs and str(portnum) == "TEXT_MESSAGE_APP" and t0.upper().startswith("#BBS"):
                    pass  # no emitimos a JSONL (bot/escucha)
                else:
                    self.hub.broadcast_line(_json_dumps(event) + "\n")
            except Exception:
                # fallback: no romper el flujo si falla el filtro
                self.hub.broadcast_line(_json_dumps(event) + "\n")


            # === Contador simple por tipo de puerto/canal ===
            try:
                self.stats.bump(portnum, canal)
                # === [NUEVO] latido para watchdog
                try:
                    WATCHDOG.beat()
                except Exception:
                    pass

            except Exception:
                pass

            # === Persistencia para replay cuando sea texto de usuario ===
            if str(portnum) == "TEXT_MESSAGE_APP":
                try:
                    append_offline_log({
                        "ts": int(_now_s()),
                        "channel": canal,
                        "channel_name": channel_name,
                        "portnum": "TEXT_MESSAGE_APP",
                        "from": who_from,
                        "to": who_to,
                        "from_alias": from_alias or None,
                        "to_alias":   to_alias   or None,
                        "text": text,
                        "rx_rssi": rssi,
                        "rx_snr": snr,
                        "hop_limit": pkt.get("hop_limit"),
                        "hop_start": pkt.get("hop_start"),
                        "relay_node": pkt.get("relay_node"),
                    })

                    # === [NUEVO] BBS: interceptar #BBS en mensajes de texto (CANAL BBS) ===
                    try:
                        bbs = globals().get("BBS_ENGINE")
                        if bbs and str(portnum) == "TEXT_MESSAGE_APP":
                            t0 = (text or "").strip()

                            if t0.upper().startswith("#BBS"):

                                # Heurística DM robusta: destino presente y NO es broadcast
                                _to_norm = _norm_node_id(who_to)
                                is_dm = bool(_to_norm) and _to_norm not in {"^all", "broadcast", "?"}

                                # Motor BBS
                                bbs = globals().get("BBS_ENGINE")
                                if not bbs:
                                    raise StopIteration

                                bbs_callsign = (os.getenv("BBS_CALLSIGN") or getattr(bbs, "bbs_callsign", "") or "").strip().upper()
                                if not bbs_callsign:
                                    raise StopIteration

                                # Política
                                dm_only = (os.getenv("BBS_DM_ONLY", "1").strip().lower() in {"1", "true", "on", "si", "sí", "y", "yes"})
                                dm_init_hint = (os.getenv("BBS_DM_INIT_HINT", "1").strip().lower() in {"1", "true", "on", "si", "sí", "y", "yes"})

                                # Canal DM (por defecto CH0)
                                try:
                                    dm_ch = int(os.getenv("BBS_DM_CHANNEL", "0"))
                                except Exception:
                                    dm_ch = 0

                                # Canales públicos permitidos (BBS_CHANNELS o fallback BBS_CHANNEL)
                                def _parse_bbs_channels() -> set:
                                    raw = (os.getenv("BBS_CHANNELS") or os.getenv("BBS_CHANNEL") or "").strip()
                                    out = set()
                                    for part in raw.split(","):
                                        p = (part or "").strip()
                                        if not p:
                                            continue
                                        try:
                                            out.add(int(p))
                                        except Exception:
                                            continue
                                    return out

                                allowed_ch = _parse_bbs_channels()

                                # Si NO es DM y no hay canales configurados, no atendemos en público (evita enganches accidentales)
                                if (not is_dm) and (not allowed_ch):
                                    raise StopIteration

                                # Filtrado: si no es DM, solo atender en canales BBS autorizados
                                if (not is_dm) and (int(canal) not in allowed_ch):
                                    raise StopIteration

                                # Parseo
                                parts = t0.split(maxsplit=2)
                                # parts[0] = #BBS
                                # parts[1] = CALLSIGN (opcional en DM, obligatorio en canal)
                                # parts[2] = RESTO (opcional)

                                # Normalización por defecto (SIEMPRE definidos)
                                text_for_bbs = t0
                                ch_for_bbs = int(canal)

                                q = globals().get("SENDQ")

                                # ─────────────────────────────
                                # CANAL PÚBLICO
                                # ─────────────────────────────
                                if not is_dm:

                                    # En canal público SIEMPRE se exige CALLSIGN
                                    if len(parts) < 2:
                                        if dm_only and dm_init_hint and (q is not None) and hasattr(q, "offer"):
                                            hint = (
                                                "BBS: sintaxis obligatoria en canal (multi-BBS).\n"
                                                f"Usa: #BBS {bbs_callsign} <COMANDO>\n"
                                                "Responderé por DM.\n"
                                                "En DM puedes iniciar con: #BBS"
                                            )
                                            q.offer(
                                                {"channel": dm_ch, "text": hint, "destination": str(who_from), "require_ack": False, "type": "text",
                                                "no_bridge": True, "origin": "bbs", "meta": {"bbs": 1}
                                            },
                                                coalesce=False
                                            )
                                        raise StopIteration

                                    target_bbs = (parts[1] or "").strip().upper()

                                    def _looks_like_callsign(tok: str) -> bool:
                                        t = (tok or "").strip().upper()
                                        if len(t) < 3 or len(t) > 16:
                                            return False
                                        if not any(c.isalpha() for c in t):
                                            return False
                                        if not any(c.isdigit() for c in t):
                                            return False
                                        return all(c.isalnum() or c in "-/" for c in t)

                                    if target_bbs != bbs_callsign:
                                        # Si parece que se han olvidado el callsign (p.ej. "#BBS MENU"), mandamos hint por DM.
                                        if (dm_only and dm_init_hint and (q is not None) and hasattr(q, "offer") and (not _looks_like_callsign(target_bbs))):
                                            hint = (
                                                "BBS: sintaxis obligatoria en canal (multi-BBS).\n"
                                                f"Usa: #BBS {bbs_callsign} <COMANDO>\n"
                                                "Responderé por DM.\n"
                                                "En DM puedes iniciar con: #BBS"
                                            )
                                            q.offer(
                                                {"channel": dm_ch, "text": hint, "destination": str(who_from), "require_ack": False, "type": "text",
                                                "no_bridge": True, "origin": "bbs", "meta": {"bbs": 1}
                                            },
                                                coalesce=False
                                            )
                                        raise StopIteration

                                    # dm_only: bootstrap a DM y normaliza a formato corto "#BBS <COMANDO...>"
                                    if dm_only:
                                        if len(parts) == 2:
                                            text_for_bbs = "#BBS"
                                        else:
                                            text_for_bbs = "#BBS " + (parts[2] or "").strip()
                                        ch_for_bbs = int(dm_ch)
                                    else:
                                        text_for_bbs = t0
                                        ch_for_bbs = int(canal)

                                # ─────────────────────────────
                                # DM
                                # ─────────────────────────────
                                else:

                                    # Si llega "#BBS <CALLSIGN> ..." en DM:
                                    # - si es nuestra BBS, se normaliza a "#BBS <COMANDO...>"
                                    # - si es otra BBS, se ignora
                                    def _looks_like_callsign(tok: str) -> bool:
                                        t = (tok or "").strip().upper()
                                        if len(t) < 3 or len(t) > 16:
                                            return False
                                        if not any(c.isalpha() for c in t):
                                            return False
                                        if not any(c.isdigit() for c in t):
                                            return False
                                        return all(c.isalnum() or c in "-/" for c in t)

                                    if len(parts) >= 2:
                                        maybe = (parts[1] or "").strip().upper()
                                        if _looks_like_callsign(maybe):
                                            if maybe != bbs_callsign:
                                                raise StopIteration
                                            # es nuestra BBS: normalizar
                                            if len(parts) == 2:
                                                text_for_bbs = "#BBS"
                                            else:
                                                text_for_bbs = "#BBS " + (parts[2] or "").strip()

                                    # En DM siempre usamos dm_ch
                                    ch_for_bbs = int(dm_ch)

                                # Procesar por el motor BBS
                                chunks = bbs.handle_text(from_id=str(who_from), ch=int(ch_for_bbs), text=text_for_bbs)

                                # Enviar respuesta
                                if chunks and (q is not None) and hasattr(q, "offer"):
                                    for c in chunks:
                                        c = (c or "").strip()
                                        if not c:
                                            continue
                                        # Responder por DM cuando sea DM o cuando dm_only esté activo
                                        if is_dm or dm_only:
                                            q.offer(
                                                {
                                                    "channel": int(dm_ch),
                                                    "text": c,
                                                    "destination": str(who_from),
                                                    "require_ack": False,
                                                    "type": "text",
                                                    "no_bridge": True,
                                                    "origin": "bbs",
                                                    "meta": {"bbs": 1},
                                                },
                                                coalesce=False,
                                            )
                                        else:
                                            q.offer(
                                                {
                                                    "channel": int(canal),
                                                    "text": c,
                                                    "destination": None,
                                                    "require_ack": False,
                                                    "type": "text",
                                                    "no_bridge": True,
                                                    "origin": "bbs",
                                                    "meta": {"bbs": 1},
                                                },
                                                coalesce=False,
                                            )

                                # Si era BBS, no continuar con el bloque /aprs (evita interferencias)
                                raise StopIteration
                                           

                    except StopIteration:
                        return
                    except Exception as _e_bbs:
                        if self.verbose:
                            print(f"⚠️ bbs: {_e_bbs}", flush=True)
                    # === [NUEVO] Malla -> correo electrónico desde Meshtastic ===
                    try:
                        if str(portnum) == "TEXT_MESSAGE_APP" and isinstance(text, str):
                            mail_reply = _handle_mesh_mail_command_if_needed(text, source=(from_alias or str(who_from)))
                            if mail_reply is not None:
                                q = globals().get("SENDQ")
                                if q is not None and hasattr(q, "offer"):
                                    q.offer({
                                        "channel": int(canal),
                                        "text": str(mail_reply),
                                        "destination": None,
                                        "require_ack": False,
                                        "type": "text",
                                        "no_bridge": True,
                                        "origin": "email",
                                        "meta": {"mail_command": 1},
                                    }, coalesce=False)
                                raise StopIteration
                    except StopIteration:
                        return
                    except Exception as _e_mail:
                        try:
                            q = globals().get("SENDQ")
                            if q is not None and hasattr(q, "offer"):
                                q.offer({
                                    "channel": int(canal),
                                    "text": f"Error correo: {_e_mail}",
                                    "destination": None,
                                    "require_ack": False,
                                    "type": "text",
                                    "no_bridge": True,
                                    "origin": "email",
                                    "meta": {"mail_command": 1, "error": 1},
                                }, coalesce=False)
                        except Exception:
                            pass
                        return

                    # === [NUEVO] MeshCore embebido: reenviar TEXT_MESSAGE_APP Meshtastic -> MeshCore ===
                    try:
                        mc = globals().get("MESHCORE_ENGINE")
                        if mc and str(portnum) == "TEXT_MESSAGE_APP":
                            # hops reales (si están disponibles)
                            hop_real = None
                            try:
                                hs = pkt.get("hop_start")
                                hl = pkt.get("hop_limit")
                                if isinstance(hs, (int, float)) and isinstance(hl, (int, float)):
                                    hop_real = int(hs) - int(hl)
                            except Exception:
                                hop_real = None

                            mc.forward_from_meshtastic(
                                ch=int(canal),
                                text=str(text or ""),
                                from_id=str(who_from),
                                from_alias=(from_alias or None),
                                channel_name=(channel_name or None),
                                hop_real=hop_real,
                            )
                    except Exception as _e_mc_fw:
                        if self.verbose:
                            print(f"⚠️ meshcore→fw: {_e_mc_fw}", flush=True)

                  
                    # === [NUEVO] DM /aprs canal N ... -> reinyectar SOLO el texto limpio en canal N ===
                    # Motivo: permitir mandar una orden por privado (no visible en canales públicos)
                    # y que el broker publique únicamente el texto resultante en el canal indicado.
                    #
                    # Formato aceptado (DM):
                    #   /aprs canal 2 EB2EAS-7: Hola
                    #   /aprs ch 2 broadcast: Hola
                    #
                    # Nota:
                    #   - El envío APRS (RF/IS) lo seguirá haciendo la pasarela APRS al ver el /aprs en el stream.
                    #   - Aquí SOLO reinyectamos el texto a Mesh, sin /aprs, para evitar bucles.
                    try:
                        if str(portnum) == "TEXT_MESSAGE_APP" and isinstance(text, str) and text.lstrip().lower().startswith("/aprs"):
                            _to = _norm_node_id(who_to)

                            # DM estrictamente dirigido a ESTE nodo (HOME_NODE_ID).
                            # Si HOME_NODE_ID no está configurado, fallback al comportamiento anterior (no romper nada).
                            if HOME_NODE_ID:
                                if _to != HOME_NODE_ID:
                                     # No es un DM dirigido a mi nodo -> no hacer nada
                                     raise StopIteration
                                
                            else:
                                # fallback: DM genérico (no broadcast)
                                if not who_to or who_to in ("^all", "?"):
                                    raise StopIteration
                                                                
                            m_cmd = _APRS_CANAL_CMD_RE.match(text)
                            if m_cmd:
                                ch_out = int(m_cmd.group(1))
                                clean_txt = (m_cmd.group(3) or "").strip()
                                if clean_txt:
                                    q = globals().get("SENDQ")
                                    if q is not None and hasattr(q, "offer"):
                                        # Reinyecta SOLO el texto limpio al canal Mesh indicado (broadcast) y evita bridge.
                                        q.offer(
                                            {
                                                "channel": int(ch_out),
                                                "text": clean_txt,
                                                "destination": None,
                                                "require_ack": False,
                                                "type": "text",
                                                "no_bridge": True,
                                                "origin": "aprs",
                                                "meta": {"aprs": 1},
                                            },
                                            coalesce=False,
                                        )

                                        if self.verbose:
                                            print(
                                                f"[dm→mesh] Reinyectado CH{ch_out} len={len(clean_txt.encode('utf-8'))}",
                                                flush=True,
                                            )
                   
                   
                    except StopIteration:
                        pass           
                    except Exception as _e_dm:
                        if self.verbose:
                            print(f"⚠️ dm→mesh: {_e_dm}", flush=True)


                except Exception as _e:
                    if self.verbose:
                        print(f"⚠️ offline_log: {_e}", flush=True)

            # === Guardar POSITIONS robusto (una sola vez) ===
            if str(portnum) == "POSITION_APP":
                try:
                    from positions_store import append_position_record, _ts_now_utc
                    pos = decoded.get("position") or pkt.get("position") or {}
                    if not isinstance(pos, dict):
                        pos = {}
                    lat = pos.get("latitude") or pos.get("lat")
                    lon = pos.get("longitude") or pos.get("lon")
                    alt = pos.get("altitude") or pos.get("alt")

                    if lat is not None and lon is not None:
                        rec = {
                            "ts": _ts_now_utc(),
                            "id": who_from,
                            "alias": from_alias,
                            "lat": float(lat),
                            "lon": float(lon),
                            "alt": (float(alt) if alt is not None else None),
                            "rx_rssi": rssi,
                            "rx_snr": snr,
                            "channel": int(canal) if isinstance(canal, (int, float)) else 0,
                            "rf_channel": int(rfch) if isinstance(rfch, (int, float)) else None,
                        }
                        append_position_record(rec)
                except Exception as e_save:
                    # No interrumpir el flujo del broker si falla un guardado puntual
                    print(f"⚠️ no se pudo guardar posición: {e_save} (from {who_from} → {who_to})", flush=True)

                # === [NUEVO] Persistir POSITION_APP en backlog JSONL ===
                try:
                    _ts = pkt.get("rxTime") or pkt.get("timestamp") or time.time()
                    pos = decoded.get("position") or pkt.get("position") or {}
                    lat = pos.get("lat"); lon = pos.get("lon")
                    if (lat is not None) and (lon is not None):
                        rec_pos = {
                            "ts":        int(_ts),
                            "rx_time":   int(_ts),          # 👈 lo usa _iter_backlog_jsonl() para filtrar since_ts
                            "channel":   canal,
                            "portnum":   "POSITION_APP",
                            "from":      who_from,
                            "to":        who_to,
                            "from_alias": from_alias or None,
                            "to_alias":   to_alias   or None,
                            "lat":       lat,
                            "lon":       lon,
                            "rssi":      rssi,
                            "snr":       snr,
                        }
                        append_offline_log(rec_pos)
                except Exception as e_off:
                    if self.verbose:
                        print(f"⚠️ offline_log POSITION_APP: {e_off}", flush=True)



            # === [NUEVO] Handler TELEMETRY_APP ===========================================
            if str(portnum) == "TELEMETRY_APP" and TELE_STORE is not None:
                try:
                    TELE_STORE.ingest_packet(pkt)
                except Exception as e:
                    logging.warning(f"[telemetry] fallo al ingerir telemetría: {e}")
           
                # === [NUEVO] Persistir TELEMETRY_APP en backlog JSONL ===
                try:
                    _ts = pkt.get("rxTime") or pkt.get("timestamp") or time.time()
                    tel = (decoded.get("telemetry") or pkt.get("telemetry") or {}) if isinstance(decoded, dict) else {}
                    # Normalización de campos típicos
                    telemetry_norm = {}
                    if isinstance(tel, dict):
                        telemetry_norm = {
                            "battery":     tel.get("battery") or tel.get("batt"),
                            "voltage":     tel.get("voltage") or tel.get("volt"),
                            "temperature": tel.get("temperature") or tel.get("temp"),
                            "humidity":    tel.get("humidity") or tel.get("hum"),
                            "pressure":    tel.get("pressure") or tel.get("press"),
                        }
                    # Solo persistimos si hay algo útil
                    if any(v is not None for v in telemetry_norm.values()):
                        rec_tel = {
                            "ts":        int(_ts),
                            "rx_time":   int(_ts),
                            "channel":   canal,
                            "portnum":   "TELEMETRY_APP",
                            "from":      who_from,
                            "to":        who_to,
                            "from_alias": from_alias or None,
                            "to_alias":   to_alias   or None,
                            "telemetry": telemetry_norm,
                            "rssi":      rssi,
                            "snr":       snr,
                        }
                        append_offline_log(rec_tel)
                except Exception as e_off:
                    if self.verbose:
                        print(f"⚠️ offline_log TELEMETRY_APP: {e_off}", flush=True)
                    
           
           # === [FIN TELEMETRY_APP] ======================================================
   
            # === [OPCIONAL] Persistir NEIGHBORINFO_APP en backlog JSONL ===
            if str(portnum) == "NEIGHBORINFO_APP":
                try:
                    _ts = pkt.get("rxTime") or pkt.get("timestamp") or time.time()
                    hops_val = decoded.get("hops") if isinstance(decoded, dict) else None
                    rec_nei = {
                        "ts":        int(_ts),
                        "rx_time":   int(_ts),
                        "channel":   canal,
                        "portnum":   "NEIGHBORINFO_APP",
                        "from":      who_from,
                        "to":        who_to,
                        "from_alias": from_alias or None,
                        "to_alias":   to_alias   or None,
                        "hops":      hops_val if isinstance(hops_val, int) else None,
                        "rssi":      rssi,
                        "snr":       snr,
                    }
                    append_offline_log(rec_nei)
                except Exception as e_off:
                    if self.verbose:
                        print(f"⚠️ offline_log NEIGHBORINFO_APP: {e_off}", flush=True)



            # === [WEBPANEL/TRACEROUTE v7.0.2] Persistir TRACEROUTE/ROUTING RX en OFFLINE_LOG ===
            # Objetivo:
            #   - Hacer visible la respuesta real al WebPanel mediante FETCH_BACKLOG.
            #   - No leer docker logs.
            #   - No bloquear el broker.
            #   - No alterar BBS/APRS/MeshCore/bridge.
            try:
                port = (decoded.get("portnum") or pkt.get("portnum") or "").upper()
                is_tr = port in ("TRACEROUTE_APP", "ROUTING_APP", "ADMIN_APP:TRACEROUTE", "ADMIN_TRACEROUTE")

                if is_tr:
                    _ts = pkt.get("rxTime") or pkt.get("timestamp") or time.time()
                    pending_ctx = _traceroute_match_pending(pkt, decoded)

                    rec = {
                        "ts": int(_ts),
                        "rx_time": int(_ts),
                        "channel": canal,
                        "portnum": port,
                        "from": pkt.get("fromId") or pkt.get("from") or pkt.get("from_id"),
                        "to": pkt.get("toId") or pkt.get("to") or pkt.get("to_id"),
                        "from_alias": from_alias or None,
                        "to_alias": to_alias or None,
                        "relay_node": pkt.get("relay_node") or decoded.get("viaNode"),
                        "rx_rssi": rssi,
                        "rx_snr": snr,
                        "event_type": "traceroute_result",
                        "trace_event": "traceroute_result",
                        "pending_ctx": pending_ctx,
                    }

                    try:
                        rec["route_text"] = _traceroute_compact_text(pkt, decoded, rec)
                    except Exception:
                        rec["route_text"] = "traceroute_result"

                                        # Campos que algunas versiones de la API exponen directamente.
                    # Cambio quirúrgico v7.0.3:
                    #   - Se amplía la copia defensiva de campos posibles.
                    #   - No se cambia el envío RF.
                    #   - No se cambia la correlación pendiente.
                    #   - append_offline_log() hará la normalización final.
                    for k in (
                        "hop",
                        "via",
                        "route",
                        "routes",
                        "snrTowards",
                        "routeBack",
                        "routeBackSnr",
                        "route_back_snr",
                        "snrBack",
                        "snr_back",
                        "snr_towards",
                        "routing",
                        "traceroute",
                        "payload",
                        "raw_payload",
                        "payload_hex",
                    ):
                        try:
                            if decoded.get(k) is not None:
                                rec[k] = decoded.get(k)
                            elif pkt.get(k) is not None:
                                rec[k] = pkt.get(k)
                        except Exception:
                            pass

                    # Payload enriquecido preliminar, útil aunque el SDK cambie
                    # el nombre de los campos internos.
                    try:
                        rec["traceroute_payload_preview"] = _traceroute_safe_jsonable(
                            {
                                "decoded": decoded,
                                "packet_keys": sorted(list(pkt.keys())) if isinstance(pkt, dict) else [],
                            },
                            max_depth=4,
                        )
                    except Exception:
                        pass
                    
                    # Reflejar también el contexto pendiente en rec para que el log RX
                    # muestre el target real y no target=None.
                    if isinstance(pending_ctx, dict) and pending_ctx:
                        rec["target_requested"] = pending_ctx.get("target_requested")
                        rec["target_norm"] = pending_ctx.get("target_norm")
                        rec["dest_node_num"] = pending_ctx.get("dest_node_num")
                        rec["trace_hop_limit"] = pending_ctx.get("hop_limit")
                        rec["trace_ch_index"] = pending_ctx.get("ch_index")
                        rec["trace_started_ts"] = pending_ctx.get("started_ts")

                    # Captura cruda diagnóstica v7.0.7b antes de normalizar/persistir.
                    # No altera el flujo existente: si no procede capturar, no hace nada.
                    try:
                        _raw_reason = _traceroute_raw_debug_reason(pkt, decoded, rec)
                        if _raw_reason:
                            _traceroute_append_raw_debug(pkt, decoded, rec, reason=_raw_reason)
                    except Exception as _e_raw_dbg:
                        try:
                            print(
                                f"⚠️ traceroute raw debug skipped: {type(_e_raw_dbg).__name__}: {str(_e_raw_dbg)[:200]}",
                                flush=True,
                            )
                        except Exception:
                            pass

                    append_offline_log(rec)

                    if self.verbose:
                        try:
                            print(
                                f"[traceroute] RX persisted "
                                f"port={portnum} "
                                f"from={rec.get('from')} "
                                f"to={rec.get('to')} "
                                f"target={rec.get('target_norm') or rec.get('target_requested')} "
                                f"event={rec.get('event_type') or rec.get('trace_event')}",
                                flush=True,
                            )
                        except Exception:
                            pass

            except Exception as e_tr:
                logging.warning(f"[traceroute] persist fail: {e_tr}")
            # === [FIN WEBPANEL/TRACEROUTE] ===============================================

        except Exception as e:
            if self.verbose:
                print(f"ERROR en _on_rx: {e}", flush=True)

  
    def _on_connection(self, interface=None, **kwargs):

        # --- IGNORAR conexiones que no sean la interfaz principal del broker (A) ---
        try:
            main_iface = self.iface_mgr.get_iface()
            if interface is not None and interface is not main_iface:
                if self.verbose:
                    print("[conn] (ignorado) established de interfaz no principal (bridge/B).", flush=True)
                return
        except Exception:
            pass
    
        # Leer y cachear RF slot por defecto
        try:
            self._rf_slot_default = _read_local_frequency_slot(interface or self.iface_mgr.get_iface())
        except Exception:
            self._rf_slot_default = None

        # === [NUEVO] Anti-doble-conexión: aceptar solo la PRIMERA y cerrar duplicadas ===
        try:
            import time as _t
            now = _t.time()
            owner = globals().get("_CON_OWNER_ID")
            owner_ts = float(globals().get("_CON_OWNER_TS") or 0.0)

            if owner is None or (now - owner_ts) > float(globals().get("_DUP_CLOSE_GRACE", 3.0)):
                # No hay “dueño” reciente → esta interface pasa a ser la dueña
                globals()["_CON_OWNER_ID"] = id(interface or self.iface_mgr.get_iface())
                globals()["_CON_OWNER_TS"] = now
            else:
                # Hay dueño reciente y esta NO es la misma → cerrar duplicada y salir
                cur_id = id(interface or self.iface_mgr.get_iface())
                if cur_id != owner:
                    try:
                        # Cierra la conexión duplicada sin tocar la “dueña”
                        if interface and hasattr(interface, "close"):
                            interface.close()
                        elif self.iface_mgr and hasattr(self.iface_mgr, "iface") and self.iface_mgr.iface:
                            # si por algún motivo la duplicada se convirtió en self.iface
                            self.iface_mgr.iface.close()
                    except Exception:
                        pass
                    if self.verbose:
                        print(f"[conn] ⚠️ Conexión duplicada detectada (id={cur_id}). Cerrada (owner={owner}).", flush=True)
                    return  # no sigas procesando esta conexión
        except Exception:
            pass

        # Sellar TS y estado SIEMPRE
        try:
            import time as _t
            globals()["_LAST_CONNECT_TS"] = _t.time()
            globals()["_IS_CONNECTED"] = True
        except Exception:
            pass

        # 🔧 Cancelar cualquier timer pendiente y marcar no-conectando (NO dentro de verbose)
        try:
            t = globals().get("_RECONNECT_TIMER")
            if t and hasattr(t, "cancel"):
                t.cancel()
        except Exception:
            pass
        try:
            globals()["_CONNECTING"] = False
        except Exception:
            pass

        if self.verbose:
            print("ℹ️ Broker: conectado al nodo Meshtastic", flush=True)

        # Limpiar flags/guardas al conectar
        try:
            globals().get("TX_BLOCKED") and globals()["TX_BLOCKED"].clear()
            globals().get("MGR_PAUSED") and globals()["MGR_PAUSED"].clear()
            globals().get("COOLDOWN") and globals()["COOLDOWN"].clear()

            if hasattr(self.iface_mgr, "resume") and callable(self.iface_mgr.resume):
                self.iface_mgr.resume()

            if globals().get("COOLDOWN_FORCE_NEXT") is None:
                globals()["COOLDOWN_SECS"] = int(globals().get("BASE_COOLDOWN_SECS", 90))

            print("[cooldown] Limpio tras reconexión exitosa.", flush=True)
                    # Gracia post-conexión: deja pasar TX internos durante X seg
            try:
                globals()["_POST_CONNECT_ALLOW_UNTIL"] = time.time() + float(globals().get("_POST_CONN_ALLOW_SECS", 8.0))
            except Exception:
                pass

        except Exception:
            pass

        self.hub.broadcast_line(_json_dumps({"type":"status","status":"connected","ts":_now_s()}) + "\n")
        # Comprobación simple del nodo B embebido tras recuperar A.
        try:
            iface_a = interface or self.iface_mgr.get_iface()
            if iface_a:
                time.sleep(2.0)
                _check_and_reconnect_embedded_b(iface_a=iface_a, reason="connection.established")
        except Exception as e:
            print(f"[broker] post-connect embedded check ERROR: {type(e).__name__}: {e}", flush=True)

    def _on_disconnect(self, interface=None, **kwargs):
        
        # --- IGNORAR desconexiones que no sean la interfaz principal del broker (A),
        #     PERO cerrarlas para evitar fuga de hilos/timers internos de meshtastic. ---
        try:
            main_iface = self.iface_mgr.get_iface()
            if interface is not None and interface is not main_iface:
                if self.verbose:
                    print("[conn] (ignorado) disconnect de interfaz no principal (bridge/B) -> close() para evitar fuga.", flush=True)

                # Cierre agresivo: evita que queden heartbeatTimer/Timers vivos en esa interfaz “fantasma”
                try:
                    if hasattr(interface, "close"):
                        interface.close()
                except Exception:
                    pass

                return
        except Exception:
            pass

        
        if self.verbose:
            print("ℹ️ Broker: desconectado del nodo Meshtastic", flush=True)
       
       
        self.hub.broadcast_line(_json_dumps({"type": "status", "status": "disconnected", "ts": _now_s()}) + "\n")

        # 1) Estado mínimo consistente: limpiar owner + marcar no conectado
        try:
            globals()["_CON_OWNER_ID"] = None
            globals()["_CON_OWNER_TS"] = 0.0
        except Exception:
            pass
        try:
            globals()["_IS_CONNECTED"] = False
        except Exception:
            pass

        # 2) Guard de PAUSA (si está pausado, no programar cooldown/reconexión)
        try:
            paused = False

            # 2.1) Bandera interna
            try:
                if getattr(self, "_paused", None) and self._paused.is_set():
                    paused = True
            except Exception:
                pass

            # 2.2) Manager de interfaz
            try:
                if hasattr(self.iface_mgr, "is_paused") and callable(self.iface_mgr.is_paused):
                    if self.iface_mgr.is_paused():
                        paused = True
            except Exception:
                pass
            try:
                if getattr(self.iface_mgr, "_paused", None) and self.iface_mgr._paused.is_set():
                    paused = True
            except Exception:
                pass

            # 2.3) Flag global BROKER_PAUSED
            try:
                if bool(globals().get("BROKER_PAUSED")):
                    paused = True
            except Exception:
                pass

            if paused:
                if self.verbose:
                    # Anti-chatter en PAUSA (máx. 1 cada 0.8s)
                    try:
                        import time as _t
                        p_last = float(globals().get("_LAST_PAUSE_DISC_LOG", 0.0))
                        now    = _t.time()
                        if (now - p_last) >= 0.8:
                            print("⛔ Desconexión durante PAUSA: no programo cooldown/reconexión; esperar a BROKER_RESUME.", flush=True)
                            globals()["_LAST_PAUSE_DISC_LOG"] = now
                    except Exception:
                        print("⛔ Desconexión durante PAUSA: no programo cooldown/reconexión; esperar a BROKER_RESUME.", flush=True)

                return
        except Exception:
            # si algo falla aquí, seguimos con la lógica normal
            pass

        # 3) Lógica de cooldown / reconexión
        try:
            import time as _t, threading
            now = _t.time()

            # 3.0) Override manual (FORCE_RECONNECT N)
            secs_override = None
            try:
                with COOLDOWN_FORCE_LOCK:
                    if globals().get("COOLDOWN_FORCE_NEXT") is not None:
                        secs_override = int(globals()["COOLDOWN_FORCE_NEXT"])
                        globals()["COOLDOWN_FORCE_NEXT"] = None
            except Exception:
                secs_override = None

            if secs_override is not None:
                target = max(1, int(secs_override))
                COOLDOWN.enter(target)
                print(f"[cooldown] Activado en _on_disconnect (forzado) → {target}s", flush=True)

                # Sella estado para BROKER_STATUS
                try:
                    cd = globals().get("COOLDOWN") or {}
                    cd["total"] = target
                    if not cd.get("until"):
                        cd["until"] = now + target
                    globals()["COOLDOWN"] = cd
                except Exception:
                    pass

                def _forced_resume():
                    try:
                        # Si siguen en pausa, NO reconectar aún
                        try:
                            if getattr(self, "_paused", None) and self._paused.is_set():
                                if self.verbose:
                                    print("[cooldown] Timer forzado disparado pero broker sigue en PAUSA: no reconecto.", flush=True)
                                return
                        except Exception:
                            pass

                        # Si ya estamos conectados, salir
                        try:
                            mgr = globals().get("BROKER_IFACE_MGR")
                            if mgr and getattr(mgr, "iface", None):
                                return
                        except Exception:
                            pass

                        # Serializar y resetear pool antes de reanudar
                        from tcpinterface_persistent import TCPInterfacePool
                        with globals()["_CONNECT_LOCK"]:
                            if globals().get("_CONNECTING"):
                                return
                            globals()["_CONNECTING"] = True
                            try:
                                try:
                                    TCPInterfacePool.reset(
                                        globals().get("RUNTIME_MESH_HOST") or "",
                                        int(globals().get("RUNTIME_MESH_PORT") or 4403)
                                    )
                                except Exception:
                                    pass

                                mgr = globals().get("BROKER_IFACE_MGR")
                                if mgr and hasattr(mgr, "resume"):
                                    mgr.resume()

                                try:
                                    globals().get("COOLDOWN") and globals()["COOLDOWN"].clear()
                                except Exception:
                                    pass
                                try:
                                    globals().get("TX_BLOCKED") and globals()["TX_BLOCKED"].clear()
                                except Exception:
                                    pass

                                print("[cooldown] Finalizado (forzado): limpiado y reanudado", flush=True)
                            finally:
                                globals()["_CONNECTING"] = False
                    except Exception as e:
                        print(f"[cooldown] Forzado: error al reanudar: {type(e).__name__}: {e}", flush=True)

                # Cancelar timer previo y programar el nuevo
                try:
                    prev = globals().get("_RECONNECT_TIMER")
                    if prev and hasattr(prev, "cancel"):
                        prev.cancel()
                except Exception:
                    pass

                t = threading.Timer(float(target), _forced_resume)
                t.daemon = True
                globals()["_RECONNECT_TIMER"] = t
                t.start()

                if self.verbose:
                    print(f"🔕 Pausa de {target}s antes de reconectar (cooldown solicitado).", flush=True)
                return  # salimos: no seguimos con el flujo por defecto

            # 3.1) Lógica por defecto: decidir un único cooldown 'target'
            base = int(globals().get("COOLDOWN_SECS", int(globals().get("BASE_COOLDOWN_SECS", 90))))

            # Ventana de gracia por /reconectar
            sup_until = float(globals().get("_SUPPRESS_EARLY_ESC_UNTIL") or 0.0)
            sup_rem   = int(globals().get("_SUPPRESS_EARLY_ESC_REMAIN") or 0)

            # ¿Caída temprana?
            last_ts   = float(globals().get("_LAST_CONNECT_TS") or 0.0)
            early_win = float(globals().get("_EARLY_DROP_WINDOW") or 5.0)
            dt        = (now - last_ts) if last_ts > 0.0 else None
            is_early  = (dt is not None) and (dt < early_win)

            suppress_escalation = (now < sup_until) or (sup_rem > 0)

            if is_early and not suppress_escalation:
                esc_target = int(globals().get("_EARLY_ESC_TARGET", 180)) or 180
                target = max(base, esc_target)
                print(f"[cooldown] Caída temprana (<{early_win:.0f}s): subo cooldown a {target}s", flush=True)
            else:
                target = (3 if is_early else base)
                if is_early:
                    print("[cooldown] Escalado SUPRIMIDO por ventana de gracia: aplico cooldown corto de 3s.", flush=True)
                    if sup_rem > 0:
                        globals()["_SUPPRESS_EARLY_ESC_REMAIN"] = sup_rem - 1
                else:
                    print(f"[cooldown] Activado en _on_disconnect → {target}s", flush=True)

            # 3.2) Aplicar cooldown
            COOLDOWN.enter(target)

            # Sella estado para BROKER_STATUS
            try:
                cd = globals().get("COOLDOWN") or {}
                cd["total"] = int(target)
                if not cd.get("until"):
                    cd["until"] = now + int(target)
                globals()["COOLDOWN"] = cd
            except Exception:
                pass

            # 3.3) Activar barrera de TX y pausar la interfaz actual
            try:
                TX_BLOCKED.set()
            except Exception:
                pass

            if hasattr(self.iface_mgr, "pause") and callable(self.iface_mgr.pause):
                self.iface_mgr.pause()
            else:
                self.iface_mgr.signal_disconnect()


            # 3.4) Programar reanudación al cumplirse 'target'
            def _delayed_resume():
                try:
                    # Si siguen en pausa, NO reconectar aún
                    try:
                        if getattr(self, "_paused", None) and self._paused.is_set():
                            if self.verbose:
                                print("[cooldown] Timer disparado pero broker sigue en PAUSA: no reconecto.", flush=True)
                            return
                    except Exception:
                        pass

                    # Si ya estamos conectados, salir
                    try:
                        mgr = globals().get("BROKER_IFACE_MGR")
                        if mgr and getattr(mgr, "iface", None):
                            return
                    except Exception:
                        pass

                    print("[cooldown] Finalizado (Timer alcanzado): reanudando reconexión…", flush=True)

                    if hasattr(self.iface_mgr, "resume") and callable(self.iface_mgr.resume):
                        if self.verbose:
                            print(f"⏳ Pasaron {target}s: reanudando y solicitando reconexión…", flush=True)

                        # Serializar reintentos y matar sesiones zombies
                        from tcpinterface_persistent import TCPInterfacePool
                        with globals()["_CONNECT_LOCK"]:
                            if globals().get("_CONNECTING"):
                                return
                            globals()["_CONNECTING"] = True
                            try:
                                try:
                                    TCPInterfacePool.reset(
                                        globals().get("RUNTIME_MESH_HOST") or "",
                                        int(globals().get("RUNTIME_MESH_PORT") or 4403)
                                    )
                                except Exception:
                                    pass

                                self.iface_mgr.resume()
                                try:
                                    globals().get("COOLDOWN") and globals()["COOLDOWN"].clear()
                                    globals().get("TX_BLOCKED") and globals()["TX_BLOCKED"].clear()
                                except Exception:
                                    pass
                                print("[cooldown] Finalizado (resume): limpiado y reanudado", flush=True)
                            finally:
                                globals()["_CONNECTING"] = False
                    else:
                        self.iface_mgr.signal_disconnect()

                except Exception:
                    try:
                        self.iface_mgr.signal_disconnect()
                    except Exception:
                        pass

            # Cancelar timer previo (si lo hubiera) y programar el nuevo
            try:
                prev = globals().get("_RECONNECT_TIMER")
                if prev and hasattr(prev, "cancel"):
                    prev.cancel()
            except Exception:
                pass

            t = threading.Timer(float(target), _delayed_resume)
            t.daemon = True
            globals()["_RECONNECT_TIMER"] = t
            t.start()

            if self.verbose:
                print(f"🔕 Pausa de {target}s antes de reconectar (cooldown solicitado).", flush=True)

        except Exception:
            # Fallback robusto
            self.iface_mgr.signal_disconnect()


    def _alias_cache_get(self, node_id: str) -> str | None:
        import time
        try:
            if not node_id:
                return None
            node_id = _norm_id(node_id)
            rec = self._alias_cache.get(node_id)
            if not rec:
                return None
            alias, ts = rec
            if int(time.time()) - int(ts) > int(self._alias_cache_ttl):
                self._alias_cache.pop(node_id, None)
                return None
            return alias
        except Exception:
            return None

    def _alias_cache_put(self, node_id: str, alias: str) -> None:
        import time
        try:
            if node_id and alias:
                self._alias_cache[_norm_id(node_id)] = (str(alias), int(time.time()))
        except Exception:
            pass

    def _alias_from_iface(self, iface, node_id: str) -> str | None:
        """
        Busca el alias en la tabla de nodos expuesta por la interfaz (API),
        sin abrir sockets nuevos.
        """
        try:
            nid = _norm_id(node_id)
            raw = getattr(iface, "nodes", None)
            if raw and isinstance(raw, dict):
                it = raw.values()
            elif isinstance(raw, list):
                it = raw
            else:
                getnodes = getattr(iface, "getNodes", None)
                it = getnodes() if callable(getnodes) else []
            for n in (it or []):
                u = n.get("user") or {}
                uid = _norm_id(u.get("id") or n.get("id") or n.get("num") or n.get("nodeId") or "")
                if uid == nid:
                    return u.get("longName") or u.get("shortName") or n.get("name")
        except Exception:
            return None
        return None


# ===================== Servidor TCP JSONL =====================

class JsonLineServer(threading.Thread):
    daemon = True
    def __init__(self, bind: str, port: int, hub: JsonLineHub, verbose: bool = False):
        super().__init__(name="jsonl-server")
        self.bind = bind
        self.port = port
        self.hub = hub
        self.verbose = verbose
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        sel = selectors.DefaultSelector()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(50)
        srv.setblocking(False)
        sel.register(srv, selectors.EVENT_READ)

        if self.verbose:
            print(f"🔌 Broker JSONL escuchando en ({self.bind!r}, {self.port})", flush=True)

        while not self._stop.is_set():
            for key, _ in sel.select(timeout=0.5):
                if key.fileobj is srv:
                    try:
                        client, addr = srv.accept()
                        if self.verbose:
                            print(f"👤 Cliente conectado desde {addr}", flush=True)
                        self.hub.add_client(client)
                    except Exception:
                        pass

        try: sel.unregister(srv)
        except Exception: pass
        try: srv.close()
        except Exception: pass

# ===================== Heartbeats =====================

class HeartbeatThread(threading.Thread):
    daemon = True
    def __init__(self, hub: JsonLineHub, stats: BrokerStats, every_s: int, target_host: str, verbose: bool = False):
        super().__init__(name="heartbeat")
        self.hub = hub
        self.stats = stats
        self.every_s = max(1, int(every_s))
        self.target_host = target_host
        self.verbose = verbose
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            time.sleep(self.every_s)
            hb = {
                "type": "heartbeat",
                "ts": _now_s(),
                "target": self.target_host,
                "stats": self.stats.as_dict(),
            }
            #if self.verbose:
            #    print(f"💓 Heartbeat {self.target_host} {hb['stats']}", flush=True)
            self.hub.broadcast_line(_json_dumps(hb) + "\n")

# === [NUEVO] Soporte de persistencia de posiciones ===
import os, json, time

def _safe_get(d, *keys, default=None):
    cur = d or {}
    for k in keys:
        if cur is None: return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _extract_float(v):
    try:
        return float(v)
    except Exception:
        return None

def _scale_if_int_micro(x):
    # Meshtastic a veces manda latitudeI/longitudeI escalados 1e-7
    try:
        if isinstance(x, int):
            return x / 1e7
        return float(x)
    except Exception:
        return None

def _ensure_pos_store(self):
    """
    Inicializa estructuras perezosas para posiciones, sin romper constructor.
    Crea ./bot_data si no existe.
    """
    if not hasattr(self, "_pos_last"):
        self._pos_last = {}  # !id -> dict con última posición

    # data_dir: intenta reutilizar el ya usado por tu broker
    base = getattr(self, "data_dir", None) or "./bot_data"
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = "."

    self._pos_jsonl_path = os.path.join(base, "positions.jsonl")
    self._pos_summary_path = os.path.join(base, "positions_last.json")

def _append_jsonl(path, obj):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass  # no rompemos el flujo del broker

def _dump_summary(path, mapping):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass

def _extract_position_from_packet(pkt: dict) -> dict | None:
    """
    Devuelve un dict normalizado con lat, lon, alt, time, sats, vel, etc. o None si no hay posición válida.
    Acepta varias variantes (latitude/longitude, latitudeI/longitudeI).
    """
    decoded = _safe_get(pkt, "decoded", default={}) or {}
    portnum = decoded.get("portnum")
    if portnum != "POSITION_APP":
        return None

    pos = decoded.get("position") or {}
    # Variantes comunes
    lat = pos.get("latitude")
    lon = pos.get("longitude")
    alt = pos.get("altitude")
    # Integer escalado
    if lat is None:
        lat = _scale_if_int_micro(pos.get("latitudeI"))
    else:
        lat = _extract_float(lat)
    if lon is None:
        lon = _scale_if_int_micro(pos.get("longitudeI"))
    else:
        lon = _extract_float(lon)
    if alt is not None:
        alt = _extract_float(alt)

    if lat is None or lon is None:
        # Como último recurso, algunos firmwares meten lat/lon en 'payload' ya decodificado; aquí no lo forzamos.
        return None

    # Extras útiles si existen
    sats = pos.get("satsInView") or pos.get("sats") or None
    vel  = pos.get("velocity") or pos.get("vel") or None
    hdop = pos.get("hdop") or None
    ts   = pos.get("time") or None
    # Métricas de RF junto al paquete
    snr  = _safe_get(pkt, "rxSnr")
    rssi = _safe_get(pkt, "rxRssi")

    # Identidades
    from_id   = pkt.get("fromId") or pkt.get("from") or None
    to_id     = pkt.get("toId") or None
    from_name = _safe_get(pkt, "from", "longName") or _safe_get(pkt, "from", "user", "longName") or None
    # Canal lógico si lo habéis inferido ya en otro lado
    ch = _safe_get(pkt, "channel") or _safe_get(pkt, "decoded", "channel") or None

    # Marca de tiempo local si no viene
    now = int(time.time())
    tpos = int(ts) if (isinstance(ts, (int,float)) and ts > 0) else now

    return {
        "fromId": from_id,
        "fromName": from_name,
        "toId": to_id,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "sats": sats,
        "vel": vel,
        "hdop": hdop,
        "snr": snr,
        "rssi": rssi,
        "chan": ch,
        "t_pos": tpos,    # timestamp de la posición (si venía) o now
        "t_rx":  now,     # timestamp de recepción en el broker
    }

def _norm_id(x):
    if not x:
        return None
    # Asegura formato !ID
    s = str(x)
    return s if s.startswith("!") else f"!{s}"

def _position_record_key(rec: dict) -> str | None:
    return _norm_id(rec.get("fromId"))

def _index_position(self, rec: dict):
    """
    Actualiza el índice en memoria y los ficheros (jsonl e índice quick).
    """
    _ensure_pos_store(self)
    k = _position_record_key(rec)
    if not k:
        return

    # Última posición por !id
    self._pos_last[k] = rec

    # Historiza
    out = {
        "type": "POSITION",
        "key": k,
        "data": rec,
    }
    _append_jsonl(self._pos_jsonl_path, out)

    # Resumen completo (!id -> último rec)
    try:
        # Guardamos un resumen compacto
        summary = {kk: self._pos_last[kk] for kk in self._pos_last.keys()}
        _dump_summary(self._pos_summary_path, summary)
    except Exception:
        pass

def _handle_position_packet(self, pkt: dict):
    """
    Punto de entrada público (desde _on_rx) para una trama POSITION_APP.
    """
    rec = _extract_position_from_packet(pkt)
    if not rec:
        return
    _index_position(self, rec)

    # (Opcional) Si tu broker emite líneas “humanas” en consola/broadcast_line, puedes añadir algo compacto:
    try:
        alias = rec.get("fromName") or rec.get("fromId")
        lat = rec.get("lat"); lon = rec.get("lon")
        alt = rec.get("alt")
        snr = rec.get("snr"); rssi = rec.get("rssi")
        ch  = rec.get("chan")
        line = f"📍 POSITION {alias} → {lat:.5f},{lon:.5f}" + (f" alt {alt}m" if alt is not None else "")
        if snr is not None:  line += f" | SNR {snr} dB"
        if rssi is not None: line += f" | RSSI {rssi} dBm"
        if ch is not None:   line += f" | ch {ch}"
        # Si tienes un método self.broadcast_line(...) úsalo; si no, print condicionado por verbose
        bl = getattr(self, "broadcast_line", None)
        if callable(bl):
            bl(line)
        elif getattr(self, "verbose", False):
            print(line, flush=True)
    except Exception:
        pass


# === [NUEVO] Helpers globales del broker para pausa/reanuda ===
def pause_broker() -> bool:
    mgr = globals().get("BROKER_IFACE_MGR")
    if not mgr:
        return False
    try:
        mgr.pause()
        return True
    except Exception:
        return False

def resume_broker() -> bool:
    mgr = globals().get("BROKER_IFACE_MGR")
    if not mgr:
        return False
    try:
        mgr.resume()
        return True
    except Exception:
        return False

def is_broker_paused() -> bool:
    mgr = globals().get("BROKER_IFACE_MGR")
    return bool(mgr and mgr.is_paused())

# ===================== main() =====================

def main():
    _load_dotenv_runtime()
    _apply_radio_profile_runtime(verbose=_env_truthy("RADIO_PROFILE_VERBOSE", "0"))
    ap = argparse.ArgumentParser(description="Broker JSONL para Meshtastic (v3.3, salida limpia + inferencias)")
    ap.add_argument("--host", default=(os.getenv("MESHTASTIC_HOST") or os.getenv("MESH_NODE_HOST") or ""), help="IP o hostname del nodo Meshtastic (TCPInterface)")
    ap.add_argument("--bind", default="127.0.0.1", help="IP local para escuchar clientes JSONL")
    ap.add_argument("--port", type=int, default=8765, help="Puerto local para escuchar clientes JSONL")
    ap.add_argument("--heartbeat", type=int, default=15, help="Segundos entre heartbeats JSONL")
    ap.add_argument("--verbose", action="store_true", help="Salida humana fundamental por consola")
    ap.add_argument("--debug-packets", action="store_true", help="Mostrar estructura/decodificación detallada de paquetes")
    ap.add_argument("--text-only", action="store_true", help="En modo verboso, imprime solo textos (oculta resúmenes no TEXT_MESSAGE_APP)")
    # --- NUEVO: activar la visualización de heartbeats (por defecto se ocultan)
    ap.add_argument(
        "--show-heartbeats",
        dest="show_heartbeats",
        action="store_true",
        help="Muestra también los heartbeats en logs y RX (por defecto se ocultan)."
    )

    ap.add_argument("--no-heartbeat", action="store_true", help="Desactiva los envíos de heartbeat del SDK (no afecta a la recepción)."
)

    # === [NUEVO] opciones de logging de posiciones ===
    ap.add_argument("--positions-log",
                    default="positions.jsonl",
                    help="Ruta del JSONL donde se guardan posiciones POSITION_APP")
    ap.add_argument("--positions-keep-days",
                    type=int,
                    default=7,
                    help="Días a conservar al compactar/rotar el log de posiciones (0 = sin compactar)")

    # === [NUEVO v7.0.14A] configuración externa del Bridge por JSON =========
    ap.add_argument("--bridge-config",
                    default=None,
                    help="Ruta opcional de bridge_config.json. Si no existe, se usa .env sin romper el arranque.")

    # BooleanOptionalAction para banderas on/off (si lo soporta la versión de Python)
    try:
        bool_action = argparse.BooleanOptionalAction
    except Exception:
        bool_action = None

    if bool_action:
        ap.add_argument("--assume-primary", dest="assume_primary", action=bool_action,
                        default=True, help="Si falta canal lógico, asumir Canal 0 para puertos de sistema.")
        ap.add_argument("--assume-rfslot", dest="assume_rfslot", action=bool_action,
                        default=True, help="Si falta RFch, usar Frequency Slot local.")
        ap.add_argument("--reconnect", dest="reconnect", action=bool_action,
                        default=True, help="Autoreconectar al nodo si se pierde la TCP.")
    else:
        # Fallback simple: solo bandera positiva (True si se pasa)
        ap.add_argument("--assume-primary", dest="assume_primary", action="store_true", default=True)
        ap.add_argument("--assume-rfslot", dest="assume_rfslot", action="store_true", default=True)
        ap.add_argument("--reconnect", dest="reconnect", action="store_true", default=True)

    args = ap.parse_args()
    meshcore_only = _is_meshcore_only_profile()

    # La validación final del host se realiza después de aplicar bridge_config.json.
    # El perfil invertido puede definir el nodo Meshtastic B exclusivamente en el
    # JSON; validar aquí impediría arrancar antes de que ese overlay fuese leído.

    # Reaplica tras parsear por si el proceso recibió variables desde .env o CLI indirecta.
    globals()["RADIO_PROFILE_RUNTIME"] = _apply_radio_profile_runtime(verbose=bool(getattr(args, "verbose", False)))
    meshcore_only = _is_meshcore_only_profile()

    # === [NUEVO v7.0.14A] aplicar bridge_config.json como overlay seguro =====
    # Debe ejecutarse muy pronto para que BRIDGE_ENABLED, MESHCORE_ENABLE, mapas,
    # límites, tags y bloqueos BBS queden resueltos antes de arrancar bridge,
    # MeshCore, backlog, tareas o hooks de conexión. Si no hay JSON válido, no
    # modifica nada y se conserva el comportamiento histórico basado en .env.
    globals()["BRIDGE_CONFIG_RUNTIME"] = _apply_bridge_config_runtime_once(
        getattr(args, "bridge_config", None),
        verbose=bool(getattr(args, "verbose", False)),
    )
    # RADIO_PROFILE se reaplica siempre después del overlay. Así sus capacidades
    # y flags mínimos permanecen autoritativos incluso cuando el JSON contiene
    # variables compatibles del mismo perfil.
    globals()["RADIO_PROFILE_RUNTIME"] = _apply_radio_profile_runtime(
        verbose=bool(getattr(args, "verbose", False))
    )
    meshcore_only = _is_meshcore_only_profile()

    # En el perfil invertido, nodes.B es el Meshtastic embebido que controla el
    # broker. Aplicamos su host resuelto al argumento runtime para que el JSON
    # sea realmente autoritativo sin afectar a los perfiles históricos.
    _bridge_runtime = globals().get("BRIDGE_CONFIG_RUNTIME") or {}
    if (
        _bridge_runtime.get("applied")
        and _bridge_runtime.get("profile") == "meshcore_a_meshtastic_embedded_b"
    ):
        _mesh_b_host = (os.getenv("B_HOST") or os.getenv("MESHTASTIC_HOST") or "").strip()
        if _mesh_b_host:
            args.host = _mesh_b_host
            if bool(getattr(args, "verbose", False)):
                print(f"[bridge-config] nodo B Meshtastic aplicado a --host={args.host}", flush=True)

    # Validación final, una vez resueltos .env, RADIO_PROFILE y bridge_config.json.
    if not args.host and not meshcore_only:
        ap.error(
            "--host/MESHTASTIC_HOST es obligatorio para el perfil activo; "
            "también puede definirse nodes.B.host en bridge_config.json para "
            "meshcore_a_meshtastic_embedded_b"
        )

    # === [NUEVO] Modo sin heartbeat si el usuario lo pide
    if args.no_heartbeat:
        ok = install_no_heartbeat_mode(verbose=args.verbose)
        msg = "activado" if ok else "no disponible"
        globals()["NO_HEARTBEAT_MODE"] = True
        print(f"🔕 Modo sin heartbeat {msg}.", flush=True)


   
    # === [NUEVO] aplicar preferencia de heartbeats en logs y RX
    global SHOW_HEARTBEATS
    SHOW_HEARTBEATS = bool(getattr(args, "show_heartbeats", False))
    install_heartbeat_log_filter()

    if meshcore_only:
        print("[radio-profile] meshcore_only: no se inicializan guards ni pool TCP Meshtastic", flush=True)
    else:
        # === [NUEVO] blindaje contra 10053/10054 en hilos internos del SDK
        install_meshtastic_send_guards(verbose=args.verbose)

      # === [NUEVO] Aviso de guards activos (y asegurar parche del pool persistente)
        try:
            import tcpinterface_persistent  # asegura guards del pool/reconexión
            print("🛡️ Guards anti-heartbeat activos (sendHeartbeat protegido).", flush=True)
        except Exception as e:
            print(f"⚠️ No se pudo activar guards anti-heartbeat: {e}", flush=True)

        # === [NUEVO] Reenlazar el alias local TCPInterface al wrapper del pool ===
        try:
            import meshtastic.tcp_interface as _tcp_mod
            TCPInterface = getattr(_tcp_mod, "TCPInterface")
            if args.verbose:
                print("ℹ️ Broker: TCPInterface enlazado al pool persistente.", flush=True)
        except Exception as e:
            if args.verbose:
                print(f"⚠️ No se pudo enlazar TCPInterface del broker al pool: {e}", flush=True)


    # === MODIFICADO: fijar host/port runtime para las tareas
    globals()["RUNTIME_MESH_HOST"] = args.host
    globals()["RUNTIME_MESH_PORT"] = 4403  # cambia si usas otro puerto TCP para Meshtastic

    # === [NUEVO] globals para posiciones ===
    globals()["POSITIONS_LOG_PATH"] = args.positions_log
    globals()["POSITIONS_KEEP_DAYS"] = int(args.positions_keep_days or 0)


    hub = JsonLineHub()
    globals()["BROKER_HUB"] = hub  # MeshCore->BOT: acceso global al hub

    stats = BrokerStats()

    srv = JsonLineServer(args.bind, args.port, hub, verbose=args.verbose)
    srv.start()

    # ===================== NUEVO: arrancar persistencia + servidor backlog =====================
    # ===================== NUEVO: arrancar persistencia + servidor backlog =====================
    ctrl_bind = os.getenv("BROKER_CTRL_BIND", "127.0.0.1")
    start_backlog_server(bind_host=ctrl_bind, port=BACKLOG_PORT)

    start_backlog_worker()  # ← NUEVO: comienza a vaciar SENDQ

    # ==========================================================================================

    # === NUEVO: iniciar scheduler de tareas ===
    init_broker_tasks()

    if meshcore_only:
        print(f"🟢 Broker v7.0.30 listo en RADIO_PROFILE=meshcore_only; sirviendo en {args.bind}:{args.port}", flush=True)
    else:
        print(f"🟢 Broker v7.0.30 listo. Conectando a nodo {args.host} y sirviendo en {args.bind}:{args.port}", flush=True)
    print("   Clientes pueden conectarse por TCP y leer líneas JSONL (una por evento).", flush=True)

    # === [NUEVO] Inicializar motor BBS (broker-side) ======================================
    try:
        enabled_raw = (os.getenv("BBS_ENABLED") or os.getenv("BBS_ENABLE") or "0").strip().lower()
        enabled = enabled_raw in {"1", "true", "on", "si", "sí", "y", "yes"}

        if enabled and (BbsServer is not None):
            bbs_callsign = os.getenv("BBS_CALLSIGN", "EB2EAS-5").strip() or "EB2EAS-5"
            bbs_channel = _safe_first_int(os.getenv("BBS_CHANNELS") or os.getenv("BBS_CHANNEL", "5"), default=5)
            bbs_max_tx = int(os.getenv("BBS_MAX_TX", "234"))
            
            # Base de datos del contenedor (misma idea que el bot)
            BROKER_DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "/app/bot_data")).resolve()
            BROKER_DATA_DIR.mkdir(parents=True, exist_ok=True)

            bbs_db = (os.getenv("BBS_DB_PATH", "bbs/bbs_data.db").strip() or "bbs/bbs_data.db")
            bbs_key = (os.getenv("BBS_KEY_PATH", "bbs/.bbs_key").strip() or "bbs/.bbs_key")

            # Si vienen como carpeta, normaliza a archivo
            if bbs_db.endswith(os.sep) or (os.path.splitext(bbs_db)[1] == ""):
                bbs_db = os.path.join(bbs_db, "bbs_data.db")
            if bbs_key.endswith(os.sep) or (os.path.splitext(bbs_key)[1] == ""):
                bbs_key = os.path.join(bbs_key, ".bbs_key")

            # Resolver rutas relativas contra /app/bot_data
            bbs_db_p = Path(bbs_db)
            bbs_key_p = Path(bbs_key)
            if not bbs_db_p.is_absolute():
                bbs_db_p = (BROKER_DATA_DIR / bbs_db_p).resolve()
            if not bbs_key_p.is_absolute():
                bbs_key_p = (BROKER_DATA_DIR / bbs_key_p).resolve()

            bbs_db_p.parent.mkdir(parents=True, exist_ok=True)
            bbs_key_p.parent.mkdir(parents=True, exist_ok=True)

            bbs_db = str(bbs_db_p)
            bbs_key = str(bbs_key_p)


            globals()["BBS_ENGINE"] = BbsServer(
                send_func=lambda dest, ch, txt: None,  # broker: no usa send_func
                enabled=True,
                bbs_callsign=bbs_callsign,
                bbs_channel=bbs_channel,
                db_path=bbs_db,
                key_path=bbs_key,
                max_tx=bbs_max_tx,

                # Techos por comando (tus env)
                list_limit=int(os.getenv("BBS_LIST_LIMIT", "6")),
                all_list_limit=int(os.getenv("BBS_ALL_LIST_LIMIT", "10")),
                read_list_limit=int(os.getenv("BBS_READ_LIST_LIMIT", "10")),
                search_limit=int(os.getenv("BBS_SEARCH_LIMIT", "10")),
                inbox_limit=int(os.getenv("BBS_INBOX_LIMIT", "6")),
                poll_list_limit=int(os.getenv("BBS_POLL_LIST_LIMIT", "3")),
            )

            #if args.verbose:
            print(f"[BBS] ✅ Inicializado callsign={bbs_callsign} ch={bbs_channel} db={bbs_db}", flush=True)

        else:
            globals()["BBS_ENGINE"] = None
            #if args.verbose:
            if not enabled:
                print(f"[BBS] Desactivado (BBS_ENABLED/BBS_ENABLE='{enabled_raw}').", flush=True)
            else:
                print(f"[BBS] Desactivado: módulo BBS no disponible (import bbs_server falló: {_BBS_IMPORT_ERROR}).", flush=True)

    except Exception as e:
        globals()["BBS_ENGINE"] = None
        print(f"[BBS] ⚠️ No se pudo iniciar: {type(e).__name__}: {e}", flush=True)
    # ======================================================================================


    # Gestor de conexión Meshtastic (autoreconexión). En meshcore_only no se
    # crea TCPInterface, no hay receptor Meshtastic y no se suscriben hooks del SDK.
    iface_mgr = None
    receiver = None
    if meshcore_only:
        globals()["BROKER_IFACE_MGR"] = None
        print("[radio-profile] meshcore_only: interfaz Meshtastic completamente desactivada", flush=True)
    else:
        iface_mgr = InterfaceManager(host=args.host, verbose=args.verbose, enable_reconnect=bool(getattr(args, "reconnect", True)))
        iface_mgr.start()

        # === NUEVO: exponer el gestor al resto del módulo (tareas, etc.)
        globals()["BROKER_IFACE_MGR"] = iface_mgr

        # Receptor y suscripciones pubsub
        receiver = MeshReceiver(
            hub, stats, verbose=args.verbose,
            assume_primary=bool(getattr(args, "assume_primary", True)),
            assume_rfslot=bool(getattr(args, "assume_rfslot", True)),
            iface_mgr=iface_mgr,
            debug_packets=bool(getattr(args, "debug_packets", False)),
            text_only=bool(getattr(args, "text_only", False)),
        )
        receiver.assume_user_primary = True

        pub.subscribe(receiver._on_rx, "meshtastic.receive")
        pub.subscribe(receiver._on_connection, "meshtastic.connection.established")
        pub.subscribe(receiver._on_disconnect, "meshtastic.connection.lost")

    # === [NUEVO] Arranque condicional de la pasarela embebida al establecer conexión ===

    def _start_or_verify_embedded_on_connection(interface=None, **kwargs):
        """
        Arranca los servicios embebidos cuando aún no existen y, en reconexiones
        posteriores, verifica que siguen sanos.

        Reglas (mutua exclusión):
          - BRIDGE_ENABLED=1  -> pasarela Meshtastic embebida
          - MESHCORE_ENABLE=1 -> pasarela MeshCore embebida
          - Si ambas están activas, BRIDGE_ENABLED tiene prioridad y MeshCore se deshabilita

        Comportamiento:
          - Primera conexión: arranca el backend embebido configurado
          - Reconexiones: no desuscribe; vuelve a comprobar/rearmar si hace falta
        """
        try:
            import os
            bridge_enabled = _env_truthy("BRIDGE_ENABLED", "0")
            meshcore_enabled = _env_truthy("MESHCORE_ENABLE", "0")

            if bridge_enabled and meshcore_enabled:
                print("[bridge] ⚠️ BRIDGE_ENABLED=1 y MESHCORE_ENABLE=1: se prioriza BRIDGE_ENABLED (MeshCore OFF).", flush=True)
                meshcore_enabled = False

            # ---- MeshCore embebido ----
            # v7.0.14: la creación/arranque real se delega en el helper autónomo.
            # Así este hook queda como verificación en reconexión de A, pero B ya puede
            # haber arrancado antes aunque A no haya conectado nunca.
            if meshcore_enabled:
                _start_meshcore_embedded_autonomous("connection.established")

            # ---- Bridge Meshtastic embebido ----
            if bridge_enabled:
                try:
                    iface_for_bridge = interface or iface_mgr.get_iface()
                    if not iface_for_bridge:
                        print("[bridge] ⚠️ sin interface todavía; espero al próximo established…", flush=True)
                        return

                    st = bridge_status_in_broker()
                    running = bool((st or {}).get("running"))
                    iface_b_ok = bool((st or {}).get("iface_b"))

                    if not (running and iface_b_ok):
                        st = bridge_start_in_broker(iface_for_bridge)
                        print("[bridge] embebida habilitada:", st, flush=True)
                    else:
                        print("[bridge] embebida ya operativa", flush=True)

                except Exception as e:
                    print(f"[bridge] ⚠️ no se pudo iniciar/verificar la pasarela embebida: {type(e).__name__}: {e}", flush=True)
            else:
                if not meshcore_enabled:
                    print("[bridge] embebida desactivada (BRIDGE_ENABLED=0, MESHCORE_ENABLE=0)", flush=True)

        except Exception as e:
            print(f"[bridge] hook embedded ERROR: {type(e).__name__}: {e}", flush=True)

    # Suscribir el hook al evento de conexión establecida solo si existe interfaz Meshtastic.
    if not meshcore_only:
        pub.subscribe(_start_or_verify_embedded_on_connection, "meshtastic.connection.established")

    # === [NUEVO v7.0.14] Arranque autónomo de B MeshCore =====================
    # Punto crítico: si A Meshtastic no establece TCP, el evento
    # "meshtastic.connection.established" no se produce. Por tanto, MeshCore B
    # debe arrancar aquí, de forma independiente, antes de lanzar/reintentar A.
    _start_meshcore_embedded_autonomous("broker.startup.autonomous")

    # Lanzar primera conexión Meshtastic si procede.
    if iface_mgr is not None:
        iface_mgr.signal_disconnect()

    # Heartbeat (estadísticas internas del broker). En meshcore_only se etiqueta
    # como MeshCore para no sugerir conexión a un nodo Meshtastic inexistente.
    hb_target = "meshcore_only" if meshcore_only else args.host
    hb = HeartbeatThread(hub, stats, every_s=args.heartbeat, target_host=hb_target, verbose=args.verbose)
    hb.start()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        hb.stop()
        srv.stop()
        if iface_mgr is not None:
            iface_mgr.stop()
        try:
            bridge_stop_in_broker()
        except Exception:
            pass
        try:
            mc = globals().get("MESHCORE_ENGINE")
            if mc:
                mc.stop()
        except Exception:
            pass


# === NUEVO: CLI para gestionar tareas programadas desde el broker ===
def _cli_tasks(argv: list[str]) -> int:
    """
    Subcomandos:
      schedule --when "YYYY-MM-DD HH:MM" --channel N --dest DEST --msg "texto" [--ack 0|1] [--max-attempts N]
      tasks [--status pending|done|failed|canceled]
      cancel --id TASK_ID
    Ejemplos:
      python Meshtastic_Broker_v3.3.2.py schedule --when "2025-09-02 09:30" --channel 0 --dest broadcast --msg "Buenos días"
      python Meshtastic_Broker_v3.3.2.py tasks --status pending
      python Meshtastic_Broker_v3.3.2.py cancel --id 123e4567-e89b-12d3-a456-426614174000
    """
    import argparse
    p = argparse.ArgumentParser(prog="broker_tasks", add_help=True)
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("schedule")
    ps.add_argument("--when", required=True, help="YYYY-MM-DD HH:MM (hora local Europe/Madrid)")
    ps.add_argument("--channel", type=int, required=True)
    ps.add_argument("--dest", default="broadcast")
    ps.add_argument("--msg", required=True)
    ps.add_argument("--ack", type=int, default=0)
    ps.add_argument("--max-attempts", type=int, default=3)

    pl = sub.add_parser("tasks")
    pl.add_argument("--status", choices=["pending", "done", "failed", "canceled"], default=None)

    pc = sub.add_parser("cancel")
    pc.add_argument("--id", required=True)

    args = p.parse_args(argv)


    # --verbose activa también la visualización de frames RX
    try:
        global SHOW_FRAMES
        SHOW_FRAMES = SHOW_FRAMES or bool(getattr(args, "verbose", False))
    except Exception:
        pass

    # (Opcional) si usas un filtro de heartbeats, alinearlo con verbose:
    try:
        global SHOW_HEARTBEATS
        SHOW_HEARTBEATS = bool(getattr(args, "verbose", False))
    except Exception:
        pass


    # Inicializa solo la persistencia para escribir/leer (no hace falta correr el broker entero)
    #broker_tasks.configure_sender(_tasks_send_adapter)
    
    broker_tasks.configure_sender(lambda ch, msg, dst, ack:
    SENDQ.offer({"channel": ch, "text": msg, "destination": (None if (not dst or dst=='broadcast') else dst), "require_ack": bool(ack), "type":"text"},
                coalesce=True) or True
)    
    broker_tasks.configure_reconnect(_tasks_reconnect_adapter)
  
    DATA_DIR_BROKER = os.getenv("BOT_DATA_DIR", "/app/bot_data")
    os.makedirs(DATA_DIR_BROKER, exist_ok=True)
    broker_tasks.init(data_dir=DATA_DIR_BROKER, tz_name="Europe/Madrid", poll_interval_sec=2.0)

    if args.cmd == "schedule":
        res = broker_tasks.schedule_message(
            when_local=args.when,
            channel=int(args.channel),
            message=args.msg,
            destination=args.dest,
            require_ack=bool(args.ack),
            max_attempts=int(args.max_attempts),
            meta={"source": "broker-cli"},
        )
        print(res)
        return 0 if res.get("ok") else 2

    if args.cmd == "tasks":
        res = broker_tasks.list_tasks(status=args.status)
        print(res)
        return 0

    if args.cmd == "cancel":
        res = broker_tasks.cancel(args.id)
        print(res)
        return 0 if res.get("ok") else 2

    p.print_help()
    return 1

if __name__ == "__main__":
    import sys  # ← NUEVO si no estaba importado arriba
    # Si llaman con subcomandos de tareas, ejecuta CLI y termina
    if len(sys.argv) > 1 and sys.argv[1] in {"schedule", "tasks", "cancel"}:
        sys.exit(_cli_tasks(sys.argv[1:]))

    # Ejecución normal del broker
    main()
