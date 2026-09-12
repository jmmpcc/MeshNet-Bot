"""Orquestador IA en sombra para Emergencias — Fase IA-2E.

Coordina las capacidades ya existentes IA-2A, IA-2B, IA-2C e IA-2D sobre un
evento YA normalizado. Este módulo no importa ni conoce engine, storage, notifier,
dispatcher, broker o radio y, por diseño, no puede modificar la operativa.

Uso::

    from shared.meshnet_ai import MeshNetAI
    from shared.meshnet_ai_emergency_orchestrator import EmergencyAIShadowOrchestrator

    orchestrator = EmergencyAIShadowOrchestrator(MeshNetAI.from_env())
    result = orchestrator.analyze(event, change="updated", peer_events=current_events)

Activación:
    MESHNET_AI_ENABLED=1
    MESHNET_AI_EMERGENCIES_ENABLED=1
    MESHNET_AI_EMERGENCY_ORCHESTRATOR_ENABLED=1

El último flag es deliberadamente independiente y vale 0 por defecto. Así una
actualización del repositorio no empieza a coordinar llamadas IA automáticamente.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergencies import (
    EmergencyAIAnalysis,
    EmergencyAIObserver,
    _clean_text,
    _event_value,
    deterministic_phase,
)
from shared.meshnet_ai_emergency_brief import (
    EmergencyAISituationalBrief,
    EmergencyAISituationalBriefBuilder,
)
from shared.meshnet_ai_emergency_correlation import (
    EmergencyAICorrelation,
    EmergencyAICorrelator,
    correlation_candidate,
)
from shared.meshnet_ai_emergency_evolution import (
    EmergencyAIEvolutionExplainer,
    EmergencyAIEvolutionExplanation,
)


_TRUE_VALUES = {"1", "true", "yes", "on", "si", "sí"}
_DEFAULT_MAX_CORRELATION_CANDIDATES = 8


@dataclass(frozen=True)
class EmergencyAIShadowResult:
    """Resultado agregado, inmutable y exclusivamente auxiliar de IA-2E.

    Campos:
        ok: True cuando existe al menos un resultado IA válido.
        event_id: identificador del evento principal, solo para trazabilidad.
        deterministic_phase: fase calculada por la lógica determinista existente.
        analysis: resultado IA-2A, o None si la fase no pudo ejecutarse.
        evolution: resultado IA-2C, o None si la fase no pudo ejecutarse.
        correlations: resultados IA-2B de candidatos prefiltrados, en orden estable.
        brief: resultado IA-2D, o None si el builder no pudo ejecutarse.
        status: ok, partial, disabled, no_analysis o error.
        error: resumen seguro de fallos internos; nunca contiene secretos.
        duration_ms: duración total del ciclo IA-2E.

    Ninguno de estos campos tiene autoridad operativa. IA-2F podrá persistir una
    representación separada en una fase posterior, pero IA-2E trabaja en memoria.
    """

    ok: bool
    event_id: str = ""
    deterministic_phase: str = "unknown"
    analysis: EmergencyAIAnalysis | None = None
    evolution: EmergencyAIEvolutionExplanation | None = None
    correlations: tuple[EmergencyAICorrelation, ...] = ()
    brief: EmergencyAISituationalBrief | None = None
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


def _env_enabled(name: str) -> bool:
    """Lee un flag booleano opt-in; cualquier valor desconocido equivale a False."""

    return str(os.environ.get(name, "")).strip().casefold() in _TRUE_VALUES


def _event_id(event: Any) -> str:
    """Obtiene un identificador estable sin modificar ni exigir el tipo Event."""

    return _clean_text(_event_value(event, "event_id", ""))


def _safe_error(exc: BaseException) -> str:
    """Convierte una excepción interna en texto corto sin reproducir payloads."""

    name = type(exc).__name__
    message = _clean_text(str(exc))
    if not message:
        return name
    # El orquestador no necesita conservar prompts/respuestas completos. Limitar el
    # texto reduce además el riesgo de arrastrar datos del proveedor a logs futuros.
    return f"{name}: {message[:180]}"


class EmergencyAIShadowOrchestrator:
    """Coordina IA-2A/B/C/D sin introducir decisiones ni efectos laterales.

    Cómo se llama:
        ``EmergencyAIShadowOrchestrator(ai).analyze(event, change=..., peer_events=...)``.

    Parámetros del constructor:
        ai: infraestructura ``MeshNetAI`` ya existente.
        enabled: override opcional para tests. En producción debe omitirse y se lee
            ``MESHNET_AI_EMERGENCY_ORCHESTRATOR_ENABLED``.
        observer/correlator/evolution_explainer/brief_builder: dependencias
            inyectables para tests de aislamiento. Si se omiten se reutilizan las
            implementaciones IA-2A/B/C/D existentes sin modificarlas.

    Funcionalidad:
        1. valida los tres interruptores antes de coordinar nada;
        2. ejecuta IA-2A y IA-2C de forma aislada;
        3. prefiltra IA-2B de forma determinista y limita candidatos antes de IA;
        4. entrega a IA-2D los resultados obtenidos;
        5. conserva resultados parciales válidos aunque falle otro componente;
        6. devuelve únicamente un objeto en memoria.
    """

    def __init__(
        self,
        ai: MeshNetAI,
        *,
        enabled: bool | None = None,
        observer: Any = None,
        correlator: Any = None,
        evolution_explainer: Any = None,
        brief_builder: Any = None,
    ) -> None:
        self.ai = ai
        self._enabled_override = enabled
        self.observer = observer if observer is not None else EmergencyAIObserver(ai)
        self.correlator = correlator if correlator is not None else EmergencyAICorrelator(ai)
        self.evolution_explainer = (
            evolution_explainer
            if evolution_explainer is not None
            else EmergencyAIEvolutionExplainer(ai)
        )
        self.brief_builder = (
            brief_builder
            if brief_builder is not None
            else EmergencyAISituationalBriefBuilder(ai)
        )

    def _orchestrator_enabled(self) -> bool:
        """Resuelve el flag propio de IA-2E manteniendo False como valor seguro."""

        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return _env_enabled("MESHNET_AI_EMERGENCY_ORCHESTRATOR_ENABLED")

    def _correlation_enabled(self) -> bool:
        """Comprueba el flag IA-2B sin asumir detalles internos de ``MeshNetAI``."""

        try:
            return bool(self.ai.feature_enabled("correlation"))
        except Exception:
            return False

    def _select_candidates(
        self,
        event: Any,
        peer_events: Iterable[Any],
        *,
        max_candidates: int,
        max_distance_km: float,
        max_time_hours: float,
    ) -> list[Any]:
        """Selecciona pares elegibles con IA-2B antes de cualquier llamada al modelo.

        Los candidatos se ordenan por ``event_id`` para que la selección sea estable
        aunque el contenedor de entrada tenga otro orden. El evento principal y los
        peers sin identificador pueden seguir comparándose; en empate se conserva un
        índice local estable. Nunca se modifica la colección recibida.
        """

        selected: list[tuple[str, int, Any]] = []
        main_id = _event_id(event)
        for index, peer in enumerate(tuple(peer_events or ())):
            if peer is event:
                continue
            peer_id = _event_id(peer)
            if main_id and peer_id and main_id == peer_id:
                continue
            try:
                candidate = correlation_candidate(
                    event,
                    peer,
                    max_distance_km=max_distance_km,
                    max_time_hours=max_time_hours,
                )
            except Exception:
                # El selector es auxiliar: un peer defectuoso no cancela el ciclo.
                continue
            if candidate.eligible:
                selected.append((peer_id, index, peer))

        selected.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in selected[:max_candidates]]

    def analyze(
        self,
        event: Any,
        *,
        change: str = "",
        peer_events: Iterable[Any] = (),
        max_correlation_candidates: int = _DEFAULT_MAX_CORRELATION_CANDIDATES,
        max_distance_km: float = 50.0,
        max_time_hours: float = 24.0,
    ) -> EmergencyAIShadowResult:
        """Ejecuta un ciclo IA-2E completamente en sombra y en memoria.

        Parámetros:
            event: evento normalizado principal.
            change: cambio determinista conocido (new/updated/resolved).
            peer_events: otros eventos normalizados disponibles para IA-2B.
            max_correlation_candidates: máximo de pares que pueden alcanzar IA-2B.
                Se limita adicionalmente a 8 para coincidir con el contrato IA-2D.
            max_distance_km/max_time_hours: límites del prefiltrado determinista IA-2B.

        Retorna:
            ``EmergencyAIShadowResult``. Un resultado parcial nunca sustituye el
            procesamiento clásico y no se escribe ni transmite desde este módulo.
        """

        started = time.monotonic()
        phase = deterministic_phase(event, change)
        main_event_id = _event_id(event)

        def finish(**kwargs: Any) -> EmergencyAIShadowResult:
            kwargs.setdefault("event_id", main_event_id)
            kwargs.setdefault("deterministic_phase", phase)
            kwargs["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))
            return EmergencyAIShadowResult(**kwargs)

        if not getattr(getattr(self.ai, "config", None), "enabled", False):
            return finish(ok=False, status="disabled", error="IA desactivada")
        try:
            emergencies_enabled = bool(self.ai.feature_enabled("emergencies"))
        except Exception:
            emergencies_enabled = False
        if not emergencies_enabled:
            return finish(
                ok=False,
                status="disabled",
                error="análisis IA de emergencias desactivado",
            )
        if not self._orchestrator_enabled():
            return finish(
                ok=False,
                status="disabled",
                error="orquestador IA de emergencias desactivado",
            )

        try:
            requested_candidates = int(max_correlation_candidates)
        except (TypeError, ValueError, OverflowError):
            return finish(ok=False, status="error", error="límite de candidatos inválido")
        if requested_candidates <= 0:
            return finish(ok=False, status="error", error="límite de candidatos debe ser positivo")
        max_candidates = min(requested_candidates, _DEFAULT_MAX_CORRELATION_CANDIDATES)

        errors: list[str] = []
        analysis: EmergencyAIAnalysis | None = None
        evolution: EmergencyAIEvolutionExplanation | None = None
        correlations: list[EmergencyAICorrelation] = []
        brief: EmergencyAISituationalBrief | None = None

        try:
            analysis = self.observer.analyze_event(event, change=change)
        except Exception as exc:
            errors.append("IA-2A " + _safe_error(exc))

        try:
            evolution = self.evolution_explainer.explain(event)
        except Exception as exc:
            errors.append("IA-2C " + _safe_error(exc))

        if self._correlation_enabled():
            peers = self._select_candidates(
                event,
                peer_events,
                max_candidates=max_candidates,
                max_distance_km=max_distance_km,
                max_time_hours=max_time_hours,
            )
            for peer in peers:
                try:
                    result = self.correlator.correlate(
                        event,
                        peer,
                        max_distance_km=max_distance_km,
                        max_time_hours=max_time_hours,
                    )
                    correlations.append(result)
                except Exception as exc:
                    errors.append("IA-2B " + _safe_error(exc))

        try:
            brief = self.brief_builder.build(
                event,
                analysis=analysis,
                evolution=evolution,
                correlations=tuple(correlations),
            )
        except Exception as exc:
            errors.append("IA-2D " + _safe_error(exc))

        valid_count = int(bool(analysis and analysis.ok))
        valid_count += int(bool(evolution and evolution.ok))
        valid_count += sum(1 for item in correlations if item.ok)
        valid_count += int(bool(brief and brief.ok))

        component_failures = 0
        for result in (analysis, evolution, brief):
            if result is not None and not result.ok and result.status not in {
                "not_available",
                "not_candidate",
            }:
                component_failures += 1
        component_failures += sum(
            1
            for item in correlations
            if not item.ok and item.status not in {"not_available", "not_candidate"}
        )
        component_failures += len(errors)

        if valid_count == 0:
            status = "error" if component_failures else "no_analysis"
            error = "; ".join(errors) if errors else "no hay resultados IA válidos"
            return finish(
                ok=False,
                analysis=analysis,
                evolution=evolution,
                correlations=tuple(correlations),
                brief=brief,
                status=status,
                error=error,
            )

        status = "partial" if component_failures else "ok"
        return finish(
            ok=True,
            analysis=analysis,
            evolution=evolution,
            correlations=tuple(correlations),
            brief=brief,
            status=status,
            error="; ".join(errors),
        )
