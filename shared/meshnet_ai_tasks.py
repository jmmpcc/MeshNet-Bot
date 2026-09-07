"""Tareas de alto nivel para MeshNet Intelligence Fase IA-1.

Este módulo implementa exclusivamente operaciones de clasificación y resumen sobre
la infraestructura opcional definida en ``shared.meshnet_ai``. No modifica ningún
flujo de broker, radio, APRS, BBS, correo, Telegram o Emergencias.

Uso::

    from shared.meshnet_ai import MeshNetAI
    from shared.meshnet_ai_tasks import MeshNetAITasks

    tasks = MeshNetAITasks(MeshNetAI.from_env())
    result = tasks.summarize_text(texto, max_chars=140)

Reglas:
- si IA está desactivada, devuelve ``ok=False`` sin llamadas externas;
- resumen requiere ``MESHNET_AI_SUMMARIZE_ENABLED=1``;
- clasificación requiere IA global activa y una lista cerrada de etiquetas;
- ningún resultado inválido se acepta como válido;
- el consumidor conserva siempre su comportamiento clásico cuando ``ok=False``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from shared.meshnet_ai import MeshNetAI


@dataclass(frozen=True)
class SummaryResult:
    """Resultado validado de una operación de resumen.

    Parámetros devueltos:
        ok: ``True`` únicamente cuando existe un resumen válido.
        text: Resumen final ya ajustado al límite solicitado.
        status: Estado heredado de la infraestructura IA.
        error: Motivo seguro del fallback cuando ``ok=False``.
        duration_ms: Duración reportada por el proveedor.
    """

    ok: bool
    text: str = ""
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class ClassificationResult:
    """Resultado validado de clasificación contra etiquetas cerradas."""

    ok: bool
    label: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


def _normalize_spaces(text: str) -> str:
    """Normaliza espacios y saltos de línea sin alterar el contenido semántico."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _fit_text(text: str, max_chars: int) -> str:
    """Ajusta texto al límite positivo exacto solicitado.

    Cómo se llama:
        ``_fit_text(respuesta_ia, max_chars)`` tras obtener una respuesta del
        proveedor. También se reutiliza para limitar el razonamiento de una
        clasificación.

    Parámetros:
        text: Texto que debe ajustarse.
        max_chars: Presupuesto máximo de caracteres. Cualquier valor positivo se
            respeta literalmente; ``0`` o negativos producen cadena vacía.

    Funcionalidad:
        Primero normaliza espacios. Si hay que recortar, intenta terminar en un
        límite de palabra siempre que ello no vacíe el resultado. Nunca amplía el
        presupuesto indicado por el llamador y nunca devuelve más de
        ``max_chars`` caracteres.

    Esta función se aplica únicamente a una respuesta IA ya obtenida. No sustituye
    los formateadores deterministas existentes de MeshNet.
    """
    clean = _normalize_spaces(text)
    limit = int(max_chars)
    if limit <= 0:
        return ""
    if len(clean) <= limit:
        return clean
    candidate = clean[:limit].rstrip()
    if " " in candidate:
        shortened = candidate.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if shortened:
            candidate = shortened
    return candidate.rstrip(" .")


def _clean_labels(labels: Iterable[str]) -> list[str]:
    """Normaliza etiquetas, elimina vacías/duplicadas y conserva el orden."""
    result: list[str] = []
    seen: set[str] = set()
    for item in labels:
        label = _normalize_spaces(str(item)).casefold()
        if not label or label in seen:
            continue
        seen.add(label)
        result.append(label)
    return result


class MeshNetAITasks:
    """Fachada IA-1 para resumen y clasificación con validación estricta.

    Cómo se llama:
        ``MeshNetAITasks(MeshNetAI.from_env())``.

    Parámetros:
        ai: Instancia de ``MeshNetAI`` que mantiene proveedor, timeout, circuit
            breaker y protección de credenciales de IA-0.

    La clase no transmite, persiste ni modifica datos del proyecto. El llamador
    decide si utiliza el resultado; ``ok=False`` significa fallback obligatorio.
    """

    def __init__(self, ai: MeshNetAI):
        self.ai = ai

    def summarize_text(
        self,
        text: str,
        *,
        max_chars: int = 140,
        context: str = "",
        preserve: Iterable[str] = (),
    ) -> SummaryResult:
        """Resume un texto manteniendo un presupuesto máximo estricto.

        Cómo se llama:
            ``tasks.summarize_text(texto, max_chars=67)``. El consumidor debe usar
            el texto únicamente cuando ``result.ok`` sea verdadero; en cualquier
            otro caso mantiene su comportamiento clásico.

        Parámetros:
            text: Texto de entrada, previamente anonimizado si procede.
            max_chars: Límite final estricto. Cualquier entero positivo se respeta
                literalmente. Los valores ``0`` o negativos se rechazan sin
                realizar ninguna llamada al proveedor.
            context: Contexto funcional opcional para mejorar el resumen.
            preserve: Conceptos que el modelo debe conservar si están presentes.

        Retorna:
            ``SummaryResult(ok=True)`` solo cuando IA global y el flag ``summarize``
            están activos, el límite es válido y el proveedor produce contenido no
            vacío. El texto devuelto nunca supera ``max_chars``.
        """
        if not self.ai.feature_enabled("summarize"):
            state = "disabled" if not self.ai.config.enabled else "feature_disabled"
            return SummaryResult(ok=False, status=state, error="resumen IA desactivado")

        source = _normalize_spaces(text)
        if not source:
            return SummaryResult(ok=False, status="error", error="texto vacío")

        try:
            limit = int(max_chars)
        except (TypeError, ValueError):
            return SummaryResult(ok=False, status="error", error="max_chars inválido")
        if limit <= 0:
            return SummaryResult(ok=False, status="error", error="max_chars debe ser positivo")

        preserve_items = [_normalize_spaces(x) for x in preserve if _normalize_spaces(x)]
        preserve_text = ", ".join(preserve_items) if preserve_items else "ninguno"
        prompt = (
            f"Resume el siguiente texto en español en un máximo estricto de {limit} caracteres. "
            "Conserva los hechos operativos y no inventes datos. "
            f"Elementos a preservar si aparecen: {preserve_text}. "
            f"Contexto: {_normalize_spaces(context) or 'general'}.\n\n"
            f"TEXTO:\n{source}"
        )
        result = self.ai.generate_text(
            prompt,
            system=(
                "Eres el motor de resumen de MeshNet. Devuelve exclusivamente el resumen, "
                "sin explicaciones, encabezados ni markdown. No añadas información ausente."
            ),
        )
        if not result.ok:
            return SummaryResult(
                ok=False,
                status=result.status,
                error=result.error,
                duration_ms=result.duration_ms,
            )

        fitted = _fit_text(result.text, limit)
        if not fitted:
            return SummaryResult(
                ok=False,
                status="degraded",
                error="resumen IA vacío",
                duration_ms=result.duration_ms,
            )
        return SummaryResult(
            ok=True,
            text=fitted,
            status=result.status,
            duration_ms=result.duration_ms,
        )

    def classify_text(
        self,
        text: str,
        labels: Iterable[str],
        *,
        context: str = "",
    ) -> ClassificationResult:
        """Clasifica texto contra una lista cerrada de etiquetas permitidas.

        La clasificación no puede devolver una categoría inventada: cualquier
        etiqueta fuera de la lista provoca ``ok=False``. La confianza se limita a
        ``0.0..1.0`` y el razonamiento es breve y opcional.
        """
        if not self.ai.config.enabled:
            return ClassificationResult(ok=False, status="disabled", error="IA desactivada")

        source = _normalize_spaces(text)
        allowed = _clean_labels(labels)
        if not source:
            return ClassificationResult(ok=False, status="error", error="texto vacío")
        if len(allowed) < 2:
            return ClassificationResult(
                ok=False,
                status="error",
                error="se requieren al menos dos etiquetas",
            )

        prompt = (
            "Clasifica el texto usando exclusivamente una de estas etiquetas: "
            + ", ".join(allowed)
            + ". Devuelve JSON válido con las claves label, confidence y reasoning. "
            "confidence debe estar entre 0 y 1. No inventes etiquetas. "
            f"Contexto: {_normalize_spaces(context) or 'general'}.\n\n"
            f"TEXTO:\n{source}"
        )
        result = self.ai.generate_text(
            prompt,
            system=(
                "Eres el clasificador de MeshNet. Devuelve únicamente un objeto JSON válido, "
                "sin markdown ni texto adicional."
            ),
        )
        if not result.ok:
            return ClassificationResult(
                ok=False,
                status=result.status,
                error=result.error,
                duration_ms=result.duration_ms,
            )

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError:
            return ClassificationResult(
                ok=False,
                status="degraded",
                error="clasificación IA no es JSON válido",
                duration_ms=result.duration_ms,
            )
        if not isinstance(payload, dict):
            return ClassificationResult(
                ok=False,
                status="degraded",
                error="clasificación IA inválida",
                duration_ms=result.duration_ms,
            )

        label = _normalize_spaces(payload.get("label", "")).casefold()
        if label not in allowed:
            return ClassificationResult(
                ok=False,
                status="degraded",
                error="etiqueta IA fuera del catálogo permitido",
                duration_ms=result.duration_ms,
            )
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        reasoning = _fit_text(str(payload.get("reasoning", "")), 240)
        return ClassificationResult(
            ok=True,
            label=label,
            confidence=confidence,
            reasoning=reasoning,
            status=result.status,
            duration_ms=result.duration_ms,
        )
