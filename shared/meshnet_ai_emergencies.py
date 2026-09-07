"""Observador IA en sombra para Emergencias — Fase IA-2A.

Este módulo añade análisis semántico opcional sobre eventos YA normalizados por el
sistema de Emergencias. No modifica categorías, severidad, verificación, estado,
tracking, deduplicación, rutas, formateadores ni salidas Mesh/APRS/voz.

Uso::

    from shared.meshnet_ai import MeshNetAI
    from shared.meshnet_ai_emergencies import EmergencyAIObserver

    observer = EmergencyAIObserver(MeshNetAI.from_env())
    result = observer.analyze_event(event, change="updated")

    if result.ok:
        # Resultado auxiliar para auditoría/presentación futura.
        print(result.summary)

Reglas de seguridad:
- requiere MESHNET_AI_ENABLED=1 y MESHNET_AI_EMERGENCIES_ENABLED=1;
- con cualquier flag desactivado no se llama al proveedor;
- no persiste datos ni transmite nada;
- la fase operativa se deriva SOLO de datos deterministas del evento;
- una respuesta IA inválida produce ``ok=False`` y no altera el evento.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from shared.meshnet_ai import MeshNetAI


_ALLOWED_CHANGES = {"", "new", "updated", "resolved"}
_TERMINAL_STATUSES = {"resolved", "cancelled", "expired", "closed"}


@dataclass(frozen=True)
class EmergencyAIAnalysis:
    """Resultado seguro y no operativo del observador IA-2A.

    Campos:
        ok: ``True`` únicamente si la respuesta IA ha superado validación.
        summary: resumen auxiliar del evento, limitado por el llamador.
        notes: observaciones auxiliares; nunca instrucciones operativas.
        confidence: confianza declarada por el modelo normalizada a ``0..1``.
        phase: fase DETERMINISTA derivada del evento, no decidida por la IA.
        status: estado de la infraestructura IA (disabled/feature_disabled/etc.).
        error: explicación segura cuando ``ok=False``.
        duration_ms: tiempo reportado por la infraestructura IA.
    """

    ok: bool
    summary: str = ""
    notes: str = ""
    confidence: float = 0.0
    phase: str = "unknown"
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


def _clean_text(value: Any) -> str:
    """Normaliza espacios para validación y salida sin cambiar significado."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fit_text(value: Any, max_chars: int) -> str:
    """Ajusta texto a un presupuesto positivo exacto de caracteres.

    Cómo se llama:
        ``_fit_text(valor, limite)`` tras validar una respuesta IA.

    Parámetros:
        value: texto de entrada ya validado como ``str``.
        max_chars: límite máximo exacto; 0 o negativo devuelve cadena vacía.

    Funcionalidad:
        Normaliza espacios. Si hace falta recortar, intenta conservar primero una
        frase completa suficientemente representativa; si no existe, recorta por
        palabra. Los puntos decimales (por ejemplo ``1.2`` o ``18.4``) NO se
        consideran finales de frase. Nunca devuelve más caracteres que el límite
        solicitado y evita, cuando es posible, dejar una cláusula final claramente
        incompleta.
    """
    limit = int(max_chars)
    if limit <= 0:
        return ""
    clean = _clean_text(value)
    if len(clean) <= limit:
        return clean

    candidate = clean[:limit].rstrip()

    # Solo consideramos cierre de frase la puntuación seguida de espacio/fin y,
    # para el punto, que no esté precedido por un dígito. Esto impide interpretar
    # el punto de ``1.2`` como final de oración y producir salidas como "... 1.".
    sentence_ends = [
        match.end()
        for match in re.finditer(r"(?<!\d)[.!?](?=\s|$)", candidate)
    ]
    if sentence_ends:
        sentence_end = sentence_ends[-1]
        if sentence_end >= int(limit * 0.55):
            return candidate[:sentence_end].rstrip()

    if " " in candidate:
        shortened = candidate.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if shortened:
            candidate = shortened
    return candidate.rstrip(" ,;:-")


def _event_value(event: Any, name: str, default: Any = "") -> Any:
    """Lee un campo desde objeto Event o mapping sin imponer dependencias.

    Esto permite probar IA-2A aisladamente y evita acoplar ``shared`` al paquete
    ejecutable de Emergencias.
    """
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _event_metadata(event: Any) -> dict[str, Any]:
    """Obtiene una copia superficial de metadata sin modificar el evento original."""
    value = _event_value(event, "metadata", {})
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_float(value: Any) -> float | None:
    """Normaliza una coordenada numérica para el payload mínimo del observador.

    Cómo se llama:
        ``_optional_float(_event_value(event, "latitude", None))``.

    Parámetros:
        value: valor original de latitud o longitud del evento normalizado.

    Funcionalidad:
        Devuelve ``float`` cuando el valor es numérico y finito para JSON. Si el
        campo está ausente o no es convertible devuelve ``None``. Esta función no
        modifica el evento y evita que tipos auxiliares no serializables rompan la
        llamada opcional a IA.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def deterministic_phase(event: Any, change: str = "") -> str:
    """Deriva la fase observada exclusivamente mediante datos deterministas.

    Cómo se llama:
        ``deterministic_phase(event, change)`` antes de cualquier petición IA.

    Parámetros:
        event: ``Event`` de Emergencias o mapping equivalente.
        change: cambio detectado por el motor: new/updated/resolved.

    Funcionalidad:
        1. Un estado terminal o ``change=resolved`` siempre produce ``resolved``.
        2. SOLO para la fuente ``nasa_firms`` reutiliza ``metadata['firms_phase']``
           existente cuando contiene initial, growth o stable.
        3. Para el resto de fuentes, ``new`` y ``updated`` se conservan literalmente.
        4. Si no existe información suficiente devuelve ``unknown``.

    La IA nunca participa en esta decisión. El aislamiento por ``source`` evita
    que una clave metadata ajena pueda adoptar accidentalmente una fase FIRMS.
    """
    normalized_change = _clean_text(change).casefold()
    if normalized_change not in _ALLOWED_CHANGES:
        normalized_change = ""

    status = _clean_text(_event_value(event, "status", "")).casefold()
    if normalized_change == "resolved" or status in _TERMINAL_STATUSES:
        return "resolved"

    source = _clean_text(_event_value(event, "source", "")).casefold()
    if source == "nasa_firms":
        metadata = _event_metadata(event)
        firms_phase = _clean_text(metadata.get("firms_phase")).casefold()
        if firms_phase in {"initial", "growth", "stable"}:
            return firms_phase

    if normalized_change in {"new", "updated"}:
        return normalized_change
    return "unknown"


def _safe_event_payload(event: Any, change: str, phase: str) -> dict[str, Any]:
    """Construye la representación mínima que IA-2A puede enviar al proveedor.

    Solo incluye datos normalizados necesarios para resumir el evento. Se añaden
    latitud y longitud porque forman parte del propio ``Event`` y son información
    operativamente relevante para describir una emergencia sin inventar ubicación.
    No se incorpora el diccionario ``metadata`` completo, evitando enviar campos
    auxiliares no requeridos. La función no modifica el objeto recibido.
    """
    return {
        "event_id": _clean_text(_event_value(event, "event_id", "")),
        "source": _clean_text(_event_value(event, "source", "")),
        "category": _clean_text(_event_value(event, "category", "")),
        "severity": _clean_text(_event_value(event, "severity", "")),
        "verification": _clean_text(_event_value(event, "verification", "")),
        "status": _clean_text(_event_value(event, "status", "")),
        "change": _clean_text(change).casefold(),
        "phase": phase,
        "title": _clean_text(_event_value(event, "title", "")),
        "description": _clean_text(_event_value(event, "description", "")),
        "road": _clean_text(_event_value(event, "road", "")),
        "municipality": _clean_text(_event_value(event, "municipality", "")),
        "province": _clean_text(_event_value(event, "province", "")),
        "latitude": _optional_float(_event_value(event, "latitude", None)),
        "longitude": _optional_float(_event_value(event, "longitude", None)),
        "started_at": _clean_text(_event_value(event, "started_at", "")),
        "updated_at": _clean_text(_event_value(event, "updated_at", "")),
    }


class EmergencyAIObserver:
    """Fachada IA-2A para análisis en sombra de un evento normalizado.

    Cómo se llama:
        ``EmergencyAIObserver(MeshNetAI.from_env()).analyze_event(event)``.

    Parámetros del constructor:
        ai: infraestructura MeshNetAI de IA-0. Gestiona proveedor, timeout,
            circuit breaker y fallback.

    Esta clase no conoce notifier, broker, APRS ni radio y, por diseño, no puede
    enviar ni modificar decisiones operativas.
    """

    def __init__(self, ai: MeshNetAI):
        self.ai = ai

    def analyze_event(
        self,
        event: Any,
        *,
        change: str = "",
        max_summary_chars: int = 240,
        max_notes_chars: int = 400,
    ) -> EmergencyAIAnalysis:
        """Analiza un evento en modo sombra y valida estrictamente la respuesta.

        Parámetros:
            event: evento normalizado de Emergencias o mapping equivalente.
            change: cambio determinista conocido (new/updated/resolved).
            max_summary_chars: límite exacto del resumen; debe ser positivo.
            max_notes_chars: límite exacto de notas; debe ser positivo.

        Retorna:
            ``EmergencyAIAnalysis``. ``ok=False`` implica ignorar completamente
            el análisis y conservar el comportamiento clásico.

        Funcionalidad:
            - comprueba flags antes de cualquier llamada externa;
            - deriva la fase determinísticamente;
            - envía solo un subconjunto mínimo del evento;
            - exige JSON objeto con ``summary`` y ``notes`` de tipo ``str``;
            - exige ``confidence`` convertible a número finito y la normaliza a 0..1;
            - nunca modifica el evento recibido.
        """
        phase = deterministic_phase(event, change)

        if not self.ai.config.enabled:
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="disabled",
                error="IA desactivada",
            )
        if not self.ai.feature_enabled("emergencies"):
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="feature_disabled",
                error="análisis IA de emergencias desactivado",
            )

        try:
            summary_limit = int(max_summary_chars)
            notes_limit = int(max_notes_chars)
        except (TypeError, ValueError):
            return EmergencyAIAnalysis(
                ok=False, phase=phase, status="error", error="límites inválidos"
            )
        if summary_limit <= 0 or notes_limit <= 0:
            return EmergencyAIAnalysis(
                ok=False, phase=phase, status="error", error="los límites deben ser positivos"
            )

        payload = _safe_event_payload(event, change, phase)
        if not payload["event_id"] and not payload["title"] and not payload["description"]:
            return EmergencyAIAnalysis(
                ok=False, phase=phase, status="error", error="evento vacío"
            )

        system = (
            "Eres un observador auxiliar de emergencias. No tomas decisiones operativas. "
            "No cambies categoría, severidad, verificación, estado ni fase. No inventes "
            "hechos ni conviertas una detección o medida observada en una consecuencia no "
            "confirmada. En particular, para NASA FIRMS una 'extensión observada' o una "
            "extensión de cluster NO equivale a superficie o área afectada: conserva ese "
            "significado literal y no uses 'afecta', 'afectando' o equivalentes salvo que "
            "el dato de afectación esté explícitamente presente. Devuelve exclusivamente "
            "un objeto JSON con las claves summary, notes y confidence. summary debe "
            "describir solo los datos recibidos, respetar el límite indicado y terminar "
            "como una frase completa, sin dejar una cláusula inacabada. notes puede indicar "
            "incertidumbres o datos faltantes, nunca órdenes de actuación. confidence debe "
            "estar entre 0 y 1."
        )
        prompt = json.dumps(
            {
                "task": "shadow_emergency_observation",
                "event": payload,
                "constraints": {
                    "summary_max_chars": summary_limit,
                    "notes_max_chars": notes_limit,
                    "phase_is_authoritative": phase,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        result = self.ai.generate_text(prompt, system=system)
        if not result.ok:
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status=result.status,
                error=result.error or "análisis IA no disponible",
                duration_ms=result.duration_ms,
            )

        try:
            parsed = json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="error",
                error="respuesta IA de emergencias no es JSON válido",
                duration_ms=result.duration_ms,
            )
        if not isinstance(parsed, dict):
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="error",
                error="respuesta IA de emergencias no es un objeto JSON",
                duration_ms=result.duration_ms,
            )

        summary_raw = parsed.get("summary")
        notes_raw = parsed.get("notes")
        if not isinstance(summary_raw, str) or not isinstance(notes_raw, str):
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="error",
                error="summary/notes inválidos en respuesta IA de emergencias",
                duration_ms=result.duration_ms,
            )

        summary = _fit_text(summary_raw, summary_limit)
        notes = _fit_text(notes_raw, notes_limit)
        if not summary:
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="error",
                error="respuesta IA de emergencias sin resumen",
                duration_ms=result.duration_ms,
            )

        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError, OverflowError):
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="error",
                error="confidence inválida en respuesta IA de emergencias",
                duration_ms=result.duration_ms,
            )
        if not math.isfinite(confidence):
            return EmergencyAIAnalysis(
                ok=False,
                phase=phase,
                status="error",
                error="confidence no finita en respuesta IA de emergencias",
                duration_ms=result.duration_ms,
            )
        confidence = max(0.0, min(1.0, confidence))

        return EmergencyAIAnalysis(
            ok=True,
            summary=summary,
            notes=notes,
            confidence=confidence,
            phase=phase,
            status=result.status,
            duration_ms=result.duration_ms,
        )
