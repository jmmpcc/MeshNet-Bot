#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Modelo común de observabilidad MeshCore para MeshNet-Bot.

Fase 1 de MeshCore Observability.

Este módulo NO está conectado todavía al flujo operativo del broker. Su objetivo
es definir un contrato estable para representar eventos MeshCore antes de que
fases posteriores añadan persistencia, topología, estadísticas o APIs.

Principios de diseño:
- No abre conexiones de radio.
- No transmite mensajes.
- No modifica cachés del broker.
- No escribe en disco.
- No importa la librería ``meshcore``.
- Acepta únicamente datos ya normalizados/recibidos por el broker.
- La serialización siempre devuelve estructuras JSON compatibles.

Uso previsto en fases posteriores::

    event = build_meshcore_message_event(
        payload=data,
        kind="chan",
        channel_idx=2,
        channel_tag="EMERGENCIAS",
        transport="serial",
    )
    serialized = event.to_dict()

La Fase 2 podrá entregar ``serialized`` a un PacketArchive best-effort sin tocar
la lógica actual de recepción/transmisión.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


MESHCORE_EVENT_SCHEMA_VERSION = 1

_ALLOWED_DIRECTIONS = {"rx", "tx", "internal"}
_ALLOWED_MESSAGE_KINDS = {"contact", "chan", "system", "unknown"}
_ALLOWED_TRANSPORTS = {"serial", "tcp", "ble", "unknown"}


def _utc_now_iso() -> str:
    """Devuelve un timestamp UTC ISO-8601 apto para JSON y SQLite.

    Uso::

        timestamp_utc = _utc_now_iso()

    La función está aislada para permitir pruebas deterministas y para que las
    fases de persistencia no tengan que decidir formatos de fecha diferentes.
    """

    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    """Normaliza un valor opcional a texto sin espacios exteriores."""

    if value is None:
        return ""
    return str(value).strip()


def _safe_int(value: Any) -> int | None:
    """Convierte un valor a entero o devuelve ``None`` sin lanzar excepción."""

    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    """Convierte un valor a float o devuelve ``None`` sin lanzar excepción."""

    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    """Convierte recursivamente datos heterogéneos a valores serializables JSON.

    Parámetros:
        value: Valor procedente del payload o de metadatos MeshCore.

    Funcionalidad:
        - Conserva ``None``, bool, int, float y str.
        - Convierte bytes/bytearray a hexadecimal.
        - Convierte mappings, listas, tuplas y sets recursivamente.
        - Usa ``str()`` como último recurso para objetos de librerías externas.

    Esta función permite que el modelo sea independiente de las clases internas
    de distintas versiones de la librería ``meshcore``.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


@dataclass(slots=True)
class MeshCoreRepeaterHop:
    """Representa un salto/repetidor observado dentro de una ruta MeshCore.

    Cómo se crea:
        ``build_meshcore_message_event`` reutiliza ``meshcore_repeaters`` ya
        calculado por ``MeshCoreEmbeddedBridge._meshcore_enrich_path_info``.

    Campos:
        hash: Hash/prefijo de ruta recibido desde MeshCore.
        name: Nombre humano resuelto cuando el broker conoce el contacto.
        resolved: True si el hash pudo asociarse inequívocamente a un contacto.
        ambiguous: True cuando el mismo prefijo coincide con varios contactos.
        snr: SNR por salto cuando el protocolo/librería lo proporciona.
        lat/lon: Posición anunciada del repetidor cuando está disponible.
    """

    hash: str = ""
    name: str = ""
    resolved: bool = False
    ambiguous: bool = False
    snr: float | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass(slots=True)
class MeshCoreEvent:
    """Contrato normalizado de un evento observable de MeshCore.

    El modelo separa los campos de consulta frecuente de ``payload`` y
    ``metadata``. De esta forma las fases posteriores podrán indexar remitente,
    canal, ruta o tipo de evento sin depender del formato concreto de la
    librería MeshCore.

    No debe utilizarse para controlar la radio. Es únicamente observabilidad.
    """

    schema_version: int = MESHCORE_EVENT_SCHEMA_VERSION
    timestamp_utc: str = field(default_factory=_utc_now_iso)
    event_type: str = "message_rx"
    direction: str = "rx"
    transport: str = "unknown"
    message_kind: str = "unknown"

    packet_id: str = ""
    sender_prefix: str = ""
    sender_public_key: str = ""
    sender_alias: str = ""

    channel_idx: int | None = None
    channel_tag: str = ""
    text: str = ""

    source_lat: float | None = None
    source_lon: float | None = None
    path_hex: str = ""
    path_hops: list[MeshCoreRepeaterHop] = field(default_factory=list)

    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normaliza valores sin alterar la semántica del evento.

        No lanza errores por valores desconocidos; los reduce a variantes
        seguras. Un evento de observabilidad nunca debe romper el flujo que lo
        genera.
        """

        self.event_type = _clean_text(self.event_type) or "unknown"

        direction = _clean_text(self.direction).casefold()
        self.direction = direction if direction in _ALLOWED_DIRECTIONS else "internal"

        transport = _clean_text(self.transport).casefold()
        self.transport = transport if transport in _ALLOWED_TRANSPORTS else "unknown"

        kind = _clean_text(self.message_kind).casefold()
        self.message_kind = kind if kind in _ALLOWED_MESSAGE_KINDS else "unknown"

        self.packet_id = _clean_text(self.packet_id)
        self.sender_prefix = _clean_text(self.sender_prefix).lower()
        self.sender_public_key = _clean_text(self.sender_public_key).lower()
        self.sender_alias = _clean_text(self.sender_alias)
        self.channel_idx = _safe_int(self.channel_idx)
        self.channel_tag = _clean_text(self.channel_tag)
        self.text = str(self.text or "")
        self.source_lat = _safe_float(self.source_lat)
        self.source_lon = _safe_float(self.source_lon)
        self.path_hex = _clean_text(self.path_hex).lower()
        self.payload = dict(_json_safe(self.payload or {}))
        self.metadata = dict(_json_safe(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        """Devuelve el evento como diccionario completamente serializable JSON.

        Uso::

            record = event.to_dict()

        La salida será el contrato utilizado por Packet Archive y APIs en fases
        posteriores. No devuelve referencias mutables a los dataclass internos.
        """

        return _json_safe(asdict(self))


def build_meshcore_message_event(
    *,
    payload: Mapping[str, Any] | None,
    kind: str,
    channel_idx: int | None = None,
    channel_tag: str | None = None,
    transport: str = "unknown",
    event_type: str = "message_rx",
    timestamp_utc: str | None = None,
    sender_alias: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> MeshCoreEvent:
    """Construye un ``MeshCoreEvent`` desde un payload ya recibido por el broker.

    Uso previsto::

        event = build_meshcore_message_event(
            payload=data,
            kind=kind,
            channel_idx=chan_idx,
            channel_tag=mc_chan_tag,
            transport=self.mode,
            sender_alias=alias,
        )

    Parámetros:
        payload:
            Diccionario recibido/enriquecido por el callback MeshCore actual.
        kind:
            ``contact`` para DM o ``chan`` para mensaje de canal.
        channel_idx:
            Índice real de canal MeshCore cuando existe.
        channel_tag:
            Alias lógico configurado por el broker para ese canal.
        transport:
            Transporte de la sesión: serial, tcp o ble.
        event_type:
            Tipo lógico estable. En Fase 1 se usa principalmente ``message_rx``.
        timestamp_utc:
            Timestamp opcional para importaciones/pruebas. Por defecto usa UTC.
        sender_alias:
            Alias ya resuelto por el broker; tiene prioridad sobre el payload.
        metadata:
            Información adicional de observabilidad que no forma parte del
            payload original.

    Funcionalidad:
        Reutiliza exclusivamente datos que el Broker ya calcula. No consulta la
        radio ni intenta resolver contactos o rutas por su cuenta.
    """

    data = dict(payload or {})
    repeaters: list[MeshCoreRepeaterHop] = []
    for item in data.get("meshcore_repeaters") or []:
        if not isinstance(item, Mapping):
            continue
        repeaters.append(
            MeshCoreRepeaterHop(
                hash=_clean_text(item.get("hash")).lower(),
                name=_clean_text(item.get("name")),
                resolved=bool(item.get("resolved")),
                ambiguous=bool(item.get("ambiguous")),
                snr=_safe_float(item.get("snr")),
                lat=_safe_float(item.get("lat")),
                lon=_safe_float(item.get("lon")),
            )
        )

    packet_id = (
        data.get("id")
        or data.get("message_id")
        or data.get("packet_id")
        or data.get("timestamp")
        or ""
    )
    public_key = data.get("public_key") or data.get("pubkey") or data.get("key") or ""
    prefix = data.get("pubkey_prefix") or data.get("key_prefix") or data.get("prefix") or ""
    alias = sender_alias or data.get("from_name") or data.get("alias") or data.get("name") or ""

    path_hex = data.get("path_hex") or ""
    if not path_hex:
        raw_path = data.get("path")
        if isinstance(raw_path, (bytes, bytearray)):
            path_hex = bytes(raw_path).hex()

    return MeshCoreEvent(
        timestamp_utc=timestamp_utc or _utc_now_iso(),
        event_type=event_type,
        direction="rx",
        transport=transport,
        message_kind=kind,
        packet_id=_clean_text(packet_id),
        sender_prefix=_clean_text(prefix),
        sender_public_key=_clean_text(public_key),
        sender_alias=_clean_text(alias),
        channel_idx=channel_idx if channel_idx is not None else data.get("channel_idx"),
        channel_tag=_clean_text(channel_tag),
        text=str(data.get("text") or ""),
        source_lat=_safe_float(data.get("from_lat") if data.get("from_lat") is not None else data.get("lat")),
        source_lon=_safe_float(data.get("from_lon") if data.get("from_lon") is not None else data.get("lon")),
        path_hex=_clean_text(path_hex),
        path_hops=repeaters,
        payload=data,
        metadata=dict(metadata or {}),
    )
