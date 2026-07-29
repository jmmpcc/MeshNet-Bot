from __future__ import annotations

import math
from typing import Any

from .models import Event, SEVERITY_RANK, fold_text, utc_now
from .sources import SOURCE_TYPES
from .sources.base import SourceError
from .storage import (
    append_history, load_current, load_state, save_current, save_state,
)


TERMINAL_STATUSES = {"resolved", "cancelled", "expired"}


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
    kind = area.get("type")
    if kind == "province":
        return fold_text(event.province) == fold_text(area.get("name"))
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
            report["sources"][source_id] = {"ok": False, "skipped": "disabled"}
            continue
        if source_config.get("require_areas") and not any(
            area.get("enabled", True) for area in config.get("areas", [])
        ):
            report["sources"][source_id] = {
                "ok": False,
                "error": "la fuente requiere al menos un área geográfica habilitada",
            }
            continue
        source_type = SOURCE_TYPES.get(source_config.get("type"))
        if source_type is None:
            report["sources"][source_id] = {"ok": False, "error": "tipo de fuente desconocido"}
            continue
        try:
            events, not_modified = source_type(source_id, source_config, config).fetch()
            filtered = {event.event_id: event for event in events if event_matches(event, config)}
            _merge_source(current, filtered, source_id, now, threshold, report)
            source_states[source_id] = {
                "ok": True, "last_success": now, "records": len(events),
                "accepted": len(filtered), "not_modified": not_modified,
            }
            report["sources"][source_id] = source_states[source_id]
        except (SourceError, ValueError, TypeError) as exc:
            previous = source_states.get(source_id, {})
            source_states[source_id] = previous | {"ok": False, "last_error": now, "error": str(exc)}
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
