#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mesh_triple_bridge.py  v6.2.5 — Pasarela externa A↔B y A↔C usando TCP Meshtastic.

Modo tcp:
- Abre TCP directo a A, B y C.
- Reenvía A→B, A→C, B→A, C→A según mapas.

Modo broker (HUB_MODE=broker):
- NO abre TCP a A (evita colisión con el broker).
- Lee RX de A vía BacklogServer (FETCH_BACKLOG).
- Envía hacia A vía BacklogServer (SEND_TEXT).
- Abre TCP solo a B y C.
"""
from __future__ import annotations

import os
import time
import json
import threading
import re
import hashlib
import socket
from collections import deque
from typing import Optional, Dict
import heapq

from pubsub import pub

# ============================================================
#  Compat TCPInterface: pool persistente si existe, si no SDK
# ============================================================

_TCPI = None
try:
    from tcpinterface_persistent import TCPInterface as _PoolTCPIF  # type: ignore
    _TCPI = _PoolTCPIF
except Exception:
    try:
        from meshtastic.tcp_interface import TCPInterface as _SDKTCPIF  # type: ignore
        _TCPI = _SDKTCPIF
    except Exception as e:
        raise SystemExit(f"[FATAL] No se pudo importar TCPInterface: {e}")

# ============================================================
#  Helpers básicos (mapeos, texto, hash)
# ============================================================


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

# ============================
#  [NUEVO] Filtro BBS (no bridge)
# ============================

def _csv_int_set(raw: str | None) -> set[int]:
    out: set[int] = set()
    s = (raw or "").strip()
    if not s:
        return out
    for part in s.split(","):
        p = (part or "").strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out

# - Por defecto, bloqueamos tráfico BBS en el bridge (más seguro 24/7).
# - Puedes desactivarlo con TRIPLE_BLOCK_BBS=0 si alguna vez lo necesitas.
_TRIPLE_BLOCK_BBS = (os.getenv("TRIPLE_BLOCK_BBS", "1") or "1").strip().lower() in {"1","true","yes","on","si","sí"}

# Canales BBS (compat con tu broker/BBS):
#   - BBS_CHANNELS puede venir como CSV
#   - BBS_CHANNEL como fallback simple
#   - default conservador: 5 (tu histórico)
_BBS_CH_SET = (
    _csv_int_set(os.getenv("BBS_CHANNELS"))
    or _csv_int_set(os.getenv("BBS_CHANNEL"))
    or {5}
)

def _strip_leading_tags(s: str) -> str:
    """
    El bridge puede ver textos con prefijos tipo "[B] ..." o "[BRIDGE] ...".
    Eliminamos tags consecutivos al inicio: "[...]" repetidos + espacios.
    Se limita longitud del contenido del tag para evitar falsos positivos.
    """
    t = (s or "").strip()
    # Quita uno o varios "[...]" al principio, donde "..." tiene 1..24 chars y no contiene ']'
    t = re.sub(r"^(?:\[[^\]\r\n]{1,24}\]\s*)+", "", t).strip()
    return t


def _is_bbs_command(text: str) -> bool:
    """
    Detecta comandos BBS (solicitudes).
    Regla práctica: empieza por '#BBS' (case-insensitive) tras tags/espacios.
    (Incluye compat '@bbs' por si aparece en pruebas antiguas.)
    """
    t = _strip_leading_tags(text)
    tl = t.lower().lstrip()
    return tl.startswith("#bbs") or tl.startswith("@bbs")

def _should_block_bbs_bridge(ch: int, text: str) -> bool:
    """
    Bloquea SOLO comandos (solicitudes) de BBS para que no crucen el bridge.
    - Limitamos por canal para no cortar textos casuales '#bbs...' fuera del canal BBS.
    - Aun así, si quieres bloquear globalmente, pon TRIPLE_BLOCK_BBS_FORCE=1.
    """
    if not _TRIPLE_BLOCK_BBS:
        return False

    force = (os.getenv("TRIPLE_BLOCK_BBS_FORCE", "0") or "0").strip().lower() in {"1","true","yes","on","si","sí"}
    if force:
        return _is_bbs_command(text)

    try:
        ch_i = int(ch)
    except Exception:
        ch_i = 0

    if ch_i in _BBS_CH_SET and _is_bbs_command(text):
        return True

    return False

def _norm_text(s: str) -> str:
    if not s:
        return ""
    rep = {
        "“": '"', "”": '"',
        "’": "'", "‘": "'",
        "—": "-", "–": "-",
        "…": ".",
        "\u00A0": " ",
    }
    s = s.translate(str.maketrans(rep))
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _split_text_chunks(text: str, max_len: int) -> list[str]:
    """
    Divide texto en trozos <= max_len, intentando cortar por espacios.
    Si no encuentra un espacio razonable, corta a longitud fija.
    """
    if text is None:
        return []
    text = str(text).strip()
    if not text:
        return []

    max_len = int(max_len or 160)
    if max_len < 40:
        max_len = 40

    chunks: list[str] = []
    while len(text) > max_len:
        cut = text.rfind(" ", 0, max_len + 1)
        if cut < 20:
            cut = max_len
        chunk = text[:cut].strip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].strip()

    if text:
        chunks.append(text)

    return chunks


def _safe_max_text_len() -> int:
    """
    Límite conservador para evitar 'Data payload too big' por overhead interno.
    Configurable por env TRIPLE_MAX_TEXT_LEN (default 160).
    """
    try:
        v = int((os.getenv("TRIPLE_MAX_TEXT_LEN") or "160").strip())
    except Exception:
        v = 160
    return max(80, min(v, 220))


def _hash_key(direction: str, from_id: str, ch: int, payload: str) -> str:
    h = hashlib.sha256()
    h.update(direction.encode("utf-8"))
    h.update(str(from_id or "?").encode("utf-8"))
    h.update(str(int(ch)).encode("utf-8"))
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


# ============================================================
#  Estado auxiliar: rate-limit y deduplicación
# ============================================================

class RateLimiter:
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


class DedupWindow:
    def __init__(self, ttl_sec: int = 45):
        self.ttl = max(5, int(ttl_sec))
        self.store: Dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        dead = [k for k, ts in self.store.items() if (now - ts) > self.ttl]
        for k in dead:
            self.store.pop(k, None)
        if key in self.store:
            return True
        self.store[key] = now
        return False


# ============================================================
#  Broker Hub (BacklogServer): recibir desde A sin abrir TCP a A
# ============================================================

class BrokerBacklogClient:
    def __init__(self, host: str, port: int, timeout_s: float = 8.0):
        self.host = str(host or "127.0.0.1").strip()
        self.port = int(port or 8766)
        self.timeout_s = float(timeout_s or 8.0)

    def _request(self, payload: dict) -> dict:
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as s:
            s.settimeout(self.timeout_s)
            s.sendall(line)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        try:
            return json.loads(buf.decode("utf-8", "ignore").strip() or "{}")
        except Exception:
            return {"ok": False, "error": "invalid_response"}

    def fetch_backlog(self, since_ts: int | None, portnums: list[str] | None, limit: int = 2000) -> list[dict]:
        req = {
            "cmd": "FETCH_BACKLOG",
            "params": {
                "since_ts": int(since_ts) if since_ts is not None else None,
                "until_ts": None,
                "channel": None,
                "portnums": portnums or ["TEXT_MESSAGE_APP"],
                "limit": int(limit or 2000),
            },
        }
        resp = self._request(req)
        if not resp.get("ok"):
            return []
        data = resp.get("data") or []
        return data if isinstance(data, list) else []

    def send_text(self, text: str, ch: int = 0, dest: str | None = None, ack: bool = False) -> bool:
        params = {"text": str(text or ""), "ch": int(ch or 0)}
        if dest and str(dest).strip() and str(dest).strip().lower() != "broadcast":
            params["dest"] = str(dest).strip()
            params["ack"] = bool(ack)
        req = {"cmd": "SEND_TEXT", "params": params}
        resp = self._request(req)
        return bool(resp.get("ok"))


# ============================================================
#  TripleBridge: A↔B y A↔C
# ============================================================

class TripleBridge:
    """
    Triple bridge "24/7":
      - HUB_MODE=tcp    : conecta A, B y C por TCP y reenvía A<->B, A<->C.
      - HUB_MODE=broker : NO conecta A por TCP. Lee eventos desde el backlog del broker y:
            * A -> (B/C) por TCP (sendText hacia B/C)
            * (B/C) -> A por broker (SEND_TEXT)

    Resiliencia:
      - TX spool con reintentos y "DEFER" cuando un peer está offline.
      - Reconexión automática de B/C cuando cae el TCP.
      - Watchdog opcional por "stale RX" (TRIPLE_B_STALE_SEC / TRIPLE_C_STALE_SEC).
    """

    def __init__(
        self,
        a_host: str,
        a_port: int,
        b_host: str,
        b_port: int,
        c_host: str,
        c_port: int,
        a2b_map: dict[int, int],
        b2a_map: dict[int, int],
        a2c_map: dict[int, int],
        c2a_map: dict[int, int],
        hub_mode: str = "tcp",
        broker_ctrl_host: str | None = None,
        broker_ctrl_port: int | None = None,
        broker_poll_sec: float = 1.5,
        broker_fetch_limit: int = 2000,
        broker_timeout_s: float = 8.0,
        forward_text: bool = True,
        forward_position: bool = False,
        require_ack: bool = False,
        rate_limit_per_side: int = 8,
        dedup_ttl: int = 45,
        tag_bridge: str = "[BRIDGE]",
        tag_bridge_a2b: str | None = None,
        tag_bridge_b2a: str | None = None,
        tag_bridge_a2c: str | None = None,
        tag_bridge_c2a: str | None = None,
        enable_b: bool = True,
        enable_c: bool = True,
    ):
        # --- Peers habilitados por BRIDGE_PEERS ---
        self.enable_b = bool(enable_b)
        self.enable_c = bool(enable_c)

        # --- Hosts ---
        self.a_host, self.a_port = a_host, int(a_port or 4403)
        self.b_host, self.b_port = b_host, int(b_port or 4403)
        self.c_host, self.c_port = c_host, int(c_port or 4403)

        # --- Modo HUB ---
        self.hub_mode = (hub_mode or "tcp").strip().lower()

        # --- Broker backlog (solo en HUB_MODE=broker) ---
        self.broker_ctrl_host = (broker_ctrl_host or os.getenv("BROKER_CTRL_HOST") or "").strip() or None
        try:
            self.broker_ctrl_port = int(broker_ctrl_port or os.getenv("BROKER_CTRL_PORT") or 8766)
        except Exception:
            self.broker_ctrl_port = 8766

        try:
            self.broker_poll_sec = float(broker_poll_sec or os.getenv("BROKER_POLL_SEC") or 1.5)
        except Exception:
            self.broker_poll_sec = 1.5

        try:
            self.broker_fetch_limit = int(broker_fetch_limit or os.getenv("BROKER_FETCH_LIMIT") or 2000)
        except Exception:
            self.broker_fetch_limit = 2000

        try:
            self.broker_timeout_s = float(broker_timeout_s or os.getenv("BROKER_TIMEOUT_S") or 8.0)
        except Exception:
            self.broker_timeout_s = 8.0

        # --- Mapas de canal ---
        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})
        self.a2c_map = dict(a2c_map or {})
        self.c2a_map = dict(c2a_map or {})

        # --- Qué se reenvía ---
        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        # En HUB_MODE=broker, el bridge no puede verificar ACK real por RF.
        # Para evitar reintentos innecesarios y falsas expectativas, forzamos require_ack=False.
        if self.hub_mode == "broker":
            self.require_ack = False

        # Rate-limit específico para replay de backlog (HUB_MODE=broker).
        # Por defecto, lo dejamos igual que rate_limit_per_side, pero puede ajustarse por env.
        try:
            backlog_rate = int(os.getenv("TRIPLE_BACKLOG_RATE", str(rate_limit_per_side)).strip())
        except Exception:
            backlog_rate = int(rate_limit_per_side)

        # Persistencia del cursor del backlog para evitar reinyectar histórico tras reinicios.
        # Se guarda bajo BOT_DATA_DIR si existe; si no, en el directorio actual.
        base_data_dir = (os.getenv("BOT_DATA_DIR") or os.getenv("DATA_DIR") or ".").strip() or "."
        default_cursor = os.path.join(base_data_dir, "triple_bridge_hub_cursor.json")
        self._hub_cursor_path = (os.getenv("TRIPLE_HUB_CURSOR_PATH") or default_cursor).strip() or default_cursor
        try:
            self._hub_cursor_save_every_sec = float(os.getenv("TRIPLE_HUB_CURSOR_SAVE_EVERY", "5") or "5")
        except Exception:
            self._hub_cursor_save_every_sec = 5.0
        self._hub_cursor_last_save_ts = 0.0

        # --- Etiquetas ---
        base = str(tag_bridge or "").strip()
        self.tag_a2b = (tag_bridge_a2b.strip() if tag_bridge_a2b else base)
        self.tag_b2a = (tag_bridge_b2a.strip() if tag_bridge_b2a else base)
        self.tag_a2c = (tag_bridge_a2c.strip() if tag_bridge_a2c else base)
        self.tag_c2a = (tag_bridge_c2a.strip() if tag_bridge_c2a else base)

        # --- Interfaces TCP (cuando aplique) ---
        self.iface_a = None
        self.iface_b = None
        self.iface_c = None

        # --- IDs locales (para evitar eco) ---
        self.local_id_a: Optional[str] = None
        self.local_id_b: Optional[str] = None
        self.local_id_c: Optional[str] = None

        # --- Control ---
        self._lock = threading.RLock()
        self._stop = threading.Event()

        # --- Dedupe y rate-limit ---
        self.dedup = DedupWindow(dedup_ttl)
        # Limitadores por sentido:
        # - rl_*    : tráfico en tiempo real (RX por TCP)
        # - rl_*_bl : replay de backlog (HUB_MODE=broker)
        self.rl_a2b = RateLimiter(rate_limit_per_side)
        self.rl_b2a = RateLimiter(rate_limit_per_side)
        self.rl_a2c = RateLimiter(rate_limit_per_side)
        self.rl_c2a = RateLimiter(rate_limit_per_side)
        self.rl_a2b_bl = RateLimiter(backlog_rate)
        self.rl_a2c_bl = RateLimiter(backlog_rate)

        # --- Broker client (solo broker-mode) ---
        self._broker: BrokerBacklogClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._hub_last_ts: int | None = None

        # --- Estados de peers ---
        self._b_offline_until = 0.0
        self._c_offline_until = 0.0
        self._last_rx_b_ts = 0.0
        self._last_rx_c_ts = 0.0

        # --- TX spool (cola prioritaria por "due") ---
        self._pq: list[tuple[float, int, dict]] = []
        self._pq_seq = 0
        self._pq_cv = threading.Condition(self._lock)
        self._tx_thread: threading.Thread | None = None

        # --- Watchdog ---
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None

        # --- Backoff reintentos ---
        self._retry_schedule = [2.0, 5.0, 10.0, 20.0, 40.0, 75.0]

    # ---------------------- Helpers (peers online/offline) ----------------------
    def _safe_close_iface(self, iface, label: str) -> None:
        """
        Cierre duro y silencioso de una interfaz Meshtastic.
        Crítico para 24/7: evita hilos/timers colgantes tras resets/caídas TCP.
        """
        try:
            if iface and hasattr(iface, "close"):
                iface.close()
                print(f"[triple-bridge] Cerrada iface {label}", flush=True)
        except Exception as e:
            print(f"[triple-bridge] Error cerrando iface {label}: {type(e).__name__}: {e}", flush=True)

    def _is_b_suppressed(self) -> bool:
        return time.time() < float(self._b_offline_until or 0.0)

    def _is_c_suppressed(self) -> bool:
        return time.time() < float(self._c_offline_until or 0.0)

    def _mark_b_down(self, reason: str) -> None:
        old = self.iface_b
        self.iface_b = None
        self.local_id_b = None
        self._safe_close_iface(old, "B")
        self._b_offline_until = max(float(self._b_offline_until), time.time() + 75.0)
        print(f"[triple-bridge] B OFFLINE → {reason}", flush=True)

    def _mark_c_down(self, reason: str) -> None:
        old = self.iface_c
        self.iface_c = None
        self.local_id_c = None
        self._safe_close_iface(old, "C")
        self._c_offline_until = max(float(self._c_offline_until), time.time() + 75.0)
        print(f"[triple-bridge] C OFFLINE → {reason}", flush=True)

    def _is_false_offline_error(self, e: BaseException) -> bool:
        """
        Detecta el patrón de error del SDK que no implica caída real del peer,
        sino handshake/estado interno incompleto.

        Caso típico:
        MeshInterfaceError: Timed out waiting for connection completion

        Si se trata como OFFLINE 75s se provoca flapping y se pierde capacidad de envío.
        """
        try:
            msg = str(e) or ""
        except Exception:
            msg = ""
        msg_l = msg.lower()
        needles = (
            "timed out waiting for connection completion",
            "connection completion",
            "waiting for connection completion",
            "connection not completed",
        )
        return any(n in msg_l for n in needles)


    def _soft_reset_peer(self, peer: str, why: str) -> None:
        """
        Reset suave del peer (B o C) SIN entrar en supresión larga (75s).
        Acción:
        - close() de la iface si existe
        - iface=None, local_id=None
        - supresión corta (2s) para evitar bucle apretado
        """
        now = time.time()
        try:
            if peer == "B":
                if self.iface_b and hasattr(self.iface_b, "close"):
                    self.iface_b.close()
                self.iface_b = None
                self.local_id_b = None
                self._b_offline_until = max(float(self._b_offline_until), now + 2.0)
            else:
                if self.iface_c and hasattr(self.iface_c, "close"):
                    self.iface_c.close()
                self.iface_c = None
                self.local_id_c = None
                self._c_offline_until = max(float(self._c_offline_until), now + 2.0)
        except Exception:
            pass
        print(f"[triple-bridge] {peer} SOFT-RESET (sin OFFLINE 75s): {why}", flush=True)





    # ---------------------- Local IDs ----------------------

    def _discover_local_id(self, iface, label: str) -> Optional[str]:
        """
        Intenta obtener el id local (myInfo). Se usa para evitar eco:
        si un mensaje llega "desde" el propio destino, no se reinyecta.
        """
        for _ in range(20):  # ~10s
            try:
                info = getattr(iface, "myInfo", None) or {}
                local_id = (
                    info.get("my_node_num") or info.get("myNodeNum") or
                    info.get("num") or info.get("nodeNum") or
                    info.get("id")
                )
                if isinstance(local_id, int):
                    local_id = f"!{local_id:08x}"
                if local_id:
                    s = str(local_id)
                    print(f"[triple-bridge] local_id_{label}={s}", flush=True)
                    return s
            except Exception:
                pass
            time.sleep(0.5)
        print(f"[triple-bridge] ⚠️ local_id_{label} no disponible (myInfo no llegó a tiempo)", flush=True)
        return None

    # ---------------------- Peer connect/reconnect ----------------------

    def _connect_b(self, tcp_timeout_s: float) -> None:
        print(f"[triple-bridge] Conectando B: {self.b_host}:{self.b_port}", flush=True)
        try:
            self._safe_close_iface(self.iface_b, "B")  # en _connect_b
            self.iface_b = _TCPI(hostname=self.b_host, portNumber=self.b_port, timeout=tcp_timeout_s)
            self.local_id_b = self._discover_local_id(self.iface_b, "B")
            self._b_offline_until = 0.0
            print("[triple-bridge] reconexión a B OK", flush=True)
        except Exception as e:
            self.iface_b = None
            self.local_id_b = None
            self._b_offline_until = time.time() + 75.0
            print(f"[triple-bridge] B OFFLINE: {type(e).__name__}: {e}", flush=True)

    def _connect_c(self, tcp_timeout_s: float) -> None:
        print(f"[triple-bridge] Conectando C: {self.c_host}:{self.c_port}", flush=True)
        try:
            self._safe_close_iface(self.iface_c, "C")  # en _connect_c
            self.iface_c = _TCPI(hostname=self.c_host, portNumber=self.c_port, timeout=tcp_timeout_s)
            self.local_id_c = self._discover_local_id(self.iface_c, "C")
            self._c_offline_until = 0.0
            print("[triple-bridge] reconexión a C OK", flush=True)
        except Exception as e:
            self.iface_c = None
            self.local_id_c = None
            self._c_offline_until = time.time() + 75.0
            print(f"[triple-bridge] C OFFLINE: {type(e).__name__}: {e}", flush=True)

    # ---------------------- TX spool ----------------------

    def _enqueue(self, item: dict, due: float | None = None) -> None:
        """
        Encola un envío con 'due' (epoch seconds). Si due=None, sale ya.
        Item debe incluir:
          - peer: "B" o "C"
          - ch, msg, want_ack, direction
          - attempt, max_retries (opcionales)
        """
        with self._pq_cv:
            self._pq_seq += 1
            t = float(due if due is not None else time.time())
            heapq.heappush(self._pq, (t, self._pq_seq, item))
            self._pq_cv.notify()

    def _reschedule(self, item: dict, delay_s: float) -> None:
        item["attempt"] = int(item.get("attempt", 0)) + 1
        self._enqueue(item, due=time.time() + max(0.5, float(delay_s or 1.0)))

    def _tx_worker(self) -> None:
        while not self._stop.is_set():
            with self._pq_cv:
                while not self._stop.is_set() and not self._pq:
                    self._pq_cv.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                due, _, item = heapq.heappop(self._pq)
                now = time.time()
                if due > now:
                    # aún no toca
                    self._pq_seq += 1
                    heapq.heappush(self._pq, (due, self._pq_seq, item))
                    self._pq_cv.wait(timeout=min(1.0, due - now))
                    continue

            self._send_item(item)

    def _send_item(self, item: dict) -> None:
        """
        Envío real hacia B/C por TCP, con manejo de offline/reintentos.
        """
        peer = str(item.get("peer") or "").upper()
        ch = int(item.get("ch") or 0)
        msg = str(item.get("msg") or "")
        want_ack = bool(item.get("want_ack", False))
        direction = str(item.get("direction") or "?")

        # Rate-limit por dirección

        # Rate-limit por dirección (src-aware) + evita doble limitación si ya pasó por el filtro de entrada.
        # - src="tcp": tráfico RX directo desde A (modo tcp)
        # - src="hub": replay desde BacklogServer (modo broker)
        src = str(item.get("src") or "tcp").strip().lower()
        already_checked = bool(item.get("rl_checked", False))

        if not already_checked:
            if direction == "A2B":
                limiter = (self.rl_a2b_bl if src == "hub" else self.rl_a2b)
                if not limiter.allow():
                    self._reschedule(item, 2.0); return
            elif direction == "A2C":
                limiter = (self.rl_a2c_bl if src == "hub" else self.rl_a2c)
                if not limiter.allow():
                    self._reschedule(item, 2.0); return

        # Peer suprimido
        if peer == "B":
            if self._is_b_suppressed():
                remaining = max(0, int(self._b_offline_until - time.time()))
                print(
                    f"[brokerhub TX] {direction} → {peer} ch={ch} "
                    f"len={len(msg)} txt='{msg[:120]}'",
                    flush=True
                )

              
                self._enqueue(item, due=float(self._b_offline_until))
                return
            iface = self.iface_b
        elif peer == "C":
            if self._is_c_suppressed():
                remaining = max(0, int(self._c_offline_until - time.time()))
                print(
                    f"[brokerhub TX] {direction} → {peer} ch={ch} "
                    f"len={len(msg)} txt='{msg[:120]}'",
                    flush=True
                )

              
                self._enqueue(item, due=float(self._c_offline_until))
                return
            iface = self.iface_c
        else:
            return

        attempt = int(item.get("attempt", 0))
        max_r = int(item.get("max_retries", 8))

        if iface is None:
            # Aún no reconectó (watchdog lo hará). Reprograma suave.
            self._reschedule(item, 5.0)
            return

        try:
            fn = getattr(iface, "sendText", None)
            if not callable(fn):
                raise RuntimeError("iface.sendText no disponible")

            max_len = _safe_max_text_len()
            parts = _split_text_chunks(msg, max_len)

            # Si hay varias partes, numera sin pasarte del límite
            if len(parts) > 1:
                total = len(parts)
                numbered_parts: list[str] = []
                for i, p in enumerate(parts, start=1):
                    prefix = f"{i}/{total} "
                    space = max_len - len(prefix)
                    if space < 10:
                        space = max_len
                        prefix = ""
                    numbered = (prefix + p[:space]).strip()
                    numbered_parts.append(numbered)
                parts = numbered_parts

            for p in parts:
                try:
                    fn(p, destinationId="^all", wantAck=want_ack, wantResponse=False, channelIndex=int(ch))
                except TypeError:
                    # compatibilidad con SDKs distintos
                    try:
                        fn(p, channelIndex=int(ch), wantAck=want_ack)
                    except TypeError:
                        fn(p, channelIndex=int(ch))

                print(
                    f"[brokerhub TX] {direction} → {peer} ch={ch} OK "
                    f"len={len(p)} txt='{p[:120]}'",
                    flush=True
                )

            return

        except Exception as e:
            # Manejo especial: payload demasiado grande => no es caída del peer.
            msg_l = ""
            try:
                msg_l = (str(e) or "").lower()
            except Exception:
                msg_l = ""

            if "data payload too big" in msg_l:
                print(
                    f"[triple-bridge] {direction} → {peer} DROP: payload too big "
                    f"(len={len(msg)})",
                    flush=True
                )
                # No reintentar, no marcar offline.
                return

            # Caso especial: error "falso OFFLINE" del SDK (handshake incompleto)
            if self._is_false_offline_error(e):
                self._soft_reset_peer(peer, f"{type(e).__name__}: {e}")
            else:
                if peer == "B":
                    self._mark_b_down(f"{type(e).__name__}: {e}")
                else:
                    self._mark_c_down(f"{type(e).__name__}: {e}")

            if attempt < max_r:
                delay = self._retry_schedule[min(attempt, len(self._retry_schedule) - 1)]
                self._reschedule(item, delay)
            else:
                print(f"[triple-bridge] {direction} → {peer} ERROR (agotado): {type(e).__name__}: {e}", flush=True)


    # ---------------------- Watchdog ----------------------

    def _start_watchdog(self) -> None:
        """
        Watchdog:
          - Reintenta reconexión cuando expire offline_until.
          - Opcional: marca offline si no llega RX en TRIPLE_*_STALE_SEC.
        """
        tick = float(os.getenv("TRIPLE_WATCHDOG_TICK", "5") or "5")
        tcp_timeout_s = float(os.getenv("TCP_TIMEOUT_S", "25") or "25")

        def _run():
            while not self._stop.is_set() and not self._wd_stop.is_set():
                now = time.time()

                # Reconexiones por timer
                if self.enable_b and self.b_host and self.iface_b is None and not self._is_b_suppressed():
                    self._connect_b(tcp_timeout_s)

                if self.enable_c and self.c_host and self.iface_c is None and not self._is_c_suppressed():
                    self._connect_c(tcp_timeout_s)

                # Stale RX (si está configurado)
                if self.enable_b:
                    try:
                        stale_b = float(os.getenv("TRIPLE_B_STALE_SEC", "0") or "0")
                    except Exception:
                        stale_b = 0.0
                    if stale_b > 0 and self.iface_b is not None and self._last_rx_b_ts > 0 and (now - self._last_rx_b_ts) > stale_b:
                        self._mark_b_down(f"stale_rx>{int(stale_b)}s")

                if self.enable_c:
                    try:
                        stale_c = float(os.getenv("TRIPLE_C_STALE_SEC", "0") or "0")
                    except Exception:
                        stale_c = 0.0
                    if stale_c > 0 and self.iface_c is not None and self._last_rx_c_ts > 0 and (now - self._last_rx_c_ts) > stale_c:
                        self._mark_c_down(f"stale_rx>{int(stale_c)}s")

                time.sleep(max(1.0, tick))

        self._wd_thread = threading.Thread(target=_run, name="triple-watchdog", daemon=True)
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

    # ---------------------- Cursor backlog (HUB_MODE=broker) ----------------------

    def _load_hub_cursor(self) -> Optional[int]:
        """Carga el cursor persistido del backlog. Devuelve None si no existe o no es válido."""
        try:
            p = str(self._hub_cursor_path or "").strip()
            if not p or not os.path.exists(p):
                return None
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f) or {}
            v = obj.get("hub_last_ts")
            if v is None:
                return None
            v = int(v)
            return v if v > 0 else None
        except Exception:
            return None

    def _save_hub_cursor(self, force: bool = False) -> None:
        """Guarda el cursor del backlog en disco de forma segura (tmp + rename).
        Se limita por intervalo para minimizar IO (importante en SD).
        """
        try:
            if self._hub_last_ts is None:
                return
            v = int(self._hub_last_ts)
            if v <= 0:
                return

            now = time.time()
            if not force:
                if (now - float(self._hub_cursor_last_save_ts or 0.0)) < float(self._hub_cursor_save_every_sec or 5.0):
                    return

            p = str(self._hub_cursor_path or "").strip()
            if not p:
                return

            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)

            tmp = p + ".tmp"
            payload = {"hub_last_ts": v, "saved_at": int(now)}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, p)

            self._hub_cursor_last_save_ts = now
        except Exception:
            return


    # ---------------------- Connect / Close ----------------------

    def connect(self):
        """
        Conecta interfaces:
          - HUB_MODE=broker: NO abre TCP a A. Usa BacklogServer del broker.
          - HUB_MODE=tcp   : abre TCP a A, y además B/C.

        NOTA: B/C se conectan aunque HUB_MODE=broker, porque son los peers TCP.
        """
        # Timeout TCP para handshakes lentos (WiFi justo, CPU baja, etc.)
        try:
            tcp_timeout_s = float(os.getenv("TCP_TIMEOUT_S") or 25)
        except Exception:
            tcp_timeout_s = 25.0

        if self.hub_mode == "broker":
            print("[triple-bridge] HUB_MODE=broker: NO se abre TCP a A (se usa broker backlog).", flush=True)
            self.iface_a = None
            self.local_id_a = None
            if not self.broker_ctrl_host:
                raise SystemExit("Falta BROKER_CTRL_HOST en HUB_MODE=broker")
            self._broker = BrokerBacklogClient(
                host=self.broker_ctrl_host,
                port=int(self.broker_ctrl_port or 8766),
                timeout_s=float(self.broker_timeout_s or 8.0),
            )
        else:
            print(f"[triple-bridge] Conectando A: {self.a_host}:{self.a_port}", flush=True)
            self.iface_a = _TCPI(hostname=self.a_host, portNumber=self.a_port, timeout=tcp_timeout_s)
            self.local_id_a = self._discover_local_id(self.iface_a, "A")

        # B / C
        if self.enable_b and self.b_host:
            self._connect_b(tcp_timeout_s)
        else:
            print("[triple-bridge] B deshabilitado por BRIDGE_PEERS", flush=True)

        if self.enable_c and self.c_host:
            self._connect_c(tcp_timeout_s)
        else:
            print("[triple-bridge] C deshabilitado por BRIDGE_PEERS", flush=True)

        if self.iface_b is None and self.iface_c is None:
            raise SystemExit("No hay peers activos tras connect() (BRIDGE_PEERS)")

        # Subscripción RX (solo B/C y A en tcp-mode)
        pub.subscribe(self._on_rx, "meshtastic.receive")

        # Poll del backlog (solo broker-mode)
        if self.hub_mode == "broker":
            self._poll_thread = threading.Thread(target=self._hub_poll_loop, name="hub-poll", daemon=True)
            self._poll_thread.start()

        # TX spool
        self._tx_thread = threading.Thread(target=self._tx_worker, name="tx-spool", daemon=True)
        self._tx_thread.start()

        # Watchdog de reconexión/stale
        self._start_watchdog()

        print(
            f"[triple-bridge] Conectado. hub_mode={self.hub_mode} "
            f"local_id_a={self.local_id_a} local_id_b={self.local_id_b} local_id_c={self.local_id_c}",
            flush=True,
        )

    def close(self):
        self._stop.set()
        # Persistir cursor del backlog (si aplica)
        try:
            self._save_hub_cursor(force=True)
        except Exception:
            pass
        self._stop_watchdog()
        try:
            pub.unsubscribe(self._on_rx, "meshtastic.receive")
        except Exception:
            pass

        with self._pq_cv:
            self._pq_cv.notify_all()

        # Espera breve de hilos para cierre limpio (24/7, reinicios y upgrades)
        try:
            if getattr(self, "_poll_thread", None) and self._poll_thread.is_alive():
                self._poll_thread.join(timeout=2.5)
        except Exception:
            pass
        try:
            if getattr(self, "_tx_thread", None) and self._tx_thread.is_alive():
                self._tx_thread.join(timeout=2.5)
        except Exception:
            pass


        # Cierre consistente de interfaces
        self._safe_close_iface(self.iface_a, "A")
        self._safe_close_iface(self.iface_b, "B")
        self._safe_close_iface(self.iface_c, "C")

        self.iface_a = None
        self.iface_b = None
        self.iface_c = None

        
    def run_forever(self):
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    # ---------------------- HUB poll (A events en broker-mode) ----------------------

    def _hub_poll_loop(self) -> None:
        # Cursor inicial: preferimos el persistido para evitar replay tras reinicios.
        if self._hub_last_ts is None:
            persisted = self._load_hub_cursor()
            if persisted is not None:
                self._hub_last_ts = int(persisted)
            else:
                self._hub_last_ts = int(time.time())

        portnums: list[str] = []
        if self.forward_text:
            portnums.append("TEXT_MESSAGE_APP")
        if self.forward_position:
            portnums.extend(["POSITION_APP", "TELEMETRY_APP"])

        while not self._stop.is_set():
            try:
                assert self._broker is not None
                items = self._broker.fetch_backlog(
                    since_ts=int(self._hub_last_ts or 0),
                    portnums=portnums or ["TEXT_MESSAGE_APP"],
                    limit=int(self.broker_fetch_limit or 2000),
                )
                items.sort(key=lambda x: int(x.get("rx_time") or 0))
                for obj in items:
                    self._process_hub_event(obj)

                # Persistir cursor si avanzó
                self._save_hub_cursor(force=False)

                # === [NUEVO] si el fetch viene vacío, avanza ligeramente el cursor para evitar bordes pegajosos ===

            except Exception as e:
                print(f"[triple-bridge] HUB poll error: {type(e).__name__}: {e}", flush=True)

            time.sleep(float(self.broker_poll_sec or 1.5))

    def _process_hub_event(self, obj: dict) -> None:
        """
        Evento del backlog del broker (normalmente generado por A).
        En HUB_MODE=broker, esto es la fuente de mensajes "desde A".
        """
        try:
            rx_time = int(obj.get("rx_time") or 0)
            if rx_time <= 0:
                return

            if self._hub_last_ts is None or rx_time >= self._hub_last_ts:
                self._hub_last_ts = rx_time + 1

            port = str(obj.get("portnum") or "").upper()
            ch = int(obj.get("channel") or 0)
            frm = str(obj.get("fromId") or "")

            want_text = self.forward_text and ("TEXT_MESSAGE_APP" in port or port == "TEXT")
            want_pos = self.forward_position and (("POSITION_APP" in port) or ("TELEMETRY_APP" in port) or (port in {"POSITION", "TELEMETRY"}))
            if not (want_text or want_pos):
                return

            decoded = {"portnum": port, "channel": ch}
            text = ""
            if want_text:
                text = str(obj.get("text") or "")
                decoded["text"] = text
            else:
                if "position" in obj:
                    decoded["position"] = obj.get("position") or {}
                if "telemetry" in obj:
                    decoded["telemetry"] = obj.get("telemetry") or {}

            # A -> (B/C)
            print(
                f"[brokerhub RX] ch={ch} src={frm} port={port} "
                f"txt='{text[:120]}'",
                flush=True
            )

            self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos, source=("tcp" if self.hub_mode == "tcp" else "hub"))

        except Exception as e:
            print(f"[triple-bridge] HUB event error: {type(e).__name__}: {e}", flush=True)

    # ---------------------- Envío hacia A (hub) ----------------------

    def _send_text_to_hub(self, text: str, ch: int) -> bool:
        msg = _norm_text(text or "")
        if not msg:
            return False

        # broker-mode: siempre por SEND_TEXT al broker (no TCP a A)
        if self.hub_mode == "broker":
            if not self._broker:
                return False
            try:
                ok = bool(self._broker.send_text(msg, ch=int(ch or 0), dest=None, ack=False))
                print(
                    f"[brokerhub→A] TX ch={ch} {'OK' if ok else 'ERROR'} txt='{msg[:120]}'",
                    flush=True
                )
                return ok

            except Exception:
                return False

        # tcp-mode: sendText a iface_a
        if not self.iface_a:
            return False
        try:
            self.iface_a.sendText(
                msg,
                destinationId="^all",
                wantAck=bool(self.require_ack),
                wantResponse=False,
                channelIndex=int(ch or 0),
            )
            return True
        except Exception:
            return False

    # ---------------------- RX handler (B/C y A en tcp-mode) ----------------------

    def _on_rx(self, interface=None, packet=None, **kwargs):
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}

            port = decoded.get("portnum") or decoded.get("portnum_name") or decoded.get("portnum_str") or decoded.get("portnumText")
            port_str = str(port).upper() if not isinstance(port, int) else str(port)

            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT"))
            want_pos = self.forward_position and (("POSITION_APP" in port_str) or ("TELEMETRY_APP" in port_str) or (port_str in {"POSITION", "TELEMETRY"}))
            if not (want_text or want_pos):
                return

            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            frm = pkt.get("fromId") or decoded.get("fromId") or pkt.get("from")
            frm = str(frm or "")

            text = str(decoded.get("text") or decoded.get("payload") or "")

            came_from_a = (self.iface_a is not None) and (interface is self.iface_a)
            came_from_b = (self.iface_b is not None) and (interface is self.iface_b)
            came_from_c = (self.iface_c is not None) and (interface is self.iface_c)

            if came_from_b:
                self._last_rx_b_ts = time.time()
            if came_from_c:
                self._last_rx_c_ts = time.time()

            if came_from_a:
                self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos, source=("tcp" if self.hub_mode == "tcp" else "hub"))
            elif came_from_b:
                self._bridge_from_b(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_c:
                self._bridge_from_c(ch, frm, text, decoded, want_text, want_pos)

        except Exception as e:
            print(f"[triple-bridge] on_rx error: {type(e).__name__}: {e}", flush=True)

    # ---------------------- Bridging por origen ----------------------

    def _bridge_from_a(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool, *, source: str = "tcp"):
        
        # === [NUEVO] Dedupe también para A->B/A->C (protege contra duplicados del backlog) ===
        payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)

        src_l = (source or "tcp").strip().lower()

        # A -> B
        if self.enable_b and ch in self.a2b_map:
            out_ch = self.a2b_map[ch]
            if self.local_id_b and frm == self.local_id_b:
                return

            # Rate-limit por peer (A->B)
            if src_l == "hub":
                if not self.rl_a2b_bl.allow():
                    return
            else:
                if not self.rl_a2b.allow():
                    return

            if self.dedup.seen(_hash_key("A2B", frm, int(ch), payload_for_hash)):
                return

            if want_text:
                msg = _norm_text(text)

                # === [NUEVO] NO reenviar solicitudes BBS por el bridge ===
                if _should_block_bbs_bridge(int(ch), msg):
                    return
            
                if self.tag_a2b and self.tag_a2b not in msg:
                    msg = f"{self.tag_a2b} {msg}"
                self._enqueue(
                    {"peer": "B", "direction": "A2B", "ch": int(out_ch), "msg": msg, "want_ack": bool(self.require_ack), "attempt": 0, "max_retries": 8, "src": src_l, "rl_checked": True},
                    due=float(self._b_offline_until) if self._is_b_suppressed() else None,
                )

        # A -> C
        if self.enable_c and ch in self.a2c_map:
            out_ch = self.a2c_map[ch]
            if self.local_id_c and frm == self.local_id_c:
                return

            # Rate-limit por peer (A->C)
            if src_l == "hub":
                if not self.rl_a2c_bl.allow():
                    return
            else:
                if not self.rl_a2c.allow():
                    return

            if self.dedup.seen(_hash_key("A2C", frm, int(ch), payload_for_hash)):
                return

            if want_text:
                msg = _norm_text(text)
                # === [NUEVO] NO reenviar solicitudes BBS por el bridge ===
                if _should_block_bbs_bridge(int(ch), msg):
                    return
            
                if self.tag_a2c and self.tag_a2c not in msg:
                    msg = f"{self.tag_a2c} {msg}"
                self._enqueue(
                    {"peer": "C", "direction": "A2C", "ch": int(out_ch), "msg": msg, "want_ack": bool(self.require_ack), "attempt": 0, "max_retries": 8, "src": src_l, "rl_checked": True},
                    due=float(self._c_offline_until) if self._is_c_suppressed() else None,
                )

    def _bridge_from_b(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
        # B -> A
        if ch not in self.b2a_map:
            return
        out_ch = self.b2a_map[ch]

        if self.local_id_a and frm == self.local_id_a:
            return
        if not self.rl_b2a.allow():
            return

        payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
        if self.dedup.seen(_hash_key("B2A", frm, ch, payload_for_hash)):
            return

        if want_text:
            msg = _norm_text(text)

            # === [NUEVO] NO reenviar solicitudes BBS por el bridge ===
            if _should_block_bbs_bridge(int(ch), msg):
                return
            
            if self.tag_b2a and self.tag_b2a not in msg:
                msg = f"{self.tag_b2a} {msg}"
            ok = self._send_text_to_hub(msg, int(out_ch))
            print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} txt OK" if ok else "[triple-bridge] B2A B->A sendText ERROR", flush=True)

    def _bridge_from_c(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
        # C -> A
        if ch not in self.c2a_map:
            return
        out_ch = self.c2a_map[ch]

        if self.local_id_a and frm == self.local_id_a:
            return
        if not self.rl_c2a.allow():
            return

        payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
        if self.dedup.seen(_hash_key("C2A", frm, ch, payload_for_hash)):
            return

        if want_text:
            msg = _norm_text(text)
            # === [NUEVO] NO reenviar solicitudes BBS por el bridge ===
            if _should_block_bbs_bridge(int(ch), msg):
                return
            
            if self.tag_c2a and self.tag_c2a not in msg:
                msg = f"{self.tag_c2a} {msg}"
            ok = self._send_text_to_hub(msg, int(out_ch))
            print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} txt OK" if ok else "[triple-bridge] C2A C->A sendText ERROR", flush=True)


# ============================================================
#  CLI
# ============================================================

def main():
    a_host = (os.getenv("A_HOST", "") or "").strip()
    b_host = (os.getenv("B_HOST", "") or "").strip()
    c_host = (os.getenv("C_HOST", "") or "").strip()

    hub_mode = (os.getenv("HUB_MODE", "tcp") or "tcp").strip().lower()

    def _get_int(name: str, default: int) -> int:
        try:
            return int((os.getenv(name) or str(default)).strip())
        except Exception:
            return int(default)

    a_port = _get_int("A_PORT", 4403)
    b_port = _get_int("B_PORT", 4403)
    c_port = _get_int("C_PORT", 4403)

    a2b = _parse_ch_map(os.getenv("A2B_CH_MAP", "0:0"))
    b2a = _parse_ch_map(os.getenv("B2A_CH_MAP", "0:0"))
    a2c = _parse_ch_map(os.getenv("A2C_CH_MAP", "0:0"))
    c2a = _parse_ch_map(os.getenv("C2A_CH_MAP", "0:0"))

    forward_text = _truthy(os.getenv("FORWARD_TEXT", "1"), True)
    forward_position = _truthy(os.getenv("FORWARD_POSITION", "0"), False)
    require_ack = _truthy(os.getenv("REQUIRE_ACK", "0"), False)
    rate = _get_int("RATE_LIMIT_PER_SIDE", 8)
    dedup = _get_int("DEDUP_TTL", 45)

    tag_base = os.getenv("TAG_BRIDGE", "[BRIDGE]")

    tag_a2b = (os.getenv("TAG_BRIDGE_A2B", "") or "").strip() or None
    tag_b2a = (os.getenv("TAG_BRIDGE_B2A", "") or "").strip() or None
    tag_a2c = (os.getenv("TAG_BRIDGE_A2C", "") or "").strip() or None
    tag_c2a = (os.getenv("TAG_BRIDGE_C2A", "") or "").strip() or None

    # ------------------------------------------------------------
    # NUEVO: selección de peers (B/C) con BRIDGE_PEERS
    #   BRIDGE_PEERS=B,C (default)
    #   BRIDGE_PEERS=C   (solo C)
    #   BRIDGE_PEERS=B   (solo B)
    # ------------------------------------------------------------
    peers_spec = (os.getenv("BRIDGE_PEERS", "B,C") or "B,C").strip().upper()
    peers = {p.strip() for p in peers_spec.split(",") if p.strip()}

    enable_b = ("B" in peers) and bool(b_host)
    enable_c = ("C" in peers) and bool(c_host)

    # Validaciones mínimas
    if not (enable_b or enable_c):
        raise SystemExit("BRIDGE_PEERS no habilita ningún peer válido (revisa BRIDGE_PEERS y B_HOST/C_HOST).")

    if hub_mode != "broker":
        if not a_host:
            raise SystemExit("Falta A_HOST en HUB_MODE=tcp")
    else:
        # En broker, A_HOST puede estar vacío (no abrimos TCP a A).
        pass

    print(f"[triple-bridge] hub_mode={hub_mode}")
    if hub_mode == "broker":
        print("[triple-bridge] HUB_MODE=broker: NO se abre TCP a A (se usa broker backlog).")

    # Logs de estado peers
    print(f"[triple-bridge] BRIDGE_PEERS={peers_spec}  B={'ON' if enable_b else 'OFF'}  C={'ON' if enable_c else 'OFF'}")

    bridge = TripleBridge(
        a_host=a_host,
        a_port=a_port,

        b_host=b_host,
        b_port=b_port,
        c_host=c_host,
        c_port=c_port,

        # NUEVO: pásalo al constructor (tienes que añadirlo a __init__)
        enable_b=enable_b,
        enable_c=enable_c,

        a2b_map=a2b,
        b2a_map=b2a,
        a2c_map=a2c,
        c2a_map=c2a,

        hub_mode=hub_mode,
        broker_ctrl_host=(os.getenv("BROKER_CTRL_HOST") or "").strip() or None,
        broker_ctrl_port=_get_int("BROKER_CTRL_PORT", 8766),
        broker_poll_sec=float(os.getenv("BROKER_POLL_SEC") or 1.5),
        broker_fetch_limit=_get_int("BROKER_FETCH_LIMIT", 2000),
        broker_timeout_s=float(os.getenv("BROKER_TIMEOUT_S") or 8.0),

        forward_text=forward_text,
        forward_position=forward_position,
        require_ack=require_ack,
        rate_limit_per_side=rate,
        dedup_ttl=dedup,

        tag_bridge=tag_base,
        tag_bridge_a2b=tag_a2b,
        tag_bridge_b2a=tag_b2a,
        tag_bridge_a2c=tag_a2c,
        tag_bridge_c2a=tag_c2a,
    )

    bridge.connect()
    bridge.run_forever()

if __name__ == "__main__":
    main()
