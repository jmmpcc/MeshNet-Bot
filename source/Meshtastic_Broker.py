#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Version v6.1.3

from __future__ import annotations
"""
Meshtastic_Broker_v6.1.3.py
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
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from pubsub import pub
from meshtastic.tcp_interface import TCPInterface

# --- Pasarela embebida (NUEVO) ---
from bridge_in_broker import (
    bridge_start_in_broker,
    bridge_stop_in_broker,
    bridge_status_in_broker,
    bridge_mirror_outgoing_from_broker,   # ← NUEVO
)

# === NUEVO: host/port runtime para Meshtastic (rellenos en main() desde --host)
RUNTIME_MESH_HOST = None   # se fija en main()
RUNTIME_MESH_PORT = 4403   # puerto TCP del nodo Meshtastic

# --- Compat shim para Meshtastic TCPInterface (host -> hostname) + pool único ---
try:
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




# === Throttle de logs de guards y barrera TX ===
import threading, time

_guard_last_log = {"sendToRadio": 0.0, "sendHeartbeat": 0.0}

def _guard_log(kind: str, msg: str, interval: float = 5.0):
    now = time.time()
    last = _guard_last_log.get(kind, 0.0)
    if now - last >= interval:
        print(msg, flush=True)
        _guard_last_log[kind] = now



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
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if SHOW_HEARTBEATS:
            return True
        msg = record.getMessage()
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
    logging.getLogger().addFilter(f)

# === [NUEVO] Modo sin heartbeat: anula el envío del SDK de meshtastic ===
def install_no_heartbeat_mode(verbose: bool = False) -> bool:
    """
    Desactiva los envíos de heartbeat del SDK:
      - Reemplaza MeshInterface.sendHeartbeat() por un NO-OP.
    No rompe la recepción ni el resto de envíos.
    Devuelve True si se activó correctamente.
    """
    try:
        import logging
        from meshtastic import mesh_interface as _mi

        if not hasattr(_mi, "MeshInterface"):
            if verbose:
                logging.warning("[no-heartbeat] No se encontró MeshInterface en meshtastic.mesh_interface")
            return False

        if not hasattr(_mi.MeshInterface, "sendHeartbeat"):
            if verbose:
                logging.warning("[no-heartbeat] MeshInterface no tiene sendHeartbeat()")
            return False

        _orig = _mi.MeshInterface.sendHeartbeat

        def _noop(self, *a, **kw):
            if verbose:
                logging.info("[no-heartbeat] sendHeartbeat() suprimido")
            # No hacemos nada: se pierde este heartbeat y se continúa
            return None

        _mi.MeshInterface.sendHeartbeat = _noop  # <- parche activado
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
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = _BacklogWorker()
        _worker_instance.start()


def _iface_ready_reason() -> tuple[bool, str]:
    """
    Devuelve (ready, reason): False si la TX no es oportuna ahora.
    Razones: paused | cooldown | disconnected
    """
    try:
        mgr = globals().get("BROKER_IFACE_MGR")
        c   = globals().get("COOLDOWN")
        if mgr and hasattr(mgr, "is_paused") and mgr.is_paused():
            return False, "paused"
        if c and hasattr(c, "is_active") and c.is_active():
            return False, "cooldown"
        # conectado si hay iface viva
        iface = getattr(mgr, "iface", None)
        if iface is None:
            return False, "disconnected"
        return True, ""
    except Exception:
        return False, "unknown"


def _safe_send_to_radio_via_iface_or_fallback(msg: dict) -> bool:
    """
    msg: {"channel":int, "text":str, "destination":None|"!id", "require_ack":bool}
    Respeta CircuitBreaker y sólo intenta TX cuando la interfaz está lista.
    Si no lo está, reencola sin ruido (con un aviso amortiguado) y sale.
    """
    # --- Log al desencolar (diagnóstico útil) ---
    try:
        _ch   = int(msg.get("channel", 0) or 0)
        _dest = msg.get("destination") or "broadcast"
        _txt  = str(msg.get("text") or "")
        print(f"[ctrl] SEND_TEXT dequeued ch={_ch} dest={_dest} len={len(_txt.encode('utf-8'))}", flush=True)
    except Exception as _e:
        print(f"[ctrl] SEND_TEXT dequeue log error: {type(_e).__name__}: {_e}", flush=True)

    # --- CircuitBreaker: si está abierto, reencola y sal silencioso ---
    if not CIRCUIT_BREAKER.can_attempt():
        try:
            SENDQ.offer(msg, coalesce=False)  # no coalesce para no perder ACK/flags
        except Exception:
            pass
        return False

    # --- Comprobación de estado de interfaz del broker ---
    ready, reason = _iface_ready_reason()
    if not ready:
        # Reencola y emite un aviso amortiguado cada ~5 s para evitar bucle de logs
        try:
            SENDQ.offer(msg, coalesce=False)
        except Exception:
            pass
        try:
            import time as _t
            global _TX_WAIT_LOG_TS
            now = _t.time()
            if (now - float(_TX_WAIT_LOG_TS or 0.0)) >= 5.0:
                print(f"[ctrl] TX en espera — {reason}. Reintentará al reconectar.", flush=True)
                _TX_WAIT_LOG_TS = now
            # Pequeño backoff para que el worker no haga busy-loop
            _t.sleep(0.35)
        except Exception:
            pass
        return False

    # --- Interfaz lista: ejecutar ruta de envío real ---
    try:
        r = _tasks_send_adapter(
            int(msg.get("channel", 0)),
            str(msg.get("text") or ""),
            (msg.get("destination") or "broadcast"),
            bool(msg.get("require_ack"))
        )
        ok = bool(r.get("ok"))
        if ok:
            CIRCUIT_BREAKER.record_success()
        else:
            CIRCUIT_BREAKER.record_error()
        return ok
    except Exception:
        # Error en el camino de envío: marcar error y solicitar reconnect suave
        CIRCUIT_BREAKER.record_error()
        try:
            mgr = globals().get("BROKER_IFACE_MGR")
            if mgr and hasattr(mgr, "signal_disconnect"):
                mgr.signal_disconnect()
        except Exception:
            pass
        # Reencola para no perder el mensaje
        try:
            SENDQ.offer(msg, coalesce=False)
        except Exception:
            pass
        return False


def _tasks_send_adapter(channel: int, message: str, destination: str, require_ack: bool) -> dict:
    """
    1) Intentar enviar por la MISMA conexión TCP del broker (iface_mgr) para no abrir 2 sesiones al nodo.
    2) Si no es posible (no iniciado / no conectado / error), caer al adapter resiliente (pool).
    Devuelve: {ok: bool, packet_id?: int, error?: str}
    """
    dest_id = None if (not destination or destination.lower() == "broadcast") else destination

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
                message,
                destinationId=(dest_id if dest_id else "^all"),  # ← broadcast explícito
                wantAck=bool(require_ack),          # ACK solo tiene sentido en unicast
                wantResponse=False,
                channelIndex=int(channel),
            )

            # [NUEVO] espejo hacia B si la pasarela embebida está activa
            try:
                bridge_mirror_outgoing_from_broker(int(channel), message)
            except Exception as _e:
                print(f"[bridge] mirror hook ERROR: {type(_e).__name__}: {_e}", flush=True)

            print(f"[tx] broker sendText ch={int(channel)} dest={dest_id or 'broadcast'} len={len(message.encode('utf-8'))}", flush=True)


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
                    ok_ack = bool(iface.waitForAck(pid, timeout=15.0))
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
            text=message,
            dest_id=dest_id,
            channel_index=int(channel),
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
        broker_tasks.configure_sender(_tasks_send_adapter)
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
    Guarda un registro de traceroute en broker_traceroute_log.jsonl
    """
    try:
        os.makedirs(os.path.dirname(TRACEROUTE_LOG_PATH), exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _TRACEROUTE_LOCK:
            with open(TRACEROUTE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        print(f"⚠️ backlog_append error: {e}", flush=True)

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

        # Tipos permitidos (igual que tu versión nueva + fácil de ampliar)
        ALLOWED = {
            "TEXT_MESSAGE_APP",
            "POSITION_APP",
            "TELEMETRY_APP",
            "NODEINFO_APP",
            # Descomenta si quieres también estos en el offline:
            # "TRACEROUTE_APP", "ROUTING_APP", "NEIGHBORINFO_APP",
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
        }
        row_type = TYPE_MAP.get(port)
        if not row_type:
            return

        obj = {
            "id": pkt.get("id") or rec.get("id"),
            "rx_time": rx_time_val,
            "channel": (
                pkt.get("channel")
                or (pkt.get("meta") or {}).get("channelIndex")
                or rec.get("channel")
                or (rec.get("summary") or {}).get("canal")
            ),
            "portnum": port,
            "type": row_type,  # usado por el panel
            "from": rec.get("from") or pkt.get("from"),
            "to": rec.get("to") or pkt.get("to"),
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

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    except Exception as e:
        _log_ex("append_offline_log failed", e)


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
            self._sock.listen(16)
            print(f"ℹ️ BacklogServer escuchando en {self.host}:{self.port}", flush=True)
            while not self._stop.is_set():
                try:
                    self._sock.settimeout(1.0)
                    try:
                        conn, addr = self._sock.accept()
                    except socket.timeout:
                        continue
                    threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
                except Exception as e:
                    print(f"⚠️ BacklogServer accept error: {e}", flush=True)
        except Exception as e:
            print(f"⚠️ BacklogServer run error: {e}", flush=True)
            traceback.print_exc()

    def _handle_client(self, conn: socket.socket, addr):
        try:
            conn.settimeout(15.0)
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

            # --- FETCH_BACKLOG (igual que tenías) ---
            if cmd == "FETCH_BACKLOG":
                since_ts = params.get("since_ts")
                until_ts = params.get("until_ts")
                channel  = params.get("channel")
                portnums = params.get("portnums") or ["TEXT_MESSAGE_APP"]
                limit    = params.get("limit") or 1000

                out = list(_iter_backlog_jsonl(since_ts, until_ts, channel, portnums, limit))
                resp = {"ok": True, "data": out}
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

                 # === [LOG] controlar recepción (antes de encolar)
                try:
                    print(f"[ctrl] SEND_TEXT recv ch={int(ch)} dest={dest or 'broadcast'} len={len(text.encode('utf-8'))}", flush=True)
                except Exception as _e:
                    print(f"[ctrl] SEND_TEXT recv log error: {type(_e).__name__}: {_e}", flush=True)

                # === [NUEVO] Encolar (no coalesce para textos de usuario)
                try:

                    
                    SENDQ.offer({"channel": ch, "text": text, "destination": dest, "require_ack": ack_flag, "type": "text"},
                                coalesce=False)
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
                    bridge_mirror_outgoing_from_broker(
                        payload={"type": "text", "text": text, "channel": ch},
                        direction="A2B"  # semántica: enviamos desde A hacia B
                    )
                    resp = {"ok": True, "mirrored": True, "via": "B"}
                except Exception as e:
                    resp = {"ok": False, "error": f"bridge_send_failed: {type(e).__name__}: {e}"}

                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")); 
                return

            # --- control del broker (pausa/reanuda/estado/desconexion) ---
            elif cmd in {"BROKER_PAUSE", "BROKER_RESUME", "BROKER_STATUS", "BRIDGE_STATUS", "BROKER_DISCONNECT", "FORCE_RECONNECT", "BROKER_QUIT"}:

                mgr = globals().get("BROKER_IFACE_MGR")
                if not mgr:
                    resp = {"ok": False, "error": "iface manager not ready"}
                else:
                    try:
                        if cmd == "BROKER_PAUSE":
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
                        # --- [NUEVO] comando: RUN_TRACEROUTE -----------------------------------------
            elif cmd == "RUN_TRACEROUTE":
                    node = str(params.get("target") or params.get("node") or "").strip()
                    if not node:
                        resp = {"ok": False, "error": "missing target"}
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        return

                    if not node.startswith("!"):
                        node = "!" + node

                    ok = False
                    try:
                        # Host/port REALES del nodo (los fijaste en main())
                        mesh_host = globals().get("RUNTIME_MESH_HOST")
                        mesh_port = int(globals().get("RUNTIME_MESH_PORT") or 4403)

                        # Gestor global de la interfaz persistente del broker
                        mgr = globals().get("BROKER_IFACE_MGR")
                        if mgr and hasattr(mgr, "acquire") and callable(mgr.acquire):
                            iface = None
                            try:
                                try:
                                    iface = mgr.acquire(mesh_host, mesh_port, timeout=4.0, reuse_only=True)
                                except TypeError:
                                    iface = mgr.acquire(mesh_host, mesh_port, timeout=4.0)

                                if iface:
                                    # Probar varias firmas según SDK
                                    candidates = [
                                        ("traceroute",     {"node_id": node}),
                                        ("traceroute",     {"dest_id": node}),
                                        ("traceroute",     {"id": node}),
                                        ("sendTraceroute", {"dest_id": node}),
                                        ("tracerouteNode", {"dest_id": node}),
                                        ("requestTraceroute", {"dest_id": node}),
                                        ("routeDiscovery", {"dest_id": node}),
                                    ]
                                    for name, kwargs in candidates:
                                        fn = getattr(iface, name, None)
                                        if not callable(fn):
                                            continue
                                        try:
                                            import inspect
                                            sig = inspect.signature(fn)
                                            accepted = set(sig.parameters.keys())
                                            safe_kwargs = {k: v for k, v in kwargs.items() if k in accepted}
                                        except Exception:
                                            safe_kwargs = kwargs
                                        try:
                                            fn(**safe_kwargs)  # no bloqueante
                                            ok = True
                                            break
                                        except Exception:
                                            continue
                            finally:
                                try:
                                    if iface and hasattr(iface, "release"):
                                        iface.release()
                                except Exception:
                                    pass
                        # alternativa si defines self.run_traceroute(...)
                        if (not ok) and hasattr(self, "run_traceroute"):
                            try:
                                self.run_traceroute(node)
                                ok = True
                            except Exception:
                                ok = False

                    except Exception:
                        ok = False

                    resp = {"ok": bool(ok)}
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
                    try:
                        append_offline_log(packet)
                    finally:
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
        import threading, time
        backoff = [2, 4, 8, 12, 20, 30, 45, 60]
        idx = 0

        # Anti-reentradas (un solo connect en vuelo)
        if not hasattr(self, "_connecting"):
            self._connecting = threading.Event()

        # Resolver host/port de forma robusta
        host = getattr(self, "host", None) or str(globals().get("RUNTIME_MESH_HOST") or "127.0.0.1")
        try:
            port = int(globals().get("RUNTIME_MESH_PORT") or 4403)
        except Exception:
            port = 4403

        # Lock interproceso por host:port
        lock_name = f"{host}:{port}"
        if not hasattr(self, "_ip_lock"):
            self._ip_lock = SingleInstanceLock(lock_name)

        while self._want_run:
            self._reconnect_event.wait(timeout=1.0)
            if not self._reconnect_event.is_set():
                continue

            # Si está pausado (exclusiva CLI), no conectar
            if hasattr(self, "_paused") and self._paused.is_set():
                time.sleep(0.2)
                continue

            # Si ya hay un connect en curso, espera
            if self._connecting.is_set():
                time.sleep(0.1)
                continue

            # Consumimos la señal de reconexión
            self._reconnect_event.clear()

            # Cierre limpio de iface previa
            try:
                with self._lock:
                    if getattr(self, "iface", None):
                        try: self.iface.close()
                        except Exception: pass
                        self.iface = None
            except Exception:
                pass

            # Respetar cooldown (si lo usas)
            if getattr(self, "_cooldown_until", 0) > time.time():
                time.sleep(min(1.0, self._cooldown_until - time.time()))

            self._connecting.set()
            try:
                # Intentar adquirir el lock global
                got_lock = self._ip_lock.acquire(timeout_s=2.0)
                if not got_lock:
                    if getattr(self, "verbose", False):
                        print(f"[lock] Otro proceso posee {lock_name}. Esperando…", flush=True)
                    time.sleep(1.0)
                    self._reconnect_event.set()
                    continue

                # Crear TCPInterface SIEMPRE con host+port resueltos
                try:
                    if getattr(self, "verbose", False):
                        print(f"[receiver] Conectando a Meshtastic en {host}:{port}…", flush=True)
                        host_for_iface = f"{host}:{port}" if port and port != 4403 else host
                        new_iface = TCPInterface(hostname=host_for_iface)

                except Exception as e:
                    # liberar lock y backoff
                    try: self._ip_lock.release()
                    except Exception: pass
                    if getattr(self, "verbose", False):
                        print(f"[receiver] Fallo al crear TCPInterface: {e}", flush=True)
                    time.sleep(backoff[min(idx, len(backoff)-1)])
                    idx += 1
                    self._reconnect_event.set()
                    continue

                # Conexión OK
                idx = 0
                try:
                    with self._lock:
                        self.iface = new_iface
                        try:
                            # engancha tus handlers como ya hacías
                            self._attach_handlers_locked()
                        except Exception:
                            pass
                except Exception:
                    # si falló, cerramos iface y liberamos lock
                    try: new_iface.close()
                    except Exception: pass
                    try: self._ip_lock.release()
                    except Exception: pass
                    self._reconnect_event.set()
                    continue

                if getattr(self, "verbose", False):
                    print("ℹ️ Broker: conectado al nodo Meshtastic", flush=True)

                try:
                    if hasattr(self, "_on_connect_ok"): self._on_connect_ok()
                except Exception:
                    pass

                # Mantener mientras no haya motivo para reconectar
                while self._want_run and getattr(self, "iface", None) is not None and not getattr(self, "_paused", threading.Event()).is_set():
                    time.sleep(0.25)

            finally:
                self._connecting.clear()
                try: self._ip_lock.release()
                except Exception: pass

            # Cierre y call de _on_disconnect
            try:
                with self._lock:
                    if getattr(self, "iface", None):
                        try: self.iface.close()
                        except Exception: pass
                        self.iface = None
            except Exception:
                pass
            try:
                if hasattr(self, "_on_disconnect"): self._on_disconnect()
            except Exception:
                pass

            # Preparar siguiente intento (otro hilo volverá a setear el evento si procede)
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

            # === Emitir JSONL a clientes ===
            event = {
                "type": "packet",
                "packet": pkt,
                 # === NUEVO: IDs y alias en plano para que el bot los lea fácil ===
                "from": who_from,
                "to":   who_to,
                "from_alias": from_alias or None,
                "to_alias":   to_alias   or None,
                "summary": {
                    "portnum": portnum,
                    "text": text,
                    "payload_hex": payload_hex,
                    "canal": canal,
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



            # === [NUEVO] Handler TRACEROUTE_* → persistir en OFFLINE_LOG =================
            try:
                port = (decoded.get("portnum") or pkt.get("portnum") or "").upper()
                is_tr = port in ("TRACEROUTE_APP", "ROUTING_APP", "ADMIN_APP:TRACEROUTE", "ADMIN_TRACEROUTE")
                if is_tr:
                    _ts = pkt.get("rxTime") or pkt.get("timestamp") or time.time()
                    rec = {
                        "ts":       int(_ts),
                        "rx_time":  int(_ts),               # 👈 IMPORTANTE: lo usa _iter_backlog_jsonl para since_ts
                        "channel":  canal,                  # si lo tienes calculado arriba
                        "portnum":  port,
                        "from":     pkt.get("fromId") or pkt.get("from") or pkt.get("from_id"),
                        "to":       pkt.get("toId")   or pkt.get("to")   or pkt.get("to_id"),
                        "from_alias": from_alias or None,
                        "to_alias":   to_alias   or None,
                        "relay_node": pkt.get("relay_node") or decoded.get("viaNode"),
                        # campos “útiles” del traceroute:
                        "hop":      decoded.get("hop") or decoded.get("hop_index") or pkt.get("hop"),
                        "via":      decoded.get("viaNode") or decoded.get("relay_node") or pkt.get("relay_node"),
                        # guardamos el decoded entero por si luego quieres más detalles:
                        "decoded":  decoded,
                    }
                    # Reutilizamos el MISMO helper que TEXT usa y que FETCH_BACKLOG ya lee:
                    append_offline_log(rec)
                    # (opcional) traza:
                    # print(f"[trace] +TR {rec['from']} hop={rec.get('hop')} via={rec.get('via')} port={port}", flush=True)
            except Exception as e_tr:
                logging.warning(f"[traceroute] persist fail: {e_tr}")
            # === [FIN TRACEROUTE_*] ======================================================


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
    

    def _on_disconnect(self, interface=None, **kwargs):
        
        # --- IGNORAR desconexiones que no sean la interfaz principal del broker (A) ---
        try:
            main_iface = self.iface_mgr.get_iface()
            if interface is not None and interface is not main_iface:
                if self.verbose:
                    print("[conn] (ignorado) disconnect de interfaz no principal (bridge/B).", flush=True)
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
    ap = argparse.ArgumentParser(description="Broker JSONL para Meshtastic (v3.3, salida limpia + inferencias)")
    ap.add_argument("--host", required=True, help="IP o hostname del nodo Meshtastic (TCPInterface)")
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
    stats = BrokerStats()

    srv = JsonLineServer(args.bind, args.port, hub, verbose=args.verbose)
    srv.start()

    # ===================== NUEVO: arrancar persistencia + servidor backlog =====================
    # ===================== NUEVO: arrancar persistencia + servidor backlog =====================
    ctrl_bind = os.getenv("BROKER_CTRL_BIND", "127.0.0.1")
    start_backlog_server(bind_host=ctrl_bind, port=BACKLOG_PORT)

    start_backlog_worker()  # ← NUEVO: comienza a vaciar SENDQ

# ==========================================================================================

    # ==========================================================================================

    # === NUEVO: iniciar scheduler de tareas ===
    init_broker_tasks()

    print(f"🟢 Broker v6.1.4.1 listo. Conectando a nodo {args.host} y sirviendo en {args.bind}:{args.port}", flush=True)
    print("   Clientes pueden conectarse por TCP y leer líneas JSONL (una por evento).", flush=True)

    # Gestor de conexión (autoreconexión)
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
    def _start_bridge_on_first_connection(interface=None, **kwargs):
        try:
            import os
            enabled = (os.getenv("BRIDGE_ENABLED", "0").strip().lower() in {"1","true","on","si","sí","y","yes"})
            if not enabled:
                print("[bridge] embebida desactivada (BRIDGE_ENABLED=0)", flush=True)
                # ya no necesitamos este hook si está desactivada
                try: pub.unsubscribe(_start_bridge_on_first_connection, "meshtastic.connection.established")
                except Exception: pass
                return

            # Usa la interface que entrega el evento; si no viene, pide la actual al gestor
            iface_for_bridge = interface or iface_mgr.get_iface()
            if not iface_for_bridge:
                print("[bridge] ⚠️ sin interface todavía; espero al próximo established…", flush=True)
                return

            st = bridge_start_in_broker(iface_for_bridge)
            print("[bridge] embebida habilitada:", st, flush=True)

        except Exception as e:
            print(f"[bridge] ⚠️ no se pudo iniciar la pasarela embebida: {type(e).__name__}: {e}", flush=True)
        finally:
            # Ejecutarlo solo una vez; si necesitas rearmarla en reconexiones, quita esta desuscripción
            try: pub.unsubscribe(_start_bridge_on_first_connection, "meshtastic.connection.established")
            except Exception: pass

    # Suscribir el hook al evento de conexión establecida
    pub.subscribe(_start_bridge_on_first_connection, "meshtastic.connection.established")



    # Lanzar primera conexión
    iface_mgr.signal_disconnect()

    # Heartbeat (estadísticas internas del broker)
    hb = HeartbeatThread(hub, stats, every_s=args.heartbeat, target_host=args.host, verbose=args.verbose)
    hb.start()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        hb.stop()
        srv.stop()
        iface_mgr.stop()


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

