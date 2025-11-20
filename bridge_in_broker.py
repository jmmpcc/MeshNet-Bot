#!/usr/bin/env python3 	   	 	 	    	   		 
# -*- coding: utf-8 -*- 	  	   	 	 	     	 	
""" 	  		 	 					 	  		 
bridge_in_broker.py V6.1.3 — Pasarela A<->B embebida en el broker usando el pool persistente.	 		  	 	 			  		 		 

- Reutiliza la interfaz del nodo A (la que YA usa el broker).	    	   			 		    	 
- Abre (solo si se activa) la interfaz del nodo B con el mismo pool persistente.			 	   		  	 	 			 	
- Reenvía TEXT_MESSAGE_APP (y opcional POS/TELEMETRY como resumen) con anti-bucle, dedup y rate-limit.		 		    	 				  	 	 
- Filtro por canal mediante mapas A2B/B2A.					 			 		   		 		 
"""

from __future__ import annotations
import os, time, json, threading, re, hashlib
from collections import deque
from typing import Optional, Dict
import meshtastic  # ⬅️ necesario para parchear setInterface

from pubsub import pub

# --- Compat shim para Meshtastic TCPInterface (host -> hostname) + pool único ---
from tcpinterface_persistent import TCPInterface as PoolTCPIF  # solo A (pool persistente)
from meshtastic.tcp_interface import TCPInterface as SDKTCPIF   # B usa SDK directo



def _truthy(s: str | None, default: bool=False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in {"1","true","t","yes","y","on","si","sí"}

def _parse_ch_map(s: str | None) -> dict[int,int]:
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

def _hash_key(direction: str, from_id: str, ch: int, payload: str) -> str:
    h = hashlib.sha256()
    h.update(direction.encode())
    h.update(str(from_id or "?").encode())
    h.update(str(int(ch)).encode())
    h.update(payload.encode("utf-8"))
    return h.hexdigest()

class _RateLimiter:
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

def _resolve_local_id(interface, retries: int = 12, delay: float = 0.5) -> str | None:
    import time as _t
    for _ in range(retries):
        try:
            mi = getattr(interface, "myInfo", None)
            if not mi and hasattr(interface, "getMyInfo"):
                try:
                    mi = interface.getMyInfo()
                except Exception:
                    mi = None
            if mi:
                if isinstance(mi, dict):
                    num = mi.get("my_node_num") or mi.get("num")
                    if isinstance(num, int):
                        return f"!{num:08x}"
                    idv = mi.get("id")
                    if idv:
                        return str(idv)
                else:
                    num = getattr(mi, "my_node_num", None) or getattr(mi, "num", None)
                    if isinstance(num, int):
                        return f"!{num:08x}"
                    idv = getattr(mi, "id", None)
                    if idv:
                        return str(idv)
        except Exception:
            pass
        _t.sleep(delay)
    return None

class BrokerEmbeddedBridge:
    """
    Pasarela embebida en el broker. Reutiliza iface A (existente) y abre iface B con el pool.
    """
    def __init__(
        self,
        iface_a,                         # instancia existente del broker
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
        self.iface_a = iface_a
        self.b_host, self.b_port = b_host, int(b_port or 4403)

        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})

        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        base = str(tag_bridge or "").strip()
        self.tag_bridge_a2b = (tag_bridge_a2b.strip() if tag_bridge_a2b else base)
        self.tag_bridge_b2a = (tag_bridge_b2a.strip() if tag_bridge_b2a else base)

        self.iface_b = None
        self.local_id_a = None
        self.local_id_b = None

        self._running = False
        self._lock = threading.RLock()
        self.rl_a2b = _RateLimiter(rate_limit_per_side)
        self.rl_b2a = _RateLimiter(rate_limit_per_side)
        self.dedup = _DedupWindow(dedup_ttl)

         # --- [NUEVO] Estado y backoff cuando el peer (B) está caído ---
        self.peer_offline_until = 0.0         # epoch hasta el que suprimimos reenvíos A->B
        self._peer_down_notified = False      # evita logs repetidos
        self.peer_down_backoff_sec = int(os.getenv("BRIDGE_PEER_DOWN_BACKOFF", "60") or "60")
      

    def start(self):
        with self._lock:
            if self._running:
                return
            # iface_a ya viene inicializada por el broker
            self.local_id_a = _resolve_local_id(self.iface_a, retries=12, delay=0.5)

            # Abrir iface_b mediante el pool (NO rompe nada del broker)
            # Evitar que el SDK cambie el interfaz global del proceso (no queremos tocar el del broker)
            _prev_set = getattr(meshtastic, "setInterface", None)
            try:
                if _prev_set:
                    meshtastic.setInterface = lambda *_a, **_kw: None  # no-op temporal
                self.iface_b = SDKTCPIF(hostname=self.b_host, portNumber=self.b_port)
                # Asegurar que B no se convierta en default si más adelante alguien llama sin querer a setInterface
                try:
                    if hasattr(self.iface_b, "isDefault"):
                        self.iface_b.isDefault = False  # por si el SDK lo consulta
                except Exception:
                    pass

            finally:
                if _prev_set:
                    meshtastic.setInterface = _prev_set  # restaurar

            self.local_id_b = _resolve_local_id(self.iface_b, retries=12, delay=0.5)

            pub.subscribe(self._on_rx, "meshtastic.receive")
            self._running = True
            print(f"[bridge] ✅ embebida activa — local_id_a={self.local_id_a} local_id_b={self.local_id_b}")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            try:
                pub.unsubscribe(self._on_rx, "meshtastic.receive")
            except Exception:
                pass
            try:
                if self.iface_b and hasattr(self.iface_b, "close"):
                    self.iface_b.close()
            except Exception:
                pass
            self._running = False
            print("[bridge] 🛑 embebida detenida")

    def is_running(self) -> bool:
        return self._running

    def status(self) -> dict:
        """
        Devuelve un resumen del estado actual del bridge embebido,
        incluyendo si está corriendo, los mapas de canales y el estado de peer-down.
        """
        import time
        now = time.time()
        remaining = max(0, int((self.peer_offline_until or 0.0) - now))

        return {
            "running": self._running,
            "a": {"local_id": self.local_id_a},
            "b": {
                "host": self.b_host,
                "port": self.b_port,
                "local_id": self.local_id_b,
            },
            "maps": {"A2B": self.a2b_map, "B2A": self.b2a_map},
            "opts": {
                "forward_text": self.forward_text,
                "forward_position": self.forward_position,
                "require_ack": self.require_ack,
                "tag_a2b": self.tag_bridge_a2b,
                "tag_b2a": self.tag_bridge_b2a,
                "peer_down_backoff_sec": int(self.peer_down_backoff_sec or 0),
            },
            "peer_state": {
                "peer_offline_until": int(self.peer_offline_until or 0),
                "peer_offline_remaining": remaining,
                "is_peer_suppressed": self._is_peer_suppressed(),
            },
        }

    def _on_rx_old(self, interface=None, packet=None, **kwargs):
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}
            port = decoded.get("portnum") or decoded.get("portnum_name") \
                   or decoded.get("portnum_str") or decoded.get("portnumText")
            port_str = str(port).upper() if port is not None else ""

            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT"))
            want_pos  = self.forward_position and (
                ("POSITION_APP" in port_str) or (port_str == "POSITION") or
                ("TELEMETRY_APP" in port_str) or (port_str == "TELEMETRY")
            )
            if not (want_text or want_pos):
                return

            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            frm = str(pkt.get("fromId") or decoded.get("fromId") or pkt.get("from") or "")
            text = str(decoded.get("text") or decoded.get("payload") or "")

            came_from_a = (interface is self.iface_a)
            came_from_b = (interface is self.iface_b)
            if not (came_from_a or came_from_b):
                return

            # anti-eco por etiqueta (si ya viene marcado desde el otro lado, no reinyectar)
            if want_text:
                other_tag = self.tag_bridge_b2a if came_from_a else self.tag_bridge_a2b
                if other_tag and other_tag in (text or ""):
                    return

            # anti-bucle por local_id del destino
            if came_from_a and self.local_id_b and frm == self.local_id_b:
                return
            if came_from_b and self.local_id_a and frm == self.local_id_a:
                return

            # mapeo + rate limit
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

            # dedup
            payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True)
            key = _hash_key(direction, frm, ch, payload_for_hash)
            if self.dedup.seen(key):
                return

            # envío
            if want_text:
                msg = _norm_text(text)
                tag = self.tag_bridge_a2b if came_from_a else self.tag_bridge_b2a
                if tag and tag not in msg:
                    msg = f"{tag} {msg}"
                try:
                    target.sendText(
                        msg,
                        destinationId="^all",         # broadcast (el bridge no direcciona a un nodo)
                        wantAck=bool(self.require_ack),
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )

                    print(f"[bridge] {direction} ch {ch}->{out_ch} txt OK")
                except Exception as e:
                    print(f"[bridge] {direction} sendText ERROR: {type(e).__name__}: {e}")
            elif want_pos:
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
                    msg = (f"{tag} POS {summary}" if tag else f"POS {summary}")
                    target.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=False,
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )

                    print(f"[bridge] {direction} ch {ch}->{out_ch} POS OK")
                except Exception as e:
                    print(f"[bridge] {direction} sendPOS ERROR: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[bridge] on_rx error: {type(e).__name__}: {e}")

    def _on_rx(self, interface=None, packet=None, **kwargs):
        """
        Maneja paquetes recibidos por el pubsub del SDK.
        - Filtra por puertos TEXT/POS/TELEMETRY si están habilitados.
        - Aplica anti-eco por tag, anti-bucle por local_id, mapeo y rate-limit.
        - Deduplica por (direction, fromId, channel, payload).
        - A2B: si B está marcado como offline, suprime reenvío durante el backoff.
            si un envío A2B falla, marca peer-down (backoff);
            si un envío A2B tiene éxito, marca peer-up.
        """
        import json
        import time as _t

        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}

            # --- puerto / portnum ---
            port = (decoded.get("portnum") or decoded.get("portnum_name")
                    or decoded.get("portnum_str") or decoded.get("portnumText"))
            port_str = str(port).upper() if port is not None else ""

            # --- qué tipos de mensaje queremos reenviar ---
            want_text = self.forward_text and (("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT"))
            want_pos  = self.forward_position and (
                ("POSITION_APP" in port_str) or (port_str == "POSITION")
                or ("TELEMETRY_APP" in port_str) or (port_str == "TELEMETRY")
            )
            if not (want_text or want_pos):
                return

            # --- canal ---
            ch = decoded.get("channel") if decoded.get("channel") is not None else pkt.get("channel")
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            # --- origen / texto ---
            frm = str(pkt.get("fromId") or decoded.get("fromId") or pkt.get("from") or "")
            text = str(decoded.get("text") or decoded.get("payload") or "")

            # --- desde qué interfaz llegó ---
            came_from_a = (interface is self.iface_a)
            came_from_b = (interface is self.iface_b)
            if not (came_from_a or came_from_b):
                return

            # --- anti-eco por etiqueta direccional ---
            if want_text:
                other_tag = self.tag_bridge_b2a if came_from_a else self.tag_bridge_a2b
                if other_tag and other_tag in (text or ""):
                    return

            # --- anti-bucle por local_id del destino ---
            if came_from_a and self.local_id_b and frm == self.local_id_b:
                return
            if came_from_b and self.local_id_a and frm == self.local_id_a:
                return

            # --- mapeo y rate-limit + selección de destino ---
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

            # --- NUEVO: si B está caído, suprimir A->B durante el backoff ---
            if direction == "A2B" and self._is_peer_suppressed():
                remaining = max(0, int((self.peer_offline_until or 0.0) - _t.time()))
                print(f"[bridge] A2B ch {ch}->{out_ch} SKIP (B offline, {remaining}s restantes)", flush=True)
                return

            # --- dedup ---
            payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True)
            key = _hash_key(direction, frm, ch, payload_for_hash)
            if self.dedup.seen(key):
                return

            # --- envío ---
            if want_text:
                msg = _norm_text(text)
                tag = self.tag_bridge_a2b if came_from_a else self.tag_bridge_b2a
                if tag and tag not in msg:
                    msg = f"{tag} {msg}"
                try:
                    target.sendText(
                        msg,
                        destinationId="^all",          # broadcast (el bridge no direcciona a un nodo)
                        wantAck=bool(self.require_ack),
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    # Éxito: si era A2B, limpiamos estado de caída
                    if direction == "A2B":
                        self._mark_peer_up()
                    print(f"[bridge] {direction} ch {ch}->{out_ch} txt OK")
                except Exception as e:
                    # Fallo: si era A2B, marcamos peer-down con backoff
                    if direction == "A2B":
                        self._mark_peer_down(f"{type(e).__name__}: {e}")
                    print(f"[bridge] {direction} sendText ERROR: {type(e).__name__}: {e}", flush=True)

            elif want_pos:
                try:
                    summary = {
                        "via": "bridge",
                        "from": frm[-8:] if frm else "?",
                        "lat": decoded.get("position", {}).get("latitude"),
                        "lon": decoded.get("position", {}).get("longitude"),
                        "alt": decoded.get("position", {}).get("altitude"),
                        "bat": decoded.get("deviceMetrics", {}).get("batteryLevel"),
                    }
                    tag = self.tag_bridge_a2b if came_from_a else self.tag_bridge_b2a
                    msg = (f"{tag} POS {summary}" if tag else f"POS {summary}")
                    target.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=False,
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    if direction == "A2B":
                        self._mark_peer_up()
                    print(f"[bridge] {direction} ch {ch}->{out_ch} POS OK")
                except Exception as e:
                    if direction == "A2B":
                        self._mark_peer_down(f"{type(e).__name__}: {e}")
                    print(f"[bridge] {direction} sendPOS ERROR: {type(e).__name__}: {e}", flush=True)

        except Exception as e:
            print(f"[bridge] on_rx error: {type(e).__name__}: {e}", flush=True)


    def mirror_from_a(self, channel: int, text: str) -> bool:
        """
        Espeja un envío originado por el broker (nodo A) hacia B aplicando:
        - mapeo A2B
        - supresión temporal si B está 'down' (backoff configurable)
        - rate-limit A2B
        - dedup por contenido
        - etiquetado direccional
        Devuelve True si se reenvió, False si se ignoró.
        """
        try:
            ch = int(channel)
        except Exception:
            ch = 0

        # 0) ¿B suprimido por caída reciente?
        if self._is_peer_suppressed():
            import time as _t
            remaining = max(0, int((self.peer_offline_until or 0.0) - _t.time()))
            print(f"[bridge] A2B ch {ch} → suprimido (B offline, {remaining}s restantes)", flush=True)
            return False

        # 1) Mapeo de canal
        if ch not in self.a2b_map:
            print(f"[bridge] A2B TX ch {ch} → descartado (no mapeado)", flush=True)
            return False

        out_ch = self.a2b_map[ch]

        # 2) Rate-limit
        if not self.rl_a2b.allow():
            print(f"[bridge] A2B ch {ch}->{out_ch} → descartado (rate-limit)", flush=True)
            return False

        # 3) Mensaje + tag
        msg = _norm_text(text or "")
        tag = self.tag_bridge_a2b
        if tag and tag not in msg:
            msg = f"{tag} {msg}"

        # 4) Dedup (igual criterio que RX)
        key = _hash_key("A2B", "LOCAL_TX", ch, msg)
        if self.dedup.seen(key):
            print(f"[bridge] A2B ch {ch}->{out_ch} → descartado (dupe)", flush=True)
            return False

        # 5) Envío a B con control de caída
        try:
            self.iface_b.sendText(
                msg,
                destinationId="^all",
                wantAck=bool(self.require_ack),
                wantResponse=False,
                channelIndex=int(out_ch),
            )
            # Éxito → marcar peer 'up' si veníamos de caída
            self._mark_peer_up()
            print(f"[bridge] A2B ch {ch}->{out_ch} txt OK", flush=True)
            return True

        except Exception as e:
            # Cualquier fallo de socket/API → marcar peer 'down' con backoff
            self._mark_peer_down(f"{type(e).__name__}: {e}")
            print(f"[bridge] A2B sendText ERROR: {type(e).__name__}: {e}", flush=True)
            return False

    # --- [NUEVO] Métodos auxiliares para control de peer down ---
    def _is_peer_suppressed(self) -> bool:
        import time
        try:
            return time.time() < float(self.peer_offline_until or 0.0)
        except Exception:
            return False

    def _mark_peer_down(self, reason: str = "") -> None:
        import time
        backoff = max(10, int(self.peer_down_backoff_sec or 60))
        self.peer_offline_until = time.time() + backoff
        if not self._peer_down_notified:
            print(f"[bridge] B OFFLINE → suprime A2B {backoff}s ({reason})", flush=True)
            self._peer_down_notified = True

    def _mark_peer_up(self) -> None:
        # (no necesita time)
        if self._peer_down_notified:
            print("[bridge] B volvió ONLINE → reanudo A2B", flush=True)
        self.peer_offline_until = 0.0
        self._peer_down_notified = False



# API mínima para el broker
_BRIDGE: Optional[BrokerEmbeddedBridge] = None


def bridge_start_in_broker(iface_a) -> dict:
    """
    Arranca la pasarela tomando la iface A del broker ya conectada.
    Lee configuración desde .env. Devuelve status dict.
    """
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        return {"ok": True, "already_running": True, "status": _BRIDGE.status()}

    b_host = os.getenv("BRIDGE_B_HOST") or os.getenv("B_HOST") or ""
    b_port = int(os.getenv("BRIDGE_B_PORT", os.getenv("B_PORT", "4403")) or "4403")
    if not b_host:
        raise RuntimeError("BRIDGE_B_HOST/B_HOST no definido")

    a2b = _parse_ch_map(os.getenv("BRIDGE_A2B_CH_MAP", os.getenv("A2B_CH_MAP", "0:0")))
    b2a = _parse_ch_map(os.getenv("BRIDGE_B2A_CH_MAP", os.getenv("B2A_CH_MAP", "0:0")))

    forward_text = _truthy(os.getenv("BRIDGE_FORWARD_TEXT", os.getenv("FORWARD_TEXT","1")), True)
    forward_position = _truthy(os.getenv("BRIDGE_FORWARD_POSITION", os.getenv("FORWARD_POSITION","0")), False)
    require_ack = _truthy(os.getenv("BRIDGE_REQUIRE_ACK", os.getenv("REQUIRE_ACK","0")), False)
    rate = int(os.getenv("BRIDGE_RATE_LIMIT_PER_SIDE", os.getenv("RATE_LIMIT_PER_SIDE","8")) or "8")
    dedup = int(os.getenv("BRIDGE_DEDUP_TTL", os.getenv("DEDUP_TTL","45")) or "45")

    tag_base = os.getenv("TAG_BRIDGE", "[BRIDGE]")
    tag_a2b = os.getenv("TAG_BRIDGE_A2B", "").strip() or None
    tag_b2a = os.getenv("TAG_BRIDGE_B2A", "").strip() or None

    _BRIDGE = BrokerEmbeddedBridge(
        iface_a=iface_a,
        b_host=b_host, b_port=b_port,
        a2b_map=a2b, b2a_map=b2a,
        forward_text=forward_text,
        forward_position=forward_position,
        require_ack=require_ack,
        rate_limit_per_side=rate,
        dedup_ttl=dedup,
        tag_bridge=tag_base,
        tag_bridge_a2b=tag_a2b,
        tag_bridge_b2a=tag_b2a,
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
    """
    Espeja un envío local del broker (A) hacia B, si el bridge está activo.
    """
    global _BRIDGE
    if _BRIDGE and _BRIDGE.is_running():
        try:
            return _BRIDGE.mirror_from_a(channel, text)
        except Exception as e:
            print(f"[bridge] mirror_from_a ERROR: {type(e).__name__}: {e}", flush=True)
    return False

