# tcpinterface_persistent.py
# Version v6.1.1

import time
import threading
import logging
from typing import Dict, Tuple, Optional
# Guardamos el ctor original para usarlo dentro del pool sin recursión
_TCP_ORIG_CTOR = None

# === [NUEVO] Señalador de reconexiones pasivas (visible para el adapter) ===
import threading
from collections import deque
from time import time as _now
from contextlib import contextmanager

import time, socket, struct  # <-- asegura estos imports arriba del fichero

# --- [NUEVO] Estado/locks de clase para conexión única por host:port
import threading

_CONN_POOL = {}           # {(host, port): TCPInterfacePersistent}
_CONN_LOCKS = {}          # {(host, port): threading.Lock()}
_CONNECTING = set()       # {(host, port)} para evitar carreras



@contextmanager
def with_broker_paused(max_wait_s: float = 4.0):
    ...

# Cola en memoria para marcar eventos de reconexión del pool (lado reader)
_POOL_RECONNECT_EVENTS = deque(maxlen=64)
_POOL_RECONNECT_LOCK = threading.Lock()

def _mark_pool_reconnected():
    """Registrar un evento de reconexión del pool (reader).
    El adapter podrá leer y 'vaciar' este flag para informar al usuario."""
    with _POOL_RECONNECT_LOCK:
        _POOL_RECONNECT_EVENTS.append(_now())

def pop_pool_reconnect_flag() -> bool:
    """Devuelve True si hubo una reconexión reciente del pool y limpia la marca."""
    with _POOL_RECONNECT_LOCK:
        return bool(_POOL_RECONNECT_EVENTS and _POOL_RECONNECT_EVENTS.pop())


# --- Parche anti-traza para sendHeartbeat (Windows 10054) ---
try:
    import meshtastic.mesh_interface as _mesh_mod
    _orig_sendHeartbeat = getattr(_mesh_mod.MeshInterface, "sendHeartbeat", None)

    if callable(_orig_sendHeartbeat):
        def _safe_sendHeartbeat(self, *args, **kwargs):
            try:
                return _orig_sendHeartbeat(self, *args, **kwargs)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                logging.warning("MeshInterface.sendHeartbeat suprimido: %s", e)
                try:
                    s = getattr(self, "socket", None)
                    if s:
                        try: s.close()
                        except Exception: pass
                except Exception:
                    pass
                # No relanzamos: el pool detectará socket cerrado y se reconectará
                return None

        _mesh_mod.MeshInterface.sendHeartbeat = _safe_sendHeartbeat
except Exception as _e:
    logging.debug("No se pudo parchear sendHeartbeat: %s", _e)


try:
    from meshtastic.tcp_interface import TCPInterface
    # Shim opcional: mapear host->hostname y retirar port si la firma no lo permite
    try:
        import meshtastic.tcp_interface as _tcp_mod
        _TCP_orig = getattr(_tcp_mod, "TCPInterface", None)

        if _TCP_orig:
            # Guardamos el ctor original para uso interno del pool
            globals()["_TCP_ORIG_CTOR"] = _TCP_orig

            def _TCPInterface_Compat(*args, **kwargs):
                """
                Wrapper global del ctor que fuerza el uso del pool.
                Cualquier código externo que haga TCPInterface(...) acabará
                reutilizando la interfaz única del pool por (host, port).
                """
                # Normalizamos parámetros
                host = kwargs.get("hostname") or kwargs.get("host")
                port = int(kwargs.get("port", 4403))

                # Si vino por args posicionales (libs antiguas)
                if not host and args:
                    host = args[0]
                if not host:
                    host = "127.0.0.1"

                # Devolver SIEMPRE la interfaz única del pool
                return TCPInterfacePool.get(host, port)

            # Inyectamos el wrapper en el módulo y en el alias local
            _tcp_mod.TCPInterface = _TCPInterface_Compat
            TCPInterface = _TCPInterface_Compat



    except Exception as _e:
     logging.debug("Shim TCPInterface pool no aplicado: %s", _e)

except Exception as e:
    TCPInterface = None
    logging.error("No se pudo importar meshtastic.tcp_interface.TCPInterface: %s", e)

try:
    from threading import Event
except ImportError:
    Event = None

DISCONNECTED_FLAG = Event() if Event else None

class _SafeCloser:
    """Cierra una interfaz Meshtastic con tolerancia a errores."""
    @staticmethod
    def close_iface(iface):
        try:
            if iface:
                # Algunos TCPInterface exponen .close(), otros ._socket etc.
                if hasattr(iface, "close"):
                    iface.close()
                if hasattr(iface, "_socket") and getattr(iface, "_socket", None):
                    try:
                        iface._socket.close()
                    except Exception:
                        pass
        except Exception:
            pass

class TCPInterfacePool:
    """
    Pool persistente de TCPInterface por (host, port).
    - Reutiliza la misma conexión.
    - Reconecta si detecta socket roto.
    - Incluye warm-up post-conexión (waitForConnected / waitForConfigComplete / heartbeat).
    - Cierra todo en shutdown().
    """
    _lock = threading.Lock()
    _pool: Dict[Tuple[str, int], Dict[str, object]] = {}

    @classmethod
    def get(cls, host: str, port: int = 4403, **kwargs):
        if TCPInterface is None:
            raise RuntimeError("Meshtastic TCPInterface no disponible")

        key = (host, port)
        with cls._lock:
            entry = cls._pool.get(key)
            if entry is None:
                entry = {"if": None, "ts": 0.0, "lock": threading.Lock()}
                cls._pool[key] = entry

        # lock por conexión
        with entry["lock"]:
            iface = entry["if"]
            if not cls._is_alive(iface):
                iface = cls._connect_fresh(host, port, **kwargs)
                entry["if"] = iface
            entry["ts"] = time.time()
            return iface

    @staticmethod
    def _is_alive(iface) -> bool:
        """
        Comprueba si la interfaz sigue utilizable:
        1) Si existe bool iface.isConnected → úsalo.
        2) Si no, valida socket abierto y (si existe) hilo RX vivo.
        """
        try:
            if iface is None:
                return False

            # 0) Algunas versiones exponen isConnected (bool)
            is_conn = getattr(iface, "isConnected", None)
            if isinstance(is_conn, bool):
                return is_conn

            # 1) Socket abierto
            s = getattr(iface, "socket", None)
            if not s or getattr(s, "_closed", False):
                return False

            # 2) Hilo RX (si existe)
            rx = getattr(iface, "_rxThread", None)
            return (rx is None) or rx.is_alive()
        except Exception:
            return False

    @staticmethod
    def _post_connect_warmup(iface) -> None:
        """
        Warm-up compatible tras construir la TCPInterface:
        - waitForConnected(timeout=10) si existe (no propaga timeouts)
        - waitForConfigComplete(timeout=15) si existe (no propaga timeouts)
        - sendHeartbeat() opcional para "despertar"
        """
        import time as _t
        import logging as _lg

        # 1) Conexión de enlace
        try:
            wfc = getattr(iface, "waitForConnected", None)
            if callable(wfc):
                try:
                    wfc(timeout=10)
                except Exception as e:
                    _lg.debug("waitForConnected timeout/err: %s", e)
        except Exception as e:
            _lg.debug("waitForConnected fallo/omitido: %s", e)

        # 2) Configuración completa
        try:
            wcc = getattr(iface, "waitForConfigComplete", None)
            if callable(wcc):
                try:
                    wcc(timeout=15)
                except Exception as e:
                    _lg.debug("waitForConfigComplete timeout/err: %s", e)
                    _t.sleep(1.0)
            else:
                _t.sleep(0.7)
        except Exception as e:
            _lg.debug("waitForConfigComplete fallo/omitido: %s", e)

        # 3) Heartbeat opcional
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
        """
        Crea una nueva TCPInterface probando varias firmas según versión de la lib,
        y realiza warm-up sin propagar timeouts.
        """
        import time as _t
        import logging as _lg

        _t.sleep(0.2)

        # Normaliza si nos pasan "host:port" en host
        try:
            if isinstance(host, str) and ":" in host:
                h, p = host.rsplit(":", 1)
                if p.isdigit():
                    if not port or int(port) == 4403:
                        port = int(p)
                    host = h
        except Exception:
            pass


        # Variantes de firma aceptadas por distintas versiones
        variants = [
           {"hostname": host},                   # 1) la más compatible
           {"hostname": host, "port": port},     # 2) con port
           # ↓ solo si todo lo anterior falla, se probará 'host' (por si hay libs viejas que lo piden)
           {"host": host},
           {"host": host, "port": port},
        ]

        last_type_err = None       # TypeError por firma (p.ej. 'host' no aceptado)
        last_conn_err = None       # errores reales de conexión (timeout, refused, etc.)
        iface = None


               # Reintentos suaves de conexión (evita estrellarse por timeouts cortos)
               # Reintentos suaves de conexión (evita estrellarse por timeouts cortos)
        for attempt in range(1, 4):
            for kw in variants:
                try:
                    iface = _TCP_ORIG_CTOR(**kw)
                    break
                except TypeError as e:
                    # firma incompatible (p.ej. 'host' no válido en libs recientes)
                    last_type_err = e
                    continue
                except Exception as e:
                    # error real de conexión (timeout, refused, etc.)
                    last_conn_err = e
                    continue
            if iface is not None:
                break
            _t.sleep(min(0.5 * attempt, 2.0))



        if iface is None:
            # último intento mínimo viable
            try:
                iface = _TCP_ORIG_CTOR(hostname=host)
            except Exception as e:
                # Prioriza el último error REAL de conexión si existe
                if last_conn_err is not None:
                    raise last_conn_err
                # Si no hubo errores de conexión, pero sí de firma, muestra ese
                if last_type_err is not None:
                    raise last_type_err
                # En última instancia, eleva el del último intento
                raise e


        # === [NUEVO] Keepalive TCP en el socket (opcional pero recomendado) ===
        try:
            s = getattr(iface, "socket", None)
            if s:
                import socket
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # Nota: en Windows, los intervalos finos de keepalive requieren ioctl/ctypes (opcional).
                # Esto activa el keepalive con valores por defecto del sistema.
        except Exception:
            pass

        # Warm-up post conexión (no rompe si falta algo)
        try:
            TCPInterfacePool._post_connect_warmup(iface)
        except Exception as e:
            _lg.debug("post_connect_warmup: %s", e)

        return iface
   
    # imports arriba del fichero si no están:
    # import threading, time, socket, struct

    @classmethod
    def reset(cls, host: str, port: int = 4403):
        """
        Invalida la conexión (si existe) para forzar reconstrucción limpia
        en la próxima llamada a get(). Hace cierre 'duro' del socket (RST).
        """
        key = (host, int(port))
        with cls._lock:
            entry = cls._pool.get(key)

        if not entry:
            # si no existía, garantiza que la próxima get() cree una entrada limpia
            with cls._lock:
                cls._pool[key] = {"if": None, "ts": 0.0, "lock": threading.Lock()}
            return

        with entry["lock"]:
            iface = entry.get("if")
            try:
                # intenta llegar al socket crudo dentro de la interfaz
                s = None
                try:
                    s = getattr(iface, "sock", None) or getattr(iface, "_sock", None) or getattr(iface, "socket", None)
                except Exception:
                    s = None

                if s:
                    try:
                        # SO_LINGER(1,0) → RST inmediato al cerrar
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

                if iface and hasattr(iface, "close"):
                    try:
                        iface.close()
                    except Exception:
                        pass
                try:
                    if iface and hasattr(iface, "signal_disconnect"):
                        iface.signal_disconnect()
                except Exception:
                    pass
            finally:
                entry["if"] = None
                entry["ts"] = 0.0
                with cls._lock:
                    cls._pool[key] = entry

        # pequeña espera para que el peer libere recursos
        try:
            time.sleep(0.2)
        except Exception:
            pass

   
# imports necesarios arriba del fichero (si no están ya):
# import threading, time, socket, struct

    @classmethod
    def shutdown(cls):
        """
        Cierra y limpia TODAS las entradas del pool de forma segura.
        Pensado para llamarse en atexit.
        """
        try:
            # Tomamos snapshot de claves para no mutar el dict mientras iteramos
            with cls._lock:
                keys = list(getattr(cls, "_pool", {}).keys())
        except Exception:
            keys = []

        for key in keys:
            try:
                host, port = key
            except Exception:
                continue
            try:
                # Reutilizamos el cierre 'duro' de reset por cada entrada
                cls.reset(host, int(port))
            except Exception:
                pass

        # Como extra: vaciar el pool
        try:
            with cls._lock:
                cls._pool.clear()
        except Exception:
            pass


# === NUEVO: helper de compatibilidad con el bot ===
def get_tcp_pool(host: str, port: int = 4403):
    """
    Devuelve un objeto ligero con:
      - iface: la TCPInterface activa (el pool la crea/reusa)
      - get_adapter(): idem (compatibilidad)
      - client: alias a iface
    """
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
