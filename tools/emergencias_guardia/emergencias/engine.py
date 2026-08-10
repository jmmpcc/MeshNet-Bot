from __future__ import annotations

import math
import os
from typing import Any

from .models import Event, SEVERITY_RANK, fold_text, utc_now
from .geo_admin import enrich_event_province
from .sources import SOURCE_TYPES
from .sources.base import SourceError
from .storage import (
    append_history, load_current, load_state, save_current, save_state,
)


TERMINAL_STATUSES = {"resolved", "cancelled", "expired"}


def _env_enabled(name: str, default: bool = False) -> bool:
    """Devuelve una variable booleana de entorno con semántica tolerante.

    Uso:
        active = _env_enabled("AEMET_ALERTS_ENABLED", True)

    Parámetros:
        name: nombre de la variable de entorno.
        default: valor usado cuando la variable no existe.

    Funcionalidad:
        Reconoce 1/true/yes/on/si/sí como verdadero. Se usa para impedir que
        una fuente auxiliar de Emergencias duplique un subsistema ya activo en
        el broker, sin modificar ni depender del código de dicho subsistema.
    """
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().casefold() in {"1", "true", "yes", "y", "on", "si", "sí"}


def _source_disabled_by_external_owner(source_config: dict[str, Any]) -> str:
    """Indica si una fuente debe ceder la autoridad a otro subsistema activo.

    Uso:
        reason = _source_disabled_by_external_owner(source_config)

    Funcionalidad:
        Algunas fuentes pueden existir como fallback o futura migración, pero
        no deben ejecutarse en paralelo con un recolector histórico que ya
        gestiona deduplicación y transmisión. La configuración declara la
        variable mediante ``disabled_if_env_enabled`` y opcionalmente su valor
        por defecto mediante ``disabled_if_env_default``.
    """
    env_name = str(source_config.get("disabled_if_env_enabled") or "").strip()
    if not env_name:
        return ""
    default = bool(source_config.get("disabled_if_env_default", False))
    if _env_enabled(env_name, default):
        return f"gestionada por subsistema existente ({env_name}=1)"
    return ""


def _source_disabled_by_configuration(source_config: dict[str, Any]) -> str:
    """Devuelve el motivo por el que una fuente está declarada no operativa.

    Uso:
        reason = _source_disabled_by_configuration(source_config)

    Funcionalidad:
        Permite mantener una fuente visible y documentada sin intentar usar un
        endpoint que se haya comprobado que no es apto para consumo automático.
        Se evita así convertir silenciosamente una página HTML en un supuesto
        feed/API operativo.
    """
    if source_config.get("operational", True) is not False:
        return ""
    return str(source_config.get("disabled_reason") or "fuente no operativa").strip()


def _failure_state(previous: Any, now: str, error: str) -> dict[str, Any]:
    """Construye un estado de fallo sin arrastrar flags incompatibles previos.

    Uso:
        state = _failure_state(previous, now, str(exc))

    Funcionalidad:
        Conserva únicamente datos históricos útiles de una ejecución correcta
        anterior y elimina campos transitorios como ``skipped`` y ``reason``.
        Así un fallo actual no puede mostrarse simultáneamente como bloqueado por
        una condición de una ejecución anterior.
    """
    keep = {}
    if isinstance(previous, dict):
        for key in ("last_success", "records", "accepted", "not_modified"):
            if key in previous:
                keep[key] = previous[key]
    return keep | {"ok": False, "last_error": now, "error": error}


def _enrich_events_for_province_areas(events: list[Event], config: dict[str, Any]) -> int:
    """Enriquece provincia solo cuando el usuario filtra por provincias.

    Uso:
        enriched = _enrich_events_for_province_areas(events, config)

    Parámetros:
        events: eventos ya normalizados por el conector.
        config: configuración completa de emergencias.

    Funcionalidad:
        Conserva el flujo histórico si no hay áreas provinciales activas. Si las
        hay, intenta resolver mediante cartografía local únicamente los eventos
        que todavía no traen provincia. ``enrich_event_province`` es tolerante a
        ausencia/corrupción del fichero, por lo que el radio geográfico existente
        continúa siendo el fallback y no se bloquea la fuente.
    """
    has_province_area = any(
        area.get("enabled", True) and area.get("type") == "province"
        for area in config.get("areas", [])
    )
    if not has_province_area:
        return 0
    return sum(1 for event in events if enrich_event_province(event))


def event_matches(event: Event, config: dict[str, Any], query: dict[str, Any] | None = None) -> bool:
    query = query or {}
    categories = set(config["filters"].get("categories", []))
    if "categories" in config["filters"] and event.category not in categories:
        return False
    minimum = query.get("minimum_severity") or config["filters"].get("minimum_severity", "low")
    if SEVERITY_RANK.get(event.severity, 0) < SEVERITY_RANK.get(minimum, 0):
        return False
    if query.get("category") and event.category != query["category"]:
        return False
    if query.get("province") and fold_text(event.province) != fold_text(query["province"]):
        return False
    if query.get("municipality") and fold_text(event.municipality) != fold_text(query["municipality"]):
        return False
    if query.get("road") and fold_text(query["road"]) not in fold_text(event.road):
        return False
    text = fold_text(query.get("text", ""))
    if text and text not in fold_text(" ".join((
        event.title, event.description, event.road, event.municipality, event.province,
    ))):
        return False
    areas = [area for area in config.get("areas", []) if area.get("enabled", True)]
    return not areas or any(_area_matches(event, area) for area in areas)


def _area_matches(event: Event, area: dict[str, Any]) -> bool:
    """Comprueba el ámbito de un evento sin cambiar los filtros existentes.

    Los conectores históricos siguen usando ``Event.province`` y coordenadas.
    Las fuentes multizona de v7.0.43 pueden añadir ``metadata['provinces']`` o
    ``metadata['cap_area']``; ambos campos se consultan únicamente como respaldo
    cuando el dato provincial simple no es suficiente.
    """
    kind = area.get("type")
    if kind == "province":
        wanted = fold_text(area.get("name"))
        if fold_text(event.province) == wanted:
            return True
        metadata_provinces = event.metadata.get("provinces", [])
        if isinstance(metadata_provinces, (list, tuple, set)) and any(
            fold_text(value) == wanted for value in metadata_provinces
        ):
            return True
        cap_area = fold_text(event.metadata.get("cap_area", ""))
        if wanted and wanted in cap_area:
            return True
        cap_parameters = event.metadata.get("cap_parameters", {})
        if isinstance(cap_parameters, dict):
            parameter_text = fold_text(" ".join(str(value) for value in cap_parameters.values()))
            if wanted and wanted in parameter_text:
                return True
        return False
    if kind == "municipality":
        return fold_text(event.municipality) == fold_text(area.get("name"))
    if kind == "radius" and event.latitude is not None and event.longitude is not None:
        distance = _haversine(
            event.latitude, event.longitude, float(area["latitude"]), float(area["longitude"])
        )
        return distance <= float(area["radius_km"])
    return False


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    value = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def list_events(config: dict[str, Any], query: dict[str, Any] | None = None) -> list[Event]:
    query = query or {}
    include_resolved = bool(query.get("include_resolved"))
    events = [
        event for event in load_current().values()
        if (include_resolved or event.status not in TERMINAL_STATUSES) and event_matches(event, config, query)
    ]
    return sorted(events, key=lambda item: (
        -SEVERITY_RANK.get(item.severity, 0), item.province, item.municipality, item.event_id
    ))


def fetch_sources(config: dict[str, Any], only: str | None = None) -> dict[str, Any]:
    current = load_current()
    state = load_state()
    source_states = state.setdefault("sources", {})
    report: dict[str, Any] = {"sources": {}, "changes": {"new": 0, "updated": 0, "resolved": 0}}
    now = utc_now()
    threshold = max(1, int(config["fetch"].get("resolve_after_missing_fetches", 2)))
    for source_id, source_config in config.get("sources", {}).items():
        if only and source_id != only:
            continue
        if not source_config.get("enabled", False):
            skipped = {"ok": False, "skipped": "disabled"}
            source_states[source_id] = skipped
            report["sources"][source_id] = skipped
            continue
        configuration_reason = _source_disabled_by_configuration(source_config)
        if configuration_reason:
            skipped = {"ok": False, "skipped": "not_operational", "reason": configuration_reason}
            source_states[source_id] = skipped
            report["sources"][source_id] = skipped
            continue
        external_reason = _source_disabled_by_external_owner(source_config)
        if external_reason:
            skipped = {"ok": False, "skipped": "external_owner", "reason": external_reason}
            source_states[source_id] = skipped
            report["sources"][source_id] = skipped
            continue
        if source_config.get("require_areas") and not any(
            area.get("enabled", True) for area in config.get("areas", [])
        ):
            failed = {"ok": False, "error": "la fuente requiere al menos un área geográfica habilitada"}
            source_states[source_id] = failed
            report["sources"][source_id] = failed
            continue
        source_type = SOURCE_TYPES.get(source_config.get("type"))
        if source_type is None:
            failed = {"ok": False, "error": "tipo de fuente desconocido"}
            source_states[source_id] = failed
            report["sources"][source_id] = failed
            continue
        try:
            events, not_modified = source_type(source_id, source_config, config).fetch()
            _enrich_events_for_province_areas(events, config)
            filtered = {event.event_id: event for event in events if event_matches(event, config)}
            _merge_source(current, filtered, source_id, now, threshold, report)
            source_states[source_id] = {
                "ok": True, "last_success": now, "records": len(events),
                "accepted": len(filtered), "not_modified": not_modified,
            }
            report["sources"][source_id] = source_states[source_id]
        except (SourceError, ValueError, TypeError) as exc:
            source_states[source_id] = _failure_state(source_states.get(source_id), now, str(exc))
            report["sources"][source_id] = source_states[source_id]
    save_current(current)
    save_state(state)
    return report


def _merge_source(
    current: dict[str, Event], fetched: dict[str, Event], source_id: str,
    now: str, threshold: int, report: dict[str, Any],
) -> None:
    for event_id, event in fetched.items():
        old = current.get(event_id)
        event.first_seen = old.first_seen if old and old.first_seen else now
        event.last_seen = now
        event.metadata["missing_fetches"] = 0
        change = None
        if old is None or old.status in TERMINAL_STATUSES:
            change = "new"
        elif old.raw_hash != event.raw_hash:
            change = "updated"
        current[event_id] = event
        if change:
            append_history(change, event)
            report["changes"][change] += 1
    for event_id, old in list(current.items()):
        if old.source != source_id or event_id in fetched or old.status in TERMINAL_STATUSES:
            continue
        missing = int(old.metadata.get("missing_fetches", 0)) + 1
        old.metadata["missing_fetches"] = missing
        if missing >= threshold:
            old.status = "resolved"
            old.updated_at = now
            old.raw_hash = old.content_hash()
            append_history("resolved", old)
            report["changes"]["resolved"] += 1
