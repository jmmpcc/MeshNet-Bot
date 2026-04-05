#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge_in_broker.py V6.2.2 — Pasarela A<->B embebida en el broker usando lógica "peer-safe".

Problemas reales observados:
- Error falso: 'Timed out waiting for connection completion' aunque el TCP real esté vivo.
- Mensajes A->B perdidos si B está en 'offline suppression' (se descartaban).

Solución aplicada aquí:
1) NUNCA llama a sendText() desde callbacks RX del pubsub.
   - Todo envío pasa por una cola TX y un único hilo worker.
2) Cuando B está offline (peer_offline_until), A->B NO se descarta:
   - Se difiere y se envía cuando B vuelve online.
3) Reintentos configurables por env vars con backoff.
4) Hook opcional: on_force_reconnect_a(reason)
   - Si B->A falla repetidamente, se pide al broker que recupere A (FORCE_RECONNECT interno).
5) Caso especial (solución "real"):
   - Si aparece "Timed out waiting for connection completion", NO marcar OFFLINE 75s.
     En su lugar: soft-reset de iface_b y reintento corto.

Se mantiene:
- Anti-bucle (tag) y anti-eco (dedup global por contenido).
- Rate-limit por lado.
- Reconexión suave a B con un hilo aparte.
- Watchdog opcional (stale + TCP probe).
"""

from __future__ import annotations

import os
import time
import json
import threading
import re
import hashlib
import heapq
import itertools
from collections import deque
from typing import Optional, Dict, Callable, Any, Tuple

import meshtastic  # necesario para parchear setInterface en modo embebido
from pubsub import pub

# B usa SDK directo (interfaz secundaria dentro del proceso)
# from meshtastic.tcp_interface import TCPInterface as SDKTCPIF
# B usa pool persistente (mismo endurecimiento que A: keepalive, reconexión, etc.)
from tcpinterface_persistent import TCPInterface as PoolTCPIF


def _truthy(s: str | None, default: bool = False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in {"1", "true", "t", "yes", "y", "on", "si", "sí"}


def _parse_ch_map(s: str | None) -> dict[int, int]:
    out: dict[int, int] = {}
    if not s:
        return out
    for part in s.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        a, b = part.split(":", 1)
        try:
            out[int(a.strip())] = int(b.strip())
        except Exception:
            continue
    return out


def _norm_text(s: str) -> str:
    if not s:
        return ""
    rep = {'“': '"', '”': '"', '’': "'", '‘': "'", '—': '-', '–': '-', '…': '...', "\u00A0": " "}
    s = s.translate(str.maketrans(rep))
    s = re.sub(r"\s+", " ", s).strip()
    return s


_RE_BRIDGE_TAG = re.compile(r"\s*\[BRIDGE[^\]]*\]\s*", re.IGNORECASE)


def _norm_payload(payload: str) -> str:
    """Normaliza para deduplicación: quita tags [BRIDGE ...] y colapsa espacios."""
    t = (payload or "").strip()
    t = _RE_BRIDGE_TAG.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _hash_key(payload: str) -> str:
    """Dedup GLOBAL por contenido (no depende de ch/dirección)."""
    h = hashlib.sha256()
    h.update(_norm_payload(payload).encode("utf-8"))
    return h.hexdigest()


class _RateLimiter:
    """Rate-limit simple por lado: X envíos por minuto."""

    def __init__(self, max_per_min: int = 8):
        self.max = max(1, int(max_per_min))
        self.ts = deque()

    def allow(self) -> bool:
        now = time.time()
        while self.ts and (now - self.ts[0]) > 60.0:
            self.ts.popleft()
        if len(self.ts) < self.max:
            self.ts.append(now)
            return True
        return False


class _DedupWindow:
    """Ventana TTL para dedup global."""

    def __init__(self, ttl_sec: int = 45):
        self.ttl = max(5, int(ttl_sec))
        self.store: Dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        for k in list(self.store.keys()):
            if (now - self.store[k]) > self.ttl:
                self.store.pop(k, None)
        if key in self.store:
            return True
        self.store[key] = now
        return False


class BrokerEmbeddedBridge:
    """
    Pasarela A<->B embebida.
    - iface_a: interfaz principal del broker (A), ya conectada.
    - iface_b: interfaz secundaria creada aquí (B).
    """

    def __init__(
        self,
        iface_a: Any,
        b_host: str,
        b_port: int,
        a2b_map: dict[int, int],
        b2a_map: dict[int, int],
        forward_text: bool,
        forward_position: bool,
        require_ack: bool,
        rate_limit_per_side: int,
        dedup_ttl: int,
        tag_bridge: str = "[BRIDGE]",
        tag_bridge_a2b: Optional[str] = None,
        tag_bridge_b2a: Optional[str] = None,
        on_force_reconnect_a: Optional[Callable[[str], None]] = None,
    ):
        self.iface_a = iface_a
        self.iface_b = None
        self.b_host = (b_host or "").strip()
        self.b_port = int(b_port or 4403)

        self.a2b_map = a2b_map or {}
        self.b2a_map = b2a_map or {}

        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        self.tag_bridge = tag_bridge or "[BRIDGE]"
        self.tag_bridge_a2b = tag_bridge_a2b
        self.tag_bridge_b2a = tag_bridge_b2a

        self._rl_a2b = _RateLimiter(rate_limit_per_side)
        self._rl_b2a = _RateLimiter(rate_limit_per_side)
        self._dedup = _DedupWindow(dedup_ttl)

        # --- peer/offline suppression (solo A->B) ---
        self.peer_offline_until: float = 0.0
        self._peer_lock = threading.Lock()

        # --- reconexión a B ---
        self._reconnect_lock = threading.Lock()
        self._reconnect_thread_active = False

        # --- callback para recuperar A (broker) ---
        self._on_force_reconnect_a = on_force_reconnect_a
        self._a_recover_last_ts = 0.0

        # --- TX queue (priority by due) ---
        self._pq: list[tuple[float, int, dict]] = []
        self._pq_seq = itertools.count()
        self._pq_cv = threading.Condition()
        self._tx_thread = None
        self._running = False

        # Reintentos/backoff (env)
        self._max_retries_a2b = int(os.getenv("BRIDGE_RETRIES_A2B", "3") or "3")
        self._max_retries_b2a = int(os.getenv("BRIDGE_RETRIES_B2A", "2") or "2")

        # Watchdog opcional: si no entra RX desde B durante BRIDGE_B_STALE_SEC, forzamos peer-down
        self._last_rx_b_ts = 0.0
        self._last_probe_b_ts = 0.0
        self._wd_stop = threading.Event()
        self._wd_thread = None

        # schedule de reintentos: segundos
        sched = (os.getenv("BRIDGE_RETRY_SCHEDULE", "2,6,15") or "2,6,15").strip()
        try:
            self._retry_schedule = [max(1.0, float(x.strip())) for x in sched.split(",") if x.strip()]
        except Exception:
            self._retry_schedule = [2.0, 6.0, 15.0]
        if not self._retry_schedule:
            self._retry_schedule = [2.0, 6.0, 15.0]

        # offline suppression por defecto (cuando B falla)
        self._peer_suppress_secs = int(os.getenv("BRIDGE_PEER_SUPPRESS_SECS", "75") or "75")

        # tiempo mínimo entre requests de recover A
        self._recover_a_min_gap = float(os.getenv("BRIDGE_RECOVER_A_MIN_GAP", "12") or "12")

        # Identificador de interfaz "secundaria" para evitar que meshtastic la trate como principal
        self._iface_b_setinterface = _truthy(os.getenv("BRIDGE_B_SETINTERFACE", "0"), False)

        # Expiración de mensajes en cola (wall-clock). 0 = nunca expira.
        self._msg_expiry_secs = float(os.getenv("BRIDGE_MSG_EXPIRY_SECS", "0") or "0")

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # 1) levantar B
        self._connect_b()

        # 2) suscripción pubsub (un único handler para ambos)
        pub.subscribe(self._on_rx, "meshtastic.receive")

        # 3) hilo TX
        self._tx_thread = threading.Thread(target=self._tx_worker, name="bridge-tx", daemon=True)
        self._tx_thread.start()

        self._start_watchdog()

    def stop(self) -> None:
        self._running = False
        self._stop_watchdog()

        try:
            pub.unsubscribe(self._on_rx, "meshtastic.receive")
        except Exception:
            pass
        with self._pq_cv:
            self._pq_cv.notify_all()
        try:
            if self.iface_b and hasattr(self.iface_b, "close"):
                self.iface_b.close()
        except Exception:
            pass

    def is_running(self) -> bool:
        return bool(self._running)

    def status(self) -> dict:
        return {
            "running": self._running,
            "b_host": self.b_host,
            "b_port": self.b_port,
            "peer_offline_for": max(0, int(self.peer_offline_until - time.time())),
            "iface_b": bool(self.iface_b),
            "queue_len": len(self._pq),
        }

    # ----------------------------------------------------------------------------------
    # Helpers peer/offline + reconnect
    # ----------------------------------------------------------------------------------

    def _is_false_offline_error(self, e: BaseException) -> bool:
        """Detecta el error "falso OFFLINE" del SDK."""
        try:
            msg = str(e) or ""
        except Exception:
            msg = ""
        m = msg.lower()
        needles = (
            "timed out waiting for connection completion",
            "connection completion",
            "waiting for connection completion",
            "connection not completed",
        )
        return any(n in m for n in needles)

    def _soft_reset_b(self, why: str) -> None:
        """Reset suave de B sin marcar OFFLINE 75s (evita flapping)."""
        try:
            if self.iface_b and hasattr(self.iface_b, "close"):
                self.iface_b.close()
        except Exception:
            pass
        self.iface_b = None
        print(f"[bridge] B SOFT-RESET (sin OFFLINE): {why}", flush=True)
        self._schedule_reconnect_b()

    def _is_peer_suppressed(self) -> bool:
        with self._peer_lock:
            return time.time() < float(self.peer_offline_until or 0.0)

    def _mark_peer_down(self, why: str) -> None:
        with self._peer_lock:
            self.peer_offline_until = time.time() + float(self._peer_suppress_secs)
        print(f"[bridge] B OFFLINE → suprime A2B {self._peer_suppress_secs}s ({why})", flush=True)
        self._schedule_reconnect_b()

    def _mark_peer_up(self) -> None:
        with self._peer_lock:
            self.peer_offline_until = 0.0
        print("[bridge] B volvió ONLINE → reanudo A2B", flush=True)

    def _schedule_reconnect_b(self) -> None:
        with self._reconnect_lock:
            if self._reconnect_thread_active:
                return
            self._reconnect_thread_active = True

        def _worker():
            try:
                time.sleep(1.0)
                while self._running:
                    ok = self._connect_b()
                    if ok:
                        self._mark_peer_up()
                        print("[bridge] reconexión a B OK", flush=True)
                        return
                    time.sleep(3.0)
            finally:
                with self._reconnect_lock:
                    self._reconnect_thread_active = False

        threading.Thread(target=_worker, name="bridge-reconnect-b", daemon=True).start()

    def _connect_b(self) -> bool:
        """Conecta/reconecta B de forma robusta para operación 24/7."""
        if not self.b_host:
            return False

        try:
            if self.iface_b and hasattr(self.iface_b, "close"):
                self.iface_b.close()
        except Exception:
            pass
        finally:
            self.iface_b = None

        try:
            from tcpinterface_persistent import TCPInterfacePool  # type: ignore
            TCPInterfacePool.reset(self.b_host, int(self.b_port))
        except Exception:
            pass

        if not self._iface_b_setinterface:
            try:
                orig = getattr(meshtastic, "setInterface", None)
                if callable(orig):
                    meshtastic.setInterface = lambda *a, **k: None
                try:
                    self.iface_b = PoolTCPIF(hostname=self.b_host, port=self.b_port)
                finally:
                    if callable(orig):
                        meshtastic.setInterface = orig
            except Exception:
                self.iface_b = None
                return False
        else:
            try:
                self.iface_b = PoolTCPIF(hostname=self.b_host, port=self.b_port)
            except Exception:
                self.iface_b = None
                return False

        try:
            if getattr(self.iface_b, "localNode", None) is not None:
                return True

            wfc = getattr(self.iface_b, "waitForConnected", None)
            if callable(wfc):
                try:
                    wfc(timeout=6)
                except TypeError:
                    wfc()

            wcfg = getattr(self.iface_b, "waitForConfigComplete", None)
            if callable(wcfg):
                try:
                    wcfg(timeout=8)
                except TypeError:
                    wcfg()

            if getattr(self.iface_b, "localNode", None) is None:
                raise RuntimeError("Conexión B incompleta (localNode no disponible)")

            return True

        except Exception:
            try:
                if self.iface_b and hasattr(self.iface_b, "close"):
                    self.iface_b.close()
            except Exception:
                pass
            self.iface_b = None
            return False

    def _request_a_recover(self, reason: str) -> None:
        cb = self._on_force_reconnect_a
        if not cb:
            return
        now = time.time()
        if (now - self._a_recover_last_ts) < float(self._recover_a_min_gap):
            return
        self._a_recover_last_ts = now
        try:
            cb(reason)
        except Exception:
            pass

    # ----------------------------------------------------------------------------------
    # Watchdog
    # ----------------------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        try:
            stale = float(os.getenv("BRIDGE_B_STALE_SEC", "0") or "0")
        except Exception:
            stale = 0.0
        if stale <= 0:
            return

        if self._wd_thread and self._wd_thread.is_alive():
            return

        self._wd_stop.clear()

        def _run():
            while not self._wd_stop.is_set():
                try:
                    try:
                        stale_sec = float(os.getenv("BRIDGE_B_STALE_SEC", "0") or "0")
                    except Exception:
                        stale_sec = 0.0
                    if stale_sec <= 0:
                        time.sleep(2.0)
                        continue

                    try:
                        tick = float(os.getenv("BRIDGE_WATCHDOG_TICK", "5") or "5")
                    except Exception:
                        tick = 5.0

                    now = time.time()
                    last = float(self._last_rx_b_ts or 0.0)
                    if last > 0.0 and (now - last) > stale_sec:
                        if not self._is_peer_suppressed():
                            self._mark_peer_down(f"stale_rx>{int(stale_sec)}s")

                    # --- TCP probe opcional ---
                    # Útil cuando hay "silencio" en la malla o cuando la conexión TCP queda medio-colgada:
                    # un connect() rápido (sin enviar payload Meshtastic) suele "despertar" WiFi/ARP y, si falla,
                    # dispara reconexión suave.
                    try:
                        probe_sec = float(os.getenv("BRIDGE_B_TCP_PROBE_SEC", "0") or "0")
                    except Exception:
                        probe_sec = 0.0

                    if probe_sec > 0.0:
                        lastp = float(self._last_probe_b_ts or 0.0)
                        if (now - lastp) >= probe_sec:
                            self._last_probe_b_ts = now
                            ok, why = self._tcp_probe_b()
                            if not ok:
                                # Marca caída para activar supresión + reconector
                                self._mark_peer_down(f"tcp_probe_fail: {why}")
                            else:
                                # Si el puerto responde pero la iface aún no está lista, intenta reconectar.
                                if self.iface_b is None:
                                    self._schedule_reconnect_b()

                    time.sleep(max(1.0, tick))
                except Exception:
                    time.sleep(2.0)

        self._wd_thread = threading.Thread(target=_run, name="bridge_b_watchdog", daemon=True)
        self._wd_thread.start()

    def _stop_watchdog(self) -> None:
        try:
            self._wd_stop.set()
        except Exception:
            pass
        try:
            th = self._wd_thread
            if th and th.is_alive():
                th.join(timeout=2.0)
        except Exception:
            pass

    def _tcp_probe_b(self) -> Tuple[bool, str]:
        """
        Sonda TCP mínima contra B (host:port).
        - No usa el SDK Meshtastic.
        - No manda ningún paquete a la radio.
        - Sirve para:
            * detectar rápido sockets colgados o rutas WiFi muertas
            * refrescar ARP/WiFi en entornos donde un simple tráfico "despierta" el enlace

        Control por env vars:
          - BRIDGE_B_TCP_PROBE_TIMEOUT  (segundos, default 2)
        """
        import socket

        host = (self.b_host or "").strip()
        port = int(self.b_port or 4403)
        if not host:
            return False, "no_host"

        try:
            timeout = float(os.getenv("BRIDGE_B_TCP_PROBE_TIMEOUT", "2") or "2")
        except Exception:
            timeout = 2.0

        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                try:
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
            return True, "ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ----------------------------------------------------------------------------------
    # TX Queue
    # ----------------------------------------------------------------------------------

    def _enqueue(self, *, direction: str, ch: int, msg: str, want_ack: bool, due: Optional[float] = None) -> None:
        """
        Encola un envío. No guarda iface y no descarta por iface None.
        La resolución de iface y la tolerancia a reconexión ocurre en _send_item().
        """
        if not self._running:
            return

        item = {
            "direction": direction,
            "ch": int(ch),
            "msg": msg,
            "want_ack": bool(want_ack),
            "attempt": 0,
            "max_retries": int(self._max_retries_a2b if direction == "A2B" else self._max_retries_b2a),
            "queued_at": time.time(),
        }

        if due is None:
            due = time.time()

        with self._pq_cv:
            heapq.heappush(self._pq, (float(due), next(self._pq_seq), item))
            self._pq_cv.notify()

    def _reschedule(self, item: dict, delay: float) -> None:
        item["attempt"] = int(item.get("attempt", 0)) + 1
        due = time.time() + float(delay)
        with self._pq_cv:
            heapq.heappush(self._pq, (due, next(self._pq_seq), item))
            self._pq_cv.notify()

    def _defer(self, item: dict, delay: float) -> None:
        """Re-encola item con delay SIN incrementar attempt. Usar para aplazamientos
        que no son fallos de envío reales (peer offline, iface en reconexión)."""
        due = time.time() + float(delay)
        with self._pq_cv:
            heapq.heappush(self._pq, (due, next(self._pq_seq), item))
            self._pq_cv.notify()

    def _tx_worker(self) -> None:
        while self._running:
            with self._pq_cv:
                while self._running and not self._pq:
                    self._pq_cv.wait(timeout=1.0)
                if not self._running:
                    break

                due, _, item = self._pq[0]
                now = time.time()
                if due > now:
                    self._pq_cv.wait(timeout=min(1.0, due - now))
                    continue
                heapq.heappop(self._pq)

            # A->B: si sigue offline, difiere hasta peer_offline_until (sin consumir reintento)
            if item["direction"] == "A2B" and self._is_peer_suppressed():
                remaining = max(0, float(self.peer_offline_until - time.time()))
                self._defer(item, max(0.5, remaining))
                continue

            self._send_item(item)

    def _send_item(self, item: dict) -> None:
        """
        Envía un item de la cola TX.

        Cambio estratégico 24/7:
        - NO usa item["iface"] (puede quedar obsoleta tras reconexión).
        - Resuelve iface justo antes de enviar (self.iface_b / self.iface_a).
        - Si iface es None durante reconexión, reprograma con backoff sin reventar.

        Mantiene:
        - rate-limit A2B/B2A
        - lógica de reintentos con _retry_schedule
        - _mark_peer_down en fallos A2B
        - _request_a_recover en fallos B2A
        """

        direction = item["direction"]
        ch = int(item["ch"])
        msg = item["msg"]
        want_ack = bool(item["want_ack"])

        # Rate-limit (antes de intentar)
        if direction == "A2B" and not self._rl_a2b.allow():
            self._reschedule(item, 2.0)
            return
        if direction == "B2A" and not self._rl_b2a.allow():
            self._reschedule(item, 2.0)
            return

        # === CAMBIO CLAVE: resolver iface en este instante ===
        iface = self.iface_b if direction == "A2B" else self.iface_a

        # Si iface aún no está lista: distinguir reconexión en curso (A2B) de fallo real (B2A).
        if iface is None or not hasattr(iface, "sendText"):
            if direction == "A2B":
                # iface None significa reconexión en curso, NO es un fallo de envío real.
                # Aseguramos que el reconector esté activo.
                if not self._is_peer_suppressed():
                    self._schedule_reconnect_b()

                # Expiración wall-clock opcional (BRIDGE_MSG_EXPIRY_SECS, 0=nunca expira).
                expiry = float(self._msg_expiry_secs or 0.0)
                if expiry > 0:
                    age = time.time() - float(item.get("queued_at", time.time()))
                    if age > expiry:
                        print(
                            f"[bridge] A2B ch {item.get('ch')} EXPIRED tras {int(age)}s en cola "
                            f"(BRIDGE_MSG_EXPIRY_SECS={int(expiry)}), descartado.",
                            flush=True,
                        )
                        return

                # No consumir reintento: diferir hasta que B reconecte.
                delay = self._retry_schedule[min(
                    int(item.get("attempt", 0)), len(self._retry_schedule) - 1
                )]
                self._defer(item, delay)
            else:
                # B2A: iface_a None es excepcional; consumimos reintento normalmente.
                attempt = int(item.get("attempt", 0))
                max_r = int(item.get("max_retries", 0))
                if attempt < max_r:
                    delay = self._retry_schedule[min(attempt, len(self._retry_schedule) - 1)]
                    self._reschedule(item, delay)
                else:
                    print("[bridge] B2A sendText ERROR (agotado): iface None", flush=True)
            return

        try:
            # Firmas compatibles: sendText(text, channelIndex=?, wantAck=?)
            fn = getattr(iface, "sendText", None)
            if not callable(fn):
                raise RuntimeError("iface.sendText no disponible")
           
            # === [NUEVO] Retardo opcional SOLO en A->B (emisión por nodo B embebido) ===
            # Variable de entorno:
            #   BRIDGE_B_TX_DELAY_MS=0  (por defecto sin retardo)
            # Ejemplo:
            #   BRIDGE_B_TX_DELAY_MS=350  -> 350ms antes de cada TX hacia B
            if direction == "A2B":
                try:
                    d_ms = float(os.getenv("BRIDGE_B_TX_DELAY_MS", "0") or "0")
                except Exception:
                    d_ms = 0.0

                # Evita valores absurdos que bloqueen el worker: cap a 10s
                if d_ms > 0:
                    time.sleep(min(10.0, d_ms / 1000.0))

            try:
                fn(msg, channelIndex=ch, wantAck=want_ack)
            except TypeError:
                # SDKs viejos
                try:
                    fn(msg, channelIndex=ch)
                except TypeError:
                    fn(msg, ch)

            if direction == "A2B":
                print(f"[bridge] A2B ch {ch} txt OK", flush=True)
            else:
                print(f"[bridge] B2A ch {ch} txt OK", flush=True)
            return

        except Exception as e:
            # decide reintento / marca offline / pide recover A
            attempt = int(item.get("attempt", 0))
            max_r = int(item.get("max_retries", 0))

            if direction == "A2B":
                # Solución real: NO marcar OFFLINE 75s por "connection completion".
                if self._is_false_offline_error(e):
                    self._soft_reset_b(f"{type(e).__name__}: {e}")
                    if attempt < max_r:
                        self._reschedule(item, 2.0)
                    else:
                        print(f"[bridge] A2B sendText ERROR (agotado tras soft-reset): {type(e).__name__}: {e}", flush=True)
                    return

                self._mark_peer_down(f"{type(e).__name__}: {e}")
                if attempt < max_r:
                    delay = self._retry_schedule[min(attempt, len(self._retry_schedule) - 1)]
                    self._reschedule(item, delay)
                else:
                    print(f"[bridge] A2B sendText ERROR (agotado): {type(e).__name__}: {e}", flush=True)
                return

            # B2A
            if attempt < max_r:
                delay = self._retry_schedule[min(attempt, len(self._retry_schedule) - 1)]
                print(f"[bridge] B2A sendText ERROR: {type(e).__name__}: {e} (retry {attempt+1}/{max_r})", flush=True)
                self._reschedule(item, delay)
                # Si el error es de conexión, pedimos recover de A (suave)
                self._request_a_recover(f"B2A sendText error: {type(e).__name__}: {e}")
            else:
                print(f"[bridge] B2A sendText ERROR (agotado): {type(e).__name__}: {e}", flush=True)
                self._request_a_recover(f"B2A sendText exhausted: {type(e).__name__}: {e}")

    # ----------------------------------------------------------------------------------
    # RX handler
    # ----------------------------------------------------------------------------------

    def _on_rx(self, interface=None, packet=None, **kwargs):
        """Handler único: recibe tráfico de A y B y decide si reenviar. Aquí NO se envía; solo se encola."""
        try:
            if not self._running or not packet:
                return

            iface = interface
            is_from_a = (iface is self.iface_a)
            is_from_b = (iface is self.iface_b)

            if is_from_b:
                try:
                    self._last_rx_b_ts = time.time()
                except Exception:
                    pass

            if not is_from_a and not is_from_b:
                return

            decoded = packet.get("decoded") or {}
            portnum = decoded.get("portnum")
            payload = decoded.get("payload")
            ch = int(packet.get("channel") or packet.get("channelIndex") or 0)

            # TEXT
            if portnum == "TEXT_MESSAGE_APP" and self.forward_text:
                try:
                    if isinstance(payload, (bytes, bytearray)):
                        text = payload.decode("utf-8", "ignore")
                    elif isinstance(payload, str):
                        text = payload
                    else:
                        text = str(payload or "")
                except Exception:
                    text = str(payload or "")

                text = _norm_text(text)
                if not text:
                    return

                if self.tag_bridge and self.tag_bridge in text:
                    return

                direction = "A2B" if is_from_a else "B2A"
                out_map = self.a2b_map if direction == "A2B" else self.b2a_map
                out_ch = out_map.get(ch, ch)

                key = _hash_key(text)
                if self._dedup.seen(key):
                    return

                tag = self.tag_bridge_a2b if direction == "A2B" else self.tag_bridge_b2a
                if tag:
                    text = f"{text} {tag}"
                elif self.tag_bridge:
                    text = f"{text} {self.tag_bridge}"

                due = None
                if direction == "A2B" and self._is_peer_suppressed():
                    remaining = max(0, int(self.peer_offline_until - time.time()))
                    print(f"[bridge] A2B ch {ch}->{out_ch} DEFER (B offline, {remaining}s restantes)", flush=True)
                    due = float(self.peer_offline_until)

                self._enqueue(direction=direction, ch=out_ch, msg=text, want_ack=self.require_ack, due=due)
                return

            # POSITION (opcional)
            if portnum == "POSITION_APP" and self.forward_position:
                try:
                    pos = decoded.get("position") or {}
                    lat = pos.get("latitudeI")
                    lon = pos.get("longitudeI")
                    if lat is None or lon is None:
                        return
                    lat_f = float(lat) / 1e7
                    lon_f = float(lon) / 1e7
                    msg = f"POS {lat_f:.6f},{lon_f:.6f} {self.tag_bridge}"
                except Exception:
                    return

                direction = "A2B" if is_from_a else "B2A"
                out_map = self.a2b_map if direction == "A2B" else self.b2a_map
                out_ch = out_map.get(ch, ch)

                key = _hash_key(msg)
                if self._dedup.seen(key):
                    return

                due = None
                if direction == "A2B" and self._is_peer_suppressed():
                    due = float(self.peer_offline_until)

                self._enqueue(direction=direction, ch=out_ch, msg=msg, want_ack=self.require_ack, due=due)
                return

        except Exception:
            return

    # ----------------------------------------------------------------------------------
    # API usada por el broker para espejar envíos locales (A -> B)
    # ----------------------------------------------------------------------------------

    def mirror_from_a(self, ch: int, text: str) -> bool:
        """Encola un envío A->B originado por el broker/bot."""
        try:
            if not self._running:
                return False
            if not self.forward_text:
                return False

            text = _norm_text(text or "")
            if not text:
                return False
            if self.tag_bridge and self.tag_bridge in text:
                return False

            out_ch = self.a2b_map.get(int(ch), int(ch))

            key = _hash_key(text)
            if self._dedup.seen(key):
                return False

            tag = self.tag_bridge_a2b or self.tag_bridge
            msg = f"{text} {tag}".strip()

            due = None
            if self._is_peer_suppressed():
                remaining = max(0, int(self.peer_offline_until - time.time()))
                print(f"[bridge] A2B ch {ch}->{out_ch} DEFER (B offline, {remaining}s restantes)", flush=True)
                due = float(self.peer_offline_until)

            self._enqueue(direction="A2B", ch=out_ch, msg=msg, want_ack=self.require_ack, due=due)
            return True
        except Exception:
            return False


# API mínima para el broker
_BRIDGE: Optional[BrokerEmbeddedBridge] = None


def bridge_start_in_broker(iface_a, on_force_reconnect_a: Optional[Callable[[str], None]] = None) -> dict:
    """Arranca la pasarela tomando la iface A del broker ya conectada."""
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        return {"ok": True, "already_running": True, "status": _BRIDGE.status()}

    b_host = os.getenv("BRIDGE_B_HOST") or os.getenv("B_HOST") or ""
    b_port = int(os.getenv("BRIDGE_B_PORT", os.getenv("B_PORT", "4403")) or "4403")
    if not b_host:
        raise RuntimeError("BRIDGE_B_HOST/B_HOST no definido")

    a2b = _parse_ch_map(os.getenv("BRIDGE_A2B_CH_MAP", os.getenv("A2B_CH_MAP", "0:0")))
    b2a = _parse_ch_map(os.getenv("BRIDGE_B2A_CH_MAP", os.getenv("B2A_CH_MAP", "0:0")))

    forward_text = _truthy(os.getenv("BRIDGE_FORWARD_TEXT", os.getenv("FORWARD_TEXT", "1")), True)
    forward_position = _truthy(os.getenv("BRIDGE_FORWARD_POSITION", os.getenv("FORWARD_POSITION", "0")), False)
    require_ack = _truthy(os.getenv("BRIDGE_REQUIRE_ACK", os.getenv("REQUIRE_ACK", "0")), False)
    rate = int(os.getenv("BRIDGE_RATE_LIMIT_PER_SIDE", os.getenv("RATE_LIMIT_PER_SIDE", "8")) or "8")
    dedup = int(os.getenv("BRIDGE_DEDUP_TTL", os.getenv("DEDUP_TTL", "45")) or "45")

    tag_base = os.getenv("TAG_BRIDGE", "[BRIDGE]")
    tag_a2b = os.getenv("TAG_BRIDGE_A2B", "").strip() or None
    tag_b2a = os.getenv("TAG_BRIDGE_B2A", "").strip() or None

    _BRIDGE = BrokerEmbeddedBridge(
        iface_a=iface_a,
        b_host=b_host,
        b_port=b_port,
        a2b_map=a2b,
        b2a_map=b2a,
        forward_text=forward_text,
        forward_position=forward_position,
        require_ack=require_ack,
        rate_limit_per_side=rate,
        dedup_ttl=dedup,
        tag_bridge=tag_base,
        tag_bridge_a2b=tag_a2b,
        tag_bridge_b2a=tag_b2a,
        on_force_reconnect_a=on_force_reconnect_a,
    )
    _BRIDGE.start()
    return {"ok": True, "status": _BRIDGE.status()}


def bridge_stop_in_broker() -> dict:
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        _BRIDGE.stop()
        return {"ok": True}
    return {"ok": True, "already_stopped": True}


def bridge_status_in_broker() -> dict:
    if _BRIDGE:
        return _BRIDGE.status()
    return {"running": False}


def bridge_mirror_outgoing_from_broker(channel: int, text: str) -> bool:
    """Espeja un envío local del broker (A) hacia B, si el bridge está activo."""
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        try:
            return _BRIDGE.mirror_from_a(channel, text)
        except Exception as e:
            print(f"[bridge] mirror_from_a ERROR: {type(e).__name__}: {e}", flush=True)
    return False
