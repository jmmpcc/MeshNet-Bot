from __future__ import annotations

import hashlib
import json
import socket
import time
from datetime import datetime, timezone
from typing import Any

from .emergency_dispatcher import dispatch_secondary_outputs
from .formatters import compact_messages
from .models import Event, SEVERITY_RANK
from .storage import load_state, save_state


ROUTE_PREFIXES = {
    "emergencias": "EMERG",
    "servicios": "SERV",
    "meteo": "METEO",
}
CHANGE_PREFIXES = {
    "new": "NUEVA",
    "updated": "ACTUALIZACIÓN",
    "resolved": "FINALIZADA",
}

WEATHER_CATEGORIES = {
    "storm", "snow", "strong_wind", "extreme_temperature",
}
SERVICE_CATEGORIES = {
    "traffic_collision", "road_closed", "lane_closed", "traffic_obstruction",
    "power_outage", "water_outage", "gas_outage", "other",
}
SERIOUS_CATEGORIES = {
    "wildfire", "urban_fire", "industrial_fire", "traffic_collision",
    "earthquake", "tsunami", "volcanic", "landslide",
    "road_closed", "flood", "chemical", "public_safety", "civil_protection",
}
OFFICIAL_VERIFICATIONS = {"official", "confirmed_multi_source"}


def route_event(event: Event, config: dict[str, Any]) -> str | None:
    """Selecciona canal lógico sin rebajar la confianza de la información."""
    if not is_current(event):
        return None
    notifications = config["notifications"]
    propagation = notifications.get("propagation_filter", {})
    rules = propagation.get("rules")
    matrix_enabled = isinstance(rules, dict)
    if matrix_enabled:
        if event.category not in set(rules.get(event.severity, [])):
            return None
    else:
        configured_categories = propagation.get("categories")
        selected_categories = set(configured_categories or [])
        if configured_categories is not None and event.category not in selected_categories:
            return None
        configured_severities = propagation.get("severities")
        if configured_severities is not None:
            if event.severity not in set(configured_severities):
                return None
        else:
            minimum = propagation.get("minimum_severity", "low")
            if SEVERITY_RANK.get(event.severity, 0) < SEVERITY_RANK.get(minimum, 0):
                return None
    if (
        event.verification == "satellite_detection"
        and not notifications.get("allow_satellite_detection", False)
    ):
        return None
    if event.category in WEATHER_CATEGORIES:
        return "meteo"
    if (
        event.category in SERIOUS_CATEGORIES
        and (matrix_enabled or SEVERITY_RANK.get(event.severity, 0) >= SEVERITY_RANK["high"])
        and event.verification in OFFICIAL_VERIFICATIONS
    ):
        return "emergencias"
    if event.category in SERVICE_CATEGORIES:
        return "servicios"
    return None


def is_current(event: Event, now: datetime | None = None) -> bool:
    if event.status in {"resolved", "cancelled", "expired"}:
        return False
    now = now or datetime.now(timezone.utc)
    started = _parse_datetime(event.started_at)
    expected_end = _parse_datetime(event.expected_end)
    if started and started > now:
        return False
    if expected_end and expected_end < now:
        return False
    return True


def routed_events(
    events: list[Event],
    config: dict[str, Any],
    route: str,
) -> list[Event]:
    selected = [event for event in events if route_event(event, config) == route]
    selected.sort(key=lambda event: (
        SEVERITY_RANK.get(event.severity, 0),
        event.updated_at or event.started_at or event.first_seen,
        event.event_id,
    ), reverse=True)
    maximum = max(1, int(config["notifications"].get("max_events_per_broadcast", 3)))
    return selected[:maximum]


def preview_routes(
    events: list[Event],
    config: dict[str, Any],
    only_route: str | None = None,
) -> dict[str, Any]:
    routes = [only_route] if only_route else list(ROUTE_PREFIXES)
    maximum = int(config["notifications"].get("max_bytes", 140))
    result: dict[str, Any] = {}
    for route in routes:
        selected = routed_events(events, config, route)
        result[route] = {
            "events": len(selected),
            "target": target_for(config, route),
            "messages": compact_messages(
                selected,
                max_bytes=maximum,
                prefix=ROUTE_PREFIXES[route],
            ) if selected else [],
        }
    return result


def target_for(config: dict[str, Any], route: str) -> dict[str, Any]:
    notifications = config["notifications"]
    transport = str(notifications.get("transport", "meshcore")).strip().lower()
    if transport not in {"meshcore", "meshtastic", "both"}:
        raise ValueError("transport debe ser meshcore, meshtastic o both")
    route_config = notifications["routes"][route]
    if transport == "both":
        return {"network": "both", "channel": -1, "targets": [
            {"network": network, "channel": int(route_config[f"{network}_channel"])}
            for network in ("meshcore", "meshtastic")
        ]}
    return {
        "network": transport,
        "channel": int(route_config[f"{transport}_channel"]),
    }


def send_route(
    events: list[Event],
    config: dict[str, Any],
    route: str,
    force: bool = False,
) -> dict[str, Any]:
    notifications = config["notifications"]
    if not notifications.get("enabled", False):
        return {"sent": False, "reason": "notifications_disabled", "route": route}
    selected = routed_events(events, config, route)
    if not selected:
        return {"sent": False, "reason": "no_eligible_events", "route": route}
    target = target_for(config, route)
    targets = target.get("targets") or [target]
    if any(destination["channel"] < 0 for destination in targets):
        return {"sent": False, "reason": "channel_not_configured", "route": route, "target": target}
    messages = compact_messages(
        selected,
        max_bytes=int(notifications.get("max_bytes", 140)),
        prefix=ROUTE_PREFIXES[route],
    )
    digest = _messages_hash(route, target, messages)
    state = load_state()
    notification_state = state.setdefault("notifications", {}).setdefault(route, {})
    if notification_state.get("last_sent_hash") == digest and not force:
        return {"sent": False, "reason": "unchanged", "route": route, "target": target}

    results = []
    delay = max(0.0, float(notifications.get("inter_message_delay_seconds", 8)))
    for destination in targets:
        for index, message in enumerate(messages):
            response = _send_message(config, destination, message)
            if not response.get("ok"):
                raise RuntimeError(
                    f"broker rechazó mensaje {index + 1} ({destination['network']}): {response}"
                )
            results.append({"target": destination, "response": response})
            if delay and index + 1 < len(messages):
                time.sleep(delay)
    notification_state.update({
        "last_sent_hash": digest,
        "last_sent_at": datetime.now(timezone.utc).isoformat(),
        "network": target["network"],
        "channel": target["channel"],
        "targets": targets,
        "messages": len(messages),
        "event_ids": [event.event_id for event in selected],
    })
    save_state(state)
    secondary_outputs = []
    if route == "emergencias":
        for event in selected:
            event_message = compact_messages(
                [event],
                max_bytes=int(notifications.get("max_bytes", 140)),
                prefix=ROUTE_PREFIXES[route],
            )[0]
            secondary_outputs.append(dispatch_secondary_outputs(event, event_message))
    return {
        "sent": True, "route": route, "target": target,
        "events": len(selected), "messages": len(messages), "results": results,
        "secondary_outputs": secondary_outputs,
        "aprs_rf": [item["aprs_rf"] for item in secondary_outputs],
        "aprsis_bulletins": [item["aprsis_bulletin"] for item in secondary_outputs],
    }


def process_incremental(
    events: list[Event],
    config: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Detecta cambios, mantiene un spool y envía únicamente elementos debidos."""
    now = now or datetime.now(timezone.utc)
    timestamp = now.timestamp()
    state = load_state()
    notification_root = state.setdefault("notifications", {})
    incremental = notification_root.setdefault("incremental", {
        "initialized": False,
        "observed": {},
        "delivered": {},
        "pending": [],
    })
    observed = incremental.setdefault("observed", {})
    delivered = incremental.setdefault("delivered", {})
    pending = incremental.setdefault("pending", [])
    report: dict[str, Any] = {
        "initialized": bool(incremental.get("initialized")),
        "baseline": 0,
        "queued": {"new": 0, "updated": 0, "resolved": 0},
        "sent": 0,
        "failed": 0,
        "pending": len(pending),
    }

    current_routes = {
        event.event_id: route_event(event, config)
        for event in events
    }
    if not incremental.get("initialized"):
        for event in events:
            route = current_routes[event.event_id]
            if route:
                observed[event.event_id] = _event_snapshot(event, route)
        incremental.update({
            "initialized": True,
            "baseline_at": now.isoformat(),
            "last_check_at": now.isoformat(),
        })
        report.update({
            "initialized": True,
            "baseline": len(observed),
            "pending": 0,
        })
        save_state(state)
        return report

    if not config["notifications"].get("enabled", False):
        incremental["pending"] = []
        for event in events:
            route = current_routes[event.event_id]
            old_route = observed.get(event.event_id, {}).get("route")
            if route:
                observed[event.event_id] = _event_snapshot(event, route)
            elif event.status in {"resolved", "cancelled", "expired"} and old_route:
                observed[event.event_id] = _event_snapshot(event, old_route)
        report.update({"disabled": True, "pending": 0})
        incremental["last_check_at"] = now.isoformat()
        save_state(state)
        return report

    for event in events:
        event_id = event.event_id
        route = current_routes[event_id]
        old = observed.get(event_id)
        terminal = event.status in {"resolved", "cancelled", "expired"}
        if terminal:
            pending[:] = [item for item in pending if item.get("event_id") != event_id]
            old_route = old.get("route") if old else None
            if old_route and event_id in delivered and _route_is_configured(config, old_route):
                if not old or old.get("status") not in {"resolved", "cancelled", "expired"}:
                    _enqueue_pending(
                        pending, event, old_route, "resolved", timestamp, config,
                    )
                    report["queued"]["resolved"] += 1
                observed[event_id] = _event_snapshot(event, old_route)
            continue
        if not route:
            continue
        change = None
        if old is None:
            change = "new"
        elif old.get("raw_hash") != event.raw_hash or old.get("route") != route:
            change = "updated" if event_id in delivered else "new"
        if change and _route_is_configured(config, route):
            _enqueue_pending(pending, event, route, change, timestamp, config)
            report["queued"][change] += 1
        observed[event_id] = _event_snapshot(event, route)

    max_pending = max(
        1,
        int(config["notifications"]["incremental"].get("max_pending", 200)),
    )
    if len(pending) > max_pending:
        pending[:] = pending[-max_pending:]
        report["trimmed"] = True

    delivery = _deliver_pending(pending, delivered, config, timestamp)
    report.update(delivery)
    report["pending"] = len(pending)
    incremental["last_check_at"] = now.isoformat()
    save_state(state)
    return report


def _enqueue_pending(
    pending: list[dict[str, Any]],
    event: Event,
    route: str,
    change: str,
    timestamp: float,
    config: dict[str, Any],
) -> None:
    existing = next(
        (item for item in pending if item.get("event_id") == event.event_id),
        None,
    )
    if existing and existing.get("change") == "new":
        change = "new"
    pending[:] = [item for item in pending if item.get("event_id") != event.event_id]
    delay = float(
        config["notifications"]["incremental"]
        .get("batch_window_seconds", {})
        .get(route, 300)
    )
    pending.append({
        "event_id": event.event_id,
        "route": route,
        "change": change,
        "event": event.to_dict(),
        "created_at": timestamp,
        "not_before": existing.get("not_before", timestamp + delay)
        if existing else timestamp + delay,
        "attempts": int(existing.get("attempts", 0)) if existing else 0,
    })


def _deliver_pending(
    pending: list[dict[str, Any]],
    delivered: dict[str, Any],
    config: dict[str, Any],
    timestamp: float,
) -> dict[str, int]:
    report = {"sent": 0, "failed": 0}
    maximum = max(1, int(config["notifications"].get("max_events_per_broadcast", 3)))
    delay = max(0.0, float(config["notifications"].get("inter_message_delay_seconds", 8)))
    due_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in pending:
        if float(item.get("not_before", 0)) <= timestamp:
            key = (str(item["route"]), str(item["change"]))
            due_groups.setdefault(key, []).append(item)

    for (route, change), items in due_groups.items():
        target = target_for(config, route)
        targets = target.get("targets") or [target]
        if any(destination["channel"] < 0 for destination in targets):
            continue
        batch = items[:maximum]
        events = [Event.from_dict(item["event"]) for item in batch]
        prefix = f"{CHANGE_PREFIXES[change]} · {ROUTE_PREFIXES[route]}"
        messages = [
            compact_messages(
                [event],
                max_bytes=int(config["notifications"].get("max_bytes", 140)),
                prefix=prefix,
            )[0]
            for event in events
        ]
        for index, (item, event, message) in enumerate(zip(batch, events, messages)):
            try:
                for destination in targets:
                    response = _send_message(config, destination, message)
                    if not response.get("ok"):
                        raise RuntimeError(
                            f"broker rechazó el mensaje ({destination['network']}): {response}"
                        )
            except Exception as exc:
                attempts = int(item.get("attempts", 0)) + 1
                incremental_config = config["notifications"]["incremental"]
                base = max(1, int(incremental_config.get("retry_base_seconds", 60)))
                maximum_retry = max(base, int(incremental_config.get("retry_max_seconds", 3600)))
                item.update({
                    "attempts": attempts,
                    "last_error": str(exc),
                    "not_before": timestamp + min(maximum_retry, base * (2 ** (attempts - 1))),
                })
                report["failed"] += 1
                break
            if route == "emergencias":
                secondary = dispatch_secondary_outputs(event, message)
                aprs_rf_result = secondary.get("aprs_rf", {})
                if not aprs_rf_result.get("ok", True):
                    print(
                        f"[emergencias] APRS RF WARN event={event.event_id}: "
                        f"{aprs_rf_result}",
                        flush=True,
                    )
                aprsis_result = secondary.get("aprsis_bulletin", {})
                if not aprsis_result.get("ok", True):
                    print(
                        f"[emergencias] APRS-IS bulletin WARN event={event.event_id}: "
                        f"{aprsis_result}",
                        flush=True,
                    )
            pending.remove(item)
            delivered[event.event_id] = {
                "raw_hash": event.raw_hash,
                "status": event.status,
                "route": route,
                "sent_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "change": change,
            }
            report["sent"] += 1
            if delay and index + 1 < len(batch):
                time.sleep(delay)
    return report


def _event_snapshot(event: Event, route: str) -> dict[str, Any]:
    return {
        "raw_hash": event.raw_hash,
        "status": event.status,
        "route": route,
    }


def _route_is_configured(config: dict[str, Any], route: str) -> bool:
    try:
        target = target_for(config, route)
        return all(item["channel"] >= 0 for item in (target.get("targets") or [target]))
    except (KeyError, TypeError, ValueError):
        return False



def _send_message(
    config: dict[str, Any],
    target: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    if target["network"] == "meshcore":
        return broker_request(config, "MESHCORE_SEND", {
            "kind": "chan", "channel_idx": target["channel"], "text": message,
        })
    return broker_request(config, "SEND_TEXT", {
        "ch": target["channel"], "dest": None, "ack": 0,
        "origin": "emergencias", "no_bridge": True, "text": message,
    })


def broker_request(
    config: dict[str, Any],
    command: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    broker = config["notifications"]["broker"]
    address = (str(broker.get("host", "127.0.0.1")), int(broker.get("port", 8766)))
    timeout = float(broker.get("timeout_seconds", 10))
    payload = json.dumps(
        {"cmd": command, "params": params},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    with socket.create_connection(address, timeout=timeout) as connection:
        connection.sendall(payload)
        line = connection.makefile("rb").readline()
    if not line:
        raise RuntimeError("broker sin respuesta")
    return json.loads(line.decode("utf-8", errors="replace"))


def _messages_hash(
    route: str,
    target: dict[str, Any],
    messages: list[str],
) -> str:
    encoded = json.dumps(
        {"route": route, "target": target, "messages": messages},
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
