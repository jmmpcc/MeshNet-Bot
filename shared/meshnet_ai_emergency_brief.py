"""Brief situacional IA en sombra para Emergencias — Fase IA-2D.

IA-2D compone una síntesis informativa a partir del evento determinista y de
resultados IA-2A/IA-2B/IA-2C YA validados. No decide severidad, prioridad,
verificación, fase, routing ni acciones; tampoco persiste ni transmite nada.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergencies import (
    _clean_text,
    _event_value,
    _fit_text,
    _safe_event_payload,
    deterministic_phase,
)
from shared.meshnet_ai_emergency_evolution import _firms_explanation_is_safe


_ALLOWED_RELATIONS = {"same_incident", "contextual", "unrelated", "uncertain"}


@dataclass(frozen=True)
class EmergencyAISituationalBrief:
    """Resultado estrictamente informativo de IA-2D.

    Campos:
        ok: True solo cuando el proveedor devuelve un contrato válido y seguro.
        phase: fase determinista del evento principal; nunca generada por IA-2D.
        brief: síntesis factual de la situación observada.
        uncertainties: incertidumbres/datos faltantes, sin recomendaciones.
        confidence: confianza declarada por el proveedor en rango 0..1.
        components: componentes sombra válidos incorporados al snapshot.
        status/error/duration_ms: estado técnico de la infraestructura IA.

    Incluso con ``ok=True`` no autoriza cambios ni acciones operativas.
    """

    ok: bool
    phase: str = "unknown"
    brief: str = ""
    uncertainties: str = ""
    confidence: float = 0.0
    components: tuple[str, ...] = ()
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


def _result_value(result: Any, name: str, default: Any = None) -> Any:
    """Lee un campo de un resultado dataclass/objeto o mapping sin modificarlo."""

    if result is None:
        return default
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _safe_confidence(value: Any) -> float | None:
    """Normaliza una confianza auxiliar únicamente si es número JSON finito 0..1."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def deterministic_brief_snapshot(
    event: Any,
    *,
    analysis: Any = None,
    evolution: Any = None,
    correlations: Iterable[Any] = (),
) -> dict[str, Any]:
    """Construye el snapshot mínimo que IA-2D puede sintetizar.

    Cómo se llama:
        ``deterministic_brief_snapshot(event, analysis=..., evolution=...,
        correlations=...)`` antes de invocar al proveedor.

    Parámetros:
        event: evento normalizado principal.
        analysis: resultado IA-2A opcional ya validado.
        evolution: resultado IA-2C opcional ya validado.
        correlations: resultados IA-2B opcionales ya validados.

    Funcionalidad:
        Conserva el evento mínimo saneado de IA-2A y añade únicamente componentes
        sombra cuyo ``ok`` sea True. No incluye metadata completa ni objetos
        originales. La fase del evento principal procede exclusivamente de
        ``deterministic_phase``. Las correlaciones se limitan a ocho para acotar
        coste/payload y nunca implican fusión operativa.
    """

    change = _clean_text(_event_value(event, "change", ""))
    phase = deterministic_phase(event, change)
    snapshot: dict[str, Any] = {
        "phase": phase,
        "event": _safe_event_payload(event, change, phase),
        "shadow_components": {},
    }
    components = snapshot["shadow_components"]

    if bool(_result_value(analysis, "ok", False)):
        summary = _clean_text(_result_value(analysis, "summary", ""))
        notes = _clean_text(_result_value(analysis, "notes", ""))
        confidence = _safe_confidence(_result_value(analysis, "confidence", None))
        analysis_phase = _clean_text(_result_value(analysis, "phase", ""))
        if summary and (not analysis_phase or analysis_phase == phase):
            components["ia2a_analysis"] = {
                "summary": _fit_text(summary, 700),
                "notes": _fit_text(notes, 500),
                "confidence": confidence,
            }

    if bool(_result_value(evolution, "ok", False)):
        explanation = _clean_text(_result_value(evolution, "explanation", ""))
        evolution_phase = _clean_text(_result_value(evolution, "phase", ""))
        confidence = _safe_confidence(_result_value(evolution, "confidence", None))
        if explanation and evolution_phase == phase:
            components["ia2c_evolution"] = {
                "explanation": _fit_text(explanation, 700),
                "confidence": confidence,
            }

    valid_correlations: list[dict[str, Any]] = []
    for result in correlations:
        if len(valid_correlations) >= 8:
            break
        if not bool(_result_value(result, "ok", False)):
            continue
        if not bool(_result_value(result, "candidate", False)):
            continue
        relation = _clean_text(_result_value(result, "relation", "")).casefold()
        explanation = _clean_text(_result_value(result, "explanation", ""))
        confidence = _safe_confidence(_result_value(result, "confidence", None))
        if relation not in _ALLOWED_RELATIONS or not explanation:
            continue
        valid_correlations.append({
            "relation": relation,
            "explanation": _fit_text(explanation, 600),
            "confidence": confidence,
            "distance_km": _result_value(result, "distance_km", None),
            "time_delta_minutes": _result_value(result, "time_delta_minutes", None),
        })
    if valid_correlations:
        components["ia2b_correlations"] = valid_correlations

    return snapshot


class EmergencyAISituationalBriefBuilder:
    """Genera el brief IA-2D sin conectarse a ningún flujo operativo.

    Cómo se llama:
        ``EmergencyAISituationalBriefBuilder(MeshNetAI.from_env()).build(...)``.

    Requisitos:
        ``MESHNET_AI_ENABLED=1`` y ``MESHNET_AI_EMERGENCIES_ENABLED=1``.

    IA-2D no dispone de operaciones de escritura, notificación ni radio.
    """

    def __init__(self, ai: MeshNetAI):
        self.ai = ai

    def build(
        self,
        event: Any,
        *,
        analysis: Any = None,
        evolution: Any = None,
        correlations: Iterable[Any] = (),
        max_brief_chars: int = 800,
        max_uncertainties_chars: int = 500,
    ) -> EmergencyAISituationalBrief:
        """Sintetiza componentes sombra ya validados sin crear decisiones nuevas."""

        snapshot = deterministic_brief_snapshot(
            event,
            analysis=analysis,
            evolution=evolution,
            correlations=correlations,
        )
        phase = _clean_text(snapshot.get("phase")) or "unknown"
        component_names = tuple(snapshot["shadow_components"].keys())

        if not self.ai.config.enabled:
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="disabled", error="IA desactivada")
        if not self.ai.feature_enabled("emergencies"):
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="feature_disabled", error="análisis IA de emergencias desactivado")
        if phase == "unknown":
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="not_available", error="fase determinista no disponible")
        if not component_names:
            return EmergencyAISituationalBrief(False, phase=phase, components=(), status="not_available", error="no hay componentes sombra válidos para sintetizar")

        try:
            brief_limit = int(max_brief_chars)
            uncertainties_limit = int(max_uncertainties_chars)
        except (TypeError, ValueError, OverflowError):
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="límites de texto inválidos")
        if brief_limit <= 0 or uncertainties_limit <= 0:
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="los límites de texto deben ser positivos")

        prompt = json.dumps(
            {
                "task": "shadow_emergency_situational_brief",
                "deterministic_phase": phase,
                "input": snapshot,
                "constraints": {
                    "brief_max_chars": brief_limit,
                    "uncertainties_max_chars": uncertainties_limit,
                    "informational_only": True,
                    "no_new_facts": True,
                    "no_operational_decisions": True,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        system = (
            "Eres un sintetizador auxiliar de emergencias en modo sombra. Usa SOLO los "
            "datos incluidos en input; no añadas hechos, causalidad ni inferencias nuevas. "
            "La fase determinista es autoritativa y no puede cambiarse. No decidas prioridad, "
            "severidad, verificación, resolución, envío, evacuación ni ninguna acción. No "
            "conviertas una correlación same_incident en confirmación operativa. Para NASA "
            "FIRMS, extensión significa extensión observada de detecciones y no superficie "
            "afectada/quemada; FRP es potencia radiante observada y no autoriza afirmar "
            "intensidad del incendio. stable no significa extinguido. Devuelve exclusivamente "
            "JSON con brief, uncertainties y confidence. brief y uncertainties deben ser "
            "cadenas; confidence debe ser un número JSON finito entre 0 y 1."
        )

        result = self.ai.generate_text(prompt, system=system)
        if not result.ok:
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status=result.status, error=result.error or "brief IA no disponible", duration_ms=result.duration_ms)

        try:
            parsed = json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="respuesta de brief no es JSON válido", duration_ms=result.duration_ms)
        if not isinstance(parsed, dict):
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="respuesta de brief no es un objeto JSON", duration_ms=result.duration_ms)

        brief = parsed.get("brief")
        uncertainties = parsed.get("uncertainties")
        if not isinstance(brief, str) or not isinstance(uncertainties, str):
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="brief/uncertainties inválidos", duration_ms=result.duration_ms)
        brief = _fit_text(brief, brief_limit)
        uncertainties = _fit_text(uncertainties, uncertainties_limit)
        if not brief:
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="respuesta de brief vacía", duration_ms=result.duration_ms)

        source = _clean_text(_event_value(event, "source", "")).casefold()
        if source == "nasa_firms" and (
            not _firms_explanation_is_safe(brief)
            or (uncertainties and not _firms_explanation_is_safe(uncertainties))
        ):
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="brief FIRMS contiene una sobreafirmación no permitida", duration_ms=result.duration_ms)

        confidence = _safe_confidence(parsed.get("confidence"))
        if confidence is None:
            return EmergencyAISituationalBrief(False, phase=phase, components=component_names, status="error", error="confidence inválida en respuesta de brief", duration_ms=result.duration_ms)

        return EmergencyAISituationalBrief(
            True,
            phase=phase,
            brief=brief,
            uncertainties=uncertainties,
            confidence=confidence,
            components=component_names,
            status=result.status,
            duration_ms=result.duration_ms,
        )
