#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
channel_gateway.py — MeshNet-Bot v7.0.56
Pasarela interna multi-radio entre canales del mismo nodo, integrada en el broker.

Principios:
- RADIO_PROFILE decide qué transportes están disponibles.
- Meshtastic reutiliza meshtastic.receive y SENDQ/interfaz ya existente.
- MeshCore reutiliza MESHCORE_ENGINE, su sesión _meshcore y enqueue_send_channel().
- No abre conexiones de radio adicionales.
- Reglas: (transport, source_channel, destination_channel).
- Compatibilidad: migra estado v7.0.55 sin transporte cuando el perfil tiene un
  único transporte; en perfiles combinados conserva las reglas antiguas como
  ambiguas/inactivas para no aplicarlas al motor equivocado.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Optional

from pubsub import pub

_TRUTHY = {"1", "true", "t", "yes", "y", "on", "si", "sí"}
_BROADCAST_VALUES = {"", "^all", "broadcast", "4294967295", "0xffffffff", "ffffffff"}
_TRANSPORTS = {"meshtastic", "meshcore"}
_AMBIGUOUS_TRANSPORT = ""


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    return str(value).strip().lower() in _TRUTHY


def _parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return default


def _normalise_transport(value: Any) -> str:
    token = str(value or "").strip().lower()
    aliases = {
        "meshcore": "meshcore", "mc": "meshcore",
        "meshtastic": "meshtastic", "mesh": "meshtastic", "mt": "meshtastic",
    }
    return aliases.get(token, token if token in _TRANSPORTS else "")


def _radio_context() -> dict:
    """Resuelve RADIO_PROFILE sin modificar el entorno ni abrir radios."""
    try:
        from radio_profile import resolve_radio_profile
        caps = resolve_radio_profile(env=os.environ, strict=False)
        transports: list[str] = []
        if bool(getattr(caps, "meshtastic_enabled", False)):
            transports.append("meshtastic")
        if bool(getattr(caps, "meshcore_enabled", False)):
            transports.append("meshcore")
        return {
            "profile": str(getattr(caps, "profile", "legacy") or "legacy"),
            "valid": bool(getattr(caps, "valid", False)),
            "legacy_mode": bool(getattr(caps, "legacy_mode", False)),
            "transports": tuple(transports),
            "node_a_transport": getattr(caps, "node_a_transport", None),
            "node_b_transport": getattr(caps, "node_b_transport", None),
            "embedded_bridge_enabled": bool(getattr(caps, "embedded_bridge_enabled", False)),
        }
    except Exception:
        return {
            "profile": (os.getenv("RADIO_PROFILE") or "legacy").strip() or "legacy",
            "valid": False,
            "legacy_mode": True,
            "transports": (),
            "node_a_transport": None,
            "node_b_transport": None,
            "embedded_bridge_enabled": False,
        }


def _transport_allowed(transport: str, ctx: Optional[dict] = None) -> bool:
    ctx = ctx or _radio_context()
    return transport in tuple(ctx.get("transports") or ())


def _parse_rule_map(raw: str | None, transport: str) -> set[tuple[str, int, int]]:
    out: set[tuple[str, int, int]] = set()
    t = _normalise_transport(transport)
    if not t:
        return out
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        left, right = item.split(":", 1)
        src, dst = _parse_int(left), _parse_int(right)
        if src is None or dst is None or src < 0 or dst < 0 or src == dst:
            continue
        out.add((t, src, dst))
    return out


def _state_path() -> Path:
    explicit = (os.getenv("CHANNEL_GATEWAY_STATE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = (os.getenv("BOT_DATA_DIR") or "/app/bot_data").strip() or "/app/bot_data"
    return Path(data_dir).expanduser() / "channel_gateway.json"


def _normalise_text(text: str) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split()).strip()


def _fingerprint(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def _extract_channel(packet: dict) -> int:
    decoded = packet.get("decoded") or {}
    meta = packet.get("meta") or {}
    candidates = (
        meta.get("channelIndex"), packet.get("channel"), decoded.get("channel"),
        (decoded.get("data") or {}).get("channel") if isinstance(decoded.get("data"), dict) else None,
    )
    for value in candidates:
        parsed = _parse_int(value)
        if parsed is not None and parsed >= 0:
            return parsed
    return 0


def _extract_text(packet: dict) -> str:
    decoded = packet.get("decoded") or {}
    data = decoded.get("data") or {}
    for value in (decoded.get("text"), data.get("text") if isinstance(data, dict) else None, packet.get("text")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = decoded.get("payload")
    if payload is None and isinstance(data, dict):
        payload = data.get("payload")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            return bytes(payload).decode("utf-8", errors="strict").strip()
        except Exception:
            pass
    return ""


def _is_text_message(packet: dict) -> bool:
    decoded = packet.get("decoded") or {}
    portnum = decoded.get("portnum")
    if isinstance(portnum, int):
        return portnum == 1
    text = str(portnum or "").upper()
    return text == "1" or "TEXT_MESSAGE_APP" in text


def _extract_sender(packet: dict) -> str:
    decoded = packet.get("decoded") or {}
    value = packet.get("fromId") or packet.get("from") or decoded.get("fromId") or decoded.get("from") or ""
    if isinstance(value, int):
        return f"!{value:08x}"
    return str(value or "").strip()


def _extract_destination(packet: dict) -> str:
    decoded = packet.get("decoded") or {}
    value = packet.get("toId") or packet.get("to") or decoded.get("toId") or decoded.get("to") or ""
    if isinstance(value, int):
        return "^all" if value == 0xFFFFFFFF else f"!{value:08x}"
    return str(value or "").strip()


def _is_broadcast(packet: dict) -> bool:
    return _extract_destination(packet).strip().lower() in _BROADCAST_VALUES


def _local_node_ids(interface: Any) -> set[str]:
    out: set[str] = set()
    try:
        my_info = getattr(interface, "myInfo", None) or {}
        if isinstance(my_info, dict):
            for key in ("my_node_num", "num", "id"):
                value = my_info.get(key)
                if isinstance(value, int): out.add(f"!{value:08x}".lower())
                elif value: out.add(str(value).strip().lower())
    except Exception:
        pass
    try:
        local_node = getattr(interface, "localNode", None)
        node_num = getattr(local_node, "nodeNum", None)
        if isinstance(node_num, int): out.add(f"!{node_num:08x}".lower())
    except Exception:
        pass
    return {x for x in out if x}


class ChannelGatewayManager:
    """Gestor thread-safe de reglas multi-radio."""
    def __init__(self, state_file: Path | None = None):
        self.state_file = Path(state_file or _state_path())
        self._lock = threading.RLock()
        self.enabled = False
        self.rules: set[tuple[str, int, int]] = set()
        self.forward_direct = _truthy(os.getenv("CHANNEL_GATEWAY_FORWARD_DIRECT"), False)
        self.allow_external_bridge = _truthy(os.getenv("CHANNEL_GATEWAY_ALLOW_EXTERNAL_BRIDGE"), False)
        self.dedup_ttl = max(2.0, float(os.getenv("CHANNEL_GATEWAY_DEDUP_TTL", "12") or 12))
        self.tx_echo_ttl = max(2.0, float(os.getenv("CHANNEL_GATEWAY_TX_ECHO_TTL", "12") or 12))
        self.rate_limit_per_min = max(0, int(os.getenv("CHANNEL_GATEWAY_RATE_LIMIT", "30") or 30))
        self._recent_rx: dict[str, float] = {}
        self._recent_tx: dict[str, float] = {}
        self._rate: dict[tuple[str, int, int], deque[float]] = defaultdict(deque)
        self.stats: Dict[str, int] = {"rx_text":0,"rx_meshtastic":0,"rx_meshcore":0,"forwarded":0,"forwarded_meshtastic":0,"forwarded_meshcore":0,"duplicate_rx":0,"echo_suppressed":0,"rate_limited":0,"ignored_direct":0,"inactive_profile":0,"errors":0}
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        ctx = _radio_context(); allowed = tuple(ctx.get("transports") or ())
        with self._lock:
            if self.state_file.exists():
                try:
                    obj = json.loads(self.state_file.read_text(encoding="utf-8")); self.enabled = bool(obj.get("enabled", False)); loaded=set(); migrated=False
                    for item in obj.get("rules", []) or []:
                        if not isinstance(item, dict): continue
                        src,dst=_parse_int(item.get("source")),_parse_int(item.get("destination"))
                        if src is None or dst is None or src < 0 or dst < 0 or src == dst: continue
                        transport=_normalise_transport(item.get("transport"))
                        if not transport:
                            if len(allowed)==1: transport=allowed[0]; migrated=True
                            else: transport=_AMBIGUOUS_TRANSPORT
                        loaded.add((transport,src,dst))
                    self.rules=loaded
                    if migrated: self._save_locked()
                    return
                except Exception as exc: self.last_error=f"state_load: {type(exc).__name__}: {exc}"
            self.enabled=_truthy(os.getenv("CHANNEL_GATEWAY_ENABLED"),False); initial=set()
            initial |= _parse_rule_map(os.getenv("CHANNEL_GATEWAY_MESHTASTIC_MAP"),"meshtastic")
            initial |= _parse_rule_map(os.getenv("CHANNEL_GATEWAY_MESHCORE_MAP"),"meshcore")
            generic=os.getenv("CHANNEL_GATEWAY_MAP")
            if generic and len(allowed)==1: initial |= _parse_rule_map(generic,allowed[0])
            self.rules=initial; self._save_locked()

    def _save_locked(self) -> None:
        self.state_file.parent.mkdir(parents=True,exist_ok=True)
        payload={"version":2,"enabled":bool(self.enabled),"rules":[{"transport":t,"source":s,"destination":d,"enabled":True} for t,s,d in sorted(self.rules)],"updated_at":int(time.time())}
        tmp=self.state_file.with_suffix(self.state_file.suffix+".tmp"); tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); tmp.replace(self.state_file)

    def _purge_recent_locked(self, now: float) -> None:
        for cache,ttl in ((self._recent_rx,self.dedup_ttl),(self._recent_tx,self.tx_echo_ttl)):
            for key in [k for k,ts in cache.items() if now-ts>ttl]: cache.pop(key,None)

    def _rate_allowed_locked(self, rule: tuple[str,int,int], now: float) -> bool:
        if self.rate_limit_per_min<=0: return True
        q=self._rate[rule]
        while q and now-q[0]>60.0: q.popleft()
        if len(q)>=self.rate_limit_per_min: return False
        q.append(now); return True

    def status(self) -> dict:
        ctx=_radio_context(); allowed=tuple(ctx.get("transports") or ())
        with self._lock:
            rules=[{"transport":t,"source":s,"destination":d,"active_for_profile":bool(t and t in allowed)} for t,s,d in sorted(self.rules)]
            return {"enabled":bool(self.enabled),"profile":ctx.get("profile"),"valid_profile":bool(ctx.get("valid")),"transports":list(allowed),"node_a_transport":ctx.get("node_a_transport"),"node_b_transport":ctx.get("node_b_transport"),"embedded_bridge_enabled":bool(ctx.get("embedded_bridge_enabled")),"rules":rules,"rule_count":len(rules),"active_rule_count":sum(1 for x in rules if x["active_for_profile"]),"state_file":str(self.state_file),"forward_direct":bool(self.forward_direct),"allow_external_bridge":bool(self.allow_external_bridge),"dedup_ttl":self.dedup_ttl,"rate_limit_per_min":self.rate_limit_per_min,"stats":dict(self.stats),"last_error":self.last_error}

    def set_enabled(self, enabled: bool) -> dict:
        with self._lock: self.enabled=bool(enabled); self._save_locked()
        return self.status()

    def add_rule(self, transport: str, source: int, destination: int, both: bool=False) -> dict:
        t=_normalise_transport(transport); ctx=_radio_context()
        if not t: raise ValueError("transport debe ser meshcore o meshtastic")
        if not _transport_allowed(t,ctx): raise ValueError(f"transport {t!r} no permitido por RADIO_PROFILE={ctx.get('profile')}")
        src,dst=int(source),int(destination)
        if src<0 or dst<0 or src==dst: raise ValueError("source/destination inválidos o iguales")
        with self._lock:
            self.rules.add((t,src,dst))
            if both: self.rules.add((t,dst,src))
            self._save_locked()
        return self.status()

    def del_rule(self, transport: str, source: int, destination: int, both: bool=False) -> dict:
        t=_normalise_transport(transport); ctx=_radio_context()
        if not t: raise ValueError("transport debe ser meshcore o meshtastic")
        if not _transport_allowed(t,ctx): raise ValueError(f"transport {t!r} no permitido por RADIO_PROFILE={ctx.get('profile')}")
        src,dst=int(source),int(destination)
        with self._lock:
            self.rules.discard((t,src,dst))
            if both: self.rules.discard((t,dst,src))
            self._save_locked()
        return self.status()

    def clear_rules(self) -> dict:
        with self._lock: self.rules.clear(); self._save_locked()
        return self.status()

    def _destinations(self, transport: str, source: int) -> list[int]:
        if not _transport_allowed(transport):
            with self._lock: self.stats["inactive_profile"]+=1
            return []
        with self._lock: return [d for t,s,d in sorted(self.rules) if t==transport and s==source]

    def _prepare_forward(self, transport: str, source: int, sender: str, text: str, *, is_direct=False, local_sender=False) -> list[int]:
        now=time.time()
        with self._lock:
            self.stats["rx_text"]+=1; self.stats[f"rx_{transport}"]+=1
            if not self.enabled or not self.rules: return []
            if is_direct and not self.forward_direct: self.stats["ignored_direct"]+=1; return []
            self._purge_recent_locked(now); rx_fp=_fingerprint("rx",transport,sender.lower(),source,text)
            if rx_fp in self._recent_rx: self.stats["duplicate_rx"]+=1; return []
            self._recent_rx[rx_fp]=now; tx_fp=_fingerprint("tx",transport,source,text)
            if tx_fp in self._recent_tx and local_sender: self.stats["echo_suppressed"]+=1; return []
        return self._destinations(transport,source)

    def _after_tx(self, transport: str, destination: int, text: str) -> None:
        with self._lock:
            self._recent_tx[_fingerprint("tx",transport,destination,text)]=time.time(); self.stats["forwarded"]+=1; self.stats[f"forwarded_{transport}"]+=1

    def _rate_ok(self, transport: str, source: int, destination: int) -> bool:
        with self._lock:
            if self._rate_allowed_locked((transport,source,destination),time.time()): return True
            self.stats["rate_limited"]+=1; return False

    def _tx_error(self, transport, source, destination, exc):
        with self._lock: self.stats["errors"]+=1; self.last_error=f"{transport} tx {source}->{destination}: {type(exc).__name__}: {exc}"
        print(f"[channel-gateway] ERROR {self.last_error}",flush=True)

    def _enqueue_meshtastic(self, source, destination, text, interface) -> bool:
        payload={"channel":int(destination),"text":str(text),"destination":None,"require_ack":False,"type":"text","origin":"channel_gateway","meta":{"channel_gateway":1,"transport":"meshtastic","source_channel":int(source),"destination_channel":int(destination)}}
        if not self.allow_external_bridge: payload["no_bridge"]=True
        main_mod=sys.modules.get("__main__"); queue=getattr(main_mod,"SENDQ",None) if main_mod else None
        if queue is not None and hasattr(queue,"offer"): queue.offer(payload,coalesce=False); return True
        if interface is None or not hasattr(interface,"sendText"): return False
        interface.sendText(str(text),destinationId="^all",wantAck=False,wantResponse=False,channelIndex=int(destination)); return True

    def _meshcore_engine(self):
        main_mod=sys.modules.get("__main__"); return getattr(main_mod,"MESHCORE_ENGINE",None) if main_mod else None

    def _enqueue_meshcore(self,destination,text) -> bool:
        engine=self._meshcore_engine()
        if engine is None or not bool(getattr(engine,"enable",False)): return False
        enqueue=getattr(engine,"enqueue_send_channel",None)
        if not callable(enqueue): return False
        return enqueue(int(destination),str(text)) is not False

    def handle_meshtastic_packet(self, packet: dict|None, interface: Any=None) -> int:
        pkt=packet or {}
        if not isinstance(pkt,dict) or not _is_text_message(pkt): return 0
        text=_normalise_text(_extract_text(pkt))
        if not text: return 0
        source=_extract_channel(pkt); sender=_extract_sender(pkt); local_ids=_local_node_ids(interface); local_sender=bool(sender) and sender.lower() in local_ids
        destinations=self._prepare_forward("meshtastic",source,sender,text,is_direct=not _is_broadcast(pkt),local_sender=local_sender or not local_ids); forwarded=0
        for destination in destinations:
            if not self._rate_ok("meshtastic",source,destination): continue
            try:
                if not self._enqueue_meshtastic(source,destination,text,interface): raise RuntimeError("SENDQ/interface Meshtastic no disponible")
                self._after_tx("meshtastic",destination,text); forwarded+=1
            except Exception as exc: self._tx_error("meshtastic",source,destination,exc)
        return forwarded

    def handle_packet(self, packet: dict|None, interface: Any=None) -> int:
        return self.handle_meshtastic_packet(packet,interface)

    def handle_meshcore_message(self,event_or_payload: Any) -> int:
        if isinstance(event_or_payload,dict): payload=dict(event_or_payload.get("payload") or event_or_payload); event_type=event_or_payload.get("type")
        else: payload=dict(getattr(event_or_payload,"payload",None) or {}); event_type=getattr(event_or_payload,"type",None)
        try:
            main_mod=sys.modules.get("__main__"); mc_event_type=getattr(main_mod,"_MCEventType",None) if main_mod else None; channel_evt=getattr(mc_event_type,"CHANNEL_MSG_RECV",None) if mc_event_type else None
            if channel_evt is not None and event_type is not None and event_type!=channel_evt: return 0
        except Exception: pass
        source=_parse_int(payload.get("channel_idx")); text=_normalise_text(str(payload.get("text") or ""))
        if source is None or source<0 or not text: return 0
        sender=str(payload.get("pubkey_prefix") or payload.get("sender") or payload.get("from") or "").strip(); tx_fp=_fingerprint("tx","meshcore",source,text)
        with self._lock:
            self._purge_recent_locked(time.time())
            if tx_fp in self._recent_tx: self.stats["rx_text"]+=1; self.stats["rx_meshcore"]+=1; self.stats["echo_suppressed"]+=1; return 0
        destinations=self._prepare_forward("meshcore",source,sender,text); forwarded=0
        for destination in destinations:
            if not self._rate_ok("meshcore",source,destination): continue
            try:
                if not self._enqueue_meshcore(destination,text): raise RuntimeError("MESHCORE_ENGINE/enqueue_send_channel no disponible")
                self._after_tx("meshcore",destination,text); forwarded+=1
            except Exception as exc: self._tx_error("meshcore",source,destination,exc)
        return forwarded


class ChannelGatewayControlServer(threading.Thread):
    daemon=True
    def __init__(self,manager):
        super().__init__(name="channel-gateway-control",daemon=True); self.manager=manager; self.bind_host=(os.getenv("CHANNEL_GATEWAY_CTRL_BIND") or "0.0.0.0").strip() or "0.0.0.0"
        try: default_port=int(os.getenv("BROKER_CTRL_PORT","8766") or 8766)+1
        except Exception: default_port=8767
        self.port=int(os.getenv("CHANNEL_GATEWAY_CTRL_PORT",str(default_port)) or default_port); self.token=(os.getenv("CHANNEL_GATEWAY_CTRL_TOKEN") or "").strip(); self._stop_event=threading.Event(); self._sock=None
    def stop(self): self._stop_event.set(); self._sock and self._sock.close()
    def _reply(self,conn,payload): conn.sendall((json.dumps(payload,ensure_ascii=False)+"\n").encode("utf-8"))
    def _handle_request(self,req):
        if self.token and str(req.get("token") or "")!=self.token: return {"ok":False,"error":"unauthorized"}
        cmd=str(req.get("cmd") or "").strip().upper(); params=req.get("params") or {}
        try:
            if cmd in {"CHANNEL_GATEWAY_STATUS","STATUS"}: return {"ok":True,**self.manager.status()}
            if cmd in {"CHANNEL_GATEWAY_ON","ON"}: return {"ok":True,**self.manager.set_enabled(True)}
            if cmd in {"CHANNEL_GATEWAY_OFF","OFF"}: return {"ok":True,**self.manager.set_enabled(False)}
            if cmd in {"CHANNEL_GATEWAY_CLEAR","CLEAR"}: return {"ok":True,**self.manager.clear_rules()}
            if cmd in {"CHANNEL_GATEWAY_ADD","ADD"}: return {"ok":True,**self.manager.add_rule(str(params.get("transport") or ""),int(params.get("source")),int(params.get("destination")),bool(params.get("both",False)))}
            if cmd in {"CHANNEL_GATEWAY_DEL","DEL","DELETE"}: return {"ok":True,**self.manager.del_rule(str(params.get("transport") or ""),int(params.get("source")),int(params.get("destination")),bool(params.get("both",False)))}
            return {"ok":False,"error":f"unsupported_command: {cmd}"}
        except Exception as exc: return {"ok":False,"error":f"{type(exc).__name__}: {exc}"}
    def run(self):
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM); self._sock=sock
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); sock.bind((self.bind_host,self.port)); sock.listen(8); sock.settimeout(1.0)
            while not self._stop_event.is_set():
                try: conn,_=sock.accept()
                except socket.timeout: continue
                except OSError:
                    if self._stop_event.is_set(): break
                    raise
                with conn:
                    conn.settimeout(3.0); buf=b""
                    while b"\n" not in buf and len(buf)<65536:
                        chunk=conn.recv(4096)
                        if not chunk: break
                        buf+=chunk
                    try:
                        req=json.loads(buf.split(b"\n",1)[0].decode("utf-8",errors="strict") or "{}"); resp=self._handle_request(req if isinstance(req,dict) else {})
                    except Exception as exc: resp={"ok":False,"error":f"bad_request: {type(exc).__name__}: {exc}"}
                    self._reply(conn,resp)
        finally:
            try: sock.close()
            except Exception: pass


class MeshCoreGatewayBinder(threading.Thread):
    """Se enlaza a la sesión MeshCore ya abierta por MESHCORE_ENGINE."""
    daemon=True
    def __init__(self,manager): super().__init__(name="channel-gateway-meshcore-binder",daemon=True); self.manager=manager; self._stop_event=threading.Event(); self._bound_session_id=None
    def stop(self): self._stop_event.set()
    def _bind_if_ready(self):
        ctx=_radio_context()
        if "meshcore" not in tuple(ctx.get("transports") or ()): return
        main_mod=sys.modules.get("__main__"); engine=getattr(main_mod,"MESHCORE_ENGINE",None) if main_mod else None
        if engine is None or not bool(getattr(engine,"enable",False)): return
        mc=getattr(engine,"_meshcore",None)
        if mc is None or self._bound_session_id==id(mc): return
        mc_event_type=getattr(main_mod,"_MCEventType",None) if main_mod else None; channel_evt=getattr(mc_event_type,"CHANNEL_MSG_RECV",None) if mc_event_type else None
        if channel_evt is None or not hasattr(mc,"subscribe"): return
        def _callback(event): self.manager.handle_meshcore_message(event)
        mc.subscribe(channel_evt,_callback); self._bound_session_id=id(mc)
    def run(self):
        while not self._stop_event.is_set():
            try: self._bind_if_ready()
            except Exception as exc: print(f"[channel-gateway] binder MeshCore: {type(exc).__name__}: {exc}",flush=True)
            self._stop_event.wait(1.0)


_MANAGER=None; _CONTROL=None; _MESHCORE_BINDER=None; _STARTED=False; _START_LOCK=threading.Lock()
def channel_gateway_manager():
    global _MANAGER
    if _MANAGER is None: _MANAGER=ChannelGatewayManager()
    return _MANAGER

def _on_meshtastic_receive(packet=None,interface=None,**kwargs):
    try: channel_gateway_manager().handle_meshtastic_packet(packet or {},interface=interface)
    except Exception as exc: print(f"[channel-gateway] Meshtastic RX ERROR {type(exc).__name__}: {exc}",flush=True)

def start_channel_gateway_runtime():
    global _STARTED,_CONTROL,_MESHCORE_BINDER
    with _START_LOCK:
        if _STARTED: return channel_gateway_manager()
        mgr=channel_gateway_manager(); ctx=_radio_context(); allowed=tuple(ctx.get("transports") or ())
        if "meshtastic" in allowed: pub.subscribe(_on_meshtastic_receive,"meshtastic.receive")
        if "meshcore" in allowed: _MESHCORE_BINDER=MeshCoreGatewayBinder(mgr); _MESHCORE_BINDER.start()
        _CONTROL=ChannelGatewayControlServer(mgr); _CONTROL.start(); _STARTED=True
        print(f"[channel-gateway] runtime v7.0.56 profile={ctx.get('profile')} transports={list(allowed)} enabled={mgr.enabled} active_rules={mgr.status().get('active_rule_count')}",flush=True)
        return mgr
