#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
channel_gateway.py — Pasarela interna de canales Meshtastic integrada en el proceso del broker.

Objetivo
========
Reenviar mensajes de texto recibidos por un canal Meshtastic hacia uno o varios
canales del MISMO nodo, sin abrir una segunda conexión al dispositivo.

Integración
===========
El launcher del broker llama a ``start_channel_gateway_runtime()`` antes de
arrancar ``Meshtastic_Broker.py``. El módulo se suscribe a ``meshtastic.receive``
y, cuando el broker ya está operativo, reutiliza su cola global ``SENDQ``.

Configuración inicial (solo se usa si aún no existe el JSON persistente):
    CHANNEL_GATEWAY_ENABLED=0
    CHANNEL_GATEWAY_MAP=0:2,2:0

Persistencia:
    BOT_DATA_DIR/channel_gateway.json

Control JSONL (para el bot):
    CHANNEL_GATEWAY_CTRL_BIND=0.0.0.0
    CHANNEL_GATEWAY_CTRL_PORT=BROKER_CTRL_PORT+1   # 8767 por defecto
    CHANNEL_GATEWAY_CTRL_TOKEN=                    # opcional

Comandos RPC:
    CHANNEL_GATEWAY_STATUS
    CHANNEL_GATEWAY_ON
    CHANNEL_GATEWAY_OFF
    CHANNEL_GATEWAY_ADD   params={source,destination,both}
    CHANNEL_GATEWAY_DEL   params={source,destination,both}
    CHANNEL_GATEWAY_CLEAR

Seguridad operativa
===================
- Solo procesa TEXT_MESSAGE_APP.
- Por defecto solo reenvía mensajes broadcast; los DM no cruzan canales.
- Deduplicación RX y anti-eco de TX para evitar ping-pong en reglas 0↔2.
- Rate-limit por regla.
- Las TX se marcan ``no_bridge=True`` por defecto para no alimentar además la
  pasarela externa A↔B. Puede habilitarse expresamente con
  CHANNEL_GATEWAY_ALLOW_EXTERNAL_BRIDGE=1.
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


def _truthy(value: Any, default: bool = False) -> bool:
    """Convierte valores de entorno/configuración a booleano de forma tolerante."""
    if value is None:
        return bool(default)
    return str(value).strip().lower() in _TRUTHY


def _parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Convierte ``value`` a int o devuelve ``default`` si no es válido."""
    try:
        return int(value)
    except Exception:
        return default


def _parse_rule_map(raw: str | None) -> set[tuple[int, int]]:
    """
    Parsea ``origen:destino[,origen:destino...]``.

    Ejemplos:
        ``0:2``       -> {(0, 2)}
        ``0:2,2:0``   -> {(0, 2), (2, 0)}

    Las reglas origen==destino se descartan porque no aportan funcionalidad y
    aumentan el riesgo de eco.
    """
    out: set[tuple[int, int]] = set()
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        left, right = item.split(":", 1)
        src = _parse_int(left)
        dst = _parse_int(right)
        if src is None or dst is None or src < 0 or dst < 0 or src == dst:
            continue
        out.add((src, dst))
    return out


def _state_path() -> Path:
    """Devuelve la ruta persistente del estado del gateway."""
    explicit = (os.getenv("CHANNEL_GATEWAY_STATE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = (os.getenv("BOT_DATA_DIR") or "/app/bot_data").strip() or "/app/bot_data"
    return Path(data_dir).expanduser() / "channel_gateway.json"


def _normalise_text(text: str) -> str:
    """Normaliza lo mínimo necesario para huellas de deduplicación."""
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split()).strip()


def _fingerprint(*parts: Any) -> str:
    """Genera una huella SHA-256 estable para deduplicación y anti-eco."""
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def _extract_channel(packet: dict) -> int:
    """Extrae el índice lógico de canal del paquete; fallback conservador CH0."""
    decoded = packet.get("decoded") or {}
    meta = packet.get("meta") or {}
    candidates = (
        meta.get("channelIndex"),
        packet.get("channel"),
        decoded.get("channel"),
        (decoded.get("data") or {}).get("channel") if isinstance(decoded.get("data"), dict) else None,
    )
    for value in candidates:
        parsed = _parse_int(value)
        if parsed is not None and parsed >= 0:
            return parsed
    return 0


def _extract_text(packet: dict) -> str:
    """Extrae texto de las variantes habituales de TEXT_MESSAGE_APP."""
    decoded = packet.get("decoded") or {}
    data = decoded.get("data") or {}
    candidates = [
        decoded.get("text"),
        data.get("text") if isinstance(data, dict) else None,
        packet.get("text"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    payload = decoded.get("payload")
    if payload is None and isinstance(data, dict):
        payload = data.get("payload")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            return bytes(payload).decode("utf-8", errors="strict").strip()
        except Exception:
            return ""
    return ""


def _is_text_message(packet: dict) -> bool:
    """Indica si el paquete es TEXT_MESSAGE_APP (enum textual o valor 1)."""
    decoded = packet.get("decoded") or {}
    portnum = decoded.get("portnum")
    if isinstance(portnum, int):
        return portnum == 1
    text = str(portnum or "").upper()
    return text == "1" or "TEXT_MESSAGE_APP" in text


def _extract_sender(packet: dict) -> str:
    """Extrae el identificador del emisor para deduplicación y diagnóstico."""
    decoded = packet.get("decoded") or {}
    value = (
        packet.get("fromId")
        or packet.get("from")
        or decoded.get("fromId")
        or decoded.get("from")
        or ""
    )
    if isinstance(value, int):
        return f"!{value:08x}"
    return str(value or "").strip()


def _extract_destination(packet: dict) -> str:
    """Extrae el destinatario del paquete; vacío se trata como broadcast."""
    decoded = packet.get("decoded") or {}
    value = (
        packet.get("toId")
        or packet.get("to")
        or decoded.get("toId")
        or decoded.get("to")
        or ""
    )
    if isinstance(value, int):
        if value == 0xFFFFFFFF:
            return "^all"
        return f"!{value:08x}"
    return str(value or "").strip()


def _is_broadcast(packet: dict) -> bool:
    """Comprueba si el paquete es broadcast para no filtrar DM entre canales."""
    value = _extract_destination(packet).strip().lower()
    return value in _BROADCAST_VALUES


def _local_node_ids(interface: Any) -> set[str]:
    """Obtiene los identificadores conocidos del nodo local sin lanzar excepciones."""
    out: set[str] = set()
    try:
        my_info = getattr(interface, "myInfo", None) or {}
        if isinstance(my_info, dict):
            for key in ("my_node_num", "num", "id"):
                value = my_info.get(key)
                if isinstance(value, int):
                    out.add(f"!{value:08x}".lower())
                elif value:
                    out.add(str(value).strip().lower())
    except Exception:
        pass
    try:
        local_node = getattr(interface, "localNode", None)
        node_num = getattr(local_node, "nodeNum", None)
        if isinstance(node_num, int):
            out.add(f"!{node_num:08x}".lower())
    except Exception:
        pass
    return {x for x in out if x}


class ChannelGatewayManager:
    """
    Gestor thread-safe del gateway canal→canal.

    Cómo se usa:
        manager = ChannelGatewayManager()
        manager.handle_packet(packet, interface)

    El método ``handle_packet`` NO abre conexiones. Reutiliza preferentemente
    ``SENDQ`` del módulo ``__main__`` (Meshtastic_Broker.py). Solo si esa cola no
    está disponible usa el ``interface`` recibido por PubSub como fallback.
    """

    def __init__(self, state_file: Path | None = None):
        self.state_file = Path(state_file or _state_path())
        self._lock = threading.RLock()
        self.enabled = False
        self.rules: set[tuple[int, int]] = set()
        self.forward_direct = _truthy(os.getenv("CHANNEL_GATEWAY_FORWARD_DIRECT"), False)
        self.allow_external_bridge = _truthy(os.getenv("CHANNEL_GATEWAY_ALLOW_EXTERNAL_BRIDGE"), False)
        self.dedup_ttl = max(2.0, float(os.getenv("CHANNEL_GATEWAY_DEDUP_TTL", "12") or 12))
        self.tx_echo_ttl = max(2.0, float(os.getenv("CHANNEL_GATEWAY_TX_ECHO_TTL", "12") or 12))
        self.rate_limit_per_min = max(0, int(os.getenv("CHANNEL_GATEWAY_RATE_LIMIT", "30") or 30))
        self._recent_rx: dict[str, float] = {}
        self._recent_tx: dict[str, float] = {}
        self._rate: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.stats: Dict[str, int] = {
            "rx_text": 0,
            "forwarded": 0,
            "duplicate_rx": 0,
            "echo_suppressed": 0,
            "rate_limited": 0,
            "ignored_direct": 0,
            "errors": 0,
        }
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        """Carga estado persistente o inicializa desde variables de entorno."""
        with self._lock:
            if self.state_file.exists():
                try:
                    obj = json.loads(self.state_file.read_text(encoding="utf-8"))
                    self.enabled = bool(obj.get("enabled", False))
                    rules: set[tuple[int, int]] = set()
                    for item in obj.get("rules", []) or []:
                        if not isinstance(item, dict):
                            continue
                        src = _parse_int(item.get("source"))
                        dst = _parse_int(item.get("destination"))
                        if src is not None and dst is not None and src >= 0 and dst >= 0 and src != dst:
                            rules.add((src, dst))
                    self.rules = rules
                    return
                except Exception as exc:
                    self.last_error = f"state_load: {type(exc).__name__}: {exc}"

            self.enabled = _truthy(os.getenv("CHANNEL_GATEWAY_ENABLED"), False)
            self.rules = _parse_rule_map(os.getenv("CHANNEL_GATEWAY_MAP"))
            self._save_locked()

    def _save_locked(self) -> None:
        """Guarda el estado de forma atómica. Debe llamarse con ``self._lock``."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": bool(self.enabled),
            "rules": [
                {"source": src, "destination": dst, "enabled": True}
                for src, dst in sorted(self.rules)
            ],
            "updated_at": int(time.time()),
        }
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_file)

    def _purge_recent_locked(self, now: float) -> None:
        """Elimina huellas expiradas para mantener memoria acotada."""
        for cache, ttl in ((self._recent_rx, self.dedup_ttl), (self._recent_tx, self.tx_echo_ttl)):
            dead = [key for key, ts in cache.items() if (now - ts) > ttl]
            for key in dead:
                cache.pop(key, None)

    def _rate_allowed_locked(self, rule: tuple[int, int], now: float) -> bool:
        """Aplica rate-limit por regla durante una ventana móvil de 60 segundos."""
        if self.rate_limit_per_min <= 0:
            return True
        q = self._rate[rule]
        while q and (now - q[0]) > 60.0:
            q.popleft()
        if len(q) >= self.rate_limit_per_min:
            return False
        q.append(now)
        return True

    def status(self) -> dict:
        """Devuelve una fotografía serializable del estado y estadísticas."""
        with self._lock:
            return {
                "enabled": bool(self.enabled),
                "rules": [
                    {"source": src, "destination": dst}
                    for src, dst in sorted(self.rules)
                ],
                "rule_count": len(self.rules),
                "state_file": str(self.state_file),
                "forward_direct": bool(self.forward_direct),
                "allow_external_bridge": bool(self.allow_external_bridge),
                "dedup_ttl": self.dedup_ttl,
                "rate_limit_per_min": self.rate_limit_per_min,
                "stats": dict(self.stats),
                "last_error": self.last_error,
            }

    def set_enabled(self, enabled: bool) -> dict:
        """Activa/desactiva el gateway inmediatamente y persiste el cambio."""
        with self._lock:
            self.enabled = bool(enabled)
            self._save_locked()
            return self.status()

    def add_rule(self, source: int, destination: int, both: bool = False) -> dict:
        """Añade una regla y opcionalmente su inversa para modo bidireccional."""
        src, dst = int(source), int(destination)
        if src < 0 or dst < 0 or src == dst:
            raise ValueError("source/destination inválidos o iguales")
        with self._lock:
            self.rules.add((src, dst))
            if both:
                self.rules.add((dst, src))
            self._save_locked()
            return self.status()

    def del_rule(self, source: int, destination: int, both: bool = False) -> dict:
        """Elimina una regla y opcionalmente su inversa."""
        src, dst = int(source), int(destination)
        with self._lock:
            self.rules.discard((src, dst))
            if both:
                self.rules.discard((dst, src))
            self._save_locked()
            return self.status()

    def clear_rules(self) -> dict:
        """Elimina todas las reglas conservando el estado on/off actual."""
        with self._lock:
            self.rules.clear()
            self._save_locked()
            return self.status()

    def _enqueue_via_broker(self, source: int, destination: int, text: str, interface: Any) -> bool:
        """
        Encola una TX por la SENDQ del broker. Fallback: usa la MISMA interface RX.

        No se crea ninguna TCPInterface nueva.
        """
        payload = {
            "channel": int(destination),
            "text": str(text),
            "destination": None,
            "require_ack": False,
            "type": "text",
            "origin": "channel_gateway",
            "meta": {
                "channel_gateway": 1,
                "source_channel": int(source),
                "destination_channel": int(destination),
            },
        }
        if not self.allow_external_bridge:
            payload["no_bridge"] = True

        main_mod = sys.modules.get("__main__")
        queue = getattr(main_mod, "SENDQ", None) if main_mod is not None else None
        if queue is not None and hasattr(queue, "offer"):
            queue.offer(payload, coalesce=False)
            return True

        # Fallback seguro para ejecución/pruebas fuera del broker. Se reutiliza
        # la interfaz entregada por PubSub; jamás se abre otra conexión.
        if interface is None or not hasattr(interface, "sendText"):
            return False
        interface.sendText(
            str(text),
            destinationId="^all",
            wantAck=False,
            wantResponse=False,
            channelIndex=int(destination),
        )
        return True

    def handle_packet(self, packet: dict | None, interface: Any = None) -> int:
        """
        Procesa un paquete RX y devuelve el número de destinos encolados.

        Solo modifica la ruta adicional del gateway; el resto del broker continúa
        procesando el mismo paquete con normalidad.
        """
        pkt = packet or {}
        if not isinstance(pkt, dict) or not _is_text_message(pkt):
            return 0

        text = _normalise_text(_extract_text(pkt))
        if not text:
            return 0

        source = _extract_channel(pkt)
        sender = _extract_sender(pkt)
        now = time.time()

        with self._lock:
            self.stats["rx_text"] += 1
            if not self.enabled or not self.rules:
                return 0
            if not self.forward_direct and not _is_broadcast(pkt):
                self.stats["ignored_direct"] += 1
                return 0

            self._purge_recent_locked(now)

            rx_fp = _fingerprint("rx", sender.lower(), source, text)
            if rx_fp in self._recent_rx:
                self.stats["duplicate_rx"] += 1
                return 0
            self._recent_rx[rx_fp] = now

            # Anti-eco: una TX del gateway observada de vuelta en el canal destino
            # no debe activar la regla inversa.
            tx_fp = _fingerprint("tx", source, text)
            if tx_fp in self._recent_tx:
                local_ids = _local_node_ids(interface)
                sender_is_local = bool(sender) and sender.lower() in local_ids
                # Si conocemos el ID local, exigimos coincidencia. Si no podemos
                # resolverlo, mantenemos una protección conservadora dentro del TTL.
                if sender_is_local or not local_ids:
                    self.stats["echo_suppressed"] += 1
                    return 0

            destinations = [dst for src, dst in sorted(self.rules) if src == source]

        forwarded = 0
        for destination in destinations:
            rule = (source, destination)
            with self._lock:
                if not self._rate_allowed_locked(rule, now):
                    self.stats["rate_limited"] += 1
                    continue
            try:
                if self._enqueue_via_broker(source, destination, text, interface):
                    with self._lock:
                        self._recent_tx[_fingerprint("tx", destination, text)] = time.time()
                        self.stats["forwarded"] += 1
                    forwarded += 1
                    print(
                        f"[channel-gateway] FORWARD ch={source}->{destination} "
                        f"from={sender or '?'} len={len(text.encode('utf-8'))}",
                        flush=True,
                    )
                else:
                    raise RuntimeError("broker SENDQ/interface no disponible")
            except Exception as exc:
                with self._lock:
                    self.stats["errors"] += 1
                    self.last_error = f"tx {source}->{destination}: {type(exc).__name__}: {exc}"
                print(f"[channel-gateway] ERROR {self.last_error}", flush=True)
        return forwarded


class ChannelGatewayControlServer(threading.Thread):
    """Servidor JSONL ligero de control ejecutado dentro del proceso del broker."""

    daemon = True

    def __init__(self, manager: ChannelGatewayManager):
        super().__init__(name="channel-gateway-control", daemon=True)
        self.manager = manager
        self.bind_host = (os.getenv("CHANNEL_GATEWAY_CTRL_BIND") or "0.0.0.0").strip() or "0.0.0.0"
        try:
            default_port = int(os.getenv("BROKER_CTRL_PORT", "8766") or 8766) + 1
        except Exception:
            default_port = 8767
        self.port = int(os.getenv("CHANNEL_GATEWAY_CTRL_PORT", str(default_port)) or default_port)
        self.token = (os.getenv("CHANNEL_GATEWAY_CTRL_TOKEN") or "").strip()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        """Solicita parada del servidor y cierra el socket de escucha."""
        self._stop_event.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def _reply(self, conn: socket.socket, payload: dict) -> None:
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    def _handle_request(self, req: dict) -> dict:
        if self.token and str(req.get("token") or "") != self.token:
            return {"ok": False, "error": "unauthorized"}

        cmd = str(req.get("cmd") or "").strip().upper()
        params = req.get("params") or {}
        try:
            if cmd in {"CHANNEL_GATEWAY_STATUS", "STATUS"}:
                return {"ok": True, **self.manager.status()}
            if cmd in {"CHANNEL_GATEWAY_ON", "ON"}:
                return {"ok": True, **self.manager.set_enabled(True)}
            if cmd in {"CHANNEL_GATEWAY_OFF", "OFF"}:
                return {"ok": True, **self.manager.set_enabled(False)}
            if cmd in {"CHANNEL_GATEWAY_CLEAR", "CLEAR"}:
                return {"ok": True, **self.manager.clear_rules()}
            if cmd in {"CHANNEL_GATEWAY_ADD", "ADD"}:
                src = int(params.get("source"))
                dst = int(params.get("destination"))
                both = bool(params.get("both", False))
                return {"ok": True, **self.manager.add_rule(src, dst, both=both)}
            if cmd in {"CHANNEL_GATEWAY_DEL", "DEL", "DELETE"}:
                src = int(params.get("source"))
                dst = int(params.get("destination"))
                both = bool(params.get("both", False))
                return {"ok": True, **self.manager.del_rule(src, dst, both=both)}
            return {"ok": False, "error": f"unsupported_command: {cmd}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_host, self.port))
            sock.listen(8)
            sock.settimeout(1.0)
            print(
                f"[channel-gateway] control escuchando en {self.bind_host}:{self.port}",
                flush=True,
            )
            while not self._stop_event.is_set():
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    raise
                with conn:
                    conn.settimeout(3.0)
                    buf = b""
                    while b"\n" not in buf and len(buf) < 65536:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    try:
                        req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", errors="strict") or "{}")
                        if not isinstance(req, dict):
                            raise ValueError("request must be an object")
                        resp = self._handle_request(req)
                    except Exception as exc:
                        resp = {"ok": False, "error": f"bad_request: {type(exc).__name__}: {exc}"}
                    self._reply(conn, resp)
        except Exception as exc:
            print(
                f"[channel-gateway] control ERROR {type(exc).__name__}: {exc}",
                flush=True,
            )
        finally:
            try:
                sock.close()
            except Exception:
                pass


_MANAGER: ChannelGatewayManager | None = None
_CONTROL: ChannelGatewayControlServer | None = None
_STARTED = False
_START_LOCK = threading.Lock()


def channel_gateway_manager() -> ChannelGatewayManager:
    """Devuelve la instancia singleton del gestor, creándola si es necesario."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ChannelGatewayManager()
    return _MANAGER


def _on_meshtastic_receive(packet=None, interface=None, **kwargs) -> None:
    """Callback PubSub. Nunca propaga excepciones al receptor principal del broker."""
    try:
        channel_gateway_manager().handle_packet(packet or {}, interface=interface)
    except Exception as exc:
        mgr = channel_gateway_manager()
        with mgr._lock:
            mgr.stats["errors"] += 1
            mgr.last_error = f"rx_callback: {type(exc).__name__}: {exc}"
        print(f"[channel-gateway] RX ERROR {mgr.last_error}", flush=True)


def start_channel_gateway_runtime() -> ChannelGatewayManager:
    """
    Instala una sola vez la suscripción RX y el servidor de control.

    Debe llamarse desde el launcher del broker ANTES de ejecutar el broker real.
    El callback queda registrado y empezará a recibir eventos cuando Meshtastic
    publique ``meshtastic.receive``.
    """
    global _STARTED, _CONTROL
    with _START_LOCK:
        if _STARTED:
            return channel_gateway_manager()
        mgr = channel_gateway_manager()
        pub.subscribe(_on_meshtastic_receive, "meshtastic.receive")
        _CONTROL = ChannelGatewayControlServer(mgr)
        _CONTROL.start()
        _STARTED = True
        print(
            f"[channel-gateway] runtime instalado enabled={mgr.enabled} "
            f"rules={[(a, b) for a, b in sorted(mgr.rules)]}",
            flush=True,
        )
        return mgr
