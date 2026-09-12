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
import re
from dataclasses import dataclass
from typing import Any, Mapping

from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergencies import (
    _clean_text,
    _event_metadata,
    _event_value,
    _fit_text,
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

# Expresiones que, en una explicación FIRMS, transforman una observación satelital
# en una conclusión física que NASA FIRMS/nuestro tracker no ha determinado. Se
# mantienen deliberadamente acotadas a afirmaciones de alto riesgo ya observadas
# en la prueba real con proveedor para no intentar clasificar semánticamente toda
# la respuesta mediante heurísticas frágiles.
_FIRMS_UNSAFE_EXPLANATION_PATTERNS = (
    re.compile(r"\b(?:área|area|superficie)\s+(?:afectada|quemada)\b", re.IGNORECASE),
    re.compile(r"\bintensidad\s+(?:del|de\s+el)\s+incendio\b", re.IGNORECASE),
)

# Términos observacionales que solo pueden aparecer cuando el snapshot FIRMS
# contiene evidencia determinista del mismo tipo. Esta barrera evita inferir
# extensión o FRP a partir de un simple aumento del número de detecciones.
_FIRMS_EXTENT_TERMS = re.compile(r"\b(?:extensión|extension)\b", re.IGNORECASE)
_FIRMS_FRP_TERMS = re.compile(r"\b(?:frp|potencia\s+radiante)\b", re.IGNORECASE)


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


def _firms_explanation_is_safe(explanation: str) -> bool:
    """Valida que una explicación FIRMS no introduzca conclusiones no observadas.

    Cómo se llama:
        ``_firms_explanation_is_safe(explanation)`` después de validar el JSON y
        antes de aceptar la salida del proveedor.

    Parámetros:
        explanation: texto ya normalizado devuelto por el proveedor.

    Funcionalidad:
        Rechaza expresiones que convierten la extensión de detecciones en área o
        superficie afectada/quemada, o el FRP en intensidad del incendio. Es una
        barrera determinista adicional al prompt; ante duda falla de forma segura
        con ``ok=False`` y nunca modifica el evento ni ningún flujo operativo.
    """

    return not any(
        pattern.search(explanation)
        for pattern in _FIRMS_UNSAFE_EXPLANATION_PATTERNS
    )


def _firms_explanation_matches_snapshot(
    explanation: str,
    snapshot: Mapping[str, Any],
) -> bool:
    """Comprueba que cada concepto FIRMS citado tenga evidencia en el snapshot.

    Cómo se llama:
        ``_firms_explanation_matches_snapshot(explanation, snapshot)`` después de
        la barrera semántica general y antes de aceptar la explicación IA-2C.

    Parámetros:
        explanation: texto normalizado devuelto por el proveedor.
        snapshot: snapshot determinista construido por
            ``deterministic_evolution_snapshot()``.

    Funcionalidad:
        Un aumento de detecciones no autoriza por sí solo a afirmar cambios de
        extensión o FRP. Si la explicación menciona extensión, exige al menos una
        señal determinista de extensión en ``firms_tracking``; si menciona FRP o
        potencia radiante, exige una señal FRP equivalente. Ante ausencia de
        evidencia devuelve ``False``, sin modificar evento ni snapshot.
    """

    tracking = snapshot.get("firms_tracking", {})
    if not isinstance(tracking, Mapping):
        tracking = {}

    growth_reasons_raw = tracking.get("growth_reasons", ())
    if isinstance(growth_reasons_raw, (list, tuple, set)):
        growth_reasons = {
            _clean_text(value).casefold()
            for value in growth_reasons_raw
            if _clean_text(value)
        }
    else:
        growth_reasons = set()

    has_extent_evidence = (
        "extent" in growth_reasons
        or any(
            tracking.get(key) not in (None, "")
            for key in (
                "previous_extent_km",
                "latest_extent_km",
                "incident_peak_extent_km",
            )
        )
    )
    has_frp_evidence = (
        "frp" in growth_reasons
        or any(
            tracking.get(key) not in (None, "")
            for key in (
                "previous_frp_total_mw",
                "latest_frp_total_mw",
                "incident_peak_frp_total_mw",
            )
        )
    )

    if _FIRMS_EXTENT_TERMS.search(explanation) and not has_extent_evidence:
        return False
    if _FIRMS_FRP_TERMS.search(explanation) and not has_frp_evidence:
        return False
    return True


def _firms_evidence_constraints(snapshot: Mapping[str, Any]) -> dict[str, bool]:
    """Resume qué tipos de señal FIRMS están presentes de forma determinista.

    Cómo se llama:
        ``_firms_evidence_constraints(snapshot)`` al construir el prompt IA-2C.

    Parámetros:
        snapshot: salida de ``deterministic_evolution_snapshot()``.

    Funcionalidad:
        Expone tres booleanos independientes (detections, extent, frp) para que el
        proveedor conozca exactamente qué conceptos puede describir. La función no
        infiere significado operativo y no modifica el snapshot.
    """

    tracking = snapshot.get("firms_tracking", {})
    if not isinstance(tracking, Mapping):
        tracking = {}

    growth_reasons_raw = tracking.get("growth_reasons", ())
    if isinstance(growth_reasons_raw, (list, tuple, set)):
        growth_reasons = {
            _clean_text(value).casefold()
            for value in growth_reasons_raw
            if _clean_text(value)
        }
    else:
        growth_reasons = set()

    detections = (
        any(
            tracking.get(key) not in (None, "")
            for key in (
                "previous_detection_count",
                "latest_detection_count",
                "incident_peak_detection_count",
            )
        )
        or any("detection" in reason for reason in growth_reasons)
    )
    extent = (
        "extent" in growth_reasons
        or any(
            tracking.get(key) not in (None, "")
            for key in (
                "previous_extent_km",
                "latest_extent_km",
                "incident_peak_extent_km",
            )
        )
    )
    frp = (
        "frp" in growth_reasons
        or any(
            tracking.get(key) not in (None, "")
            for key in (
                "previous_frp_total_mw",
                "latest_frp_total_mw",
                "incident_peak_frp_total_mw",
            )
        )
    )
    return {"detections": detections, "extent": extent, "frp": frp}


def _firms_allowed_facts(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Construye hechos FIRMS permitidos a partir del snapshot determinista.

    Cómo se llama:
        ``_firms_allowed_facts(snapshot)`` al preparar el prompt IA-2C.

    Parámetros:
        snapshot: snapshot creado por ``deterministic_evolution_snapshot()``.

    Funcionalidad:
        Devuelve exclusivamente afirmaciones literales respaldadas por datos
        deterministas. No interpreta causalidad, superficie afectada, intensidad
        ni estado físico de un incendio.
    """

    tracking = snapshot.get("firms_tracking", {})
    if not isinstance(tracking, Mapping):
        return ()

    facts: list[str] = []
    previous_count = tracking.get("previous_detection_count")
    latest_count = tracking.get("latest_detection_count")
    if previous_count not in (None, "") and latest_count not in (None, ""):
        facts.append(
            f"El número de detecciones satelitales pasa de {previous_count} a {latest_count}."
        )

    previous_extent = tracking.get("previous_extent_km")
    latest_extent = tracking.get("latest_extent_km")
    if previous_extent not in (None, "") and latest_extent not in (None, ""):
        facts.append(
            "La extensión observada del conjunto de detecciones pasa de "
            f"{previous_extent} km a {latest_extent} km."
        )

    previous_frp = tracking.get("previous_frp_total_mw")
    latest_frp = tracking.get("latest_frp_total_mw")
    if previous_frp not in (None, "") and latest_frp not in (None, ""):
        facts.append(
            f"El FRP total observado pasa de {previous_frp} MW a {latest_frp} MW."
        )

    return tuple(facts)


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
                    "firms_evidence": _firms_evidence_constraints(snapshot),
                    "allowed_facts": _firms_allowed_facts(snapshot),
                    "output_language": "es",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        system = (
            "Eres un observador auxiliar de evolución de emergencias. Responde SIEMPRE "
            "en español. Usa únicamente los hechos de constraints.allowed_facts para "
            "describir cambios cuantitativos; no añadas otros cambios aunque parezcan "
            "plausibles. La fase incluida "
            "en deterministic_evolution ya ha sido decidida por lógica determinista y "
            "es autoritativa: NO la cambies, recalcules ni contradigas. Explica únicamente "
            "qué datos observados justifican o describen esa evolución. No cambies categoría, "
            "severidad, verificación o estado. No inventes causas, daños, superficie afectada "
            "ni consecuencias. Para NASA FIRMS, habla de posible foco o detecciones satelitales, "
            "no de un incendio confirmado. cluster/extent describe exclusivamente extensión "
            "observada del conjunto de detecciones satelitales: nunca la llames área/superficie "
            "afectada o quemada. El FRP es potencia radiante observada y no debe describirse "
            "como intensidad del incendio. Cada concepto citado debe estar respaldado por "
            "su señal determinista correspondiente. Usa constraints.firms_evidence como "
            "lista cerrada: si extent=false no menciones extensión; si frp=false no menciones "
            "FRP/potencia radiante; si detections=true puedes describir el cambio de detecciones. "
            "Si solo aumenta detection_count, describe únicamente un aumento de detecciones y "
            "NO infieras mayor actividad, extensión, FRP, "
            "superficie o intensidad. Solo puedes mencionar cambios de extensión cuando existan "
            "campos de extensión o growth_reasons=extent, y FRP cuando existan campos FRP o "
            "growth_reasons=frp. growth_reasons son señales deterministas ya calculadas. stable "
            "significa sin crecimiento significativo detectado en esa "
            "pasada, NO incendio extinguido. resolved solo puede afirmarse cuando la fase "
            "determinista recibida sea resolved. Devuelve exclusivamente JSON con explanation "
            "y confidence. explanation debe ser factual, sin órdenes operativas. confidence "
            "debe ser un número JSON finito entre 0 y 1."
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

        source = _clean_text(_event_value(event, "source", "")).casefold()
        if source == "nasa_firms" and not _firms_explanation_is_safe(explanation):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="explicación FIRMS contiene una conclusión no sustentada",
                duration_ms=result.duration_ms,
            )
        if source == "nasa_firms" and not _firms_explanation_matches_snapshot(
            explanation,
            snapshot,
        ):
            return EmergencyAIEvolutionExplanation(
                False,
                phase=phase,
                status="error",
                error="explicación FIRMS menciona señales sin evidencia determinista",
                duration_ms=result.duration_ms,
            )

        # Reutilizamos el recorte ya probado de IA-2A: conserva frases/palabras y
        # evita terminar a mitad de token como ocurrió en la primera prueba real.
        explanation = _fit_text(explanation, explanation_limit)

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
