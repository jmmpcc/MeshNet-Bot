# tcpinterface_persistent.py
# Pool persistente para meshtastic.tcp_interface.TCPInterface
# V6.1.4.3
# Objetivo en este proyecto:
# - Mantener UNA única conexión TCP por (host,port) dentro del proceso.
# - Evitar tormentas de reconexión y carreras cuando conviven varios componentes.
# - Ofrecer reset() y shutdown() seguros (usados por el broker).
#
# NOTA:
# - Este módulo NO debe pausar el broker (eso se gestiona en Meshtastic_Broker.py).
# - Debe ser 100% importable (sin placeholders) porque el broker lo importa en caliente.

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Dict, Tuple, Optional, Any

# --------------------------------------------------------------------------------------
# Eventos "visibles" para otros módulos: marca reconexiones del pool (para el adapter/bot)
# --------------------------------------------------------------------------------------

_POOL_RECONNECT_EVENTS = deque(maxlen=64)
_POOL_RECONNECT_LOCK = threading.Lock()

def _mark_pool_reconnected() -> None:
    with _POOL_RECONNECT_LOCK:
        _POOL_RECONNECT_EVENTS.append(time.time())

def pop_pool_reconnect_flag() -> bool:
    """True si hubo una reconexión reciente del pool. Limpia la marca."""
    with _POOL_RECONNECT_LOCK:
        return bool(_POOL_RECONNECT_EVENTS and _POOL_RECONNECT_EVENTS.pop())

# --------------------------------------------------------------------------------------
# Parche defensivo: sendHeartbeat puede disparar 10054/10053 y matar el hilo RX.
# Lo suprimimos y dejamos que el pool detecte el socket roto.
# --------------------------------------------------------------------------------------

try:
    import meshtastic.mesh_interface as _mesh_mod
    _orig_sendHeartbeat = getattr(_mesh_mod.MeshInterface, "sendHeartbeat", None)

    if callable(_orig_sendHeartbeat):
        def _safe_sendHeartbeat(self, *args, **kwargs):
            # Rate-limit para evitar floods y para mantener viva la sesión sin “silencio total”.
            try:
                import os
                min_interval = float(os.getenv("MESH_HEARTBEAT_MIN_INTERVAL", "30") or "30")
            except Exception:
                min_interval = 30.0

            try:
                last = float(getattr(self, "_hb_guard_ts", 0.0) or 0.0)
            except Exception:
                last = 0.0

            now = time.time()
            if (now - last) < max(1.0, min_interval):
                return None

            try:
                setattr(self, "_hb_guard_ts", now)
            except Exception:
                pass

            try:
                return _orig_sendHeartbeat(self, *args, **kwargs)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                logging.warning("MeshInterface.sendHeartbeat suprimido: %s", e)
                try:
                    s = getattr(self, "socket", None)
                    if s:
                        try:
                            s.close()
                        except Exception:
                            pass
                except Exception:
                    pass
                return None


        _mesh_mod.MeshInterface.sendHeartbeat = _safe_sendHeartbeat
except Exception as _e:
    logging.debug("No se pudo parchear sendHeartbeat: %s", _e)

# --------------------------------------------------------------------------------------
# Import y captura del ctor real de TCPInterface (para poder crear conexiones sin recursión)
# --------------------------------------------------------------------------------------

_TCP_ORIG_CTOR = None
TCPInterface = None

try:
    import meshtastic.tcp_interface as _tcp_mod
    _TCP_ORIG_CTOR = getattr(_tcp_mod, "TCPInterface", None)
    if _TCP_ORIG_CTOR is None:
        raise RuntimeError("meshtastic.tcp_interface.TCPInterface no encontrado")
except Exception as e:
    logging.error("No se pudo importar meshtastic.tcp_interface.TCPInterface: %s", e)
    _TCP_ORIG_CTOR = None

# --------------------------------------------------------------------------------------
# Pool
# --------------------------------------------------------------------------------------

class _SafeCloser:
    """Cierra una interfaz Meshtastic con tolerancia a errores."""
    @staticmethod
    def close_iface(iface: Any) -> None:
        try:
            if iface is None:
                return
            if hasattr(iface, "close"):
                try:
                    iface.close()
                except Exception:
                    pass
            # Algunas versiones guardan socket en 'socket', 'sock', '_sock'
            for attr in ("socket", "sock", "_sock", "_socket"):
                s = getattr(iface, attr, None)
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
        except Exception:
            pass


class TCPInterfacePool:
    """
    Pool persistente de TCPInterface por (host, port).

    API usada por el broker:
      - get(host, port) -> TCPInterface
      - reset(host, port) -> fuerza cierre duro y recreación perezosa
      - shutdown() -> cierra todo

    Interno:
      - warmup post conexión (waitForConnected/waitForConfigComplete/heartbeat)
      - keepalive TCP (SO_KEEPALIVE)
    """

    _lock = threading.Lock()
    _pool: Dict[Tuple[str, int], Dict[str, Any]] = {}

    @classmethod
    def get(cls, host: str, port: int = 4403, **kwargs):
        if _TCP_ORIG_CTOR is None:
            raise RuntimeError("Meshtastic TCPInterface no disponible (import falló)")

        host = (host or "127.0.0.1").strip()
        port = int(port or 4403)
        key = (host, port)

        with cls._lock:
            entry = cls._pool.get(key)
            if entry is None:
                entry = {"if": None, "ts": 0.0, "lock": threading.Lock()}
                cls._pool[key] = entry

        with entry["lock"]:
            iface = entry["if"]
            if not cls._is_alive(iface):
                iface = cls._connect_fresh(host, port, **kwargs)
                entry["if"] = iface
                _mark_pool_reconnected()
            entry["ts"] = time.time()
            return iface

    @staticmethod
    def _is_alive(iface: Any) -> bool:
        try:
            if iface is None:
                return False

            # 1) Algunas versiones exponen isConnected bool
            is_conn = getattr(iface, "isConnected", None)
            if isinstance(is_conn, bool):
                return is_conn

            # 2) Socket abierto (si existe)
            s = getattr(iface, "socket", None) or getattr(iface, "sock", None) or getattr(iface, "_sock", None)
            if not s:
                return True  # no hay socket accesible; asumimos vivo
            if getattr(s, "_closed", False):
                return False

            # 3) Hilo RX vivo (si existe)
            rx = getattr(iface, "_rxThread", None)
            return (rx is None) or rx.is_alive()
        except Exception:
            return False

    @staticmethod
    def _post_connect_warmup(iface: Any) -> None:
        # 1) waitForConnected
        try:
            wfc = getattr(iface, "waitForConnected", None)
            if callable(wfc):
                try:
                    wfc(timeout=10)
                except Exception as e:
                    logging.debug("waitForConnected timeout/err: %s", e)
        except Exception:
            pass

        # 2) waitForConfigComplete
        try:
            wcc = getattr(iface, "waitForConfigComplete", None)
            if callable(wcc):
                try:
                    wcc(timeout=15)
                except Exception as e:
                    logging.debug("waitForConfigComplete timeout/err: %s", e)
                    time.sleep(1.0)
            else:
                time.sleep(0.7)
        except Exception:
            pass

        # 3) heartbeat opcional
        try:
            hb = getattr(iface, "sendHeartbeat", None)
            if callable(hb):
                try:
                    hb()
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _connect_fresh(host: str, port: int, **kwargs):
        # Normaliza si llega "host:port"
        try:
            if isinstance(host, str) and ":" in host:
                h, p = host.rsplit(":", 1)
                if p.isdigit():
                    host = h
                    port = int(p)
        except Exception:
            pass

        variants = [
            {"hostname": host},
            {"hostname": host, "port": int(port)},
            {"host": host},
            {"host": host, "port": int(port)},
        ]

        last_type_err = None
        last_conn_err = None
        iface = None

        # Reintentos suaves
        for attempt in range(1, 4):
            for kw in variants:
                try:
                    iface = _TCP_ORIG_CTOR(**kw)
                    break
                except TypeError as e:
                    last_type_err = e
                    continue
                except Exception as e:
                    last_conn_err = e
                    continue
            if iface is not None:
                break
            time.sleep(min(0.5 * attempt, 2.0))

        if iface is None:
            try:
                iface = _TCP_ORIG_CTOR(hostname=host)
            except Exception as e:
                if last_conn_err is not None:
                    raise last_conn_err
                if last_type_err is not None:
                    raise last_type_err
                raise e

        # Keepalive TCP (afinable por entorno)
        try:
            s = getattr(iface, "socket", None) or getattr(iface, "sock", None) or getattr(iface, "_sock", None)
            if s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                # Afinado en Linux si está disponible
                try:
                    import os
                    idle = int(os.getenv("TCP_KEEPIDLE", "30") or "30")
                    intvl = int(os.getenv("TCP_KEEPINTVL", "10") or "10")
                    cnt = int(os.getenv("TCP_KEEPCNT", "3") or "3")
                except Exception:
                    idle, intvl, cnt = 30, 10, 3

                if hasattr(socket, "TCP_KEEPIDLE"):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, max(1, idle))
                    except Exception:
                        pass
                if hasattr(socket, "TCP_KEEPINTVL"):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, max(1, intvl))
                    except Exception:
                        pass
                if hasattr(socket, "TCP_KEEPCNT"):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, max(1, cnt))
                    except Exception:
                        pass
        except Exception:
            pass


        # Warm-up
        try:
            TCPInterfacePool._post_connect_warmup(iface)
        except Exception as e:
            logging.debug("post_connect_warmup: %s", e)

        return iface

    @classmethod
    def reset(cls, host: str, port: int = 4403) -> None:
        """Cierre duro (RST) y deja la entrada a None para recreación perezosa."""
        host = (host or "127.0.0.1").strip()
        port = int(port or 4403)
        key = (host, port)

        with cls._lock:
            entry = cls._pool.get(key)
            if entry is None:
                cls._pool[key] = {"if": None, "ts": 0.0, "lock": threading.Lock()}
                return

        with entry["lock"]:
            iface = entry.get("if")
            try:
                s = getattr(iface, "sock", None) or getattr(iface, "_sock", None) or getattr(iface, "socket", None)
                if s:
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    except Exception:
                        pass
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        s.close()
                    except Exception:
                        pass
                _SafeCloser.close_iface(iface)
            finally:
                entry["if"] = None
                entry["ts"] = 0.0

        try:
            time.sleep(0.2)
        except Exception:
            pass

    @classmethod
    def shutdown(cls) -> None:
        """Cierra y limpia TODAS las entradas del pool."""
        with cls._lock:
            keys = list(cls._pool.keys())
        for host, port in keys:
            try:
                cls.reset(host, int(port))
            except Exception:
                pass
        with cls._lock:
            cls._pool.clear()

# --------------------------------------------------------------------------------------
# Inyección: cualquier import de meshtastic.tcp_interface.TCPInterface pasa por el pool.
# Esto protege a todo el proceso sin tocar el resto del código.
# --------------------------------------------------------------------------------------

def _TCPInterface_Compat(*args, **kwargs):
    host = kwargs.get("hostname") or kwargs.get("host")
    port = int(kwargs.get("port", 4403) or 4403)

    if not host and args:
        host = args[0]
    if not host:
        host = "127.0.0.1"

    return TCPInterfacePool.get(host, port)

try:
    if _TCP_ORIG_CTOR is not None:
        import meshtastic.tcp_interface as _tcp_mod2
        _tcp_mod2.TCPInterface = _TCPInterface_Compat
        TCPInterface = _TCPInterface_Compat
except Exception as _e:
    logging.debug("Shim TCPInterface pool no aplicado: %s", _e)

# --------------------------------------------------------------------------------------
# Compat: helper para el bot (si lo usa)
# --------------------------------------------------------------------------------------

def get_tcp_pool(host: str, port: int = 4403):
    """Wrapper ligero compatible con código que espera .client o .get_adapter()."""
    iface = TCPInterfacePool.get(host, port)

    class _PoolWrapper:
        __slots__ = ("iface",)
        def __init__(self, iface):
            self.iface = iface
        def get_adapter(self):
            return self.iface
        @property
        def client(self):
            return self.iface

    return _PoolWrapper(iface)
