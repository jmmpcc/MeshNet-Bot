"""Correlación IA en sombra para Emergencias — Fase IA-2B.

Este módulo analiza pares de eventos YA normalizados y procedentes de fuentes
DISTINTAS. Su resultado es exclusivamente informativo: no modifica ``Event``, no
fusiona incidentes, no cambia verificación/severidad/categoría/estado y no toca
ninguna salida MeshCore, Meshtastic, APRS o Voice RF.

IA-2B aplica dos capas separadas:
1. un selector determinista y conservador de candidatos por fuente, distancia,
   ubicación y proximidad temporal;
2. una interpretación IA opcional del candidato mediante un contrato JSON cerrado.

La agrupación/deduplicación operativa existente continúa siendo la única autoridad.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergencies import (
    _clean_text,
    _event_value,
    _optional_float,
    _safe_event_payload,
    deterministic_phase,
)


_TERMINAL_STATUSES = {"resolved", "cancelled", "expired", "closed"}
_ALLOWED_RELATIONS = {"same_incident", "contextual", "unrelated", "uncertain"}


@dataclass(frozen=True)
class EmergencyCorrelationCandidate:
    """Resultado determinista del prefiltrado IA-2B.

    Campos:
        eligible: ``True`` cuando el par puede enviarse al observador IA.
        distance_km: distancia geodésica si ambos eventos tienen coordenadas.
        time_delta_minutes: diferencia temporal absoluta si ambas fechas son válidas.
        reasons: señales deterministas usadas para aceptar/rechazar el candidato.

    Este objeto no expresa que los eventos sean el mismo incidente; únicamente
    indica si merece la pena compararlos en modo sombra.
    """

    eligible: bool
    distance_km: float | None = None
    time_delta_minutes: float | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmergencyAICorrelation:
    """Resultado no operativo del correlador IA-2B.

    ``relation`` solo puede ser ``same_incident``, ``contextual``, ``unrelated``
    o ``uncertain``. Incluso con ``ok=True`` el resultado es auxiliar y nunca debe
    utilizarse para fusionar, resolver o retransmitir eventos automáticamente.
    """

    ok: bool
    candidate: bool = False
    relation: str = "uncertain"
    explanation: str = ""
    confidence: float = 0.0
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0
    distance_km: float | None = None
    time_delta_minutes: float | None = None


def _parse_event_timestamp(event: Any) -> datetime | None:
    """Obtiene una fecha UTC comparable desde ``updated_at`` o ``started_at``.

    Cómo se llama:
        ``_parse_event_timestamp(event)`` durante el prefiltrado determinista.

    Funcionalidad:
        Prioriza ``updated_at`` porque representa la observación más reciente y
        usa ``started_at`` como respaldo. Acepta ISO-8601 con ``Z`` u offset. Las
        fechas inválidas o ausentes devuelven ``None`` y no lanzan excepciones.
    """

    raw = _clean_text(_event_value(event, "updated_at", ""))
    if not raw:
        raw = _clean_text(_event_value(event, "started_at", ""))
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _distance_km(event_a: Any, event_b: Any) -> float | None:
    """Calcula distancia Haversine sin acoplar ``shared`` al engine operativo.

    Cómo se llama:
        ``_distance_km(event_a, event_b)`` desde ``correlation_candidate``.

    Funcionalidad:
        Utiliza exclusivamente las coordenadas normalizadas de ambos eventos. Si
        falta alguna o es inválida devuelve ``None``. Se mantiene aquí como helper
        puro para que la capa IA no importe ``engine.py`` ni altere dependencias del
        sistema determinista existente.
    """

    lat1 = _optional_float(_event_value(event_a, "latitude", None))
    lon1 = _optional_float(_event_value(event_a, "longitude", None))
    lat2 = _optional_float(_event_value(event_b, "latitude", None))
    lon2 = _optional_float(_event_value(event_b, "longitude", None))
    if None in {lat1, lon1, lat2, lon2}:
        return None

    radius = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    value = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    value = min(1.0, max(0.0, value))
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def correlation_candidate(
    event_a: Any,
    event_b: Any,
    *,
    max_distance_km: float = 50.0,
    max_time_hours: float = 24.0,
) -> EmergencyCorrelationCandidate:
    """Selecciona de forma determinista pares aptos para correlación en sombra.

    Parámetros:
        event_a/event_b: eventos normalizados o mappings equivalentes.
        max_distance_km: distancia máxima cuando existen coordenadas.
        max_time_hours: diferencia temporal máxima cuando existen ambas fechas.

    Reglas:
        - exige dos eventos distintos y de fuentes distintas;
        - descarta eventos terminales para evitar correlaciones históricas espurias;
        - con coordenadas en ambos eventos exige estar dentro del radio indicado;
        - sin coordenadas completas exige coincidencia de municipio o provincia;
        - si ambas fechas son válidas exige proximidad temporal;
        - NO decide si existe relación causal ni si son el mismo incidente.

    La función es pura y nunca modifica los eventos recibidos.
    """

    try:
        distance_limit = float(max_distance_km)
        time_limit = float(max_time_hours)
    except (TypeError, ValueError, OverflowError):
        return EmergencyCorrelationCandidate(False, reasons=("invalid_limits",))
    if not math.isfinite(distance_limit) or not math.isfinite(time_limit):
        return EmergencyCorrelationCandidate(False, reasons=("invalid_limits",))
    if distance_limit <= 0 or time_limit <= 0:
        return EmergencyCorrelationCandidate(False, reasons=("invalid_limits",))

    event_id_a = _clean_text(_event_value(event_a, "event_id", ""))
    event_id_b = _clean_text(_event_value(event_b, "event_id", ""))
    if event_id_a and event_id_b and event_id_a == event_id_b:
        return EmergencyCorrelationCandidate(False, reasons=("same_event",))

    source_a = _clean_text(_event_value(event_a, "source", "")).casefold()
    source_b = _clean_text(_event_value(event_b, "source", "")).casefold()
    if not source_a or not source_b:
        return EmergencyCorrelationCandidate(False, reasons=("missing_source",))
    if source_a == source_b:
        return EmergencyCorrelationCandidate(False, reasons=("same_source",))

    status_a = _clean_text(_event_value(event_a, "status", "")).casefold()
    status_b = _clean_text(_event_value(event_b, "status", "")).casefold()
    if status_a in _TERMINAL_STATUSES or status_b in _TERMINAL_STATUSES:
        return EmergencyCorrelationCandidate(False, reasons=("terminal_event",))

    reasons: list[str] = ["different_sources"]
    distance = _distance_km(event_a, event_b)
    if distance is not None:
        if distance > distance_limit:
            return EmergencyCorrelationCandidate(
                False,
                distance_km=round(distance, 3),
                reasons=("distance_exceeded",),
            )
        reasons.append("distance_within_limit")
    else:
        municipality_a = _clean_text(_event_value(event_a, "municipality", "")).casefold()
        municipality_b = _clean_text(_event_value(event_b, "municipality", "")).casefold()
        province_a = _clean_text(_event_value(event_a, "province", "")).casefold()
        province_b = _clean_text(_event_value(event_b, "province", "")).casefold()
        same_municipality = bool(municipality_a and municipality_a == municipality_b)
        same_province = bool(province_a and province_a == province_b)
        if not same_municipality and not same_province:
            return EmergencyCorrelationCandidate(False, reasons=("insufficient_geo_match",))
        reasons.append("same_municipality" if same_municipality else "same_province")

    timestamp_a = _parse_event_timestamp(event_a)
    timestamp_b = _parse_event_timestamp(event_b)
    delta_minutes: float | None = None
    if timestamp_a is not None and timestamp_b is not None:
        delta_minutes = abs((timestamp_a - timestamp_b).total_seconds()) / 60.0
        if delta_minutes > time_limit * 60.0:
            return EmergencyCorrelationCandidate(
                False,
                distance_km=round(distance, 3) if distance is not None else None,
                time_delta_minutes=round(delta_minutes, 1),
                reasons=("time_exceeded",),
            )
        reasons.append("time_within_limit")
    else:
        reasons.append("time_unknown")

    return EmergencyCorrelationCandidate(
        True,
        distance_km=round(distance, 3) if distance is not None else None,
        time_delta_minutes=round(delta_minutes, 1) if delta_minutes is not None else None,
        reasons=tuple(reasons),
    )


class EmergencyAICorrelator:
    """Correlador IA-2B en sombra entre dos fuentes de Emergencias.

    Cómo se llama:
        ``EmergencyAICorrelator(MeshNetAI.from_env()).correlate(event_a, event_b)``.

    Requisitos:
        ``MESHNET_AI_ENABLED=1``
        ``MESHNET_AI_EMERGENCIES_ENABLED=1``
        ``MESHNET_AI_CORRELATION_ENABLED=1``

    La clase no conoce storage, notifier, dispatcher ni broker y por diseño no
    dispone de ninguna operación de escritura o transmisión.
    """

    def __init__(self, ai: MeshNetAI):
        self.ai = ai

    def correlate(
        self,
        event_a: Any,
        event_b: Any,
        *,
        max_distance_km: float = 50.0,
        max_time_hours: float = 24.0,
        max_explanation_chars: int = 500,
    ) -> EmergencyAICorrelation:
        """Evalúa informativamente la posible relación entre dos eventos.

        El proveedor solo se consulta si ambos flags de Emergencias/Correlación
        están activos y ``correlation_candidate`` acepta el par. La respuesta IA
        nunca modifica el candidato determinista ni los eventos originales.
        """

        candidate = correlation_candidate(
            event_a,
            event_b,
            max_distance_km=max_distance_km,
            max_time_hours=max_time_hours,
        )

        if not self.ai.config.enabled:
            return EmergencyAICorrelation(
                False,
                candidate=candidate.eligible,
                status="disabled",
                error="IA desactivada",
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if not self.ai.feature_enabled("emergencies"):
            return EmergencyAICorrelation(
                False,
                candidate=candidate.eligible,
                status="feature_disabled",
                error="análisis IA de emergencias desactivado",
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if not self.ai.feature_enabled("correlation"):
            return EmergencyAICorrelation(
                False,
                candidate=candidate.eligible,
                status="feature_disabled",
                error="correlación IA de emergencias desactivada",
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if not candidate.eligible:
            return EmergencyAICorrelation(
                False,
                candidate=False,
                status="not_candidate",
                error=",".join(candidate.reasons) or "par no candidato",
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )

        try:
            explanation_limit = int(max_explanation_chars)
        except (TypeError, ValueError, OverflowError):
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="límite de explicación inválido",
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if explanation_limit <= 0:
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="el límite de explicación debe ser positivo",
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )

        payload_a = _safe_event_payload(
            event_a,
            "",
            deterministic_phase(event_a, ""),
        )
        payload_b = _safe_event_payload(
            event_b,
            "",
            deterministic_phase(event_b, ""),
        )
        prompt = json.dumps(
            {
                "task": "shadow_emergency_cross_source_correlation",
                "event_a": payload_a,
                "event_b": payload_b,
                "deterministic_candidate": {
                    "distance_km": candidate.distance_km,
                    "time_delta_minutes": candidate.time_delta_minutes,
                    "reasons": list(candidate.reasons),
                },
                "constraints": {
                    "allowed_relations": sorted(_ALLOWED_RELATIONS),
                    "explanation_max_chars": explanation_limit,
                    "informational_only": True,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        system = (
            "Eres un observador auxiliar de correlación de emergencias. El par ya ha "
            "superado únicamente un prefiltrado determinista de proximidad; eso NO prueba "
            "que sea el mismo incidente ni que exista causalidad. No cambies categoría, "
            "severidad, verificación, estado o fase. No inventes hechos. Distingue entre "
            "same_incident (dos fuentes describen probablemente el mismo hecho), contextual "
            "(un evento aporta contexto o posible consecuencia sin ser el mismo hecho), "
            "unrelated y uncertain. Devuelve exclusivamente JSON con relation, explanation "
            "y confidence. explanation debe basarse solo en los datos recibidos y no puede "
            "contener órdenes operativas. confidence debe ser numérica y finita entre 0 y 1."
        )

        result = self.ai.generate_text(prompt, system=system)
        if not result.ok:
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status=result.status,
                error=result.error or "correlación IA no disponible",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )

        try:
            parsed = json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="respuesta de correlación no es JSON válido",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if not isinstance(parsed, dict):
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="respuesta de correlación no es un objeto JSON",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )

        relation = parsed.get("relation")
        explanation = parsed.get("explanation")
        if not isinstance(relation, str) or relation not in _ALLOWED_RELATIONS:
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="relation inválida en respuesta de correlación",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if not isinstance(explanation, str):
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="explanation inválida en respuesta de correlación",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        explanation = _clean_text(explanation)
        if not explanation:
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="respuesta de correlación sin explicación",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if len(explanation) > explanation_limit:
            explanation = explanation[:explanation_limit].rstrip(" ,;:-")

        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError, OverflowError):
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="confidence inválida en respuesta de correlación",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        if not math.isfinite(confidence):
            return EmergencyAICorrelation(
                False,
                candidate=True,
                status="error",
                error="confidence no finita en respuesta de correlación",
                duration_ms=result.duration_ms,
                distance_km=candidate.distance_km,
                time_delta_minutes=candidate.time_delta_minutes,
            )
        confidence = max(0.0, min(1.0, confidence))

        return EmergencyAICorrelation(
            True,
            candidate=True,
            relation=relation,
            explanation=explanation,
            confidence=confidence,
            status=result.status,
            duration_ms=result.duration_ms,
            distance_km=candidate.distance_km,
            time_delta_minutes=candidate.time_delta_minutes,
        )
