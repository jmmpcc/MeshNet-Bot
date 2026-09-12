"""Brief situacional IA en sombra para Emergencias — Fase IA-2D.

IA-2D compone una síntesis informativa a partir del evento determinista y de
resultados IA-2A/IA-2B/IA-2C YA validados. No decide severidad, prioridad,
verificación, fase, routing ni acciones; tampoco persiste ni transmite nada.
"""

from __future__ import annotations

import json
import math
import re
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
from shared.meshnet_ai_emergency_evolution import (
    _firms_evidence_constraints,
    _firms_explanation_is_safe,
    deterministic_evolution_snapshot,
)


_ALLOWED_RELATIONS = {"same_incident", "contextual", "unrelated", "uncertain"}

# IA-2D recibe textos de síntesis y también un campo explícito de incertidumbres.
# A diferencia de IA-2C, en ``uncertainties`` es correcto mencionar conceptos como
# "superficie afectada" si se hace exclusivamente para negar que pueda determinarse.
# Estas expresiones delimitan qué negaciones explícitas pueden proteger una mención
# sensible dentro de la misma cláusula, sin relajar las prohibiciones afirmativas.
_FIRMS_SAFE_NEGATION_PREFIXES = (
    re.compile(r"\bno\s+se\s+puede\b", re.IGNORECASE),
    re.compile(r"\bno\s+es\s+posible\b", re.IGNORECASE),
    re.compile(r"\bno\s+permite(?:n)?\b", re.IGNORECASE),
    re.compile(r"\bno\s+constituye(?:n)?\b", re.IGNORECASE),
    re.compile(r"\bsin\s+(?:poder\s+)?(?:confirmar|determinar|establecer)\b", re.IGNORECASE),
)

# Tokens que IA-2C ya considera inseguros cuando se presentan como afirmaciones.
# IA-2D permite citarlos únicamente si CADA aparición concreta permanece bajo una
# negación explícita dentro de su propia cláusula.
_FIRMS_UNSAFE_TOKENS = (
    "área afectada",
    "area afectada",
    "superficie afectada",
    "área quemada",
    "area quemada",
    "superficie quemada",
    "intensidad del incendio",
)

# Formulaciones categóricas que convierten observaciones FIRMS en conclusiones físicas
# sobre un incendio confirmado o sobre su estado. Se bloquean independientemente de
# la fase determinista para que, por ejemplo, ``stable`` nunca equivalga a extinguido.
_FIRMS_CATEGORICAL_INCIDENT_PATTERNS = (
    re.compile(r"\bfase\s+de\s+(?:crecimiento|aumento)\s+del\s+incendio\b", re.IGNORECASE),
    re.compile(r"\bcrecimiento\s+del\s+incendio\b", re.IGNORECASE),
    re.compile(r"\bincendio\s+(?:está|esta|se\s+encuentra)\s+(?:confirmado|activo|real|controlado|extinguido)\b", re.IGNORECASE),
    re.compile(r"\bincendio\s+(?:confirmado|activo|real|controlado|extinguido)\b", re.IGNORECASE),
    re.compile(r"\b(?:confirmado|controlado|extinguido)\s+(?:el\s+)?incendio\b", re.IGNORECASE),
)

# Conectores que rompen el alcance de una negación. Si aparece uno entre la negación
# y una segunda afirmación sensible, esa segunda cláusula debe validarse por separado.
_FIRMS_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:,|;|:)\s*(?=(?:pero|aunque|sin\s+embargo|no\s+obstante|y\s+en\s+cambio)\b)|"
    r"\s+\b(?:pero|aunque|sin\s+embargo|no\s+obstante|y\s+en\s+cambio)\b\s+",
    re.IGNORECASE,
)

_FIRMS_FRP_MENTION_RE = re.compile(r"\b(?:frp|potencia\s+radiante)\b", re.IGNORECASE)
_FIRMS_OBSERVED_EXTENT_RE = re.compile(
    r"\b(?:extensión|extension)\s+observada\b",
    re.IGNORECASE,
)


def _firms_brief_matches_evidence(
    text: str,
    evidence: Mapping[str, bool],
) -> bool:
    """Impide citar señales FIRMS que no existen en el evento determinista.

    Cómo se llama:
        ``_firms_brief_matches_evidence(text, evidence)`` sobre ``brief`` y
        ``uncertainties`` después de la barrera semántica general.

    Parámetros:
        text: texto generado por IA-2D.
        evidence: matriz booleana detections/extent/frp derivada del evento.

    Funcionalidad:
        Si no existen datos FRP, bloquea cualquier mención de FRP o potencia
        radiante. Si no existen datos de extensión observada, bloquea afirmaciones
        de "extensión observada". Las incertidumbres genéricas sobre no poder
        determinar superficie/extensión real siguen permitidas.
    """

    if not evidence.get("frp", False) and _FIRMS_FRP_MENTION_RE.search(text):
        return False
    if (
        not evidence.get("extent", False)
        and _FIRMS_OBSERVED_EXTENT_RE.search(text)
    ):
        return False
    return True


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


def _firms_clause_is_safe(clause: str) -> bool:
    """Valida una sola cláusula FIRMS sin permitir que una negación cubra otra.

    Cómo se llama:
        ``_firms_clause_is_safe(clause)`` desde ``_firms_brief_text_is_safe``.

    Parámetros:
        clause: fragmento textual ya separado por conectores adversativos.

    Funcionalidad:
        Comprueba cada aparición de términos sensibles. Para aceptar una aparición,
        debe existir una negación permitida ANTES de esa aparición dentro de la misma
        cláusula. Una negación previa no protege una segunda cláusula afirmativa.
    """

    folded = clause.casefold()
    occurrences: list[int] = []
    for token in _FIRMS_UNSAFE_TOKENS:
        start = 0
        token_folded = token.casefold()
        while True:
            index = folded.find(token_folded, start)
            if index < 0:
                break
            occurrences.append(index)
            start = index + len(token_folded)

    if not occurrences:
        return _firms_explanation_is_safe(clause)

    for index in sorted(occurrences):
        prefix = clause[:index]
        if not any(pattern.search(prefix) for pattern in _FIRMS_SAFE_NEGATION_PREFIXES):
            return False

    return True


def _firms_brief_text_is_safe(text: str) -> bool:
    """Valida semántica FIRMS específica de un brief/uncertainties IA-2D.

    Cómo se llama:
        ``_firms_brief_text_is_safe(text)`` sobre ``brief`` y ``uncertainties``
        después de validar y recortar el JSON del proveedor.

    Parámetros:
        text: texto ya normalizado que IA-2D pretende aceptar.

    Funcionalidad:
        - reutiliza la barrera estricta IA-2C;
        - permite superficie/intensidad solo cuando cada aparición concreta queda
          bajo una negación explícita dentro de su propia cláusula;
        - separa conectores adversativos para evitar que una negación inicial proteja
          una afirmación posterior no sustentada;
        - rechaza afirmaciones categóricas de incendio confirmado, activo, real,
          controlado o extinguido, además de crecimiento físico del incendio;
        - no modifica texto, evento ni componentes sombra.
    """

    if not text:
        return True

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
        if part.strip()
    ]
    for sentence in sentences:
        if any(pattern.search(sentence) for pattern in _FIRMS_CATEGORICAL_INCIDENT_PATTERNS):
            return False

        clauses = [part.strip(" ,;:") for part in _FIRMS_CLAUSE_SPLIT_RE.split(sentence) if part.strip(" ,;:")]
        if not clauses:
            clauses = [sentence]

        for clause in clauses:
            if not _firms_clause_is_safe(clause):
                return False

    return True


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
        source = _clean_text(_event_value(event, "source", "")).casefold()
        firms_evidence = (
            _firms_evidence_constraints(deterministic_evolution_snapshot(event))
            if source == "nasa_firms"
            else {"detections": False, "extent": False, "frp": False}
        )

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
                    "output_language": "es",
                    "firms_evidence": firms_evidence,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        system = (
            "Eres un sintetizador auxiliar de emergencias en modo sombra. Responde SIEMPRE "
            "en español. Usa SOLO los "
            "datos incluidos en input; no añadas hechos, causalidad ni inferencias nuevas. "
            "La fase determinista es autoritativa y no puede cambiarse. No decidas prioridad, "
            "severidad, verificación, resolución, envío, evacuación ni ninguna acción. No "
            "conviertas una correlación same_incident en confirmación operativa. Los textos de "
            "componentes sombra son auxiliares y NO son autoridad semántica: no copies literalmente "
            "una formulación si contradice estas restricciones. Para NASA FIRMS debes hablar siempre "
            "de posible foco, evento satelital o detecciones; no afirmes incendio confirmado ni "
            "describas una fase de crecimiento del incendio. Tampoco afirmes que el incendio está "
            "activo, controlado, extinguido o confirmado solo a partir de FIRMS. Para phase=growth "
            "prefiere formulaciones como 'aumento de detecciones FIRMS observadas' o 'detecciones "
            "FIRMS en fase determinista growth', únicamente cuando esos datos estén presentes. "
            "Evita expresamente 'incendio activo', 'crecimiento del incendio', 'fase de crecimiento "
            "del incendio' y equivalentes, aunque aparezcan en un componente sombra. La fase growth "
            "describe la evolución determinista de las detecciones FIRMS y stable no significa "
            "extinguido. Respeta constraints.firms_evidence como lista cerrada: "
            "si frp=false no menciones FRP ni potencia radiante; si extent=false no afirmes "
            "que existe extensión observada. Puedes indicar "
            "en uncertainties que una superficie o intensidad NO puede determinarse con estos "
            "datos. Devuelve exclusivamente JSON con brief, uncertainties y confidence. brief "
            "y uncertainties deben ser cadenas; confidence debe ser un número JSON finito entre 0 y 1."
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

        if source == "nasa_firms" and (
            not _firms_brief_text_is_safe(brief)
            or (uncertainties and not _firms_brief_text_is_safe(uncertainties))
            or not _firms_brief_matches_evidence(brief, firms_evidence)
            or (
                uncertainties
                and not _firms_brief_matches_evidence(
                    uncertainties,
                    firms_evidence,
                )
            )
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