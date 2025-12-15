#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mesh_triple_bridge.py

Pasarela 3-way:
  - Nodo A: central (normalmente el del broker/bot)
  - Nodo B: remoto 1
  - Nodo C: remoto 2

Modos:
  - hub_mode="tcp"    -> abre TCP directo a A (modo original)
  - hub_mode="broker" -> NO abre TCP a A. Lee RX de A desde BacklogServer (FETCH_BACKLOG)
                         y envía hacia A vía BacklogServer (SEND_TEXT)

Objetivo:
  - No romper nada: mantener lógica A<->B, A<->C con rate-limit y dedup.
  - Evitar reset por doble conexión a A.
"""

from __future__ import annotations

import os
import sys
import json
import time
import socket
import signal
import threading
import hashlib
from collections import deque
from typing import Optional, Dict

from pubsub import pub

try:
    from meshtastic.tcp_interface import TCPInterface as _TCPI
except Exception:
    _TCPI = None


# ============================================================
#  Helpers
# ============================================================

def _norm_text(s: str) -> str:
    return (str(s or "")).replace("\r\n", "\n").replace("\r", "\n").strip()


def _hash_key(from_id: str, ch: int, payload: str) -> str:
    """
    Clave de deduplicación GLOBAL.
    No incluye 'direction' para evitar bucles A↔B (mismo contenido en sentidos opuestos).
    """
    h = hashlib.sha256()
    h.update(str(from_id or "?").encode("utf-8"))
    h.update(str(int(ch)).encode("utf-8"))
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


# ============================================================
#  Estado auxiliar: rate-limit y deduplicación
# ============================================================

class RateLimiter:
    """
    Limitador sencillo: máximo N mensajes por minuto.
    """

    def __init__(self, max_per_min: int = 8):
        self.max = max(1, int(max_per_min))
        self.ts = deque()  # epoch seconds

    def allow(self) -> bool:
        now = time.time()
        while self.ts and (now - self.ts[0]) > 60.0:
            self.ts.popleft()
        if len(self.ts) < self.max:
            self.ts.append(now)
            return True
        return False


class DedupWindow:
    """
    Ventana de deduplicación basada en hash:
    - TTL en segundos (por defecto 45).
    """

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
#  Broker Hub (BacklogServer)
# ============================================================

class BrokerBacklogClient:
    """
    Cliente mínimo TCP (JSON por línea) para el BacklogServer del broker.

    Soporta:
      - FETCH_BACKLOG  (leer eventos persistidos del nodo A)
      - SEND_TEXT      (inyectar textos hacia A usando la cola TX del broker)

    Importante:
      - En tu stack, el puerto habitual de control/backlog es 8766.
    """

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
        data = resp.get("data") or resp.get("items") or []
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
#  TripleBridge
# ============================================================

class TripleBridge:
    """
    Reenvío:
      Desde A:
        - A → B (si canal en A2B_CH_MAP)
        - A → C (si canal en A2C_CH_MAP)

      Desde B:
        - B → A (si canal en B2A_CH_MAP)

      Desde C:
        - C → A (si canal en C2A_CH_MAP)
    """

    def __init__(
        self,
        a_host: str,
        a_port: int,
        hub_mode: str = "tcp",
        broker_ctrl_host: str | None = None,
        broker_ctrl_port: int | None = None,
        broker_poll_sec: float = 1.5,
        broker_fetch_limit: int = 2000,
        broker_timeout_s: float = 8.0,
        b_host: str = "",
        b_port: int = 4403,
        c_host: str = "",
        c_port: int = 4403,
        a2b_map: dict[int, int] | None = None,
        b2a_map: dict[int, int] | None = None,
        a2c_map: dict[int, int] | None = None,
        c2a_map: dict[int, int] | None = None,
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
    ):
        self.a_host, self.a_port = a_host, int(a_port or 4403)

        self.hub_mode = (hub_mode or "tcp").strip().lower()
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

        self._broker: BrokerBacklogClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._hub_last_ts: int | None = None

        self.b_host, self.b_port = b_host, int(b_port or 4403)
        self.c_host, self.c_port = c_host, int(c_port or 4403)

        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})
        self.a2c_map = dict(a2c_map or {})
        self.c2a_map = dict(c2a_map or {})

        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        base = str(tag_bridge or "").strip()
        self.tag_a2b = (tag_bridge_a2b.strip() if tag_bridge_a2b else base)
        self.tag_b2a = (tag_bridge_b2a.strip() if tag_bridge_b2a else base)
        self.tag_a2c = (tag_bridge_a2c.strip() if tag_bridge_a2c else base)
        self.tag_c2a = (tag_bridge_c2a.strip() if tag_bridge_c2a else base)

        self.iface_a = None
        self.iface_b = None
        self.iface_c = None

        self.local_id_a: Optional[str] = None
        self.local_id_b: Optional[str] = None
        self.local_id_c: Optional[str] = None

        self._lock = threading.RLock()
        self._stop = threading.Event()

        self.dedup = DedupWindow(dedup_ttl)
        self.rl_a2b = RateLimiter(rate_limit_per_side)
        self.rl_b2a = RateLimiter(rate_limit_per_side)
        self.rl_a2c = RateLimiter(rate_limit_per_side)
        self.rl_c2a = RateLimiter(rate_limit_per_side)

    # --------------------------------------------------------
    #  Lifecycle
    # --------------------------------------------------------

    def connect(self):
        if _TCPI is None:
            raise RuntimeError("meshtastic.tcp_interface no disponible en el entorno")

        # B y C siempre por TCP (son externos al broker)
        print(f"[triple-bridge] Conectando B: {self.b_host}:{self.b_port}", flush=True)
        self.iface_b = _TCPI(hostname=self.b_host, portNumber=self.b_port)

        print(f"[triple-bridge] Conectando C: {self.c_host}:{self.c_port}", flush=True)
        self.iface_c = _TCPI(hostname=self.c_host, portNumber=self.c_port)

        # A depende del modo
        if self.hub_mode == "tcp":
            print(f"[triple-bridge] Conectando A: {self.a_host}:{self.a_port}", flush=True)
            self.iface_a = _TCPI(hostname=self.a_host, portNumber=self.a_port)
            self._discover_local_ids()
            pub.subscribe(self._on_rx, "meshtastic.receive")
            print(f"[triple-bridge] Conectado. hub_mode=tcp local_id_a={self.local_id_a} local_id_b={self.local_id_b} local_id_c={self.local_id_c}", flush=True)
        else:
            # broker mode: NO abrir TCP a A
            if not self.broker_ctrl_host:
                raise RuntimeError("hub_mode=broker pero BROKER_CTRL_HOST no está definido")
            self._broker = BrokerBacklogClient(
                host=self.broker_ctrl_host,
                port=self.broker_ctrl_port,
                timeout_s=self.broker_timeout_s
            )
            # Suscripción solo a B/C (lo que llegue por sus TCP)
            pub.subscribe(self._on_rx, "meshtastic.receive")
            self._discover_local_ids(bc_only=True)
            # arranca polling del backlog del broker para lo que recibe A
            self._hub_last_ts = int(time.time()) - 5
            self._poll_thread = threading.Thread(target=self._poll_hub_loop, name="hub-poller", daemon=True)
            self._poll_thread.start()
            print(f"[triple-bridge] Conectado. hub_mode=broker local_id_a=None local_id_b={self.local_id_b} local_id_c={self.local_id_c} broker={self.broker_ctrl_host}:{self.broker_ctrl_port}", flush=True)

    def close(self):
        self._stop.set()
        try:
            pub.unsubscribe(self._on_rx, "meshtastic.receive")
        except Exception:
            pass

        for iface in (self.iface_a, self.iface_b, self.iface_c):
            try:
                if iface and hasattr(iface, "close"):
                    iface.close()
            except Exception:
                pass

    def _discover_local_ids(self, bc_only: bool = False):
        # A
        if not bc_only:
            try:
                la = getattr(self.iface_a, "myInfo", None) or {}
                self.local_id_a = la.get("my_node_num") or la.get("num") or la.get("id") or ""
                if isinstance(self.local_id_a, int):
                    self.local_id_a = f"!{self.local_id_a:08x}"
                self.local_id_a = str(self.local_id_a) if self.local_id_a else None
            except Exception:
                self.local_id_a = None

        # B
        try:
            lb = getattr(self.iface_b, "myInfo", None) or {}
            self.local_id_b = lb.get("my_node_num") or lb.get("num") or lb.get("id") or ""
            if isinstance(self.local_id_b, int):
                self.local_id_b = f"!{self.local_id_b:08x}"
            self.local_id_b = str(self.local_id_b) if self.local_id_b else None
        except Exception:
            self.local_id_b = None

        # C
        try:
            lc = getattr(self.iface_c, "myInfo", None) or {}
            self.local_id_c = lc.get("my_node_num") or lc.get("num") or lc.get("id") or ""
            if isinstance(self.local_id_c, int):
                self.local_id_c = f"!{self.local_id_c:08x}"
            self.local_id_c = str(self.local_id_c) if self.local_id_c else None
        except Exception:
            self.local_id_c = None

    # --------------------------------------------------------
    #  HUB polling (A RX desde broker backlog)
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
                    portnums=portnums or None,
                    limit=self.broker_fetch_limit
                )

                max_ts = self._hub_last_ts or 0
                for r in rows:
                    ts = r.get("ts") or r.get("rxTime") or r.get("timestamp") or r.get("time")
                    try:
                        ts_i = int(ts) if ts is not None else 0
                    except Exception:
                        ts_i = 0
                    if ts_i > max_ts:
                        max_ts = ts_i

                    self._process_hub_row_as_from_a(r)

                # avanzar ventana (para no re-leer lo mismo)
                if max_ts and max_ts >= (self._hub_last_ts or 0):
                    self._hub_last_ts = max_ts + 1

            except Exception as e:
                print(f"[triple-bridge] hub poll ERROR: {type(e).__name__}: {e}", flush=True)

            self._stop.wait(self.broker_poll_sec)

    def _process_hub_row_as_from_a(self, row: dict):
        """
        Convierte una fila del backlog del broker en un "paquete" equivalente a RX de A.
        """
        try:
            decoded = row.get("decoded") or row.get("payload") or {}
            if not isinstance(decoded, dict):
                decoded = {}

            # port
            port = decoded.get("portnum") or decoded.get("portnum_name") or row.get("portnum") or row.get("type") or ""
            port_str = str(port or "").upper()

            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or ("TEXT" == port_str))
            want_pos = self.forward_position and (("POSITION" in port_str) or ("POSITION_APP" in port_str))

            if not (want_text or want_pos):
                return

            # canal
            ch = row.get("channel")
            if ch is None:
                ch = decoded.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            frm = row.get("from") or row.get("fromId") or decoded.get("fromId") or decoded.get("from") or ""
            frm = str(frm or "")

            text = decoded.get("text") or decoded.get("payload") or row.get("text") or ""
            text = str(text or "")

            # A -> (B,C)
            self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos)

        except Exception as e:
            print(f"[triple-bridge] hub row parse ERROR: {type(e).__name__}: {e}", flush=True)

    # --------------------------------------------------------
    #  Mirror local (si se integra dentro del broker)
    # --------------------------------------------------------

    def mirror_from_a(self, ch: int, text: str, frm: str = "BROKER_TX") -> bool:
        """
        Útil si se ejecuta embebido en el broker: espeja un SEND_TEXT del broker hacia B/C.
        En modo contenedor separado no se llama, pero no molesta.
        """
        try:
            decoded = {"portnum": "TEXT_MESSAGE_APP", "text": str(text or ""), "channel": int(ch or 0)}
            self._bridge_from_a(int(ch or 0), str(frm or "BROKER_TX"), str(text or ""), decoded, True, False)
            return True
        except Exception:
            return False

    # --------------------------------------------------------
    #  RX handler pubsub (B/C y, si hub_mode=tcp, también A)
    # --------------------------------------------------------

    def _on_rx(self, interface=None, packet=None, **kwargs):
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}
            port = decoded.get("portnum") or decoded.get("portnum_name") \
                or decoded.get("portnum_str") or decoded.get("portnumText")

            port_str = str(port).upper() if port is not None else ""

            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or ("TEXT" == port_str))
            want_pos = self.forward_position and (("POSITION" in port_str) or ("POSITION_APP" in port_str))
            if not (want_text or want_pos):
                return

            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            frm = pkt.get("fromId") or decoded.get("fromId") or pkt.get("from")
            frm = str(frm or "")

            text = decoded.get("text") or decoded.get("payload") or ""
            text = str(text or "")

            came_from_a = (interface is self.iface_a) if self.iface_a is not None else False
            came_from_b = (interface is self.iface_b)
            came_from_c = (interface is self.iface_c)

            if not (came_from_a or came_from_b or came_from_c):
                return

            if came_from_a:
                self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_b:
                self._bridge_from_b(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_c:
                self._bridge_from_c(ch, frm, text, decoded, want_text, want_pos)

        except Exception as e:
            print(f"[triple-bridge] on_rx error: {type(e).__name__}: {e}", flush=True)

    # --------------------------------------------------------
    #  Operaciones por origen
    # --------------------------------------------------------

    def _bridge_from_a(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
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
                decoded=decoded,
                want_text=want_text,
                want_pos=want_pos,
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
                decoded=decoded,
                want_text=want_text,
                want_pos=want_pos,
                rl=self.rl_a2c,
                tag=self.tag_a2c,
                other_tag=self.tag_c2a,
                dst_local_id=self.local_id_c,
            )

    def _bridge_from_b(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
        """
        B → A (en broker-mode: SEND_TEXT al broker; en tcp-mode: sendText directo a iface_a)
        """
        if ch not in self.b2a_map:
            return
        out_ch = self.b2a_map[ch]

        if self.local_id_a and frm == self.local_id_a:
            return

        if not self.rl_b2a.allow():
            return

        payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
        key = _hash_key(frm, ch, payload_for_hash)
        if self.dedup.seen(key):
            return

        if want_text:
            msg = _norm_text(text)

            # anti-eco por etiqueta: si ya viene marcado como A->B, no reinyectar
            if self.tag_a2b and self.tag_a2b in msg:
                return

            if self.tag_b2a and self.tag_b2a not in msg:
                msg = f"{self.tag_b2a} {msg}"

            if self.hub_mode == "broker":
                ok = self._send_text_to_hub(msg, out_ch)
                print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) {'OK' if ok else 'ERROR'} via BROKER", flush=True)
            else:
                if self.iface_a is None:
                    return
                try:
                    self.iface_a.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=bool(self.require_ack),
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK", flush=True)
                except Exception as e:
                    print(f"[triple-bridge] B2A B->A sendText ERROR: {type(e).__name__}: {e}", flush=True)

    def _bridge_from_c(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
        """
        C → A (en broker-mode: SEND_TEXT al broker; en tcp-mode: sendText directo a iface_a)
        """
        if ch not in self.c2a_map:
            return
        out_ch = self.c2a_map[ch]

        if self.local_id_a and frm == self.local_id_a:
            return

        if not self.rl_c2a.allow():
            return

        payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
        key = _hash_key(frm, ch, payload_for_hash)
        if self.dedup.seen(key):
            return

        if want_text:
            msg = _norm_text(text)

            # anti-eco por etiqueta: si ya viene marcado como A->C, no reinyectar
            if self.tag_a2c and self.tag_a2c in msg:
                return

            if self.tag_c2a and self.tag_c2a not in msg:
                msg = f"{self.tag_c2a} {msg}"

            if self.hub_mode == "broker":
                ok = self._send_text_to_hub(msg, out_ch)
                print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) {'OK' if ok else 'ERROR'} via BROKER", flush=True)
            else:
                if self.iface_a is None:
                    return
                try:
                    self.iface_a.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=bool(self.require_ack),
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK", flush=True)
                except Exception as e:
                    print(f"[triple-bridge] C2A C->A sendText ERROR: {type(e).__name__}: {e}", flush=True)

    # --------------------------------------------------------
    #  Reenvío individual A->B/A->C
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
        decoded: dict,
        want_text: bool,
        want_pos: bool,
        rl: RateLimiter,
        tag: Optional[str],
        other_tag: Optional[str],
        dst_local_id: Optional[str],
    ):
        try:
            if dst_local_id and frm == dst_local_id:
                return

            if not rl.allow():
                return

            if want_text:
                payload_for_hash = _norm_text(text)
            else:
                payload_for_hash = json.dumps(decoded, sort_keys=True, ensure_ascii=False)

            key = _hash_key(frm, ch, payload_for_hash)
            if self.dedup.seen(key):
                return

            if want_text:
                msg = _norm_text(text)

                # anti-eco: si ya viene con etiqueta del otro sentido, no reinyectar
                if other_tag and other_tag in msg:
                    return

                if tag and tag not in msg:
                    msg = f"{tag} {msg}"

                try:
                    dst_iface.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=bool(self.require_ack),
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    print(f"[triple-bridge] {src_label}->{dst_label} ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK", flush=True)
                except Exception as e:
                    print(f"[triple-bridge] {src_label}->{dst_label} sendText ERROR: {type(e).__name__}: {e}", flush=True)

        except Exception as e:
            print(f"[triple-bridge] _bridge_one error: {type(e).__name__}: {e}", flush=True)

    # --------------------------------------------------------
    #  Envío hacia A vía broker
    # --------------------------------------------------------

    def _send_text_to_hub(self, text: str, ch: int) -> bool:
        try:
            if not self._broker:
                return False
            return self._broker.send_text(text=text, ch=int(ch or 0), dest="broadcast", ack=bool(self.require_ack))
        except Exception:
            return False

    # --------------------------------------------------------
    #  Run loop
    # --------------------------------------------------------

    def run_forever(self):
        while not self._stop.is_set():
            time.sleep(0.5)


# ============================================================
#  Entrypoint
# ============================================================

def _parse_map(s: str) -> dict[int, int]:
    out: dict[int, int] = {}
    s = (s or "").strip()
    if not s:
        return out
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
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

    broker_ctrl_host = os.getenv("BROKER_CTRL_HOST", "127.0.0.1")
    broker_ctrl_port = int(os.getenv("BROKER_CTRL_PORT", "8766") or "8766")

    a2b = _parse_map(os.getenv("A2B_CH_MAP", "0:0"))
    b2a = _parse_map(os.getenv("B2A_CH_MAP", "0:0"))
    a2c = _parse_map(os.getenv("A2C_CH_MAP", "0:0"))
    c2a = _parse_map(os.getenv("C2A_CH_MAP", "0:0"))

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
    print(f"[bridgehub] A={a_host}:{a_port}  B={b_host}:{b_port}  C={c_host}:{c_port}", flush=True)
    print(f"[bridgehub] BROKER_CTRL={broker_ctrl_host}:{broker_ctrl_port}", flush=True)

    br = TripleBridge(
        a_host=a_host,
        a_port=a_port,
        hub_mode=hub_mode,
        broker_ctrl_host=broker_ctrl_host,
        broker_ctrl_port=broker_ctrl_port,
        b_host=b_host,
        b_port=b_port,
        c_host=c_host,
        c_port=c_port,
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
