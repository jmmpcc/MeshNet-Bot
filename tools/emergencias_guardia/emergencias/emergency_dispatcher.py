from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, asdict
from typing import Any

from .models import Event, SEVERITY_RANK


_TRUTHY = {"1", "true", "yes", "on", "si", "sí", "y"}


def _enabled(name: str, default: str = "0") -> bool:
    """Devuelve True cuando una variable de entorno contiene un valor afirmativo."""
    return str(os.getenv(name, default) or default).strip().lower() in _TRUTHY


def _allowed_sources() -> set[str]:
    """Obtiene la lista blanca de aplicaciones autorizadas para salidas APRS."""
    return {
        item.strip().lower()
        for item in str(os.getenv("APPS_APRS_ALLOWED_SOURCES", "") or "").split(",")
        if item.strip()
    }


@dataclass(slots=True)
class DispatchResult:
    """Resultado normalizado de las salidas secundarias de una emergencia.

    Esta estructura no sustituye el resultado Mesh existente. Se añade después
    de que el broker haya aceptado el mensaje, de forma que cualquier error en
    APRS-IS o voz nunca revierta ni repita una entrega Mesh correcta.
    """

    aprsis_bulletin: dict[str, Any]
    voice_rf: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _aprsis_bulletin_enabled() -> bool:
    """Comprueba todas las autorizaciones necesarias para publicar boletines."""
    return (
        _enabled("APPS_APRS_ENABLED")
        and "emergencias" in _allowed_sources()
        and _enabled("EMERGENCIAS_APRS_ENABLED")
        and _enabled("APRSIS_PUSH_ENABLED")
        and _enabled("APRSIS_EMERGENCY_BULLETIN_ENABLED")
    )


def _voice_result(event: Event) -> dict[str, Any]:
    """Devuelve el estado de voz RF sin realizar ninguna transmisión.

    La salida se deja preparada para la fase de voz, pero permanece bloqueada
    por defecto. En esta fase no existe acceso a PTT, audio ni radio.
    """
    if not _enabled("EMERGENCIAS_VOICE_RF_ENABLED"):
        return {"ok": True, "sent": False, "reason": "disabled"}
    if not _enabled("EMERGENCIAS_VOICE_RF_AUTOMATIC"):
        return {"ok": True, "sent": False, "reason": "automatic_disabled"}
    minimum = str(os.getenv("EMERGENCIAS_VOICE_RF_MIN_LEVEL", "high") or "high").strip().lower()
    if SEVERITY_RANK.get(event.severity, 0) < SEVERITY_RANK.get(minimum, SEVERITY_RANK["high"]):
        return {"ok": True, "sent": False, "reason": "severity_below_threshold"}
    # Defensa adicional: esta fase nunca transmite voz aunque una variable se active por error.
    return {"ok": False, "sent": False, "reason": "voice_gateway_not_deployed"}


def _send_aprsis_bulletin(event: Event, message: str) -> dict[str, Any]:
    """Solicita al gateway APRS activo un boletín público APRS-IS.

    Parámetros:
      event: emergencia normalizada.
      message: texto compacto ya aceptado por el flujo Mesh.

    Reutiliza APRS_CTRL_HOST y APRS_CTRL_PORT. No abre otra conexión APRS-IS,
    no transmite por RF y no usa APRSIS_PUSH_TO.
    """
    if not _aprsis_bulletin_enabled():
        return {"ok": True, "sent": False, "reason": "disabled"}
    minimum = str(
        os.getenv("APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL", "high") or "high"
    ).strip().lower()
    if SEVERITY_RANK.get(event.severity, 0) < SEVERITY_RANK.get(minimum, SEVERITY_RANK["high"]):
        return {"ok": True, "sent": False, "reason": "severity_below_threshold"}

    host = str(os.getenv("APRS_CTRL_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int(os.getenv("APRS_CTRL_PORT", "9464") or "9464")
    timeout = max(0.5, float(os.getenv("APRS_CTRL_ACK_TIMEOUT", "8") or "8"))
    payload = {
        "mode": "aprsis_emergency_bulletin",
        "origin": "app_emergencias",
        "event_id": event.event_id,
        "severity": event.severity,
        "status": event.status,
        "text": message,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.sendto(data, (host, port))
        response, _ = client.recvfrom(65536)
    result = json.loads(response.decode("utf-8", errors="replace"))
    return result if isinstance(result, dict) else {
        "ok": False,
        "sent": False,
        "reason": "invalid_response",
    }


def dispatch_secondary_outputs(event: Event, message: str) -> dict[str, Any]:
    """Distribuye una emergencia por las salidas secundarias configuradas.

    Debe llamarse únicamente después de una entrega Mesh correcta. Cada salida
    se evalúa de forma independiente. Una excepción APRS-IS se encapsula en el
    resultado y nunca se propaga hacia el flujo principal.
    """
    try:
        aprsis = _send_aprsis_bulletin(event, message)
    except Exception as exc:  # noqa: BLE001 - aislamiento deliberado de salida secundaria
        aprsis = {
            "ok": False,
            "sent": False,
            "reason": "request_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return DispatchResult(
        aprsis_bulletin=aprsis,
        voice_rf=_voice_result(event),
    ).to_dict()
