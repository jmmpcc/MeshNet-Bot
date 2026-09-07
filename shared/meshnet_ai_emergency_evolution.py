"""Explicación IA en sombra de la evolución de emergencias — Fase IA-2C.

Este módulo explica cambios YA determinados por el sistema de Emergencias. No
clasifica fases, no decide crecimiento, no confirma incidentes y no altera Event,
storage, notifier, dispatcher ni ninguna salida de radio.

En NASA FIRMS reutiliza exclusivamente señales deterministas ya calculadas por
``FirmsTrackedSource``: ``firms_phase``, ``growth_reasons``, valores previous/latest,
picos y número de pasadas. Para otras fuentes conserva la fase determinista que ya
expone IA-2A mediante ``deterministic_phase``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergencies import (
    _clean_text,
    _event_metadata,
    _event_value,
    _safe_event_payload,
    deterministic_phase,
)


_FIRMS_EVOLUTION_KEYS = (
    "firms_phase",
    "growth_reasons",
    "incident_first_detection_at",
    "incident_last_detection_at",
    "incident_passes",
    "previous_detection_count",
    "latest_detection_count",
    "incident_peak_detection_count",
    "previous_frp_total_mw",
    "latest_frp_total_mw",
    "incident_peak_frp_total_mw",
    "previous_extent_km",
    "latest_extent_km",
    "incident_peak_extent_km",
)


@dataclass(frozen=True)
class EmergencyAIEvolutionExplanation:
    """Resultado exclusivamente informativo de IA-2C.

    Campos:
        ok: indica si existe una explicación IA válida.
        phase: fase determinista recibida; nunca calculada por la IA.
        explanation: explicación factual del cambio observado.
        confidence: confianza del proveedor sobre la explicación, 0..1.
        status/error/duration_ms: estado técnico del proveedor o fallback.

    Incluso con ``ok=True`` este objeto no autoriza ninguna acción operativa.
    """

    ok: bool
    phase: str = "unknown"
    explanation: str = ""
    confidence: float = 0.0
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


def deterministic_evolution_snapshot(event: Any) -> dict[str, Any]:
    """Construye el snapshot mínimo y determinista que IA-2C puede explicar.

    Cómo se llama:
        ``deterministic_evolution_snapshot(event)`` antes de invocar al proveedor.

    Parámetros:
        event: ``Event`` normalizado o mapping equivalente.

    Funcionalidad:
        Reutiliza ``_safe_event_payload`` de IA-2A y ``deterministic_phase``. Para
        NASA FIRMS copia únicamente las claves de tracking necesarias para explicar
        la evolución. Nunca entrega la metadata completa ni modifica el evento.
    """

    change = _clean_text(_event_value(event, "change", ""))
    phase = deterministic_phase(event, change)
    payload = _safe_event_payload(event, change, phase)
    snapshot: dict[str, Any] = {
        "phase": phase,
        "event": payload,
    }

    source = _clean_text(_event_value(event, "source", "")).casefold()
    if source == "nasa_firms":
        metadata = _event_metadata(event)
        firms_tracking: dict[str, Any] = {}
        for key in _FIRMS_EVOLUTION_KEYS:
            if key in metadata:
                value = metadata[key]
                if isinstance(value, (str, int, float, bool, list)) or value is None:
                    firms_tracking[key] = value
        snapshot["firms_tracking"] = firms_tracking

    return snapshot


class EmergencyAIEvolutionExplainer:
    """Explicador IA-2C en sombra de una evolución ya determinada.

    Cómo se llama:
        ``EmergencyAIEvolutionExplainer(MeshNetAI.from_env()).explain(event)``.

    Requisitos:
        ``MESHNET_AI_ENABLED=1``
        ``MESHNET_AI_EMERGENCIES_ENABLED=1``

    No existe un flag nuevo: IA-2C sigue siendo una capacidad explícita del módulo
    de Emergencias y no está conectada automáticamente a ningún flujo operativo.
    """

    def __init__(self, ai: MeshNetAI):
        self.ai = ai

    def explain(
        self,
        event: Any,
        *,
        max_explanation_chars: int = 600,
    ) -> EmergencyAIEvolutionExplanation:
        """Explica una evolución sin poder cambiar su fase determinista.

        Parámetros:
            event: evento ya normalizado/evolucionado por la lógica determinista.
            max_explanation_chars: límite estricto de caracteres de salida.

        Retorna:
            ``EmergencyAIEvolutionExplanation``. Los fallos del proveedor o del
            contrato producen ``ok=False`` y nunca elevan decisiones operativas.
        """

        snapshot = deterministic_evolution_snapshot(event)
        phase = _clean_text(snapshot.get("phase")) or "unknown"

        if not self.ai.config.enabled:
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="disabled",
                error="IA desactivada",
            )
        if not self.ai.feature_enabled("emergencies"):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="feature_disabled",
                error="análisis IA de emergencias desactivado",
            )
        if phase == "unknown":
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="not_available",
                error="fase determinista no disponible",
            )

        try:
            explanation_limit = int(max_explanation_chars)
        except (TypeError, ValueError, OverflowError):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="límite de explicación inválido",
            )
        if explanation_limit <= 0:
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="el límite de explicación debe ser positivo",
            )

        prompt = json.dumps(
            {
                "task": "shadow_emergency_incident_evolution_explanation",
                "deterministic_evolution": snapshot,
                "constraints": {
                    "explanation_max_chars": explanation_limit,
                    "informational_only": True,
                    "phase_is_authoritative": True,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        system = (
            "Eres un observador auxiliar de evolución de emergencias. La fase incluida "
            "en deterministic_evolution ya ha sido decidida por lógica determinista y "
            "es autoritativa: NO la cambies, recalcules ni contradigas. Explica únicamente "
            "qué datos observados justifican o describen esa evolución. No cambies categoría, "
            "severidad, verificación o estado. No inventes causas, daños, superficie afectada "
            "ni consecuencias. Para NASA FIRMS, cluster/extent describe extensión observada "
            "de detecciones satelitales y NO superficie quemada o afectada. growth_reasons "
            "son señales deterministas ya calculadas. stable significa sin crecimiento "
            "significativo detectado en esa pasada, NO incendio extinguido. resolved solo "
            "puede afirmarse cuando la fase determinista recibida sea resolved. Devuelve "
            "exclusivamente JSON con explanation y confidence. explanation debe ser factual, "
            "sin órdenes operativas. confidence debe ser un número JSON finito entre 0 y 1."
        )

        result = self.ai.generate_text(prompt, system=system)
        if not result.ok:
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status=result.status,
                error=result.error or "explicación IA no disponible",
                duration_ms=result.duration_ms,
            )

        try:
            parsed = json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="respuesta de evolución no es JSON válido",
                duration_ms=result.duration_ms,
            )
        if not isinstance(parsed, dict):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="respuesta de evolución no es un objeto JSON",
                duration_ms=result.duration_ms,
            )

        explanation = parsed.get("explanation")
        if not isinstance(explanation, str):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="explanation inválida en respuesta de evolución",
                duration_ms=result.duration_ms,
            )
        explanation = _clean_text(explanation)
        if not explanation:
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="respuesta de evolución sin explicación",
                duration_ms=result.duration_ms,
            )
        if len(explanation) > explanation_limit:
            explanation = explanation[:explanation_limit].rstrip(" ,;:-")

        confidence_raw = parsed.get("confidence")
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="confidence inválida en respuesta de evolución",
                duration_ms=result.duration_ms,
            )
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError, OverflowError):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="confidence inválida en respuesta de evolución",
                duration_ms=result.duration_ms,
            )
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="confidence fuera de rango en respuesta de evolución",
                duration_ms=result.duration_ms,
            )

        return EmergencyAIEvolutionExplanation(
            True,
            phase=phase,
            explanation=explanation,
            confidence=confidence,
            status=result.status,
            duration_ms=result.duration_ms,
        )
