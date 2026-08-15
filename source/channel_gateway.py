#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
channel_gateway.py — MeshNet-Bot v7.0.56

Pasarela interna multi-radio entre canales del mismo nodo, integrada en el
proceso del broker.

Objetivo
========
Reenviar mensajes entre canales del mismo transporte sin abrir conexiones de
radio adicionales y sin modificar la lógica estable del broker principal.

Principios de integración
=========================
- ``RADIO_PROFILE`` determina qué transportes están disponibles.
- Meshtastic reutiliza ``meshtastic.receive`` y la ``SENDQ``/interfaz existente.
- MeshCore reutiliza exclusivamente ``MESHCORE_ENGINE``, su sesión ``_meshcore``
  ya abierta y ``enqueue_send_channel()``.
- Las reglas se identifican por ``(transport, source, destination)``.
- Deduplicación, anti-eco y rate-limit están separados por transporte.
- El estado v7.0.55 se migra de forma conservadora.

Compatibilidad
==============
Las reglas antiguas v7.0.55 no tenían transporte:
- si el perfil activo tiene un único transporte, se migran a ese transporte;
- si el perfil es combinado, se conservan como ambiguas/inactivas para evitar
  enviarlas por una radio equivocada.
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
_BROADCAST_VALUES = {
    "",
    "^all",
    "broadcast",
    "4294967295",
    "0xffffffff",
    "ffffffff",
}
_TRANSPORTS = {"meshtastic", "meshcore"}
_AMBIGUOUS_TRANSPORT = ""


def _truthy(value: Any, default: bool = False) -> bool:
    """Convierte valores de entorno/configuración a booleano tolerante."""
    if value is None:
        return bool(default)
    return str(value).strip().lower() in _TRUTHY


def _parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Convierte un valor a entero o devuelve ``default`` si no es válido."""
    try:
        return int(value)
    except Exception:
        return default


def _normalise_transport(value: Any) -> str:
    """Normaliza aliases de transporte a ``meshtastic`` o ``meshcore``."""
    token = str(value or "").strip().lower()
    aliases = {
        "meshcore": "meshcore",
        "mc": "meshcore",
        "meshtastic": "meshtastic",
        "mesh": "meshtastic",
        "mt": "meshtastic",
    }
    return aliases.get(token, token if token in _TRANSPORTS else "")


def _radio_context() -> dict:
    """
    Resuelve ``RADIO_PROFILE`` usando el resolvedor común del proyecto.

    No modifica el entorno y no abre conexiones de radio. Si el resolvedor no
    está disponible, se adopta un fallback conservador sin transportes activos.
    """
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
            "embedded_bridge_enabled": bool(
                getattr(caps, "embedded_bridge_enabled", False)
            ),
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
    """Comprueba si un transporte está permitido por el perfil activo."""
    ctx = ctx or _radio_context()
    return transport in tuple(ctx.get("transports") or ())


def _parse_rule_map(
    raw: str | None,
    transport: str,
) -> set[tuple[str, int, int]]:
    """
    Parsea ``origen:destino[,origen:destino...]`` asociado a un transporte.

    Se descartan reglas inválidas y reglas origen==destino.
    """
    out: set[tuple[str, int, int]] = set()
    normalised_transport = _normalise_transport(transport)
    if not normalised_transport:
        return out

    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue

        left, right = item.split(":", 1)
        source = _parse_int(left)
        destination = _parse_int(right)
        if (
            source is None
            or destination is None
            or source < 0
            or destination < 0
            or source == destination
        ):
            continue

        out.add((normalised_transport, source, destination))

    return out


def _state_path() -> Path:
    """Obtiene la ruta persistente del estado Channel Gateway."""
    explicit = (os.getenv("CHANNEL_GATEWAY_STATE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    data_dir = (
        (os.getenv("BOT_DATA_DIR") or "/app/bot_data").strip()
        or "/app/bot_data"
    )
    return Path(data_dir).expanduser() / "channel_gateway.json"


def _normalise_text(text: str) -> str:
    """Normaliza espacios para generar huellas de deduplicación estables."""
    return " ".join(
        str(text or "").replace("\r", " ").replace("\n", " ").split()
    ).strip()


def _fingerprint(*parts: Any) -> str:
    """Genera una huella SHA-256 estable."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", errors="ignore"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def _extract_channel(packet: dict) -> int:
    """Extrae el índice de canal Meshtastic; fallback conservador a CH0."""
    decoded = packet.get("decoded") or {}
    meta = packet.get("meta") or {}
    candidates = (
        meta.get("channelIndex"),
        packet.get("channel"),
        decoded.get("channel"),
        (decoded.get("data") or {}).get("channel")
        if isinstance(decoded.get("data"), dict)
        else None,
    )

    for value in candidates:
        parsed = _parse_int(value)
        if parsed is not None and parsed >= 0:
            return parsed
    return 0


def _extract_text(packet: dict) -> str:
    """Extrae texto de las variantes habituales de ``TEXT_MESSAGE_APP``."""
    decoded = packet.get("decoded") or {}
    data = decoded.get("data") or {}

    candidates = (
        decoded.get("text"),
        data.get("text") if isinstance(data, dict) else None,
        packet.get("text"),
    )
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
            pass

    return ""


def _is_text_message(packet: dict) -> bool:
    """Indica si el paquete es ``TEXT_MESSAGE_APP``."""
    decoded = packet.get("decoded") or {}
    portnum = decoded.get("portnum")
    if isinstance(portnum, int):
        return portnum == 1

    text = str(portnum or "").upper()
    return text == "1" or "TEXT_MESSAGE_APP" in text


def _extract_sender(packet: dict) -> str:
    """Extrae el identificador del emisor Meshtastic."""
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
    """Extrae el destinatario Meshtastic."""
    decoded = packet.get("decoded") or {}
    value = (
        packet.get("toId")
        or packet.get("to")
        or decoded.get("toId")
        or decoded.get("to")
        or ""
    )
    if isinstance(value, int):
        return "^all" if value == 0xFFFFFFFF else f"!{value:08x}"
    return str(value or "").strip()


def _is_broadcast(packet: dict) -> bool:
    """Comprueba si un mensaje Meshtastic es broadcast."""
    return _extract_destination(packet).strip().lower() in _BROADCAST_VALUES


def _local_node_ids(interface: Any) -> set[str]:
    """Obtiene IDs conocidos del nodo Meshtastic local sin lanzar excepciones."""
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

    return {item for item in out if item}


class ChannelGatewayManager:
    """
    Gestor thread-safe de reglas Channel Gateway multi-radio.

    Uso principal:
        manager = ChannelGatewayManager()
        manager.handle_meshtastic_packet(packet, interface)
        manager.handle_meshcore_message(event)

    No crea conexiones de radio. Reutiliza exclusivamente las existentes.
    """

    def __init__(self, state_file: Path | None = None):
        self.state_file = Path(state_file or _state_path())
        self._lock = threading.RLock()
        self.enabled = False
        self.rules: set[tuple[str, int, int]] = set()

        self.forward_direct = _truthy(
            os.getenv("CHANNEL_GATEWAY_FORWARD_DIRECT"),
            False,
        )
        self.allow_external_bridge = _truthy(
            os.getenv("CHANNEL_GATEWAY_ALLOW_EXTERNAL_BRIDGE"),
            False,
        )
        self.dedup_ttl = max(
            2.0,
            float(os.getenv("CHANNEL_GATEWAY_DEDUP_TTL", "12") or 12),
        )
        self.tx_echo_ttl = max(
            2.0,
            float(os.getenv("CHANNEL_GATEWAY_TX_ECHO_TTL", "12") or 12),
        )
        self.rate_limit_per_min = max(
            0,
            int(os.getenv("CHANNEL_GATEWAY_RATE_LIMIT", "30") or 30),
        )

        self._recent_rx: dict[str, float] = {}
        self._recent_tx: dict[str, float] = {}
        self._rate: dict[tuple[str, int, int], deque[float]] = defaultdict(deque)

        self.stats: Dict[str, int] = {
            "rx_text": 0,
            "rx_meshtastic": 0,
            "rx_meshcore": 0,
            "forwarded": 0,
            "forwarded_meshtastic": 0,
            "forwarded_meshcore": 0,
            "duplicate_rx": 0,
            "echo_suppressed": 0,
            "rate_limited": 0,
            "ignored_direct": 0,
            "inactive_profile": 0,
            "errors": 0,
        }
        self.last_error: str | None = None

        self._load()

    def _load(self) -> None:
        """
        Carga estado persistente y migra de forma conservadora la v7.0.55.
        """
        ctx = _radio_context()
        allowed = tuple(ctx.get("transports") or ())

        with self._lock:
            if self.state_file.exists():
                try:
                    obj = json.loads(self.state_file.read_text(encoding="utf-8"))
                    self.enabled = bool(obj.get("enabled", False))
                    loaded: set[tuple[str, int, int]] = set()
                    migrated = False

                    for item in obj.get("rules", []) or []:
                        if not isinstance(item, dict):
                            continue

                        source = _parse_int(item.get("source"))
                        destination = _parse_int(item.get("destination"))
                        if (
                            source is None
                            or destination is None
                            or source < 0
                            or destination < 0
                            or source == destination
                        ):
                            continue

                        transport = _normalise_transport(item.get("transport"))
                        if not transport:
                            if len(allowed) == 1:
                                transport = allowed[0]
                                migrated = True
                            else:
                                transport = _AMBIGUOUS_TRANSPORT

                        loaded.add((transport, source, destination))

                    self.rules = loaded
                    if migrated:
                        self._save_locked()
                    return
                except Exception as exc:
                    self.last_error = (
                        f"state_load: {type(exc).__name__}: {exc}"
                    )

            self.enabled = _truthy(
                os.getenv("CHANNEL_GATEWAY_ENABLED"),
                False,
            )
            initial: set[tuple[str, int, int]] = set()
            initial |= _parse_rule_map(
                os.getenv("CHANNEL_GATEWAY_MESHTASTIC_MAP"),
                "meshtastic",
            )
            initial |= _parse_rule_map(
                os.getenv("CHANNEL_GATEWAY_MESHCORE_MAP"),
                "meshcore",
            )

            generic = os.getenv("CHANNEL_GATEWAY_MAP")
            if generic and len(allowed) == 1:
                initial |= _parse_rule_map(generic, allowed[0])

            self.rules = initial
            self._save_locked()

    def _save_locked(self) -> None:
        """Persiste estado de forma atómica. Requiere ``self._lock``."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "enabled": bool(self.enabled),
            "rules": [
                {
                    "transport": transport,
                    "source": source,
                    "destination": destination,
                    "enabled": True,
                }
                for transport, source, destination in sorted(self.rules)
            ],
            "updated_at": int(time.time()),
        }

        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def _purge_recent_locked(self, now: float) -> None:
        """Elimina huellas RX/TX expiradas."""
        for cache, ttl in (
            (self._recent_rx, self.dedup_ttl),
            (self._recent_tx, self.tx_echo_ttl),
        ):
            expired = [
                key
                for key, timestamp in cache.items()
                if now - timestamp > ttl
            ]
            for key in expired:
                cache.pop(key, None)

    def _rate_allowed_locked(
        self,
        rule: tuple[str, int, int],
        now: float,
    ) -> bool:
        """Aplica rate-limit móvil de 60 s por regla completa."""
        if self.rate_limit_per_min <= 0:
            return True

        queue = self._rate[rule]
        while queue and now - queue[0] > 60.0:
            queue.popleft()

        if len(queue) >= self.rate_limit_per_min:
            return False

        queue.append(now)
        return True

    def status(self) -> dict:
        """Devuelve estado serializable incluyendo activación por perfil."""
        ctx = _radio_context()
        allowed = tuple(ctx.get("transports") or ())

        with self._lock:
            rules = [
                {
                    "transport": transport,
                    "source": source,
                    "destination": destination,
                    "active_for_profile": bool(
                        transport and transport in allowed
                    ),
                }
                for transport, source, destination in sorted(self.rules)
            ]

            return {
                "enabled": bool(self.enabled),
                "profile": ctx.get("profile"),
                "valid_profile": bool(ctx.get("valid")),
                "transports": list(allowed),
                "node_a_transport": ctx.get("node_a_transport"),
                "node_b_transport": ctx.get("node_b_transport"),
                "embedded_bridge_enabled": bool(
                    ctx.get("embedded_bridge_enabled")
                ),
                "rules": rules,
                "rule_count": len(rules),
                "active_rule_count": sum(
                    1 for item in rules if item["active_for_profile"]
                ),
                "state_file": str(self.state_file),
                "forward_direct": bool(self.forward_direct),
                "allow_external_bridge": bool(self.allow_external_bridge),
                "dedup_ttl": self.dedup_ttl,
                "rate_limit_per_min": self.rate_limit_per_min,
                "stats": dict(self.stats),
                "last_error": self.last_error,
            }

    def set_enabled(self, enabled: bool) -> dict:
        """Activa/desactiva el gateway y persiste el cambio."""
        with self._lock:
            self.enabled = bool(enabled)
            self._save_locked()
        return self.status()

    def add_rule(
        self,
        transport: str,
        source: int,
        destination: int,
        both: bool = False,
    ) -> dict:
        """
        Añade una regla y opcionalmente la inversa para modo bidireccional.
        """
        normalised_transport = _normalise_transport(transport)
        ctx = _radio_context()

        if not normalised_transport:
            raise ValueError("transport debe ser meshcore o meshtastic")
        if not _transport_allowed(normalised_transport, ctx):
            raise ValueError(
                f"transport {normalised_transport!r} no permitido por "
                f"RADIO_PROFILE={ctx.get('profile')}"
            )

        source = int(source)
        destination = int(destination)
        if source < 0 or destination < 0 or source == destination:
            raise ValueError("source/destination inválidos o iguales")

        with self._lock:
            self.rules.add((normalised_transport, source, destination))
            if both:
                self.rules.add((normalised_transport, destination, source))
            self._save_locked()

        return self.status()

    def del_rule(
        self,
        transport: str,
        source: int,
        destination: int,
        both: bool = False,
    ) -> dict:
        """Elimina una regla y opcionalmente su inversa."""
        normalised_transport = _normalise_transport(transport)
        ctx = _radio_context()

        if not normalised_transport:
            raise ValueError("transport debe ser meshcore o meshtastic")
        if not _transport_allowed(normalised_transport, ctx):
            raise ValueError(
                f"transport {normalised_transport!r} no permitido por "
                f"RADIO_PROFILE={ctx.get('profile')}"
            )

        source = int(source)
        destination = int(destination)

        with self._lock:
            self.rules.discard((normalised_transport, source, destination))
            if both:
                self.rules.discard((normalised_transport, destination, source))
            self._save_locked()

        return self.status()

    def clear_rules(self) -> dict:
        """Elimina todas las reglas conservando el estado global ON/OFF."""
        with self._lock:
            self.rules.clear()
            self._save_locked()
        return self.status()

    def _destinations(self, transport: str, source: int) -> list[int]:
        """Devuelve destinos activos de un transporte/canal."""
        if not _transport_allowed(transport):
            with self._lock:
                self.stats["inactive_profile"] += 1
            return []

        with self._lock:
            return [
                destination
                for rule_transport, rule_source, destination in sorted(self.rules)
                if rule_transport == transport and rule_source == source
            ]

    def _prepare_forward(
        self,
        transport: str,
        source: int,
        sender: str,
        text: str,
        *,
        is_direct: bool = False,
        local_sender: bool = False,
    ) -> list[int]:
        """
        Aplica ON/OFF, DM, dedup y anti-eco antes de calcular destinos.
        """
        now = time.time()
        with self._lock:
            self.stats["rx_text"] += 1
            self.stats[f"rx_{transport}"] += 1

            if not self.enabled or not self.rules:
                return []

            if is_direct and not self.forward_direct:
                self.stats["ignored_direct"] += 1
                return []

            self._purge_recent_locked(now)

            rx_fingerprint = _fingerprint(
                "rx",
                transport,
                sender.lower(),
                source,
                text,
            )
            if rx_fingerprint in self._recent_rx:
                self.stats["duplicate_rx"] += 1
                return []
            self._recent_rx[rx_fingerprint] = now

            tx_fingerprint = _fingerprint(
                "tx",
                transport,
                source,
                text,
            )
            if tx_fingerprint in self._recent_tx and local_sender:
                self.stats["echo_suppressed"] += 1
                return []

        return self._destinations(transport, source)

    def _after_tx(
        self,
        transport: str,
        destination: int,
        text: str,
    ) -> None:
        """Registra una TX confirmada/encolada correctamente."""
        with self._lock:
            self._recent_tx[
                _fingerprint("tx", transport, destination, text)
            ] = time.time()
            self.stats["forwarded"] += 1
            self.stats[f"forwarded_{transport}"] += 1

    def _rate_ok(
        self,
        transport: str,
        source: int,
        destination: int,
    ) -> bool:
        """Aplica rate-limit por regla y actualiza estadísticas."""
        with self._lock:
            if self._rate_allowed_locked(
                (transport, source, destination),
                time.time(),
            ):
                return True
            self.stats["rate_limited"] += 1
            return False

    def _tx_error(
        self,
        transport: str,
        source: int,
        destination: int,
        exc: Exception,
    ) -> None:
        """Registra un error TX sin propagarlo al broker principal."""
        with self._lock:
            self.stats["errors"] += 1
            self.last_error = (
                f"{transport} tx {source}->{destination}: "
                f"{type(exc).__name__}: {exc}"
            )
        print(f"[channel-gateway] ERROR {self.last_error}", flush=True)

    def _enqueue_meshtastic(
        self,
        source: int,
        destination: int,
        text: str,
        interface: Any,
    ) -> bool:
        """
        Encola una TX Meshtastic usando la ``SENDQ`` estable del broker.

        Como fallback de pruebas/ejecución aislada usa la misma interfaz RX.
        Nunca crea una interfaz Meshtastic adicional.
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
                "transport": "meshtastic",
                "source_channel": int(source),
                "destination_channel": int(destination),
            },
        }
        if not self.allow_external_bridge:
            payload["no_bridge"] = True

        main_mod = sys.modules.get("__main__")
        queue = getattr(main_mod, "SENDQ", None) if main_mod else None
        if queue is not None and hasattr(queue, "offer"):
            queue.offer(payload, coalesce=False)
            return True

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

    def _meshcore_engine(self) -> Any:
        """Obtiene el ``MESHCORE_ENGINE`` ya creado por el broker principal."""
        main_mod = sys.modules.get("__main__")
        return getattr(main_mod, "MESHCORE_ENGINE", None) if main_mod else None

    def _enqueue_meshcore(self, destination: int, text: str) -> bool:
        """
        Encola una TX MeshCore en el motor ya existente.

        ``MeshCoreEmbeddedBridge.enqueue_send_channel()`` devuelve un ``tx_id``
        cuando la operación ha sido encolada y ``None`` cuando no puede hacerlo.
        Por ello solo se considera éxito un resultado distinto de ``None`` y de
        ``False``. Esto evita contabilizar como enviada una operación rechazada.
        """
        engine = self._meshcore_engine()
        if engine is None or not bool(getattr(engine, "enable", False)):
            return False

        enqueue = getattr(engine, "enqueue_send_channel", None)
        if not callable(enqueue):
            return False

        tx_id = enqueue(int(destination), str(text))
        return tx_id is not None and tx_id is not False

    def handle_meshtastic_packet(
        self,
        packet: dict | None,
        interface: Any = None,
    ) -> int:
        """Procesa un RX Meshtastic y devuelve el número de destinos encolados."""
        packet = packet or {}
        if not isinstance(packet, dict) or not _is_text_message(packet):
            return 0

        text = _normalise_text(_extract_text(packet))
        if not text:
            return 0

        source = _extract_channel(packet)
        sender = _extract_sender(packet)
        local_ids = _local_node_ids(interface)
        sender_is_local = bool(sender) and sender.lower() in local_ids

        destinations = self._prepare_forward(
            "meshtastic",
            source,
            sender,
            text,
            is_direct=not _is_broadcast(packet),
            local_sender=sender_is_local or not local_ids,
        )

        forwarded = 0
        for destination in destinations:
            if not self._rate_ok("meshtastic", source, destination):
                continue
            try:
                if not self._enqueue_meshtastic(
                    source,
                    destination,
                    text,
                    interface,
                ):
                    raise RuntimeError(
                        "SENDQ/interface Meshtastic no disponible"
                    )
                self._after_tx("meshtastic", destination, text)
                forwarded += 1
            except Exception as exc:
                self._tx_error("meshtastic", source, destination, exc)

        return forwarded

    def handle_packet(
        self,
        packet: dict | None,
        interface: Any = None,
    ) -> int:
        """Alias de compatibilidad v7.0.55 para el camino Meshtastic."""
        return self.handle_meshtastic_packet(packet, interface)

    def handle_meshcore_message(self, event_or_payload: Any) -> int:
        """
        Procesa un ``CHANNEL_MSG_RECV`` MeshCore.

        Acepta el objeto evento real de MeshCore o un diccionario equivalente
        para pruebas. Solo trabaja con mensajes de canal; no altera los DM.
        """
        if isinstance(event_or_payload, dict):
            payload = dict(
                event_or_payload.get("payload") or event_or_payload
            )
            event_type = event_or_payload.get("type")
        else:
            payload = dict(
                getattr(event_or_payload, "payload", None) or {}
            )
            event_type = getattr(event_or_payload, "type", None)

        try:
            main_mod = sys.modules.get("__main__")
            mc_event_type = (
                getattr(main_mod, "_MCEventType", None)
                if main_mod
                else None
            )
            channel_event = (
                getattr(mc_event_type, "CHANNEL_MSG_RECV", None)
                if mc_event_type
                else None
            )
            if (
                channel_event is not None
                and event_type is not None
                and event_type != channel_event
            ):
                return 0
        except Exception:
            pass

        source = _parse_int(payload.get("channel_idx"))
        text = _normalise_text(str(payload.get("text") or ""))
        if source is None or source < 0 or not text:
            return 0

        sender = str(
            payload.get("pubkey_prefix")
            or payload.get("sender")
            or payload.get("from")
            or ""
        ).strip()

        # En MeshCore una TX propia puede volver a recibirse como evento de canal
        # sin un identificador local fiable. La huella de TX reciente es por ello
        # suficiente para suprimir exclusivamente el eco generado por el gateway.
        tx_fingerprint = _fingerprint(
            "tx",
            "meshcore",
            source,
            text,
        )
        with self._lock:
            self._purge_recent_locked(time.time())
            if tx_fingerprint in self._recent_tx:
                self.stats["rx_text"] += 1
                self.stats["rx_meshcore"] += 1
                self.stats["echo_suppressed"] += 1
                return 0

        destinations = self._prepare_forward(
            "meshcore",
            source,
            sender,
            text,
        )

        forwarded = 0
        for destination in destinations:
            if not self._rate_ok("meshcore", source, destination):
                continue
            try:
                if not self._enqueue_meshcore(destination, text):
                    raise RuntimeError(
                        "MESHCORE_ENGINE/enqueue_send_channel no disponible "
                        "o no devolvió tx_id"
                    )
                self._after_tx("meshcore", destination, text)
                forwarded += 1
            except Exception as exc:
                self._tx_error("meshcore", source, destination, exc)

        return forwarded


class ChannelGatewayControlServer(threading.Thread):
    """Servidor JSONL ligero de control ejecutado dentro del broker."""

    daemon = True

    def __init__(self, manager: ChannelGatewayManager):
        super().__init__(name="channel-gateway-control", daemon=True)
        self.manager = manager
        self.bind_host = (
            (os.getenv("CHANNEL_GATEWAY_CTRL_BIND") or "0.0.0.0").strip()
            or "0.0.0.0"
        )
        try:
            default_port = int(os.getenv("BROKER_CTRL_PORT", "8766") or 8766) + 1
        except Exception:
            default_port = 8767
        self.port = int(
            os.getenv("CHANNEL_GATEWAY_CTRL_PORT", str(default_port))
            or default_port
        )
        self.token = (os.getenv("CHANNEL_GATEWAY_CTRL_TOKEN") or "").strip()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        """Solicita parada del servidor y cierra el socket."""
        self._stop_event.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def _reply(self, conn: socket.socket, payload: dict) -> None:
        """Envía una respuesta JSONL."""
        conn.sendall(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )

    def _handle_request(self, req: dict) -> dict:
        """Ejecuta una petición RPC Channel Gateway."""
        if self.token and str(req.get("token") or "") != self.token:
            return {"ok": False, "error": "unauthorized"}

        command = str(req.get("cmd") or "").strip().upper()
        params = req.get("params") or {}

        try:
            if command in {"CHANNEL_GATEWAY_STATUS", "STATUS"}:
                return {"ok": True, **self.manager.status()}
            if command in {"CHANNEL_GATEWAY_ON", "ON"}:
                return {"ok": True, **self.manager.set_enabled(True)}
            if command in {"CHANNEL_GATEWAY_OFF", "OFF"}:
                return {"ok": True, **self.manager.set_enabled(False)}
            if command in {"CHANNEL_GATEWAY_CLEAR", "CLEAR"}:
                return {"ok": True, **self.manager.clear_rules()}
            if command in {"CHANNEL_GATEWAY_ADD", "ADD"}:
                return {
                    "ok": True,
                    **self.manager.add_rule(
                        str(params.get("transport") or ""),
                        int(params.get("source")),
                        int(params.get("destination")),
                        bool(params.get("both", False)),
                    ),
                }
            if command in {"CHANNEL_GATEWAY_DEL", "DEL", "DELETE"}:
                return {
                    "ok": True,
                    **self.manager.del_rule(
                        str(params.get("transport") or ""),
                        int(params.get("source")),
                        int(params.get("destination")),
                        bool(params.get("both", False)),
                    ),
                }
            return {
                "ok": False,
                "error": f"unsupported_command: {command}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def run(self) -> None:
        """Bucle del servidor de control."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_host, self.port))
            sock.listen(8)
            sock.settimeout(1.0)

            while not self._stop_event.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    raise

                with conn:
                    conn.settimeout(3.0)
                    buffer = b""
                    while b"\n" not in buffer and len(buffer) < 65536:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk

                    try:
                        request = json.loads(
                            buffer.split(b"\n", 1)[0].decode(
                                "utf-8",
                                errors="strict",
                            )
                            or "{}"
                        )
                        if not isinstance(request, dict):
                            raise ValueError("request must be an object")
                        response = self._handle_request(request)
                    except Exception as exc:
                        response = {
                            "ok": False,
                            "error": (
                                f"bad_request: {type(exc).__name__}: {exc}"
                            ),
                        }

                    self._reply(conn, response)
        finally:
            try:
                sock.close()
            except Exception:
                pass


class MeshCoreGatewayBinder(threading.Thread):
    """
    Enlaza Channel Gateway a la sesión MeshCore ya abierta por el broker.

    El binder no crea conexiones. Espera hasta que ``MESHCORE_ENGINE._meshcore``
    exista y registra una única suscripción por instancia de sesión.
    """

    daemon = True

    def __init__(self, manager: ChannelGatewayManager):
        super().__init__(name="channel-gateway-meshcore-binder", daemon=True)
        self.manager = manager
        self._stop_event = threading.Event()
        self._bound_session_id: int | None = None

    def stop(self) -> None:
        """Solicita la parada del binder."""
        self._stop_event.set()

    def _bind_if_ready(self) -> None:
        """
        Registra el callback MeshCore cuando la sesión existente está disponible.

        El callback es ``async def`` porque la API MeshCore usada actualmente por
        el broker ejecuta sus suscriptores como corutinas. Mantener el mismo tipo
        de callback evita cambiar el contrato del motor ya operativo.
        """
        ctx = _radio_context()
        if "meshcore" not in tuple(ctx.get("transports") or ()):
            return

        main_mod = sys.modules.get("__main__")
        engine = getattr(main_mod, "MESHCORE_ENGINE", None) if main_mod else None
        if engine is None or not bool(getattr(engine, "enable", False)):
            return

        meshcore_session = getattr(engine, "_meshcore", None)
        if (
            meshcore_session is None
            or self._bound_session_id == id(meshcore_session)
        ):
            return

        mc_event_type = (
            getattr(main_mod, "_MCEventType", None)
            if main_mod
            else None
        )
        channel_event = (
            getattr(mc_event_type, "CHANNEL_MSG_RECV", None)
            if mc_event_type
            else None
        )
        if channel_event is None or not hasattr(meshcore_session, "subscribe"):
            return

        async def _callback(event: Any) -> None:
            """Callback compatible con la API async de MeshCore."""
            try:
                self.manager.handle_meshcore_message(event)
            except Exception as exc:
                print(
                    "[channel-gateway] MeshCore RX ERROR "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

        meshcore_session.subscribe(channel_event, _callback)
        self._bound_session_id = id(meshcore_session)

    def run(self) -> None:
        """Espera y re-enlaza únicamente si cambia la sesión MeshCore."""
        while not self._stop_event.is_set():
            try:
                self._bind_if_ready()
            except Exception as exc:
                print(
                    "[channel-gateway] binder MeshCore: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            self._stop_event.wait(1.0)


_MANAGER: ChannelGatewayManager | None = None
_CONTROL: ChannelGatewayControlServer | None = None
_MESHCORE_BINDER: MeshCoreGatewayBinder | None = None
_STARTED = False
_START_LOCK = threading.Lock()


def channel_gateway_manager() -> ChannelGatewayManager:
    """Devuelve el singleton Channel Gateway."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ChannelGatewayManager()
    return _MANAGER


def _on_meshtastic_receive(
    packet: dict | None = None,
    interface: Any = None,
    **kwargs: Any,
) -> None:
    """Callback PubSub Meshtastic; nunca propaga excepciones al broker."""
    try:
        channel_gateway_manager().handle_meshtastic_packet(
            packet or {},
            interface=interface,
        )
    except Exception as exc:
        print(
            "[channel-gateway] Meshtastic RX ERROR "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def start_channel_gateway_runtime() -> ChannelGatewayManager:
    """
    Instala una sola vez el runtime Channel Gateway según ``RADIO_PROFILE``.

    Meshtastic se suscribe solo si ese transporte está activo. MeshCore usa el
    binder que espera la sesión ya levantada por ``MESHCORE_ENGINE``.
    """
    global _STARTED, _CONTROL, _MESHCORE_BINDER

    with _START_LOCK:
        if _STARTED:
            return channel_gateway_manager()

        manager = channel_gateway_manager()
        ctx = _radio_context()
        allowed = tuple(ctx.get("transports") or ())

        if "meshtastic" in allowed:
            pub.subscribe(_on_meshtastic_receive, "meshtastic.receive")

        if "meshcore" in allowed:
            _MESHCORE_BINDER = MeshCoreGatewayBinder(manager)
            _MESHCORE_BINDER.start()

        _CONTROL = ChannelGatewayControlServer(manager)
        _CONTROL.start()
        _STARTED = True

        print(
            "[channel-gateway] runtime v7.0.56 "
            f"profile={ctx.get('profile')} "
            f"transports={list(allowed)} "
            f"enabled={manager.enabled} "
            f"active_rules={manager.status().get('active_rule_count')}",
            flush=True,
        )
        return manager
