# meshtastic_api_adapter.py
# -*- coding: utf-8 -*-
# Version v6.1

"""
Capa 'API-first' para Meshtastic.

Objetivo
--------
Usar preferentemente la API Python (TCPInterface) para:
  - Listar nodos
  - Traceroute
  - Solicitud de telemetría
  - Envío de texto (con o sin ACK)

Si la API no está disponible (versión antigua de la lib, incompatibilidad, etc.)
se hace fallback a la CLI `meshtastic` con argumentos equivalentes.

Funciones públicas (firmas mantenidas)
--------------------------------------
- api_list_nodes(host: str, max_n: int = 50) -> list[dict]
- api_traceroute(host: str, dest_id: str, timeout: int = 30) -> dict
- api_request_telemetry(host: str, dest_id: str, timeout: int = 30) -> dict
- api_send_text(host: str, dest_id_or_none: str | None, text: str,
                channel: int = 0, want_ack: bool = False, timeout: int = 20) -> dict

Formatos de retorno
-------------------
api_list_nodes -> lista de dicts normalizados:
    { "id": "!abcd1234", "alias": "Zgz", "last_heard_min": int|None, "hops": int|None }

api_traceroute -> dict:
    { "ok": bool, "hops": int, "route": [ "!id", ... ], "raw": str }

api_request_telemetry -> dict:
    { "ok": bool, "raw": str }

api_send_text -> dict:
    { "ok": bool, "packet_id": int|None, "raw": str }

Variables de entorno (opcional)
-------------------------------
- MESHTASTIC_EXE : nombre o ruta del ejecutable CLI (por defecto "meshtastic")
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
from tcpinterface_persistent import TCPInterfacePool

# ===============================================================
# Utilidades genéricas (CLI, normalización)
# ===============================================================

# --- al inicio del fichero, donde declaras DEFAULT_PORT_HOST ---
DEFAULT_PORT_HOST = 4403  # antes era "4403" (str)

# --- Envío vía broker BacklogServer (127.0.0.1:8766) ---
# Envío vía broker BacklogServer (configurable por entorno)
import socket, json, os

# Prioridad:
# 1) BROKER_CTRL_HOST / BROKER_CTRL_PORT
# 2) BROKER_HOST / BACKLOG_PORT
# 3) BROKER_PORT (+1 => backlog)
# 4) 127.0.0.1:8766
BROKER_CTRL_HOST = os.getenv("BROKER_CTRL_HOST",
                     os.getenv("BROKER_HOST", "127.0.0.1"))
import os  # si no está ya importado

BROKER_CTRL_HOST = os.getenv("BROKER_CTRL_HOST", os.getenv("BROKER_HOST", "127.0.0.1")).strip()
try:
    BROKER_CTRL_PORT = int(os.getenv("BROKER_CTRL_PORT", str(int(os.getenv("BROKER_PORT", "8765")) + 1)))
except Exception:
    BROKER_CTRL_PORT = 8766


def _default_backlog_port():
    # Si dan BACKLOG_PORT explícito, úsalo
    val = os.getenv("BACKLOG_PORT")
    if val and val.isdigit():
        return int(val)
    # Si tenemos BROKER_PORT, el backlog suele ser +1
    bport = os.getenv("BROKER_PORT")
    if bport and bport.isdigit():
        return int(bport) + 1
    return 8766






def send_via_broker_iface(text: str, dest_id: str | None, channel_index: int, want_ack: bool = False, timeout: float = 6.0) -> dict:
    """
    Pide al broker que emita el mensaje usando su iface persistente.
    Devuelve: {"ok": bool, "packet_id": int|None, "path": "broker-iface", "error"?: str}
    """
    req = {
        "cmd": "SEND_TEXT",
        "params": {
            "text": text,
            "dest": dest_id,          # None => broadcast
            "ch": int(channel_index),
            "ack": 1 if want_ack else 0
        }
    }
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
    if not line:
        return {"ok": False, "error": "empty broker reply"}
    try:
        res = json.loads(line)
    except Exception as e:
        return {"ok": False, "error": f"bad json: {e}"}
    # Normalizamos salida
    return {
        "ok": bool(res.get("ok")),
        "packet_id": res.get("packet_id"),
        "path": res.get("path") or "broker-iface",
        **({"error": res.get("error")} if res.get("error") else {})
    }



# --- REEMPLAZA la función api_get_neighbors existente por esta ---
def api_get_neighbors(host: str, port: int, timeout: float = 8.0) -> Dict[str, dict]:
    """
    Devuelve un dict { '!id': { 'rssi': float|None, 'snr': float|None,
                                'hops': int|None, 'via': str|None,
                                'lastHeard': int|None } }
    Intentando todas las variantes de API conocidas. Si falla, {}.
    """
    try:
        # Preferimos el pool persistente si está disponible
        try:
            from tcpinterface_persistent import TCPInterfacePool
            iface = TCPInterfacePool.get(host=host, port=port, noReconnect=True)
            _owned = False
        except Exception:
            from meshtastic.tcp_interface import TCPInterface
            iface = TCPInterface(hostname=host, port=port, noReconnect=True)
            _owned = True

        def _norm_id(nid: Any) -> Optional[str]:
            if nid is None:
                return None
            # int -> !xxxxxxxx
            if isinstance(nid, int):
                return f"!{nid:08x}"
            s = str(nid).strip()
            if not s:
                return None
            if s.startswith("!"):
                return s
            # si viene como "a0cb0d34"
            if re.fullmatch(r"[0-9a-fA-F]{8}", s):
                return f"!{s.lower()}"
            return s  # último recurso

        def _parse_neighbor_entry(n: dict) -> Tuple[Optional[str], dict]:
            nid = (
                n.get("nodeId") or n.get("node_id") or n.get("id")
                or n.get("num")   or n.get("nodeNum")
            )
            nid = _norm_id(nid)

            # RSSI / SNR / Hops / Via / lastHeard
            rssi = n.get("rssi")
            if rssi is None:
                rssi = n.get("rssiDb") or n.get("rssiDbm") or n.get("lastRssi")
            snr = n.get("snr")
            if snr is None:
                snr = n.get("snrDb") or n.get("snr_db")
            hops = n.get("hops")
            if hops is None:
                hops = n.get("hopsAway")
            via = n.get("via") or n.get("viaId") or n.get("via_id")
            last = n.get("lastHeard") or n.get("last_seen") or n.get("seen")

            # sanea tipos
            try: rssi = float(rssi) if rssi is not None else None
            except Exception: rssi = None
            try: snr  = float(snr)  if snr  is not None else None
            except Exception: snr  = None
            try: hops = int(hops)   if hops is not None else None
            except Exception: hops = None
            try: last = int(last)   if last is not None else None
            except Exception: last = None

            return nid, {"rssi": rssi, "snr": snr, "hops": hops, "via": str(via) if via else None, "lastHeard": last}

        out: Dict[str, dict] = {}

        # 1) getMyInfo() con distintas estructuras
        try:
            my = iface.getMyInfo() or {}
        except Exception:
            my = {}

        # candidatos de contenedor donde buscar la lista de vecinos
        containers = []
        if isinstance(my, dict):
            containers.append(my)
            mi = my.get("myInfo") or my.get("my_info") or {}
            if isinstance(mi, dict):
                containers.append(mi)
                ni = mi.get("neighborInfo") or mi.get("neighborsInfo") or {}
                if isinstance(ni, dict):
                    containers.append(ni)

        neighbors_list = None
        for c in containers:
            for key in ("neighbors", "neighbours", "neighborList", "neighbourList"):
                v = c.get(key)
                if isinstance(v, list) and v:
                    neighbors_list = v
                    break
            if neighbors_list:
                break

        # 2) Si sigue vacío, intentar métodos específicos de la interfaz
        if not neighbors_list:
            for cand in ("getNeighborInfo", "getNeighboursInfo", "getNeighbors"):
                fn = getattr(iface, cand, None)
                if callable(fn):
                    try:
                        res = fn()
                        if isinstance(res, dict):
                            for k in ("neighbors", "neighbours", "list"):
                                v = res.get(k)
                                if isinstance(v, list) and v:
                                    neighbors_list = v
                                    break
                        elif isinstance(res, list):
                            neighbors_list = res
                    except Exception:
                        pass
                if neighbors_list:
                    break

        if neighbors_list:
            for n in neighbors_list:
                if isinstance(n, dict):
                    nid, data = _parse_neighbor_entry(n)
                    if nid:
                        out[nid] = data

        # cerrar si se creó directamente
        if _owned:
            try:
                iface.close()
            except Exception:
                pass

        return out
    except Exception:
        return {}


def _safe_int(x: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return default


def _format_node_id(nid: Any) -> Optional[str]:
    """
    Normaliza nodeId a formato !xxxxxxxx si viene como int,
    o devuelve la cadena si ya viene como '!abcd1234'.
    """
    if nid is None:
        return None
    if isinstance(nid, int):
        return f"!{nid:08x}"
    s = str(nid).strip()
    if not s:
        return None
    if s.startswith("!"):
        return s
    # algunos objetos podrían traer 'num' (int) y 'id' (string sin '!')
    if re.fullmatch(r"[0-9a-fA-F]{8}", s):
        return f"!{s.lower()}"
    return s


def _node_alias_from_info(info: Dict[str, Any]) -> str:
    """
    Intenta extraer el alias/longName/shortName de diferentes estructuras.
    """
    # Estructura típica: info["user"]{longName, shortName, id, name}
    user = info.get("user") or {}
    for k in ("longName", "shortName", "name", "id"):
        v = user.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Otras variantes (algunos dumps pueden tener claves al tope)
    for k in ("longName", "shortName", "name", "id", "alias"):
        v = info.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # fallback
    nid = info.get("id") or info.get("num")
    nid_fmt = _format_node_id(nid)
    return nid_fmt or ""


def _epoch_secs_to_minutes(ts: Any) -> Optional[int]:
    """
    Convierte epoch seconds a minutos transcurridos (int >= 0).
    Si no es convertible, devuelve None.
    """
    try:
        val = float(ts)
        if val <= 0:
            return None
        return max(0, int((time.time() - val) / 60.0))
    except Exception:
        return None


def _open_iface(host: str):
    """
    Devuelve una instancia persistente de TCPInterface(hostname=host)
    desde el pool (reutilizable y autoreconectable).
    """
    return TCPInterfacePool.get(host)

# === NUEVO: helper de reconexión segura para el pool TCP ===
def _pool_reconnect_safe(host: str, port: int = 4403) -> None:
    """
    Intenta reconectar/renovar la interfaz del TCPInterfacePool para `host`.
    Si el pool no expone 'reconnect', usa 'close' para que el siguiente get() reabra limpio.
    """
    try:
        from tcpinterface_persistent import TCPInterfacePool
    except Exception:
        return

    try:
        if hasattr(TCPInterfacePool, "reconnect") and callable(getattr(TCPInterfacePool, "reconnect")):
            TCPInterfacePool.reconnect(host)
        else:
            if hasattr(TCPInterfacePool, "close") and callable(getattr(TCPInterfacePool, "close")):
                TCPInterfacePool.close(host)
    except Exception:
        # No propagamos: el reintento posterior abrirá una nueva sesión si hace falta
        pass

# === MODIFICADA: envío con reintento tras reconexión del pool ===

# === MODIFICADA: envío con reintento tras reconexión del pool ===
def send_text_simple_with_retry_resilient(host, port, text, dest_id, channel_index, want_ack):
    """
    Estrategia:
      1) Intentar vía broker BacklogServer (una sola conexión real al nodo).
      2) Si broker no responde o devuelve KO, fallback a la conexión efímera 4403 + retry (tu flujo original).
    """
    # 1) Broker primero
    try:
        r = send_via_broker_iface(text, dest_id, channel_index, want_ack=want_ack, timeout=6.0)
        if r.get("ok"):
            return {"ok": True, "packet_id": r.get("packet_id")}
        # Si respondió pero KO, seguimos al fallback conservando el motivo
        broker_err = r.get("error")
    except Exception as _e:
        broker_err = f"{type(_e).__name__}: {_e}"

    # 2) Fallback: tu función existente (no se toca)
    try:
        return send_text_simple_with_retry(host, port, text, dest_id, channel_index, want_ack)
    except Exception as e:
        return {"ok": False, "error": f"fallback error: {type(e).__name__}: {e}; broker_err={broker_err}"}


# ===============================================================
# API: Listado de nodos
# ===============================================================

# === NUEVA api_list_nodes ===

import time
from typing import Any, Dict, List, Optional
from meshtastic import tcp_interface

# ===============================================================
# API: Listado de nodos (robusto hopLimit + minutos + métricas)
# ===============================================================


def api_list_nodes(
    host: str,
    port: int = 4403,
    max_n: int = 50,
    timeout_sec: float = 4.0,
    assume_hops_zero: bool = False
) -> List[Dict[str, Any]]:
    """
    Obtiene nodos vía TCPInterface y devuelve una lista normalizada:
      {
        "id": "!xxxxxxxx",
        "alias": "Nombre/alias",
        "last_heard_min": int|None,     # minutos desde último visto
        "hops": int|None,               # calculado con hopLimit, seguro
        "snr": float|None,
        "position": dict|None,
        "metrics": dict|None            # device/environment/power/localStats si existen
      }

    - Espera hasta timeout_sec a que se pueble iface.nodes.
    - Si hopLimit por nodo falta o no hay hopLimit inicial, no rompe (hops=None).
      Si assume_hops_zero=True y falta hopLimit por nodo, asume 0 (directo).
    - Ordena por más recientes primero (minutos asc), dejando None al final.
    """
    # Inicializar interfaz (con puerto si se pasa)
    iface = tcp_interface.TCPInterface(host, port=port) if port else tcp_interface.TCPInterface(host)
    try:
        # Esperar a que haya nodos o timeout
        t0 = time.time()
        while (time.time() - t0) < float(timeout_sec) and not getattr(iface, "nodes", None):
            time.sleep(0.2)

        # hopLimit por defecto del nodo local (si existe)
        try:
            hlim_default = getattr(getattr(iface.localNode, "localConfig", None), "hopLimit", None)
            hlim_default = int(hlim_default) if isinstance(hlim_default, (int, float)) else None
        except Exception:
            hlim_default = None

        out: List[Dict[str, Any]] = []

        # Iterar nodos devueltos por la API
        nodes = getattr(iface, "nodes", {}) or {}
        for node_id, info in nodes.items():
            # Alias robusto
            user = info.get("user") or {}
            alias = (
                user.get("name")
                or user.get("longName")
                or user.get("shortName")
                or str(node_id)
            )

            # lastHeard -> minutos (int >= 0) o None
            last_ts = info.get("lastHeard")
            last_min: Optional[int] = None
            if isinstance(last_ts, (int, float)) and last_ts > 0:
                try:
                    last_min = max(0, int((time.time() - float(last_ts)) / 60.0))
                except Exception:
                    last_min = None

            # hops: cálculo seguro
            hops: Optional[int] = None
            try:
                remaining = info.get("hopLimit")  # puede faltar o ser None
                if remaining is not None and hlim_default is not None:
                    hops = max(0, int(hlim_default) - int(remaining))
                elif remaining is None and assume_hops_zero:
                    hops = 0
                else:
                    hops = None
            except Exception:
                hops = None

            # Métricas opcionales (filtrar vacías)
            raw_metrics = {
                "device":      info.get("deviceMetrics"),
                "environment": info.get("environmentMetrics"),
                "power":       info.get("powerMetrics"),
                "localStats":  info.get("localStats"),
            }
            metrics = {k: v for k, v in raw_metrics.items() if v is not None} or None

            out.append({
                "id":             node_id,
                "alias":          alias,
                "last_heard_min": last_min,
                "hops":           hops,
                "snr":            info.get("snr"),
                "position":       info.get("position"),
                "metrics":        metrics,
            })

        # Ordenar: más recientes primero (menor minuto), None al final
        out.sort(key=lambda r: (float('inf') if r["last_heard_min"] is None else r["last_heard_min"]))
        return out[:max_n]

    finally:
        try:
            iface.close()
        except Exception:
            pass


# API: Traceroute
# ===============================================================

def _parse_route_from_text(raw: str) -> List[str]:
    """
    Extrae una lista de !ids a partir de un texto con flechas '-->' o tokens '!xxxxxxxx'.
    """
    if "-->" in raw:
        parts = [p.strip() for p in raw.split("-->") if p.strip()]
        ids = [p for p in parts if p.startswith("!")]
        if ids:
            return ids
    # fallback: capturar !xxxxxxxx
    ids = re.findall(r"![0-9a-fA-F]{8}", raw)
    return [i.strip() for i in ids]


def api_traceroute(host: str, dest_id: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Retorna dict {ok: bool, hops: int, route: [ids], raw: str}
    """
    dest = str(dest_id).strip()
    if not dest:
        return {"ok": False, "hops": 0, "route": [], "raw": "dest_id vacío"}

    # 1) API directa si está disponible
    try:
        iface = _open_iface(host)
        try:
            # La función puede no existir según la versión de la librería
            for cand in ("traceroute", "traceRoute", "trace_route"):
                tr_fn = getattr(iface, cand, None)
                if callable(tr_fn):
                    res = tr_fn(dest)  # firma dependiente; devolvemos str(dict)
                    raw = str(res)
                    route = _parse_route_from_text(raw)
                    hops = max(0, len(route) - 1) if route else 0
                    ok = bool(route and len(route) >= 2)
                    return {"ok": ok, "hops": hops, "route": route, "raw": raw}
        finally:
            # En el pool no cerramos la interfaz; este close es defensivo si no fuera la misma instancia
            try:
                iface.close()
            except Exception:
                pass
    except Exception:
        # fallback CLI
        pass

    # 2) CLI
    out, _rc = _run_cli(["--host", host, "--traceroute", dest], timeout=timeout)
    raw = out.strip()
    route = _parse_route_from_text(raw)
    hops = max(0, len(route) - 1) if route else 0
    ok = bool(route and len(route) >= 2)
    return {"ok": ok, "hops": hops, "route": route, "raw": raw}


# ===============================================================
# API: Telemetría
# ===============================================================


# 29-08-2025 09:37 -- REEMPLAZA ESTA FUNCIÓN EN meshtastic_api_adapter.py ---

def api_request_telemetry(
    host: str,
    dest_id: str,
    timeout: int = 25,
    allow_cli_fallback: bool = False
) -> Dict[str, Any]:
    """
    Solicita telemetría a un nodo por **API**. NO usa CLI salvo que allow_cli_fallback=True.
    Prueba métodos disponibles en la interfaz (versiones distintas de la librería):
      - requestTelemetry(destinationId=...)
      - sendRequestTelemetry(destinationId=...)
      - request_telemetry(destinationId=...)
      - sendTelemetry(destinationId=..., [telemetryType=...])
    Devuelve: { "ok": bool, "raw": str }
    """
    dest = str(dest_id or "").strip()
    if not dest:
        return {"ok": False, "raw": "dest_id vacío"}

    from tcpinterface_persistent import TCPInterfacePool  # import local para evitar ciclos
    import logging

    def _call_variants(iface):
        """
        Intenta varias combinaciones de nombre/argumentos para máxima compatibilidad.
        Retorna una cadena representando el resultado si alguna llamada funciona.
        Lanza excepción si ninguna firma es válida.
        """
        # Candidatos de nombre
        names = ("requestTelemetry", "sendRequestTelemetry", "request_telemetry", "sendTelemetry")

        # Posibles kwargs base (algunas versiones aceptan 'dest' o 'destinationId')
        kw_bases = (
            {"destinationId": dest},
            {"dest": dest},
            {"id": dest},
        )

        # Tipos de telemetría (algunas firmas lo requieren explícito en sendTelemetry)
        telem_variants = (
            {},  # sin especificar
            {"telemetryType": "device_metrics"},
            {"telemetryType": "DEVICE_METRICS"},
            {"telemetryType": 0},  # algunas enums empiezan en 0
            {"telemetryType": 1},  # otras en 1
        )

        last_err = None
        for name in names:
            fn = getattr(iface, name, None)
            if not callable(fn):
                continue

            # 1) primero intenta con cada kw_base "sin" telemetryType
            for base in kw_bases:
                try:
                    r = fn(**base)
                    return f"{name} OK → {r!s}"
                except TypeError as e:
                    last_err = e  # quizá requiere telemetryType o distinta firma
                except Exception as e:
                    # Otros errores (timeout de la radio, etc.) los propagamos
                    raise

            # 2) si el método es sendTelemetry, prueba con telemetryType explícito
            if name == "sendTelemetry":
                for base in kw_bases:
                    for extra in telem_variants[1:]:
                        try:
                            args = base.copy()
                            args.update(extra)
                            r = fn(**args)
                            return f"{name}{args} OK → {r!s}"
                        except TypeError as e:
                            last_err = e
                        except Exception as e:
                            raise

            # 3) último intento: positional (por si alguna versión no nombra kwargs)
            try:
                r = fn(dest)
                return f"{name}({dest}) OK → {r!s}"
            except TypeError as e:
                last_err = e
            except Exception as e:
                raise

        # Si llegamos aquí, no encontramos firma válida
        raise TypeError(f"No hay firma compatible para requestTelemetry/sendTelemetry ({last_err})")

    # Usamos pool persistente y un reintento ante errores de socket
    from typing import Optional
    RETRY_ERRORS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError)

    def _with_retry(hostname: str, port: int, fn):
        iface = TCPInterfacePool.get(hostname, port=port)
        try:
            return fn(iface)
        except RETRY_ERRORS as e:
            logging.warning("api_request_telemetry: %s → reconectando", type(e).__name__)
            TCPInterfacePool.reset(hostname, port)
            iface = TCPInterfacePool.get(hostname, port=port)
            return fn(iface)

    # Puerto por defecto del TCPInterface
    port = 4403
    try:
        result = _with_retry(host, port, _call_variants)
        return {"ok": True, "raw": str(result)}
    except Exception as e_api:
        # Solo si lo pides expresamente, se hace fallback a CLI
        if allow_cli_fallback:
            try:
                out, _rc = _run_cli(["--host", host, "--dest", dest, "--request-telemetry"], timeout=timeout)
                return {"ok": bool(out.strip()), "raw": out or str(e_api)}
            except Exception as e_cli:
                return {"ok": False, "raw": f"API err: {e_api} | CLI err: {e_cli}"}
        else:
            return {"ok": False, "raw": f"API no disponible para requestTelemetry/sendTelemetry: {e_api}"}


#29-08-2025 09:36 SE ANULA POR OTRA SIN CLI
def api_request_telemetry_OLD(host: str, dest_id: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Solicita telemetría a un nodo. Retorna dict:
      { "ok": bool, "raw": str }
    """
    dest = str(dest_id).strip()
    if not dest:
        return {"ok": False, "raw": "dest_id vacío"}

    # 1) API
    try:
        iface = _open_iface(host)
        try:
            # Probar múltiples nombres de función por compatibilidad.
            for name in ("requestTelemetry", "sendRequestTelemetry", "request_telemetry"):
                fn = getattr(iface, name, None)
                if callable(fn):
                    r = fn(destinationId=dest)
                    return {"ok": True, "raw": str(r)}
        finally:
            try:
                iface.close()
            except Exception:
                pass
    except Exception:
        # fallback CLI
        pass

    # 2) CLI
    out, _rc = _run_cli(["--host", host, "--dest", dest, "--request-telemetry"], timeout=timeout)
    return {"ok": bool(out.strip()), "raw": out}


# ===============================================================
# API: Envío de texto
# ===============================================================

def api_send_text(
    host: str,
    dest_id_or_none: Optional[str],
    text: str,
    channel: int = 0,
    want_ack: bool = False,
    timeout: int = 20,
) -> Dict[str, Any]:
    """
    Envía texto por API si es posible; si no, usa CLI.

    Retorna:
      { "ok": bool, "packet_id": int|None, "raw": str }
    """
    dest = dest_id_or_none or "^all"

    # 1) API
    try:
        iface = _open_iface(host)
        try:
            pkt = iface.sendText(
                text,
                destinationId=dest,
                wantAck=bool(want_ack),
                wantResponse=False,
                channelIndex=int(channel),
            )
            pid: Optional[int] = None
            if isinstance(pkt, dict):
                # variantes: {"id": 123}  o  {"_packet": {"id": 123}}
                if "id" in pkt and isinstance(pkt["id"], (int, str)):
                    pid = _safe_int(pkt["id"])
                elif "_packet" in pkt and isinstance(pkt["_packet"], dict):
                    pid = _safe_int(pkt["_packet"].get("id"))
            else:
                # objeto con atributo .id
                pid = _safe_int(getattr(pkt, "id", None))
            return {"ok": True, "packet_id": pid, "raw": str(pkt)}
        finally:
            try:
                #iface.close()
                pass
            except Exception:
                pass
    except Exception:
        # fallback CLI
        pass

    # 2) CLI
    args = ["--host", host, "--sendtext", text, "--ch-index", str(int(channel))]
    if dest_id_or_none:
        args += ["--dest", dest_id_or_none]
    out, _rc = _run_cli(args, timeout=timeout)
    return {"ok": bool(out.strip()), "packet_id": None, "raw": out}

# --- AÑADIR AL FINAL DE meshtastic_api_adapter.py ---
import logging
from typing import Optional, Tuple

from tcpinterface_persistent import TCPInterfacePool

# Errores típicos de socket a capturar para reintento
RETRY_ERRORS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError)

def _with_retry(host: str, port: int, fn):
    """
    Ejecuta fn(iface) con un intento + 1 reconexión si hay error de socket.
    """
    iface = TCPInterfacePool.get(host, port=port)
    try:
        return fn(iface)
    except RETRY_ERRORS as e:
        logging.warning("meshtastic_api_adapter: error %s → reconectando", type(e).__name__)
        TCPInterfacePool.reset(host, port)
        iface = TCPInterfacePool.get(host, port=port)
        return fn(iface)

def send_text_with_ack(
    host: str,
    port: int,
    text: str,
    dest_id: Optional[str] = None,      # '!id' o None (broadcast)
    channel_index: Optional[int] = None,
    want_ack: bool = True,
    timeout: float = 20.0
) -> Tuple[int, Optional[bool]]:
    """
    Envía texto por API meshtastic con ACK opcional.
    Devuelve: (packet_id, ack_ok | None)
    - Si want_ack=False → (packet_id, None)
    - Si want_ack=True  → (packet_id, True/False según waitForAck)
    """
    def _do(iface):
        # Nota: iface es MeshInterface(TCPInterface) ya conectado por el pool
        pkt_id = iface.sendText(
            text=text,
            destinationId=dest_id,
            channelIndex=channel_index,
            wantAck=want_ack
        )
        if want_ack:
            try:
                ok = bool(iface.waitForAck(pkt_id, timeout=timeout))
            except Exception as e:
                logging.warning("waitForAck lanzó excepción: %s", e)
                ok = False
            return pkt_id, ok
        return pkt_id, None

    return _with_retry(host, port, _do)

def send_text_simple_with_retry(
    host: str,
    port: int,
    text: str,
    dest_id: Optional[str] = None,
    channel_index: Optional[int] = None,
    want_ack: bool = False,
) -> Dict[str, Any]:
    """
    Envío sencillo con reintento automático si la TCP está rota.
    Devuelve: { ok: bool, packet_id: int|None, raw: str }
    """
    import logging

    def _do(iface):
        pkt = iface.sendText(
            text=text,
            destinationId=(dest_id or "^all"),
            channelIndex=channel_index,
            wantAck=want_ack,
            wantResponse=False,
        )
        # extraer packet_id de dict u objeto
        pid = None
        if isinstance(pkt, dict):
            pid = pkt.get("id") or ((pkt.get("_packet") or {}).get("id"))
        else:
            pid = getattr(pkt, "id", None)
        try:
            pid = int(pid) if pid is not None else None
        except Exception:
            pid = None
        logging.info("sendText OK%s", f" • packet_id={pid}" if pid is not None else "")
        return {"ok": True, "packet_id": pid, "raw": str(pkt)}

    # _with_retry ya hace: invalidar pool → reconectar → reintentar
    return _with_retry(host, port, _do)

# === NUEVO: reconexión del pool TCP ===
def mesh_reconnect(host: str = None, port: int = 4403) -> bool:
    """
    Reabre la conexión TCP del pool persistente.
    Devuelve True si la reconexión no lanzó excepción.
    """
    try:
        from tcpinterface_persistent import TCPInterfacePool
    except Exception:
        # Sin pool, nada que hacer (no es error fatal)
        return False

    try:
        # Si te interesa ignorar 'host' y usar el del pool por defecto, puedes:
        iface = TCPInterfacePool.get(host=host, port=port)
        # Algunos builds exponen close_and_reopen(); otros, reconnect()
        if hasattr(iface, "close_and_reopen"):
            iface.close_and_reopen()
        elif hasattr(iface, "reconnect"):
            iface.reconnect()
        else:
            # fallback: cerrar y volver a abrir creando un nuevo iface
            TCPInterfacePool.close(host=host, port=port)
            TCPInterfacePool.get(host=host, port=port)
        return True
    except Exception:
        return False
    
# === COMPAT PATCH: ignora kwargs inesperados (p.ej. first_timeout, retry_timeout) ===
try:
    _orig__send_text_simple_with_retry = send_text_simple_with_retry  # conserva la original

    def send_text_simple_with_retry(*args, **kwargs):
        # Ignora silenciosamente kwargs que algunas capas pasan y otras no soportan
        kwargs.pop("first_timeout", None)
        kwargs.pop("retry_timeout", None)
        return _orig__send_text_simple_with_retry(*args, **kwargs)
except NameError:
    # Si aún no existiese la base en tu versión, no hacemos nada (no rompe)
    pass

# === [NUEVO] Lista de nodos vía pool persistente, sin abrir conexiones nuevas ===
import time
from typing import Any, Dict, List, Optional

try:
    # El proyecto ya incluye este módulo en v4.x
    from tcpinterface_persistent import TCPInterfacePool
except Exception:
    TCPInterfacePool = None  # Permitimos importar el módulo sin romper si no existe aún

def api_list_nodes_via_pool(
    host: str,
    port: Optional[int] = None,
    max_n: int = 50,
    timeout_sec: float = 4.0,
    assume_hops_zero: bool = False,
    pool_cls=None
) -> List[Dict[str, Any]]:
    """
    Obtiene nodos reutilizando la conexión persistente (pool), evitando nuevas conexiones.
    - NO abre nuevos sockets: toma el interface ya conectado del pool.
    - Espera hasta 'timeout_sec' a que haya nodos recuperados.
    - 'assume_hops_zero': si True y no hay hopLimit, fuerza hops = 0.
    Devuelve: lista de dicts homogéneos con campos útiles (id, alias, snr, rssi, hops, last_heard, etc.)
    """
    if pool_cls is None:
        pool_cls = TCPInterfacePool
    if pool_cls is None:
        raise RuntimeError("TCPInterfacePool no disponible; verifica import de tcpinterface_persistent")

    # 1) Obtener (sin crear) la sesión persistente
    iface = pool_cls.get(host, port or 4403)
    if iface is None:
        raise RuntimeError("No hay interfaz activa en el pool; asegúrate de inicializar TCPInterfacePool antes.")

    # 2) Forzar/solicitar refresco local de nodos sin desconectar
    try:
        # Muchos builds de meshtastic-python exponen getNodes() o requestNodes()
        # Usamos cualquiera disponible sin romper versiones.
        if hasattr(iface, "getNodes"):
            iface.getNodes()
        elif hasattr(iface, "requestNodes"):
            iface.requestNodes()
    except Exception:
        # No forzamos; pasamos a leer estado actual
        pass

    # 3) Espera cooperativa a que el interface tenga nodos
    t0 = time.time()
    nodes: List[Dict[str, Any]] = []
    while time.time() - t0 < timeout_sec:
        try:
            # Diferentes versiones usan estructuras ligeramente distintas:
            # - iface.nodes (dict)
            # - iface._nodes (privado)
            # - iface.mesh.nodes (algunas ramas)
            raw_nodes = None
            if hasattr(iface, "nodes") and iface.nodes:
                raw_nodes = iface.nodes
            elif hasattr(iface, "_nodes") and iface._nodes:
                raw_nodes = iface._nodes
            elif hasattr(iface, "mesh") and getattr(iface.mesh, "nodes", None):
                raw_nodes = iface.mesh.nodes

            if raw_nodes:
                # raw_nodes suele ser dict {!id: nodeDict}
                out: List[Dict[str, Any]] = []
                for _id, nd in (raw_nodes.items() if isinstance(raw_nodes, dict) else []):
                    # Campos defensivos
                    user = (nd.get("user") or {}) if isinstance(nd, dict) else {}
                    metrics = (nd.get("deviceMetrics") or {}) if isinstance(nd, dict) else {}
                    rx_snr = None
                    # En algunas builds, SNR viene en 'snr' a nivel root o dentro de metrics
                    if "snr" in nd:
                        rx_snr = nd.get("snr")
                    elif "snr" in metrics:
                        rx_snr = metrics.get("snr")

                    hops = nd.get("hops")
                    if hops is None and assume_hops_zero:
                        hops = 0

                    last_heard = nd.get("lastHeard") or nd.get("lastHeardTime") or user.get("lastSeen") or None

                    out.append({
                        "id": _id,
                        "num": user.get("id") or user.get("num"),
                        "longName": user.get("longName"),
                        "shortName": user.get("shortName"),
                        "alias": user.get("longName") or user.get("shortName") or _id,
                        "snr": rx_snr,
                        "rssi": metrics.get("rssi") if isinstance(metrics, dict) else None,
                        "hops": hops,
                        "last_heard": last_heard,
                    })

                # Orden por recencia (last_heard desc, None al final)
                def _age_key(n: Dict[str, Any]):
                    ts = n.get("last_heard")
                    return -(ts or -1_000_000_000)  # None va al final
                out.sort(key=_age_key)

                nodes = out[:max_n]
                if nodes:
                    break
        except Exception:
            # Esperamos y reintentamos
            pass

        time.sleep(0.20)

    return nodes

# === [NUEVO] Vecinos (directos) vía pool persistente, sin abrir conexiones nuevas ===
import time
from typing import Any, Dict, Optional

try:
    from tcpinterface_persistent import TCPInterfacePool
except Exception:
    TCPInterfacePool = None  # Permitimos cargar el módulo incluso si aún no existe


def api_get_neighbors_via_pool(
    host: str,
    port: Optional[int] = None,
    timeout_sec: float = 4.0,
    pool_cls=None
) -> Dict[str, Dict[str, Any]]:
    """
    Devuelve vecinos 'directos' reutilizando la conexión persistente del pool.
    NO abre sockets nuevos.

    Retorno: dict con clave = "!id", valor = {"hops": int|None, "snr": float|None, "rssi": float|None}

    Heurística:
      - Lee nodos del interface en memoria.
      - Considera 'vecino directo' si hops == 1 (excluye self con hops==0).
      - snr/rssi se intentan desde deviceMetrics o campos a nivel nodo.

    Notas:
      - Si el firmware/biblioteca no expone hops, se devolverán pocos/ningún vecino.
      - Si no hay pool o no hay nodos, retorna {}.
    """
    if pool_cls is None:
        pool_cls = TCPInterfacePool
    if pool_cls is None:
        raise RuntimeError("TCPInterfacePool no disponible; verifica import de tcpinterface_persistent")

    iface = pool_cls.get(host, (port or 4403))
    if iface is None:
        return {}

    # Intentar refresco suave (no bloqueante si no existe)
    try:
        if hasattr(iface, "getNodes"):
            iface.getNodes()
        elif hasattr(iface, "requestNodes"):
            iface.requestNodes()
    except Exception:
        pass

    t0 = time.time()
    out: Dict[str, Dict[str, Any]] = {}

    while time.time() - t0 < timeout_sec:
        raw_nodes = None
        try:
            if hasattr(iface, "nodes") and iface.nodes:
                raw_nodes = iface.nodes
            elif hasattr(iface, "_nodes") and iface._nodes:
                raw_nodes = iface._nodes
            elif hasattr(iface, "mesh") and getattr(iface.mesh, "nodes", None):
                raw_nodes = iface.mesh.nodes
        except Exception:
            raw_nodes = None

        if isinstance(raw_nodes, dict) and raw_nodes:
            tmp: Dict[str, Dict[str, Any]] = {}
            for k, nd in raw_nodes.items():
                # Normalizar id → "!XXXXXXXX"
                nid = None
                try:
                    if isinstance(k, str) and k.startswith("!"):
                        nid = k
                    elif isinstance(k, int):
                        nid = f"!{int(k):08x}"
                    else:
                        nid = f"!{str(k).lstrip('!')}"
                except Exception:
                    nid = f"!{str(k)}"

                # hops
                hops = None
                if isinstance(nd, dict):
                    hops = nd.get("hops")
                    try:
                        hops = int(hops) if hops is not None else None
                    except Exception:
                        hops = None

                    # Filtrar solo vecinos directos (hops==1). Excluir self (0).
                    if hops != 1:
                        continue

                    # métricas
                    metrics = nd.get("deviceMetrics") or {}
                    rssi = nd.get("rssi")
                    snr = nd.get("snr")
                    if isinstance(metrics, dict):
                        if rssi is None:
                            rssi = metrics.get("rssi")
                        if snr is None:
                            snr = metrics.get("snr")
                    # Normalizar
                    try:
                        rssi = None if rssi is None else float(rssi)
                    except Exception:
                        rssi = None
                    try:
                        snr = None if snr is None else float(snr)
                    except Exception:
                        snr = None

                    tmp[str(nid)] = {"hops": hops, "snr": snr, "rssi": rssi}

            if tmp:
                out = tmp
                break  # ya tenemos vecinos directos
        time.sleep(0.20)

    return out
