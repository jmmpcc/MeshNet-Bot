#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mesh_preset_bridge.py v6.1.1 — Pasarela entre dos nodos Meshtastic (IP↔IP) con presets distintos.

- Conecta por TCP a Nodo A y Nodo B y reenvía mensajes entre ambos.
- Anti-bucle y anti-duplicados.
- Mapeo de canales A→B y B→A.
- Opcional: incluir POSITION_APP/TELEMETRY_APP.
- No modifica tus scripts existentes.

Dependencias:
  pip install meshtastic==2.* pubsub

Variables de entorno principales:
  A_HOST, A_PORT (default 4403)
  B_HOST, B_PORT (default 4403)
  A2B_CH_MAP="0:0,1:1,2:0"   (ejemplo; formato <chA>:<chB>)
  B2A_CH_MAP="0:0,1:1"
  FORWARD_TEXT=1
  FORWARD_POSITION=0
  REQUIRE_ACK=0
  RATE_LIMIT_PER_SIDE=8        # mensajes/60s por sentido
  DEDUP_TTL=45                 # segundos
  TAG_BRIDGE="[BRIDGE]"        # prefijo opcional en texto reenviado, vacío para no añadir

Uso:
  python mesh_preset_bridge.py --a 192.168.1.10 --b 192.168.1.11
o con .env:
  A_HOST=192.168.1.10
  B_HOST=192.168.1.11
"""

from __future__ import annotations
import os, time, json, threading, argparse, hashlib, re
from collections import deque, defaultdict
from typing import Optional, Tuple

# pubsub para eventos de meshtastic
from pubsub import pub

# Intentar usar tu pool persistente si está disponible; si no, TCPInterface nativa
_TCPI = None
try:
    # Tu proyecto suele tener un pool persistente
    from tcpinterface_persistent import TCPInterface as _POOL_TCPInterface  # type: ignore
    _TCPI = _POOL_TCPInterface
except Exception:
    try:
        from meshtastic.tcp_interface import TCPInterface as _SDK_TCPInterface  # type: ignore
        _TCPI = _SDK_TCPInterface
    except Exception as e:
        raise SystemExit(f"[FATAL] No se pudo importar TCPInterface: {e}")

# --------- Utils de entorno ----------
def _truthy(s: str | None, default: bool=False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in {"1","true","t","yes","y","on","si","sí"}

def _parse_ch_map(s: str | None) -> dict[int,int]:
    """
    "0:0,1:1,2:0" -> {0:0,1:1,2:0}
    """
    out = {}
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

# --------- Estado de pasarela ----------
class RateLimiter:
    def __init__(self, max_per_min: int = 8):
        self.max = max(1, int(max_per_min))
        self.ts = deque()  # epoch seconds

    def allow(self) -> bool:
        now = time.time()
        # purgar >60s
        while self.ts and (now - self.ts[0]) > 60.0:
            self.ts.popleft()
        if len(self.ts) < self.max:
            self.ts.append(now)
            return True
        return False

class DedupWindow:
    def __init__(self, ttl_sec: int = 45):
        self.ttl = max(5, int(ttl_sec))
        self.store: dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        # purge
        dead = [k for k, ts in self.store.items() if (now - ts) > self.ttl]
        for k in dead:
            self.store.pop(k, None)
        if key in self.store:
            return True
        self.store[key] = now
        return False

class Bridge:
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
        tag_bridge: str = "[BRIDGE]",
        tag_bridge_a2b: str | None = None,
        tag_bridge_b2a: str | None = None,
    ):
        self.a_host, self.a_port = a_host, int(a_port or 4403)
        self.b_host, self.b_port = b_host, int(b_port or 4403)
        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})
        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        # Etiquetas
        base = str(tag_bridge or "").strip()
        self.tag_bridge_a2b = (tag_bridge_a2b.strip() if tag_bridge_a2b else base)
        self.tag_bridge_b2a = (tag_bridge_b2a.strip() if tag_bridge_b2a else base)

        self.iface_a = None
        self.iface_b = None
        self._lock = threading.RLock()
        self._stop = threading.Event()

        self.dedup = DedupWindow(dedup_ttl)
        self.rl_a2b = RateLimiter(rate_limit_per_side)
        self.rl_b2a = RateLimiter(rate_limit_per_side)

        # local IDs para evitar rebotes desde el destino
        self.local_id_a = None
        self.local_id_b = None

    # ---------- Conexión ----------
    def connect(self):
        print(f"[bridge] Conectando A: {self.a_host}:{self.a_port}")
        self.iface_a = _TCPI(hostname=self.a_host, portNumber=self.a_port)
        print(f"[bridge] Conectando B: {self.b_host}:{self.b_port}")
        self.iface_b = _TCPI(hostname=self.b_host, portNumber=self.b_port)

        # Descubrir local IDs (si están disponibles)
        try:
            la = getattr(self.iface_a, "myInfo", None) or {}
            self.local_id_a = (la.get("my_node_num") or la.get("num") or la.get("id") or "")
            if isinstance(self.local_id_a, int):
                self.local_id_a = f"!{self.local_id_a:08x}"
        except Exception:
            self.local_id_a = None

        try:
            lb = getattr(self.iface_b, "myInfo", None) or {}
            self.local_id_b = (lb.get("my_node_num") or lb.get("num") or lb.get("id") or "")
            if isinstance(self.local_id_b, int):
                self.local_id_b = f"!{self.local_id_b:08x}"
        except Exception:
            self.local_id_b = None

        # Suscripción al bus de eventos
        pub.subscribe(self._on_rx, "meshtastic.receive")

        print(f"[bridge] Conectado. local_id_a={self.local_id_a} local_id_b={self.local_id_b}")

    def close(self):
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

    # ---------- RX handler ----------
    def _on_rx(self, interface=None, packet=None, **kwargs):
        """
        Callback común de Meshtastic (pubsub). 'interface' será la TCPInterface
        que recibió el paquete (A o B); reenviamos al otro.
        """
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}
            port = decoded.get("portnum") or decoded.get("portnum_name") \
                   or decoded.get("portnum_str") or decoded.get("portnumText")

            # Normalizar port a texto
            if isinstance(port, int):
                # algunos SDKs dan un int… nos basta con no reenviar si no es de interés
                port_str = str(port)
            else:
                port_str = str(port or "").upper()

            # Filtrado por tipo de app
            if self.forward_text:
                want_text = ("TEXT_MESSAGE_APP" in port_str) or ("TEXT" == port_str)
            else:
                want_text = False

            if self.forward_position:
                want_pos = ("POSITION_APP" in port_str) or ("POSITION" == port_str) \
                           or ("TELEMETRY_APP" in port_str) or ("TELEMETRY" == port_str)
            else:
                want_pos = False

            if not (want_text or want_pos):
                return

            # Canal lógico
            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            # Emisor
            frm = pkt.get("fromId") or decoded.get("fromId") or pkt.get("from")
            frm = str(frm or "")

            # Texto (si lo hay)
            text = decoded.get("text") or decoded.get("payload") or ""
            text = str(text or "")

            # Desde qué lado vino
            came_from_a = (interface is self.iface_a)
            came_from_b = (interface is self.iface_b)

            if not (came_from_a or came_from_b):
                # Paquete de otra interface (si existiera): ignorar
                return

            # Evitar bucles: si el emisor coincide con el local del destino, no lo reenvíes
            if came_from_a and self.local_id_b and frm == self.local_id_b:
                return
            if came_from_b and self.local_id_a and frm == self.local_id_a:
                return

            # Seleccionar mapeo de canal y destino
            if came_from_a:
                if ch not in self.a2b_map:
                    return
                out_ch = self.a2b_map.get(ch, 0)
                direction = "A2B"
                rl_ok = self.rl_a2b.allow()
                target_iface = self.iface_b
            else:
                if ch not in self.b2a_map:
                    return
                out_ch = self.b2a_map.get(ch, 0)
                direction = "B2A"
                rl_ok = self.rl_b2a.allow()
                target_iface = self.iface_a

            if not rl_ok:
                # Silencioso: rate-limited
                return

            # Dedup por hash
            key = _hash_key(direction, frm, ch, text if want_text else json.dumps(decoded, sort_keys=True))
            if self.dedup.seen(key):
                return

            # Construir envío
            
            # Reenvío
            if want_text:
                msg = _norm_text(text)
                # Etiqueta según dirección
                tag = self.tag_bridge_a2b if came_from_a else self.tag_bridge_b2a
                if tag:
                    # Añade etiqueta solo si no está ya presente
                    if tag not in msg:
                        msg = f"{tag} {msg}"
                try:
                    target_iface.sendText(msg, channel=out_ch, wantAck=self.require_ack)
                    print(f"[bridge] {direction} ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK")
                except Exception as e:
                    print(f"[bridge] {direction} ERROR sendText: {type(e).__name__}: {e}")

            elif want_pos:
                # Resumen ligero como texto (opcional, para no construir POSITION_APP)
                try:
                    summary = {
                        "via": "bridge",
                        "from": frm[-8:] if frm else "?",
                        "lat": decoded.get("position", {}).get("latitude"),
                        "lon": decoded.get("position", {}).get("longitude"),
                        "alt": decoded.get("position", {}).get("altitude"),
                        "bat": decoded.get("deviceMetrics", {}).get("batteryLevel")
                    }
                    tag = self.tag_bridge_a2b if came_from_a else self.tag_bridge_b2a
                    if tag:
                        msg = f"{tag} POS {summary}"
                    else:
                        msg = f"POS {summary}"
                    target_iface.sendText(msg, channel=out_ch, wantAck=False)
                    print(f"[bridge] {direction} ch {ch}->{out_ch} POS OK")
                except Exception as e:
                    print(f"[bridge] {direction} ERROR send POS: {type(e).__name__}: {e}")

        except Exception as e:
            print(f"[bridge] on_rx error: {type(e).__name__}: {e}")

    # ---------- Ciclo de vida ----------
    def run_forever(self):
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

def main():
    ap = argparse.ArgumentParser(description="Pasarela Meshtastic A<->B (IP)")
    ap.add_argument("--a", dest="a_host", default=os.getenv("A_HOST", "").strip(), help="Host nodo A")
    ap.add_argument("--b", dest="b_host", default=os.getenv("B_HOST", "").strip(), help="Host nodo B")
    ap.add_argument("--a-port", dest="a_port", type=int, default=int(os.getenv("A_PORT", "4403")), help="Puerto TCP A")
    ap.add_argument("--b-port", dest="b_port", type=int, default=int(os.getenv("B_PORT", "4403")), help="Puerto TCP B")
    args = ap.parse_args()

    if not args.a_host or not args.b_host:
        raise SystemExit("Debe indicar A_HOST y B_HOST (o usar --a/--b).")

    a2b = _parse_ch_map(os.getenv("A2B_CH_MAP", "0:0"))
    b2a = _parse_ch_map(os.getenv("B2A_CH_MAP", "0:0"))

    forward_text = _truthy(os.getenv("FORWARD_TEXT", "1"), True)
    forward_position = _truthy(os.getenv("FORWARD_POSITION", "0"), False)
    require_ack = _truthy(os.getenv("REQUIRE_ACK", "0"), False)
    rate_limit = int(os.getenv("RATE_LIMIT_PER_SIDE", "8"))
    dedup_ttl = int(os.getenv("DEDUP_TTL", "45"))
    tag_bridge = os.getenv("TAG_BRIDGE", "[BRIDGE]")
    tag_bridge = os.getenv("TAG_BRIDGE", "[BRIDGE]")
    tag_bridge_a2b = os.getenv("TAG_BRIDGE_A2B", "").strip() or None
    tag_bridge_b2a = os.getenv("TAG_BRIDGE_B2A", "").strip() or None


    br = Bridge(
        args.a_host, args.a_port,
        args.b_host, args.b_port,
        a2b, b2a,
        forward_text=forward_text,
        forward_position=forward_position,
        require_ack=require_ack,
        rate_limit_per_side=rate_limit,
        dedup_ttl=dedup_ttl,
        tag_bridge=tag_bridge,
        tag_bridge_a2b=tag_bridge_a2b,
        tag_bridge_b2a=tag_bridge_b2a
    )
    br.connect()
    br.run_forever()

if __name__ == "__main__":
    main()
