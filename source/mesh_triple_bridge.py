#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mesh_triple_bridge.py  (versión de prueba: selección de peers)

Pasarela 3-way (A + B + C):
  - Nodo A: "hub" (normalmente el nodo que usa el broker)
  - Nodo B: peer TCP remoto
  - Nodo C: peer TCP remoto

Modos:
  - HUB_MODE=tcp
      Conecta TCP a A, B y/o C (según BRIDGE_PEERS).
      El callback 'meshtastic.receive' procesa paquetes entrantes y reenvía.

  - HUB_MODE=broker
      NO conecta TCP a A.
      Lee lo que recibe A desde el BacklogServer del broker (FETCH_BACKLOG) y lo reinyecta a B/C.
      Para enviar hacia A, usa BacklogServer (SEND_TEXT).

NUEVO:
  - BRIDGE_PEERS: decide a qué peers TCP se conecta el bridge.
      BRIDGE_PEERS=B,C   (por defecto)
      BRIDGE_PEERS=C     (solo C)
      BRIDGE_PEERS=B     (solo B)

Notas importantes:
  - Si un peer está OFF, iface_b o iface_c será None y el resto del código lo respeta.
  - Este fichero está pensado para pruebas sin romper el broker embebiendo B:
      BRIDGE_PEERS=C   -> el bridge ignora B y solo conecta a C.

Requisitos:
  pip install meshtastic pypubsub
"""

from __future__ import annotations

import os
import sys
import time
import json
import socket
import signal
import hashlib
import threading
from collections import deque
from typing import Optional

try:
    from pubsub import pub  # pypubsub
except Exception:  # pragma: no cover
    pub = None

try:
    from meshtastic.tcp_interface import TCPInterface as _TCPI
except Exception:
    _TCPI = None


# ============================================================
# Helpers
# ============================================================

def _norm_text(s: str) -> str:
    """Normaliza texto para deduplicación y tags."""
    return (str(s or "")).replace("\r\n", "\n").replace("\r", "\n").strip()


def _hash_key(from_id: str, ch: int, payload: str) -> str:
    """
    Clave de deduplicación GLOBAL.
    No incluye dirección para evitar ping-pong A↔B / A↔C.
    """
    h = hashlib.sha256()
    h.update(str(from_id or "?").encode("utf-8"))
    h.update(str(int(ch)).encode("utf-8"))
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


class RateLimiter:
    """Rate-limit muy simple por minuto."""

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
    """Ventana temporal de deduplicación (TTL)."""

    def __init__(self, ttl_s: int = 45):
        self.ttl = max(1, int(ttl_s))
        self._d = {}
        self._lock = threading.Lock()

    def seen(self, k: str) -> bool:
        now = time.time()
        with self._lock:
            # purge TTL
            dead = [kk for kk, ts in self._d.items() if (now - ts) > self.ttl]
            for kk in dead:
                self._d.pop(kk, None)

            if k in self._d:
                return True
            self._d[k] = now
            return False


# ============================================================
# BrokerCtrl client (BacklogServer del broker)
# ============================================================

class BrokerCtrlClient:
    """
    Cliente TCP JSON line-based contra BacklogServer del broker.

    Comandos usados:
      - FETCH_BACKLOG: para leer lo recibido por A (hub)
      - SEND_TEXT: para reenviar hacia A (hub)
    """

    def __init__(self, host: str, port: int, timeout_s: float = 8.0):
        self.host = str(host)
        self.port = int(port)
        self.timeout_s = float(timeout_s)

    def _call(self, payload: dict) -> dict:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        try:
            s.connect((self.host, self.port))
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            s.sendall(data)

            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break

            line = buf.split(b"\n", 1)[0]
            txt = line.decode("utf-8", "ignore").strip()
            return json.loads(txt or "{}")
        finally:
            try:
                s.close()
            except Exception:
                pass

    def fetch_backlog(self, since_ts: int, portnums: list[str], limit: int = 2000) -> list[dict]:
        resp = self._call({
            "cmd": "FETCH_BACKLOG",
            "since_ts": int(since_ts),
            "portnums": list(portnums or []),
            "limit": int(limit),
        })
        rows = resp.get("rows") or resp.get("items") or resp.get("backlog") or []
        return rows if isinstance(rows, list) else []

    def send_text(self, text: str, ch: int, dest: str = "broadcast", ack: bool = False) -> bool:
        resp = self._call({
            "cmd": "SEND_TEXT",
            "ch": int(ch),
            "dest": str(dest or "broadcast"),
            "text": str(text or ""),
            "ack": bool(ack),
        })
        ok = resp.get("ok")
        if ok is None:
            ok = (resp.get("status") in ("ok", "OK")) or (resp.get("result") in ("ok", "OK"))
        return bool(ok)


# ============================================================
# Triple bridge
# ============================================================

class TripleBridge:
    """
    Reenvío:
      - A → B (según A2B_CH_MAP)
      - A → C (según A2C_CH_MAP)
      - B → A (según B2A_CH_MAP)
      - C → A (según C2A_CH_MAP)

    Dedup: sha256(from_id, ch, payload)
    Anti-eco: si el texto ya contiene la etiqueta "del otro lado", no reinyecta.
    """

    def __init__(
        self,
        a_host: str,
        a_port: int,
        hub_mode: str = "tcp",
        broker_ctrl_host: Optional[str] = None,
        broker_ctrl_port: Optional[int] = None,
        broker_poll_sec: float = 1.5,
        broker_fetch_limit: int = 2000,
        broker_timeout_s: float = 8.0,
        b_host: str = "",
        b_port: int = 4403,
        c_host: str = "",
        c_port: int = 4403,
        enable_b: bool = True,
        enable_c: bool = True,
        a2b_map: Optional[dict[int, int]] = None,
        b2a_map: Optional[dict[int, int]] = None,
        a2c_map: Optional[dict[int, int]] = None,
        c2a_map: Optional[dict[int, int]] = None,
        forward_text: bool = True,
        forward_position: bool = False,
        require_ack: bool = False,
        rate_limit_per_side: int = 8,
        dedup_ttl: int = 45,
        tag_bridge: str = "[BRIDGE]",
        tag_bridge_a2b: Optional[str] = None,
        tag_bridge_b2a: Optional[str] = None,
        tag_bridge_a2c: Optional[str] = None,
        tag_bridge_c2a: Optional[str] = None,
    ):
        self.a_host, self.a_port = str(a_host), int(a_port or 4403)
        self.hub_mode = (hub_mode or "tcp").strip().lower()

        self.broker_ctrl_host = broker_ctrl_host
        self.broker_ctrl_port = int(broker_ctrl_port or 0) if broker_ctrl_port else None
        self.broker_poll_sec = float(broker_poll_sec)
        self.broker_fetch_limit = int(broker_fetch_limit)
        self.broker_timeout_s = float(broker_timeout_s)

        self.b_host, self.b_port = str(b_host or ""), int(b_port or 4403)
        self.c_host, self.c_port = str(c_host or ""), int(c_port or 4403)

        self.enable_b = bool(enable_b)
        self.enable_c = bool(enable_c)

        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})
        self.a2c_map = dict(a2c_map or {})
        self.c2a_map = dict(c2a_map or {})

        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        self.rl_a2b = RateLimiter(rate_limit_per_side)
        self.rl_b2a = RateLimiter(rate_limit_per_side)
        self.rl_a2c = RateLimiter(rate_limit_per_side)
        self.rl_c2a = RateLimiter(rate_limit_per_side)

        self.dedup = DedupWindow(dedup_ttl)

        self.tag_base = tag_bridge or "[BRIDGE]"
        self.tag_a2b = tag_bridge_a2b
        self.tag_b2a = tag_bridge_b2a
        self.tag_a2c = tag_bridge_a2c
        self.tag_c2a = tag_bridge_c2a

        self.iface_a = None
        self.iface_b = None
        self.iface_c = None

        self.local_id_a: Optional[str] = None
        self.local_id_b: Optional[str] = None
        self.local_id_c: Optional[str] = None

        self._stop = threading.Event()
        self._broker: Optional[BrokerCtrlClient] = None
        self._hub_last_ts: int = 0
        self._poll_thread: Optional[threading.Thread] = None

    # --------------------------------------------------------
    # Connect / Close
    # --------------------------------------------------------

    def connect(self):
        if _TCPI is None:
            raise RuntimeError("meshtastic.tcp_interface no disponible en el entorno")
        if pub is None:
            raise RuntimeError("pypubsub no disponible en el entorno (from pubsub import pub)")

        # B / C por TCP, pero opcionales
        if self.enable_b and self.b_host:
            print(f"[triple-bridge] Conectando B: {self.b_host}:{self.b_port}", flush=True)
            self.iface_b = _TCPI(hostname=self.b_host, portNumber=self.b_port)
        else:
            self.iface_b = None
            print("[triple-bridge] B deshabilitado.", flush=True)

        if self.enable_c and self.c_host:
            print(f"[triple-bridge] Conectando C: {self.c_host}:{self.c_port}", flush=True)
            self.iface_c = _TCPI(hostname=self.c_host, portNumber=self.c_port)
        else:
            self.iface_c = None
            print("[triple-bridge] C deshabilitado.", flush=True)

        # A depende del modo
        if self.hub_mode == "tcp":
            print(f"[triple-bridge] Conectando A: {self.a_host}:{self.a_port}", flush=True)
            self.iface_a = _TCPI(hostname=self.a_host, portNumber=self.a_port)

            pub.subscribe(self._on_rx, "meshtastic.receive")
            self._discover_local_ids(bc_only=False)
            print(f"[triple-bridge] Conectado. hub_mode=tcp local_id_a={self.local_id_a} local_id_b={self.local_id_b} local_id_c={self.local_id_c}", flush=True)
            return

        # hub_mode=broker
        if not self.broker_ctrl_host or not self.broker_ctrl_port:
            raise RuntimeError("hub_mode=broker requiere BROKER_CTRL_HOST y BROKER_CTRL_PORT")

        self._broker = BrokerCtrlClient(self.broker_ctrl_host, self.broker_ctrl_port, timeout_s=self.broker_timeout_s)

        pub.subscribe(self._on_rx, "meshtastic.receive")
        self._discover_local_ids(bc_only=True)

        self._hub_last_ts = int(time.time()) - 5
        self._poll_thread = threading.Thread(target=self._poll_hub_loop, name="hub-poller", daemon=True)
        self._poll_thread.start()

        print(f"[triple-bridge] Conectado. hub_mode=broker local_id_a={self.local_id_a} local_id_b={self.local_id_b} local_id_c={self.local_id_c} broker={self.broker_ctrl_host}:{self.broker_ctrl_port}", flush=True)

    def close(self):
        self._stop.set()
        try:
            pub.unsubscribe(self._on_rx, "meshtastic.receive")
        except Exception:
            pass

        for iface, name in [(self.iface_a, "A"), (self.iface_b, "B"), (self.iface_c, "C")]:
            try:
                if iface is not None:
                    iface.close()
                    print(f"[triple-bridge] Cerrado iface {name}", flush=True)
            except Exception:
                pass

    # --------------------------------------------------------
    # Local IDs
    # --------------------------------------------------------

    def _discover_local_ids(self, bc_only: bool = False):
        # A
        if not bc_only:
            try:
                la = getattr(self.iface_a, "myInfo", None) or {}
                v = la.get("my_node_num") or la.get("num") or la.get("id") or ""
                if isinstance(v, int):
                    v = f"!{v:08x}"
                self.local_id_a = str(v) if v else None
            except Exception:
                self.local_id_a = None
        else:
            self.local_id_a = None

        # B
        if self.iface_b is None:
            self.local_id_b = None
        else:
            try:
                lb = getattr(self.iface_b, "myInfo", None) or {}
                v = lb.get("my_node_num") or lb.get("num") or lb.get("id") or ""
                if isinstance(v, int):
                    v = f"!{v:08x}"
                self.local_id_b = str(v) if v else None
            except Exception:
                self.local_id_b = None

        # C
        if self.iface_c is None:
            self.local_id_c = None
        else:
            try:
                lc = getattr(self.iface_c, "myInfo", None) or {}
                v = lc.get("my_node_num") or lc.get("num") or lc.get("id") or ""
                if isinstance(v, int):
                    v = f"!{v:08x}"
                self.local_id_c = str(v) if v else None
            except Exception:
                self.local_id_c = None

    # --------------------------------------------------------
    # HUB polling (A RX desde broker backlog)
    # --------------------------------------------------------

    def _poll_hub_loop(self):
        assert self._broker is not None

        portnums = []
        if self.forward_text:
            portnums.append("TEXT_MESSAGE_APP")
        if self.forward_position:
            portnums.append("POSITION_APP")

        while not self._stop.is_set():
            try:
                rows = self._broker.fetch_backlog(
                    since_ts=self._hub_last_ts,
                    portnums=portnums,
                    limit=self.broker_fetch_limit,
                )
                if rows:
                    for row in rows:
                        ts = row.get("ts") or row.get("time") or row.get("timestamp") or 0
                        try:
                            ts_i = int(ts)
                        except Exception:
                            ts_i = 0
                        if ts_i > self._hub_last_ts:
                            self._hub_last_ts = ts_i

                        pkt = row.get("packet") or row.get("pkt") or row
                        self._on_hub_packet(pkt)
            except Exception as e:
                print(f"[triple-bridge] HUB poll ERROR: {type(e).__name__}: {e}", flush=True)

            time.sleep(max(0.2, float(self.broker_poll_sec)))

    def _on_hub_packet(self, packet: dict):
        """Procesa un paquete como si viniera de A (hub)."""
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}
            port = decoded.get("portnum") or decoded.get("portnum_name") or decoded.get("portnum_str") or decoded.get("portnumText")
            port_str = str(port).upper() if port is not None else ""

            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT"))
            want_pos = self.forward_position and (("POSITION" in port_str) or ("POSITION_APP" in port_str))
            if not (want_text or want_pos):
                return

            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch)
            except Exception:
                ch = 0

            frm = pkt.get("fromId") or pkt.get("from") or decoded.get("fromId") or decoded.get("from") or ""
            if isinstance(frm, int):
                frm = f"!{frm:08x}"
            frm = str(frm or "")

            text = ""
            if want_text:
                text = _norm_text(decoded.get("text") or decoded.get("payload") or "")

            payload_key = text if want_text else json.dumps(decoded, ensure_ascii=False, sort_keys=True)
            if self.dedup.seen(_hash_key(frm, ch, payload_key)):
                return

            # A → B
            if ch in self.a2b_map and self.iface_b is not None:
                self._bridge_one(
                    src_label="A",
                    dst_label="B",
                    dst_iface=self.iface_b,
                    ch=ch,
                    out_ch=self.a2b_map[ch],
                    frm=frm,
                    text=text,
                    want_text=want_text,
                    rl=self.rl_a2b,
                    tag=self.tag_a2b,
                    other_tag=self.tag_b2a,
                    dst_local_id=self.local_id_b,
                )

            # A → C
            if ch in self.a2c_map and self.iface_c is not None:
                self._bridge_one(
                    src_label="A",
                    dst_label="C",
                    dst_iface=self.iface_c,
                    ch=ch,
                    out_ch=self.a2c_map[ch],
                    frm=frm,
                    text=text,
                    want_text=want_text,
                    rl=self.rl_a2c,
                    tag=self.tag_a2c,
                    other_tag=self.tag_c2a,
                    dst_local_id=self.local_id_c,
                )

        except Exception as e:
            print(f"[triple-bridge] _on_hub_packet ERROR: {type(e).__name__}: {e}", flush=True)

    # --------------------------------------------------------
    # RX callback (B/C en hub_mode=broker; A/B/C en hub_mode=tcp)
    # --------------------------------------------------------

    def _on_rx(self, interface=None, packet=None, **kwargs):
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}
            port = decoded.get("portnum") or decoded.get("portnum_name") or decoded.get("portnum_str") or decoded.get("portnumText")
            port_str = str(port).upper() if port is not None else ""

            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT"))
            want_pos = self.forward_position and (("POSITION" in port_str) or ("POSITION_APP" in port_str))
            if not (want_text or want_pos):
                return

            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch)
            except Exception:
                ch = 0

            frm = pkt.get("fromId") or pkt.get("from") or decoded.get("fromId") or decoded.get("from") or ""
            if isinstance(frm, int):
                frm = f"!{frm:08x}"
            frm = str(frm or "")

            text = ""
            if want_text:
                text = _norm_text(decoded.get("text") or decoded.get("payload") or "")

            payload_key = text if want_text else json.dumps(decoded, ensure_ascii=False, sort_keys=True)
            if self.dedup.seen(_hash_key(frm, ch, payload_key)):
                return

            came_from_a = (interface is self.iface_a) and (self.iface_a is not None)
            came_from_b = (interface is self.iface_b) and (self.iface_b is not None)
            came_from_c = (interface is self.iface_c) and (self.iface_c is not None)

            # anti-rebote por local_id
            if came_from_b and self.local_id_b and frm == self.local_id_b:
                return
            if came_from_c and self.local_id_c and frm == self.local_id_c:
                return
            if came_from_a and self.local_id_a and frm == self.local_id_a:
                return

            # A (solo en tcp)
            if came_from_a:
                if ch in self.a2b_map and self.iface_b is not None:
                    self._bridge_one("A", "B", self.iface_b, ch, self.a2b_map[ch], frm, text, want_text, self.rl_a2b, self.tag_a2b, self.tag_b2a, self.local_id_b)
                if ch in self.a2c_map and self.iface_c is not None:
                    self._bridge_one("A", "C", self.iface_c, ch, self.a2c_map[ch], frm, text, want_text, self.rl_a2c, self.tag_a2c, self.tag_c2a, self.local_id_c)
                return

            # B → A
            if came_from_b:
                if ch in self.b2a_map:
                    self._bridge_one("B", "A", self.iface_a, ch, self.b2a_map[ch], frm, text, want_text, self.rl_b2a, self.tag_b2a, self.tag_a2b, self.local_id_a)
                return

            # C → A
            if came_from_c:
                if ch in self.c2a_map:
                    self._bridge_one("C", "A", self.iface_a, ch, self.c2a_map[ch], frm, text, want_text, self.rl_c2a, self.tag_c2a, self.tag_a2c, self.local_id_a)
                return

        except Exception as e:
            print(f"[triple-bridge] _on_rx ERROR: {type(e).__name__}: {e}", flush=True)

    # --------------------------------------------------------
    # Bridge send
    # --------------------------------------------------------

    def _bridge_one(
        self,
        src_label: str,
        dst_label: str,
        dst_iface,
        ch: int,
        out_ch: int,
        frm: str,
        text: str,
        want_text: bool,
        rl: RateLimiter,
        tag: Optional[str],
        other_tag: Optional[str],
        dst_local_id: Optional[str],
    ) -> bool:
        if not rl.allow():
            print(f"[triple-bridge] {src_label}→{dst_label} RATE-LIMIT", flush=True)
            return False

        if not want_text:
            return False

        # anti-eco por etiqueta
        if other_tag and other_tag in (text or ""):
            return False

        # anti rebote hacia el mismo nodo
        if dst_local_id and frm == dst_local_id:
            return False

        msg = _norm_text(text)
        use_tag = tag or self.tag_base
        if use_tag and use_tag not in msg:
            msg = f"{msg} {use_tag}".strip()

        # Envío a A en modo broker
        if dst_label == "A" and self.hub_mode == "broker":
            return self._hub_send_text(msg, out_ch)

        # Envío por TCP
        if dst_iface is None:
            return False

        try:
            dst_iface.sendText(
                msg,
                destinationId="^all",
                wantAck=bool(self.require_ack),
                wantResponse=False,
                channelIndex=int(out_ch),
            )
            print(f"[triple-bridge] {src_label}→{dst_label} ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK", flush=True)
            return True
        except Exception as e:
            print(f"[triple-bridge] {src_label}→{dst_label} sendText ERROR: {type(e).__name__}: {e}", flush=True)
            return False

    def _hub_send_text(self, text: str, ch: int) -> bool:
        if not self._broker:
            return False
        try:
            ok = self._broker.send_text(text=text, ch=int(ch or 0), dest="broadcast", ack=bool(self.require_ack))
            if ok:
                print(f"[triple-bridge] →A(broker) ch={int(ch or 0)} txt({len(text.encode('utf-8'))}B) OK", flush=True)
            else:
                print(f"[triple-bridge] →A(broker) ch={int(ch or 0)} FAIL", flush=True)
            return ok
        except Exception as e:
            print(f"[triple-bridge] →A(broker) ERROR: {type(e).__name__}: {e}", flush=True)
            return False

    # --------------------------------------------------------
    # Run loop
    # --------------------------------------------------------

    def run_forever(self):
        while not self._stop.is_set():
            time.sleep(0.5)


# ============================================================
# Config parsing
# ============================================================

def _parse_ch_map(spec: str) -> dict[int, int]:
    """Parsea '0:0,2:2' -> {0:0, 2:2}."""
    out: dict[int, int] = {}
    spec = (spec or "").strip()
    if not spec:
        return out
    for part in spec.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        a, b = part.split(":", 1)
        try:
            out[int(a.strip())] = int(b.strip())
        except Exception:
            pass
    return out


def main():
    hub_mode = (os.getenv("HUB_MODE") or os.getenv("MODE") or "tcp").strip().lower()

    a_host = os.getenv("A_HOST", "127.0.0.1")
    a_port = int(os.getenv("A_PORT", "4403") or "4403")

    b_host = os.getenv("B_HOST", "")
    b_port = int(os.getenv("B_PORT", "4403") or "4403")

    c_host = os.getenv("C_HOST", "")
    c_port = int(os.getenv("C_PORT", "4403") or "4403")

    # Selección de peers (B/C)
    peers_spec = (os.getenv("BRIDGE_PEERS", "B,C") or "B,C").strip().upper()
    peers = {p.strip() for p in peers_spec.split(",") if p.strip()}
    enable_b = ("B" in peers) and bool((b_host or "").strip())
    enable_c = ("C" in peers) and bool((c_host or "").strip())

    broker_ctrl_host = os.getenv("BROKER_CTRL_HOST", "127.0.0.1")
    broker_ctrl_port = int(os.getenv("BROKER_CTRL_PORT", "8766") or "8766")
    broker_poll_sec = float(os.getenv("BROKER_POLL_SEC", "1.5") or "1.5")
    broker_fetch_limit = int(os.getenv("BROKER_FETCH_LIMIT", "2000") or "2000")
    broker_timeout_s = float(os.getenv("BROKER_TIMEOUT_S", "8.0") or "8.0")

    a2b = _parse_ch_map(os.getenv("A2B_CH_MAP", ""))
    b2a = _parse_ch_map(os.getenv("B2A_CH_MAP", ""))
    a2c = _parse_ch_map(os.getenv("A2C_CH_MAP", ""))
    c2a = _parse_ch_map(os.getenv("C2A_CH_MAP", ""))

    forward_text = (os.getenv("FORWARD_TEXT", "1").strip() != "0")
    forward_position = (os.getenv("FORWARD_POSITION", "0").strip() == "1")
    require_ack = (os.getenv("REQUIRE_ACK", "0").strip() == "1")

    rate = int(os.getenv("RATE_LIMIT_PER_SIDE", "8") or "8")
    dedup = int(os.getenv("DEDUP_TTL", "45") or "45")

    tag_base = os.getenv("TAG_BRIDGE", "[BRIDGE]").strip() or "[BRIDGE]"
    tag_a2b = (os.getenv("TAG_BRIDGE_A2B", "").strip() or None)
    tag_b2a = (os.getenv("TAG_BRIDGE_B2A", "").strip() or None)
    tag_a2c = (os.getenv("TAG_BRIDGE_A2C", "").strip() or None)
    tag_c2a = (os.getenv("TAG_BRIDGE_C2A", "").strip() or None)

    print(f"[bridgehub] MODE={hub_mode}", flush=True)
    print(f"[bridgehub] A={a_host}:{a_port}  B={b_host}:{b_port} ({'ON' if enable_b else 'OFF'})  C={c_host}:{c_port} ({'ON' if enable_c else 'OFF'})", flush=True)
    print(f"[bridgehub] BROKER_CTRL={broker_ctrl_host}:{broker_ctrl_port}", flush=True)

    br = TripleBridge(
        a_host=a_host,
        a_port=a_port,
        hub_mode=hub_mode,
        broker_ctrl_host=broker_ctrl_host,
        broker_ctrl_port=broker_ctrl_port,
        broker_poll_sec=broker_poll_sec,
        broker_fetch_limit=broker_fetch_limit,
        broker_timeout_s=broker_timeout_s,
        b_host=b_host,
        b_port=b_port,
        c_host=c_host,
        c_port=c_port,
        enable_b=enable_b,
        enable_c=enable_c,
        a2b_map=a2b,
        b2a_map=b2a,
        a2c_map=a2c,
        c2a_map=c2a,
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

    def _sig(_s, _f):
        br.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    br.connect()
    br.run_forever()


if __name__ == "__main__":
    main()
