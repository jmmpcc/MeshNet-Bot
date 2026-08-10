from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any

from .formatters import aprs_emergency_text
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

    aprs_rf: dict[str, Any]
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



def _aprs_rf_enabled() -> bool:
    """Comprueba la autorización explícita de APRS RF para Emergencias.

    Se mantiene separada de APRS-IS. Instalar esta fase no provoca emisiones
    RF mientras EMERGENCIAS_APRS_RF_ENABLED permanezca a 0.
    """
    return (
        _enabled("APPS_APRS_ENABLED")
        and "emergencias" in _allowed_sources()
        and _enabled("EMERGENCIAS_APRS_ENABLED")
        and _enabled("EMERGENCIAS_APRS_RF_ENABLED")
    )


def _send_aprs_rf(event: Event, message: str) -> dict[str, Any]:
    """Solicita APRS RF reutilizando el troceado real del gateway.

    Flujo de llamada:
      ``dispatch_secondary_outputs(event, message)`` llama a esta función después
      de una entrega Mesh correcta. Primero solicita ``aprs_preview`` al gateway,
      que calcula las partes con las mismas funciones usadas para RF y sin
      transmitir ni afectar a la deduplicación.

    Antes de consultar el gateway se genera un resumen APRS específico de hasta
    67 caracteres mediante ``aprs_emergency_text``. El resumen conserva primero
    estado y tipo de emergencia y después ubicación. Si aun así el gateway
    informa más partes que ``EMERGENCIAS_APRS_RF_MAX_CHUNKS``, se aplica un
    segundo límite y se vuelve a previsualizar antes de transmitir.

    Parámetros:
      event: emergencia normalizada que determina severidad y datos compactos.
      message: texto ya construido por el notifier para el flujo Mesh.

    La función no abre KISS ni controla PTT/radio: el envío efectivo sigue
    perteneciendo exclusivamente a ``meshtastic_to_aprs.py``.
    """
    if not _aprs_rf_enabled():
        return {"ok": True, "sent": False, "reason": "disabled"}

    minimum = str(os.getenv("EMERGENCIAS_APRS_RF_MIN_LEVEL", "high") or "high").strip().lower()
    if SEVERITY_RANK.get(event.severity, 0) < SEVERITY_RANK.get(minimum, SEVERITY_RANK["high"]):
        return {"ok": True, "sent": False, "reason": "severity_below_threshold"}

    host = str(os.getenv("APRS_CTRL_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int(os.getenv("APRS_CTRL_PORT", "9464") or "9464")
    timeout = max(0.5, float(os.getenv("APRS_CTRL_ACK_TIMEOUT", "8") or "8"))
    max_chunks = max(1, int(os.getenv("EMERGENCIAS_APRS_RF_MAX_CHUNKS", "3") or "3"))
    compact_max_bytes = max(60, int(os.getenv("EMERGENCIAS_APRS_RF_COMPACT_MAX_BYTES", "140") or "140"))

    destination = str(os.getenv("APRS_EMERG_DEST", "broadcast") or "broadcast").strip()
    path = str(os.getenv("APRS_BOT_PATH", os.getenv("APRS_PATH", "")) or "").strip()
    if path.lower() in {"none", "off", "direct", "-"}:
        path = ""

    def gateway_request(mode: str, text: str) -> dict[str, Any]:
        """Envía una petición UDP al gateway y valida que responda con JSON objeto."""
        payload = {
            "mode": mode,
            "origin": "app_emergencias",
            "dest": destination,
            "text": text,
        }
        if mode == "aprs":
            payload["path"] = path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(timeout)
            client.sendto(data, (host, port))
            response, _ = client.recvfrom(65536)
        result = json.loads(response.decode("utf-8", errors="replace"))
        if not isinstance(result, dict):
            return {"ok": False, "sent": False, "reason": "invalid_response"}
        return result

    # APRS v7.0.42: generamos desde el Event una línea específica para APRS.
    # De este modo el estado y el tipo de emergencia quedan al principio y no
    # dependen de cómo se haya formateado el mensaje Mesh. 67 caracteres es el
    # límite estándar del cuerpo APRS; puede reducirse, pero nunca ampliarse por
    # encima del valor configurado en APRS_MSG_MAX del gateway.
    aprs_text_max = max(24, min(67, int(os.getenv("EMERGENCIAS_APRS_TEXT_MAX_CHARS", "67") or "67")))
    aprs_message = aprs_emergency_text(event, max_chars=aprs_text_max)

    original_preview = gateway_request("aprs_preview", aprs_message)
    if not original_preview.get("ok"):
        return {
            "ok": False,
            "sent": False,
            "reason": "preview_failed",
            "preview": original_preview,
        }

    original_parts = max(0, int(original_preview.get("parts", 0) or 0))
    rf_message = aprs_message
    # ``compacted`` conserva su semántica histórica: solo indica que se activó
    # el segundo nivel de reducción por exceso de partes. El nuevo formateo APRS
    # específico se informa aparte mediante ``aprs_formatted``.
    compacted = False
    compact_parts = original_parts

    if original_parts > max_chunks:
        # El notifier ya genera mensajes compactos. Este segundo nivel solo se
        # activa cuando una configuración más restrictiva o datos excepcionales
        # superan el máximo RF. El segundo resumen mantiene la misma regla:
        # estado y tipo de emergencia nunca se desplazan al final del mensaje.
        rf_message = aprs_emergency_text(
            event,
            max_chars=max(24, min(67, compact_max_bytes)),
        )
        compacted = rf_message != message
        compact_preview = gateway_request("aprs_preview", rf_message)
        if not compact_preview.get("ok"):
            return {
                "ok": False,
                "sent": False,
                "reason": "compact_preview_failed",
                "preview": compact_preview,
                "original_parts": original_parts,
                "max_chunks": max_chunks,
            }
        compact_parts = max(0, int(compact_preview.get("parts", 0) or 0))
        if compact_parts > max_chunks:
            return {
                "ok": False,
                "sent": False,
                "reason": "chunk_limit_exceeded",
                "original_parts": original_parts,
                "compact_parts": compact_parts,
                "max_chunks": max_chunks,
            }

    result = gateway_request("aprs", rf_message)
    if result.get("duplicate"):
        result = {**result, "ok": True, "sent": False, "reason": "duplicate"}

    # Metadatos diagnósticos añadidos sin alterar las claves históricas que ya
    # consumen notifier/tests. Permiten saber si hubo compactación automática.
    return {
        **result,
        "original_parts": original_parts,
        "rf_parts": compact_parts,
        "max_chunks": max_chunks,
        "compacted": compacted,
        "aprs_formatted": True,
        "aprs_text": rf_message,
    }

def _voice_result(event: Event, message: str) -> dict[str, Any]:
    """Solicita síntesis al servicio Voice RF cuando está autorizada.

    Esta función no controla audio, PTT ni radio. Envía un evento al servicio
    local desplegado en v7.0.34, que únicamente genera/valida un WAV y devuelve
    siempre `sent=false`. Las tres autorizaciones son independientes:

    - EMERGENCIAS_VOICE_RF_ENABLED
    - EMERGENCIAS_VOICE_RF_AUTOMATIC
    - VOICE_RF_SERVICE_ENABLED
    """
    if not _enabled("EMERGENCIAS_VOICE_RF_ENABLED"):
        return {"ok": True, "generated": False, "sent": False, "reason": "disabled"}
    if not _enabled("EMERGENCIAS_VOICE_RF_AUTOMATIC"):
        return {
            "ok": True,
            "generated": False,
            "sent": False,
            "reason": "automatic_disabled",
        }
    minimum = str(os.getenv("EMERGENCIAS_VOICE_RF_MIN_LEVEL", "high") or "high").strip().lower()
    if SEVERITY_RANK.get(event.severity, 0) < SEVERITY_RANK.get(minimum, SEVERITY_RANK["high"]):
        return {
            "ok": True,
            "generated": False,
            "sent": False,
            "reason": "severity_below_threshold",
        }
    if not _enabled("VOICE_RF_SERVICE_ENABLED"):
        return {
            "ok": True,
            "generated": False,
            "sent": False,
            "reason": "service_disabled",
        }

    host = str(os.getenv("VOICE_RF_SERVICE_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int(os.getenv("VOICE_RF_SERVICE_PORT", "8790") or "8790")
    timeout = max(0.5, float(os.getenv("VOICE_RF_SERVICE_TIMEOUT_SEC", "15") or "15"))
    payload = {
        "source": "emergencias",
        "event_id": event.event_id,
        "severity": event.severity,
        "status": event.status,
        "category": event.category,
        "province": event.province,
        "municipality": event.municipality,
        "text": message,
        "is_test": bool(event.metadata.get("is_test", False)),
    }
    request = urllib.request.Request(
        f"http://{host}:{port}/dispatch",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(65536)
    except urllib.error.HTTPError as exc:
        data = exc.read(65536)
    result = json.loads(data.decode("utf-8", errors="replace"))
    return result if isinstance(result, dict) else {
        "ok": False,
        "generated": False,
        "sent": False,
        "reason": "invalid_response",
    }


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
    aprs_text_max = max(24, min(67, int(os.getenv("EMERGENCIAS_APRS_TEXT_MAX_CHARS", "67") or "67")))
    bulletin_text = aprs_emergency_text(event, max_chars=aprs_text_max)
    payload = {
        "mode": "aprsis_emergency_bulletin",
        "origin": "app_emergencias",
        "event_id": event.event_id,
        "severity": event.severity,
        "status": event.status,
        "text": bulletin_text,
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
        aprs_rf = _send_aprs_rf(event, message)
    except Exception as exc:  # noqa: BLE001 - aislamiento deliberado de salida secundaria
        aprs_rf = {
            "ok": False,
            "sent": False,
            "reason": "request_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
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
        aprs_rf=aprs_rf,
        aprsis_bulletin=aprsis,
        voice_rf=_voice_result(event, message),
    ).to_dict()
