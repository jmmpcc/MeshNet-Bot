"""
Despachador APRS reutilizable para aplicaciones independientes de MeshNet-Bot.

La función pública ``send_application_aprs`` envía un texto al gateway APRS ya
existente mediante su interfaz UDP de control. No abre conexiones KISS, no
accede directamente a soundmodem/direwolf y no modifica el flujo Mesh.

El envío exige dos autorizaciones:
  1. ``APPS_APRS_ENABLED=1`` habilita globalmente las aplicaciones.
  2. La aplicación debe figurar en ``APPS_APRS_ALLOWED_SOURCES``.

Cada aplicación conserva además su propio interruptor antes de llamar al helper.
Esto crea una doble barrera para impedir emisiones RF accidentales.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, asdict
from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on", "si", "sí", "y"}


def env_bool(name: str, default: str = "0") -> bool:
    """Devuelve una variable de entorno como booleano tolerante."""
    return str(os.getenv(name, default) or default).strip().casefold() in _TRUE_VALUES


def _allowed_sources() -> set[str]:
    """Normaliza la lista global de aplicaciones autorizadas para APRS."""
    raw = os.getenv("APPS_APRS_ALLOWED_SOURCES", "")
    return {item.strip().casefold() for item in raw.split(",") if item.strip()}


def _normalize_path(raw: str) -> list[str] | None:
    """
    Convierte la ruta APRS configurada al formato esperado por el gateway.

    ``None`` significa que el gateway debe reutilizar su ``APRS_PATH`` actual.
    Una lista vacía fuerza transmisión local sin digipeaters.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if value.casefold() in {"none", "direct", "local", "sin", "no", "0"}:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def estimate_aprs_parts(text: str, max_len: int) -> int:
    """
    Estima de forma conservadora las partes APRS necesarias.

    Reserva ocho caracteres por fragmento para el sufijo ``(i/N)`` que añade el
    gateway. Se usa únicamente para bloquear mensajes excesivos antes de RF.
    """
    clean = " ".join((text or "").split())
    if not clean:
        return 0
    usable = max(8, int(max_len) - 8)
    return (len(clean) + usable - 1) // usable


@dataclass(frozen=True)
class AprsDispatchResult:
    """Resultado estable devuelto a cualquier aplicación llamante."""

    ok: bool
    source: str
    dest: str
    chunks: int = 0
    sent: int = 0
    duplicate: bool = False
    udp_sent: bool = False
    skipped: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def send_application_aprs(
    *,
    source: str,
    text: str,
    dest: str | None = None,
    origin: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """
    Envía un mensaje de una aplicación independiente al gateway APRS existente.

    Parámetros:
      source: nombre lógico de la aplicación, por ejemplo ``farmacias``.
      text: texto APRS ya resumido por la aplicación.
      dest: destino APRS; vacío reutiliza ``APPS_APRS_DESTINATION``.
      origin: etiqueta opcional para logs/deduplicación del gateway.
      timeout: espera máxima de confirmación UDP.

    Retorna un diccionario serializable. ``skipped=True`` indica que el envío no
    se intentó por configuración, no que exista un fallo del gateway.
    """
    source_norm = (source or "").strip().casefold()
    dest_norm = (dest or os.getenv("APPS_APRS_DESTINATION", "broadcast")).strip() or "broadcast"
    aprs_dest = "broadcast" if dest_norm.casefold() in {"broadcast", "all"} else dest_norm.upper()
    text_clean = " ".join((text or "").split())

    if not source_norm:
        return AprsDispatchResult(False, "", aprs_dest, error="missing source").to_dict()
    if not text_clean:
        return AprsDispatchResult(False, source_norm, aprs_dest, error="missing text").to_dict()
    if not env_bool("APPS_APRS_ENABLED", "0"):
        return AprsDispatchResult(True, source_norm, aprs_dest, skipped=True, error="apps_aprs_disabled").to_dict()
    if source_norm not in _allowed_sources():
        return AprsDispatchResult(True, source_norm, aprs_dest, skipped=True, error="source_not_allowed").to_dict()

    try:
        max_len = max(20, int(os.getenv("APRS_MAX_LEN", "67")))
    except (TypeError, ValueError):
        max_len = 67
    try:
        max_chunks = max(1, int(os.getenv("APPS_APRS_MAX_CHUNKS", "2")))
    except (TypeError, ValueError):
        max_chunks = 2

    estimated_parts = estimate_aprs_parts(text_clean, max_len)
    if estimated_parts > max_chunks:
        return AprsDispatchResult(
            False,
            source_norm,
            aprs_dest,
            chunks=estimated_parts,
            error=f"message_requires_{estimated_parts}_chunks_limit_{max_chunks}",
        ).to_dict()

    host = os.getenv("APRS_CTRL_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("APRS_CTRL_PORT", "9464"))
    except (TypeError, ValueError):
        port = 9464
    try:
        timeout_s = float(timeout if timeout is not None else os.getenv("APRS_CTRL_ACK_TIMEOUT", "8.0"))
    except (TypeError, ValueError):
        timeout_s = 8.0
    timeout_s = max(1.0, min(timeout_s, 30.0))

    control: dict[str, Any] = {
        "mode": "aprs",
        "dest": aprs_dest,
        "text": text_clean,
        "ack": True,
        "origin": (origin or f"app_{source_norm}").strip(),
    }
    path = _normalize_path(os.getenv("APPS_APRS_PATH", os.getenv("APRS_BOT_PATH", "")))
    if path is not None:
        control["path"] = path

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            sock.sendto(json.dumps(control, ensure_ascii=False).encode("utf-8"), (host, port))
            try:
                raw, _address = sock.recvfrom(8192)
            except socket.timeout:
                return AprsDispatchResult(
                    False,
                    source_norm,
                    aprs_dest,
                    udp_sent=True,
                    error=f"gateway_ack_timeout_{timeout_s:.1f}s",
                ).to_dict()
    except OSError as exc:
        return AprsDispatchResult(
            False,
            source_norm,
            aprs_dest,
            error=f"{type(exc).__name__}: {exc}",
        ).to_dict()

    try:
        response = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return AprsDispatchResult(
            False,
            source_norm,
            aprs_dest,
            udp_sent=True,
            error=f"invalid_gateway_response: {type(exc).__name__}",
        ).to_dict()
    if not isinstance(response, dict):
        return AprsDispatchResult(False, source_norm, aprs_dest, udp_sent=True, error="gateway_response_not_object").to_dict()

    try:
        chunks = int(response.get("parts", response.get("chunks", 0)) or 0)
    except (TypeError, ValueError):
        chunks = 0
    try:
        sent = int(response.get("sent", chunks if response.get("ok") else 0) or 0)
    except (TypeError, ValueError):
        sent = 0

    return AprsDispatchResult(
        ok=bool(response.get("ok")),
        source=source_norm,
        dest=str(response.get("dest") or aprs_dest),
        chunks=chunks,
        sent=sent,
        duplicate=bool(response.get("duplicate")),
        udp_sent=True,
        error=response.get("error"),
    ).to_dict()
