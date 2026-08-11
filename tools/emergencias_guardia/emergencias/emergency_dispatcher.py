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
from shared.delivery_audit import audit_delivery, new_operation_id, result_from_response


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


def _secondary_category_allowed(env_name: str, event: Event) -> bool:
    """Comprueba la autorización opcional de categoría para una salida secundaria.

    Cómo se llama:
        ``_secondary_category_allowed("EMERGENCIAS_APRS_RF_CATEGORIES", event)``
        ``_secondary_category_allowed("EMERGENCIAS_APRSIS_CATEGORIES", event)``

    Parámetros:
        env_name:
            Nombre de la variable de entorno que contiene categorías separadas
            por comas.
        event:
            Evento normalizado cuya ``category`` se va a comprobar.

    Compatibilidad:
        Si la variable NO existe en el entorno se devuelve ``True`` y se
        conserva exactamente el comportamiento anterior a v7.0.50. Esto es
        deliberado para que desplegar la nueva versión no cambie ninguna salida.

        Si la variable existe pero está vacía, no se autoriza ninguna categoría
        para ese transporte. De este modo el ControlPanel puede representar una
        columna APRS completamente desmarcada sin recurrir a valores especiales.

    Esta función únicamente añade una autorización por categoría. Nunca sustituye
    ni rebaja los interruptores generales, los ``MIN_LEVEL``, la deduplicación o
    cualquier comprobación posterior del gateway APRS.
    """
    raw = os.environ.get(env_name)
    if raw is None:
        return True
    allowed = {
        item.strip().lower()
        for item in str(raw).split(",")
        if item.strip()
    }
    return str(event.category or "").strip().lower() in allowed


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

    Para fuentes normales se conserva el comportamiento histórico: el resumen
    APRS se limita a 67 caracteres. Para ``nasa_firms`` se permite un resumen
    RF mayor, configurable mediante ``EMERGENCIAS_APRS_RF_FIRMS_TEXT_MAX_CHARS``
    (160 por defecto), de modo que el gateway pueda repartir coordenadas y
    telemetría FIRMS en varias tramas de estado. El número máximo de tramas sigue
    limitado por ``EMERGENCIAS_APRS_RF_MAX_CHUNKS``.

    APRS-IS no usa esta ampliación: sus boletines siguen generándose de forma
    independiente en ``_send_aprsis_bulletin`` con un máximo de 67 caracteres.

    Parámetros:
      event: emergencia normalizada que determina severidad y datos compactos.
      message: texto ya construido por el notifier para el flujo Mesh.

    La función no abre KISS ni controla PTT/radio: el envío efectivo sigue
    perteneciendo exclusivamente a ``meshtastic_to_aprs.py``.
    """
    if not _aprs_rf_enabled():
        return {"ok": True, "sent": False, "reason": "disabled"}

    if not _secondary_category_allowed("EMERGENCIAS_APRS_RF_CATEGORIES", event):
        return {"ok": True, "sent": False, "reason": "category_not_allowed"}

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

    is_firms = event.source == "nasa_firms" and event.category == "wildfire"
    if is_firms:
        aprs_text_max = max(
            67,
            min(
                240,
                int(os.getenv("EMERGENCIAS_APRS_RF_FIRMS_TEXT_MAX_CHARS", "160") or "160"),
            ),
        )
    else:
        aprs_text_max = max(
            24,
            min(67, int(os.getenv("EMERGENCIAS_APRS_TEXT_MAX_CHARS", "67") or "67")),
        )

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
    compacted = False
    compact_parts = original_parts

    if original_parts > max_chunks:
        # FIRMS puede usar varias tramas, pero nunca sobrepasa el máximo global.
        # En ese caso se vuelve a generar el texto con el presupuesto compacto.
        compact_limit = max(24, min(aprs_text_max, compact_max_bytes))
        if not is_firms:
            compact_limit = min(67, compact_limit)
        rf_message = aprs_emergency_text(event, max_chars=compact_limit)
        compacted = rf_message != aprs_message
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

    return {
        **result,
        "original_parts": original_parts,
        "rf_parts": compact_parts,
        "max_chunks": max_chunks,
        "compacted": compacted,
        "aprs_formatted": True,
        "aprs_text": rf_message,
        "firms_multipart": bool(is_firms and compact_parts > 1),
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

    APRS-IS conserva deliberadamente el límite de 67 caracteres del cuerpo de
    mensaje APRS clásico. Para FIRMS, ``aprs_emergency_text`` sitúa coordenadas
    inmediatamente después de ``INCENDIO SAT`` para que nunca se pierdan por el
    límite, en vez de depender de boletines largos no interoperables.
    """
    if not _aprsis_bulletin_enabled():
        return {"ok": True, "sent": False, "reason": "disabled"}

    if not _secondary_category_allowed("EMERGENCIAS_APRSIS_CATEGORIES", event):
        return {"ok": True, "sent": False, "reason": "category_not_allowed"}

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


def dispatch_secondary_outputs(
    event: Event,
    message: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Distribuye una emergencia por las salidas secundarias configuradas.

    Debe llamarse únicamente después de una entrega Mesh correcta. Cada salida
    se evalúa de forma independiente. Una excepción APRS-IS se encapsula en el
    resultado y nunca se propaga hacia el flujo principal.

    ``operation_id`` es opcional y solo enlaza observabilidad. Si se omite se
    crea uno nuevo; no interviene en deduplicación, MIN_INTERVAL ni transporte.
    """
    op_id = operation_id or new_operation_id("emergencias")
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

    voice_rf = _voice_result(event, message)
    result = DispatchResult(
        aprs_rf=aprs_rf,
        aprsis_bulletin=aprsis,
        voice_rf=voice_rf,
    ).to_dict()

    audit_items = (
        ("aprs_rf", aprs_rf, str(aprs_rf.get("dest") or os.getenv("APRS_EMERG_DEST", "broadcast"))),
        ("aprsis", aprsis, str(aprsis.get("bulletin") or "boletin")),
        ("voice_rf", voice_rf, "servicio-voz"),
    )
    for transport, response, destination in audit_items:
        detail = str(response.get("reason") or response.get("error") or "")
        parts = response.get("rf_parts", response.get("parts", response.get("chunks")))
        audit_delivery(
            app="emergencias",
            source=event.source,
            event_id=event.event_id,
            operation_id=op_id,
            category=event.category,
            severity=event.severity,
            status=event.status,
            transport=transport,
            destination=destination,
            message=str(response.get("aprs_text") or response.get("text") or message),
            result=result_from_response(response),
            result_detail=detail,
            parts=parts if isinstance(parts, int) else None,
            metadata={"response": response, "secondary": True},
        )
    return result
