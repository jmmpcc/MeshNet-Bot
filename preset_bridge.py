#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preset_bridge.py v6.1.1 — Pasarela entre dos nodos Meshtastic (IP↔IP) con presets/canales distintos.
Funciones públicas clave:
  - start_preset_bridge(...)
  - stop_preset_bridge(...)
  - bridge_is_running()
  - bridge_status()

Modo standalone:
  python preset_bridge.py --a 192.168.1.201 --b 192.168.1.202 --a-port 4403 --b-port 4403
"""

from __future__ import annotations
import os, time, json, threading, argparse, hashlib, re, atexit
from collections import deque
from typing import Optional, Dict

# pubsub del SDK Meshtastic
from pubsub import pub

# Elige TCPInterface de tu pool persistente si está disponible
_TCPIF = None
try:
    from tcpinterface_persistent import TCPInterface as _POOL_TCPIF  # type: ignore
    _TCPIF = _POOL_TCPIF
except Exception:
    try:
        from meshtastic.tcp_interface import TCPInterface as _SDK_TCPIF  # type: ignore
        _TCPIF = _SDK_TCPIF
    except Exception as e:
        raise SystemExit(f"[preset_bridge] FATAL: no puedo importar TCPInterface: {e}")

# ------------------ Utils ------------------

def _truthy(s: str | None, default: bool=False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in {"1","true","t","yes","y","on","si","sí"}

def _parse_ch_map(s: str | None) -> dict[int,int]:
    """'0:0,2:0,3:1' -> {0:0,2:0,3:1}"""
    out: dict[int,int] = {}
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
    rep = {'“':'"', '”':'"', '’':"'", '‘':"'", '—':'-', '–':'-', '…':'...', '\u00A0':' '}
    s = s.translate(str.maketrans(rep))
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _hash_key(direction: str, from_id: str, ch: int, text: str) -> str:
    h = hashlib.sha256()
    h.update(direction.encode())
    h.update(str(from_id or "?").encode())
    h.update(str(int(ch)).encode())
    h.update((_norm_text(text)).encode("utf-8"))
    return h.hexdigest()

class _RateLimiter:
    def __init__(self, max_per_min: int = 8):
        self.max = max(1, int(max_per_min))
        self.ts = deque()  # epoch

    def allow(self) -> bool:
        now = time.time()
        while self.ts and (now - self.ts[0]) > 60.0:
            self.ts.popleft()
        if len(self.ts) < self.max:
            self.ts.append(now)
            return True
        return False

class _DedupWindow:
    def __init__(self, ttl_sec: int = 45):
        self.ttl = max(5, int(ttl_sec))
        self.store: Dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        # purge expirados
        for k in list(self.store.keys()):
            if (now - self.store[k]) > self.ttl:
                self.store.pop(k, None)
        if key in self.store:
            return True
        self.store[key] = now
        return False

# ------------------ Núcleo puente ------------------

class PresetBridge:
    """
    Pasarela A<->B. Conecta a ambos nodos (IP/TCP) y reenvía (texto/pos) según mapeo de canales.
    Anti-bucle, dedup y rate-limit por sentido.
    """
    def __init__(
        self,
        a_host: str, a_port: int,
        b_host: str, b_port: int,
        a2b_map: dict[int,int],
        b2a_map: dict[int,int],
        forward_text: bool = True,
        forward_position: bool = False,
        require_ack: bool = False,
        rate_limit_per_side: int = 8,
        dedup_ttl: int = 45,
        tag_bridge: str = "[BRIDGE]"
    ):
        self.a_host, self.a_port = a_host, int(a_port or 4403)
        self.b_host, self.b_port = b_host, int(b_port or 4403)
        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})
        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)
        self.tag_bridge = str(tag_bridge or "").strip()

        self.iface_a = None
        self.iface_b = None
        self._stop = threading.Event()
        self._running = False

        self.dedup = _DedupWindow(dedup_ttl)
        self.rl_a2b = _RateLimiter(rate_limit_per_side)
        self.rl_b2a = _RateLimiter(rate_limit_per_side)

        self.local_id_a = None
        self.local_id_b = None

        self._lock = threading.RLock()

    # ----- ciclo de vida -----
    def start(self):
        with self._lock:
            if self._running:
                return
            self._connect()
            pub.subscribe(self._on_rx, "meshtastic.receive")
            self._running = True
            print("[preset_bridge] ✅ Iniciada")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            try:
                pub.unsubscribe(self._on_rx, "meshtastic.receive")
            except Exception:
                pass
            try:
                if self.iface_a and hasattr(self.iface_a, "close"):
                    self.iface_a.close()
            except Exception:
                pass
            try:
                if self.iface_b and hasattr(self.iface_b, "close"):
                    self.iface_b.close()
            except Exception:
                pass
            self._running = False
            print("[preset_bridge] 🛑 Detenida")

    def is_running(self) -> bool:
        return self._running

    def status(self) -> dict:
        return {
            "running": self._running,
            "a": {"host": self.a_host, "port": self.a_port, "local_id": self.local_id_a},
            "b": {"host": self.b_host, "port": self.b_port, "local_id": self.local_id_b},
            "maps": {"A2B": self.a2b_map, "B2A": self.b2a_map},
            "opts": {
                "forward_text": self.forward_text,
                "forward_position": self.forward_position,
                "require_ack": self.require_ack,
                "tag": self.tag_bridge
            }
        }

    # ----- conexión -----
    def _connect(self):
        print(f"[preset_bridge] Conectando A: {self.a_host}:{self.a_port}")
        self.iface_a = _TCPIF(hostname=self.a_host, port=self.a_port)
        print(f"[preset_bridge] Conectando B: {self.b_host}:{self.b_port}")
        self.iface_b = _TCPIF(hostname=self.b_host, port=self.b_port)

        # Local IDs (si el SDK expone myInfo/num/id)
        try:
            la = getattr(self.iface_a, "myInfo", None) or {}
            n = la.get("my_node_num") or la.get("num") or la.get("id")
            self.local_id_a = f"!{int(n):08x}" if isinstance(n, int) else (n or None)
        except Exception:
            self.local_id_a = None

        try:
            lb = getattr(self.iface_b, "myInfo", None) or {}
            n = lb.get("my_node_num") or lb.get("num") or lb.get("id")
            self.local_id_b = f"!{int(n):08x}" if isinstance(n, int) else (n or None)
        except Exception:
            self.local_id_b = None

        print(f"[preset_bridge] Conectado. local_id_a={self.local_id_a} local_id_b={self.local_id_b}")

    # ----- RX handler -----
    def _on_rx(self, interface=None, packet=None, **kwargs):
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}
            port = decoded.get("portnum") or decoded.get("portnum_name") \
                   or decoded.get("portnum_str") or decoded.get("portnumText")

            # Qué tipos de app queremos
            port_str = str(port).upper() if port is not None else ""
            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT"))
            want_pos  = self.forward_position and (
                ("POSITION_APP" in port_str) or (port_str == "POSITION") or
                ("TELEMETRY_APP" in port_str) or (port_str == "TELEMETRY")
            )
            if not (want_text or want_pos):
                return

            # Canal
            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            # Emisor
            frm = str(pkt.get("fromId") or decoded.get("fromId") or pkt.get("from") or "")

            # Texto
            text = str(decoded.get("text") or decoded.get("payload") or "")

            came_from_a = (interface is self.iface_a)
            came_from_b = (interface is self.iface_b)
            if not (came_from_a or came_from_b):
                return

            # Anti-bucle: no reenvíes si el emisor es el local del destino
            if came_from_a and self.local_id_b and frm == self.local_id_b:
                return
            if came_from_b and self.local_id_a and frm == self.local_id_a:
                return

            # Mapeo canal + limitación por sentido
            if came_from_a:
                if ch not in self.a2b_map:
                    return
                out_ch = self.a2b_map[ch]
                direction = "A2B"
                if not self.rl_a2b.allow():
                    return
                target = self.iface_b
            else:
                if ch not in self.b2a_map:
                    return
                out_ch = self.b2a_map[ch]
                direction = "B2A"
                if not self.rl_b2a.allow():
                    return
                target = self.iface_a

            # Deduplicación
            key = _hash_key(direction, frm, ch, text if want_text else json.dumps(decoded, sort_keys=True))
            if self.dedup.seen(key):
                return

            # Reenvío
            if want_text:
                msg = _norm_text(text)
                if self.tag_bridge and self.tag_bridge not in msg:
                    msg = f"{self.tag_bridge} {msg}"
                try:
                    target.sendText(msg, channel=out_ch, wantAck=self.require_ack)
                    print(f"[preset_bridge] {direction} ch {ch}->{out_ch} txt OK")
                except Exception as e:
                    print(f"[preset_bridge] {direction} sendText ERROR: {type(e).__name__}: {e}")
            elif want_pos:
                # Resumen ligero como texto (opcional, para no construir POSITION_APP)
                try:
                    summary = {
                        "via": "bridge",
                        "from": frm[-8:] if frm else "?",
                        "lat": decoded.get("position", {}).get("latitude"),
                        "lon": decoded.get("position", {}).get("longitude"),
                        "alt": decoded.get("position", {}).get("altitude"),
                        "bat": decoded.get("deviceMetrics", {}).get("batteryLevel"),
                    }
                    msg = f"{self.tag_bridge} POS {summary}" if self.tag_bridge else f"POS {summary}"
                    target.sendText(msg, channel=out_ch, wantAck=False)
                    print(f"[preset_bridge] {direction} ch {ch}->{out_ch} pos OK")
                except Exception as e:
                    print(f"[preset_bridge] {direction} sendPOS ERROR: {type(e).__name__}: {e}")

        except Exception as e:
            print(f"[preset_bridge] on_rx error: {type(e).__name__}: {e}")

# ------------------ API sencilla para importar ------------------

_BRIDGE: Optional[PresetBridge] = None

def start_preset_bridge(
    a_host: str, b_host: str,
    a_port: int = 4403, b_port: int = 4403,
    a2b_map: dict[int,int] | None = None,
    b2a_map: dict[int,int] | None = None,
    forward_text: bool = True,
    forward_position: bool = False,
    require_ack: bool = False,
    rate_limit_per_side: int = 8,
    dedup_ttl: int = 45,
    tag_bridge: str = "[BRIDGE]"
) -> dict:
    """
    Arranca la pasarela si no está ya corriendo.
    Devuelve un dict con 'ok': True y 'status': {...}
    """
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        return {"ok": True, "already_running": True, "status": _BRIDGE.status()}

    a2b = dict(a2b_map or {})
    b2a = dict(b2a_map or {})

    _BRIDGE = PresetBridge(
        a_host, a_port, b_host, b_port,
        a2b, b2a,
        forward_text=forward_text,
        forward_position=forward_position,
        require_ack=require_ack,
        rate_limit_per_side=rate_limit_per_side,
        dedup_ttl=dedup_ttl,
        tag_bridge=tag_bridge
    )
    _BRIDGE.start()
    return {"ok": True, "status": _BRIDGE.status()}

def stop_preset_bridge() -> dict:
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        _BRIDGE.stop()
        return {"ok": True}
    return {"ok": True, "already_stopped": True}

def bridge_is_running() -> bool:
    return bool(_BRIDGE and _BRIDGE.is_running())

def bridge_status() -> dict:
    if _BRIDGE:
        return _BRIDGE.status()
    return {"running": False}

# Parada segura al salir si se ejecuta standalone
def _atexit_cleanup():
    try:
        stop_preset_bridge()
    except Exception:
        pass

atexit.register(_atexit_cleanup)

# ------------------ CLI standalone ------------------

def _cli_main():
    ap = argparse.ArgumentParser(description="Pasarela Meshtastic A<->B (IP)")
    ap.add_argument("--a", dest="a_host", default=os.getenv("A_HOST", "").strip(), help="Host nodo A")
    ap.add_argument("--b", dest="b_host", default=os.getenv("B_HOST", "").strip(), help="Host nodo B")
    ap.add_argument("--a-port", dest="a_port", type=int, default=int(os.getenv("A_PORT", "4403")), help="Puerto TCP A")
    ap.add_argument("--b-port", dest="b_port", type=int, default=int(os.getenv("B_PORT", "4403")), help="Puerto TCP B")
    ap.add_argument("--a2b", dest="a2b", default=os.getenv("A2B_CH_MAP", "0:0"), help="Mapeo canales A->B")
    ap.add_argument("--b2a", dest="b2a", default=os.getenv("B2A_CH_MAP", "0:0"), help="Mapeo canales B->A")
    ap.add_argument("--no-text", action="store_true", help="No reenviar TEXT_MESSAGE_APP")
    ap.add_argument("--pos", action="store_true", help="Reenviar POSITION/TELEMETRY como resumen texto")
    ap.add_argument("--ack", action="store_true", help="Pedir ACK en sendText")
    ap.add_argument("--rate", type=int, default=int(os.getenv("RATE_LIMIT_PER_SIDE", "8")), help="msgs/min por sentido")
    ap.add_argument("--dedup", type=int, default=int(os.getenv("DEDUP_TTL", "45")), help="TTL dedup (s)")
    ap.add_argument("--tag", default=os.getenv("TAG_BRIDGE", "[BRIDGE]"), help="Etiqueta para textos reenviados")
    args = ap.parse_args()

    if not args.a_host or not args.b_host:
        raise SystemExit("Debe indicar A_HOST y B_HOST (o usar --a/--b).")

    st = start_preset_bridge(
        args.a_host, args.b_host,
        args.a_port, args.b_port,
        a2b_map=_parse_ch_map(args.a2b),
        b2a_map=_parse_ch_map(args.b2a),
        forward_text=(not args.no_text),
        forward_position=bool(args.pos),
        require_ack=bool(args.ack),
        rate_limit_per_side=args.rate,
        dedup_ttl=args.dedup,
        tag_bridge=args.tag
    )
    print("[preset_bridge] Estado:", json.dumps(st["status"], ensure_ascii=False))
    # Bucle mínimo para que el proceso viva
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    _cli_main()
