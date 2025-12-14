#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mesh_triple_bridge.py  — Pasarela externa A↔B y A↔C usando TCP Meshtastic.

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
    ):
        self.a_host, self.a_port = a_host, int(a_port or 4403)
        self.b_host, self.b_port = b_host, int(b_port or 4403)
        self.c_host, self.c_port = c_host, int(c_port or 4403)

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

        self._broker: BrokerBacklogClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._hub_last_ts: int | None = None


    def connect(self):
        """
        Conecta interfaces:
        - HUB_MODE=broker: NO abre TCP a A. Usa BacklogServer del broker.
        - HUB_MODE=tcp: abre TCP a A, B y C.

        Mejora: timeout TCP configurable (TCP_TIMEOUT_S) y modo degradado si C falla.
        """
        # Timeout TCP para handshakes lentos (WiFi justo, CPU baja, etc.)
        try:
            tcp_timeout_s = float(os.getenv("TCP_TIMEOUT_S") or 25)
        except Exception:
            tcp_timeout_s = 25.0

        if self.hub_mode == "broker":
            print("[triple-bridge] HUB_MODE=broker: NO se abre TCP a A (se usa broker backlog).")
            if not self.broker_ctrl_host:
                raise SystemExit("Falta BROKER_CTRL_HOST en HUB_MODE=broker")
            self._broker = BrokerBacklogClient(
                host=self.broker_ctrl_host,
                port=int(self.broker_ctrl_port or 8766),
                timeout_s=float(self.broker_timeout_s or 8.0),
            )
        else:
            print(f"[triple-bridge] Conectando A: {self.a_host}:{self.a_port}")
            self.iface_a = _TCPI(hostname=self.a_host, portNumber=self.a_port, timeout=tcp_timeout_s)
            self.local_id_a = self._discover_local_id(self.iface_a, "A")

        # B (obligatorio)
        print(f"[triple-bridge] Conectando B: {self.b_host}:{self.b_port}")
        try:
            self.iface_b = _TCPI(hostname=self.b_host, portNumber=self.b_port, timeout=tcp_timeout_s)
            self.local_id_b = self._discover_local_id(self.iface_b, "B")
        except Exception as e:
            self.iface_b = None
            self.local_id_b = None
            raise SystemExit(f"No se pudo conectar B ({self.b_host}:{self.b_port}): {type(e).__name__}: {e}")

        # C (degradado si falla)
        print(f"[triple-bridge] Conectando C: {self.c_host}:{self.c_port}")
        try:
            self.iface_c = _TCPI(hostname=self.c_host, portNumber=self.c_port, timeout=tcp_timeout_s)
            self.local_id_c = self._discover_local_id(self.iface_c, "C")
        except Exception as e:
            self.iface_c = None
            self.local_id_c = None
            print(f"[triple-bridge] ⚠️ No se pudo conectar C ({self.c_host}:{self.c_port}): {type(e).__name__}: {e}")

        pub.subscribe(self._on_rx, "meshtastic.receive")

        if self.hub_mode == "broker":
            self._poll_thread = threading.Thread(target=self._hub_poll_loop, name="hub-poll", daemon=True)
            self._poll_thread.start()

        print(
            f"[triple-bridge] Conectado. hub_mode={self.hub_mode} "
            f"local_id_a={self.local_id_a} local_id_b={self.local_id_b} local_id_c={self.local_id_c}"
        )



    def _discover_local_id(self, iface, label: str) -> Optional[str]:
        """
        Intenta obtener el id local de la interfaz.
        En Meshtastic, myInfo puede tardar en llegar tras abrir TCP, así que reintentamos.
        """
        for _ in range(20):  # ~10s si sleep=0.5
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
                    print(f"[triple-bridge] local_id_{label}={s}")
                    return s
            except Exception:
                pass
            time.sleep(0.5)
        print(f"[triple-bridge] ⚠️ local_id_{label} no disponible (myInfo no llegó a tiempo)")
        return None



    def close(self):
        self._stop.set()
        try:
            pub.unsubscribe(self._on_rx, "meshtastic.receive")
        except Exception:
            pass

        for name, iface in (("A", self.iface_a), ("B", self.iface_b), ("C", self.iface_c)):
            try:
                if iface and hasattr(iface, "close"):
                    iface.close()
                    print(f"[triple-bridge] Cerrada interfaz {name}")
            except Exception as e:
                print(f"[triple-bridge] Error cerrando interfaz {name}: {type(e).__name__}: {e}")

    def run_forever(self):
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    # ---------------------- HUB (broker backlog) ----------------------

    def _hub_poll_loop(self) -> None:
        if self._hub_last_ts is None:
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
            except Exception as e:
                print(f"[triple-bridge] HUB poll error: {type(e).__name__}: {e}")

            time.sleep(float(self.broker_poll_sec or 1.5))

    def _process_hub_event(self, obj: dict) -> None:
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

            self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos)

        except Exception as e:
            print(f"[triple-bridge] HUB event error: {type(e).__name__}: {e}")

    def _send_text_to_hub(self, text: str, ch: int) -> bool:
        msg = _norm_text(text or "")
        if not msg:
            return False

        if self.hub_mode == "broker":
            if not self._broker:
                return False
            return self._broker.send_text(msg, ch=int(ch or 0), dest=None, ack=False)

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

    # ---------------------- RX handler (B/C y A en modo tcp) ----------------------

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
            came_from_b = (interface is self.iface_b)
            came_from_c = (interface is self.iface_c)

            if came_from_a:
                self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_b:
                self._bridge_from_b(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_c:
                self._bridge_from_c(ch, frm, text, decoded, want_text, want_pos)

        except Exception as e:
            print(f"[triple-bridge] on_rx error: {type(e).__name__}: {e}")

    # ---------------------- Bridging por origen ----------------------

    def _bridge_from_a(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
        if ch in self.a2b_map and self.iface_b is not None:
            self._bridge_one(
                direction="A2B",
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
                dst_local_id=self.local_id_b,
            )

        if ch in self.a2c_map and self.iface_c is not None:
            self._bridge_one(
                direction="A2C",
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
                dst_local_id=self.local_id_c,
            )

    def _bridge_from_b(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
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
            if self.tag_b2a and self.tag_b2a not in msg:
                msg = f"{self.tag_b2a} {msg}"
            ok = self._send_text_to_hub(msg, out_ch)
            print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} txt OK" if ok else "[triple-bridge] B2A B->A sendText ERROR")

        elif want_pos:
            # resumen opcional
            summary = {"via": "triple-bridge", "from": frm[-8:] if frm else "?"}
            msg = f"POS {summary}"
            if self.tag_b2a:
                msg = f"{self.tag_b2a} {msg}"
            ok = self._send_text_to_hub(msg, out_ch)
            if ok:
                print(f"[triple-bridge] B2A B->A POS OK")

    def _bridge_from_c(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
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
            if self.tag_c2a and self.tag_c2a not in msg:
                msg = f"{self.tag_c2a} {msg}"
            ok = self._send_text_to_hub(msg, out_ch)
            print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} txt OK" if ok else "[triple-bridge] C2A C->A sendText ERROR")

        elif want_pos:
            summary = {"via": "triple-bridge", "from": frm[-8:] if frm else "?"}
            msg = f"POS {summary}"
            if self.tag_c2a:
                msg = f"{self.tag_c2a} {msg}"
            ok = self._send_text_to_hub(msg, out_ch)
            if ok:
                print(f"[triple-bridge] C2A C->A POS OK")

    def _bridge_one(
        self,
        direction: str,
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
        dst_local_id: Optional[str],
    ):
        try:
            if dst_local_id and frm == dst_local_id:
                return
            if not rl.allow():
                return

            payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
            if self.dedup.seen(_hash_key(direction, frm, ch, payload_for_hash)):
                return

            if want_text:
                msg = _norm_text(text)
                if tag and tag not in msg:
                    msg = f"{tag} {msg}"
                dst_iface.sendText(
                    msg,
                    destinationId="^all",
                    wantAck=bool(self.require_ack),
                    wantResponse=False,
                    channelIndex=int(out_ch),
                )
                print(f"[triple-bridge] {direction} {src_label}->{dst_label} ch {ch}->{out_ch} txt OK")

            elif want_pos:
                msg = f"POS via {src_label}"
                if tag:
                    msg = f"{tag} {msg}"
                dst_iface.sendText(
                    msg,
                    destinationId="^all",
                    wantAck=False,
                    wantResponse=False,
                    channelIndex=int(out_ch),
                )
                print(f"[triple-bridge] {direction} {src_label}->{dst_label} POS OK")

        except Exception as e:
            print(f"[triple-bridge] {direction} {src_label}->{dst_label} ERROR: {type(e).__name__}: {e}")


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

    # Validaciones mínimas
    if not b_host or not c_host:
        raise SystemExit("Faltan B_HOST y/o C_HOST")

    if hub_mode != "broker":
        if not a_host:
            raise SystemExit("Falta A_HOST en HUB_MODE=tcp")
    else:
        # En broker, A_HOST puede estar vacío (no abrimos TCP a A).
        # Se recomienda dejarlo por claridad en logs, pero no es obligatorio.
        pass

    print(f"[triple-bridge] hub_mode={hub_mode}")
    if hub_mode == "broker":
        print("[triple-bridge] HUB_MODE=broker: NO se abre TCP a A (se usa broker backlog).")

    bridge = TripleBridge(
        a_host=a_host,
        a_port=a_port,
        b_host=b_host,
        b_port=b_port,
        c_host=c_host,
        c_port=c_port,
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
