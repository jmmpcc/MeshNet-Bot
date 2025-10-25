# === broker_resilience.py ===
# NUEVO fichero - utilidades de resiliencia para el broker
# Version v6.1

import time
import threading
import queue
from typing import Callable, Dict, Any, Optional


class CircuitBreaker:
    """
    Abre el circuito si en una ventana móvil se exceden N errores,
    y lo cierra tras open_secs + umbral de éxitos en estado half-open.
    """
    def __init__(self, max_errors=5, window_secs=30, open_secs=90, halfopen_successes=3):
        self.max_errors = max_errors
        self.window_secs = window_secs
        self.open_secs = open_secs
        self.halfopen_successes = halfopen_successes
        self._errors = []
        self._opened_at = 0.0
        self._halfopen_ok = 0
        self._state = "closed"  # closed | open | half-open
        self._lock = threading.Lock()

    def _prune(self, now: float):
        cutoff = now - self.window_secs
        self._errors = [t for t in self._errors if t >= cutoff]

    def record_error(self):
        with self._lock:
            now = time.time()
            self._errors.append(now)
            self._prune(now)
            if self._state == "closed" and len(self._errors) >= self.max_errors:
                self._state = "open"
                self._opened_at = now
                self._halfopen_ok = 0

    def record_success(self):
        with self._lock:
            if self._state == "half-open":
                self._halfopen_ok += 1
                if self._halfopen_ok >= self.halfopen_successes:
                    self._state = "closed"
                    self._errors.clear()
                    self._opened_at = 0.0

    def can_attempt(self) -> bool:
        with self._lock:
            now = time.time()
            if self._state == "open":
                if (now - self._opened_at) >= self.open_secs:
                    self._state = "half-open"
                    self._halfopen_ok = 0
                    return True
                return False
            return True

    @property
    def state(self):
        with self._lock:
            return self._state


class Watchdog:
    """
    Dispara una callback si no hay 'latidos' (beat()) durante idle_secs.
    No mata hilos: pide reapertura suave y se rearma.
    """
    def __init__(self, idle_secs=120, on_starvation: Optional[Callable[[], None]] = None):
        self.idle_secs = idle_secs
        self._on_starvation = on_starvation
        self._last_seen = time.time()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)

    def beat(self):
        self._last_seen = time.time()

    def start(self):
        if not self._thr.is_alive():
            self._stop.clear()
            self._thr.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            time.sleep(2.0)
            if (time.time() - self._last_seen) > self.idle_secs:
                try:
                    if self._on_starvation:
                        self._on_starvation()
                finally:
                    # Rearma para evitar bucles de disparo continuo
                    self._last_seen = time.time()


class SendQueue:
    """
    Cola de envío con backpressure y coalescing opcional.
    - set_sender(fn): fn(msg_dict) -> bool (True si envío OK)
    - offer(msg, coalesce=True): encola mensaje; coalesce compacta por key.
    """
    def __init__(self, maxsize=200, coalesce_keys=("destination", "channel", "type")):
        self.q = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._coalesce_keys = tuple(coalesce_keys) if coalesce_keys else tuple()
        self._pending: Dict[tuple, Dict[str, Any]] = {}
        self._send_func: Optional[Callable[[Dict[str, Any]], bool]] = None
        self._on_error: Optional[Callable[[Exception], None]] = None
        self._on_success: Optional[Callable[[], None]] = None

    def set_sender(self, fn: Callable[[Dict[str, Any]], bool]):
        self._send_func = fn

    def on_error(self, fn: Callable[[Exception], None]):
        self._on_error = fn

    def on_success(self, fn: Callable[[], None]):
        self._on_success = fn

    def start(self):
        if not self._thr.is_alive():
            self._stop.clear()
            self._thr.start()

    def stop(self):
        self._stop.set()

    def offer(self, msg: Dict[str, Any], coalesce: bool = True):
        if coalesce and self._coalesce_keys:
            key = tuple(msg.get(k) for k in self._coalesce_keys)
            self._pending[key] = msg
            try:
                self.q.put_nowait(("flush", None))
            except queue.Full:
                pass
        else:
            self.q.put(("direct", msg))

    def _run(self):
        while not self._stop.is_set():
            try:
                kind, payload = self.q.get(timeout=0.5)
            except queue.Empty:
                continue

            if kind == "direct":
                self._try_send(payload)
            elif kind == "flush":
                items = list(self._pending.items())
                self._pending.clear()
                for _, m in items:
                    self._try_send(m)

    def _try_send(self, m: Dict[str, Any]):
        if not self._send_func:
            return
        try:
            ok = self._send_func(m)
            if ok and self._on_success:
                self._on_success()
        except Exception as e:
            if self._on_error:
                self._on_error(e)


# Escritura robusta: reemplaza usos directos de socket.send por esta utilidad
def robust_sendall(sock, data: bytes, timeout: float = 5.0):
    sock.settimeout(timeout)
    total = 0
    n = len(data)
    while total < n:
        sent = sock.send(data[total:])
        if sent == 0:
            raise ConnectionResetError("socket closed during send")
        total += sent
