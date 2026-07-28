from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api import serve
from .config import CONFIG_FILE, DATA_DIR, DEFAULT_CONFIG, load_config, save_config
from .engine import fetch_sources, list_events
from .models import VALID_CATEGORIES, VALID_SEVERITIES
from .notifier import (
    ROUTE_PREFIXES, preview_routes, process_incremental, send_route, target_for,
)
from .sources import SOURCE_TYPES
from .storage import CURRENT_FILE, load_state, read_history


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Agregador independiente de emergencias para MeshNet")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="crea la configuración inicial")
    fetch = commands.add_parser("fetch", help="actualiza las fuentes habilitadas")
    fetch.add_argument("--source")
    check = commands.add_parser(
        "check",
        help="actualiza fuentes y procesa cambios incrementales",
    )
    check.add_argument("--source")
    check.add_argument("--notify-changes", action="store_true")
    listing = commands.add_parser("list", help="muestra emergencias actuales")
    _query_arguments(listing)
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--include-resolved", action="store_true")
    history = commands.add_parser("history", help="muestra el histórico local")
    history.add_argument("--limit", type=int, default=100)
    commands.add_parser("status", help="muestra el estado de las fuentes")
    commands.add_parser("doctor", help="valida configuración y almacenamiento")
    server = commands.add_parser("serve", help="inicia la API HTTP local")
    server.add_argument("--host")
    server.add_argument("--port", type=int)

    area = commands.add_parser("area", help="gestiona áreas geográficas").add_subparsers(
        dest="area_command", required=True
    )
    area.add_parser("list")
    area_add = area.add_parser("add")
    area_add.add_argument("type", choices=("province", "municipality", "radius"))
    area_add.add_argument("name")
    area_add.add_argument("--lat", type=float)
    area_add.add_argument("--lon", type=float)
    area_add.add_argument("--km", type=float)
    area_remove = area.add_parser("remove")
    area_remove.add_argument("name")

    category = commands.add_parser("category", help="gestiona categorías").add_subparsers(
        dest="category_command", required=True
    )
    category.add_parser("list")
    for action in ("enable", "disable"):
        item = category.add_parser(action)
        item.add_argument("name", choices=sorted(VALID_CATEGORIES))

    filters = commands.add_parser("filters", help="gestiona qué alertas se propagan").add_subparsers(
        dest="filters_command", required=True
    )
    filters.add_parser("show")
    filters_set = filters.add_parser("set")
    filters_set.add_argument("--minimum-severity", required=True, choices=VALID_SEVERITIES)
    filters_set.add_argument("--categories", required=True)

    source = commands.add_parser("source", help="gestiona conectores").add_subparsers(
        dest="source_command", required=True
    )
    source.add_parser("list")
    for action in ("enable", "disable", "test"):
        item = source.add_parser(action)
        item.add_argument("name")
    source_url = source.add_parser("set-url")
    source_url.add_argument("name")
    source_url.add_argument("url")

    notify = commands.add_parser(
        "notify",
        help="previsualiza y gestiona difusión por canales",
    ).add_subparsers(dest="notify_command", required=True)
    notify.add_parser("status")
    preview = notify.add_parser("preview")
    preview.add_argument("--route", choices=sorted(ROUTE_PREFIXES))
    set_channel = notify.add_parser("set-channel")
    set_channel.add_argument("route", choices=sorted(ROUTE_PREFIXES))
    set_channel.add_argument("network", choices=("meshcore", "meshtastic"))
    set_channel.add_argument("channel", type=int)
    set_transport = notify.add_parser("set-transport")
    set_transport.add_argument("network", choices=("meshcore", "meshtastic"))
    notify.add_parser("enable")
    notify.add_parser("disable")
    send = notify.add_parser("send")
    send.add_argument("route", choices=sorted(ROUTE_PREFIXES))
    send.add_argument("--force", action="store_true")
    return root


def _query_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--province")
    command.add_argument("--municipality")
    command.add_argument("--category", choices=sorted(VALID_CATEGORIES))
    command.add_argument("--road")
    command.add_argument("--minimum-severity", choices=VALID_SEVERITIES)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_config()
    if args.command == "init":
        print_json({"ok": True, "config": str(CONFIG_FILE), "data_dir": str(DATA_DIR)})
    elif args.command == "fetch":
        report = fetch_sources(config, args.source)
        print_json(report)
        if args.source and args.source not in config.get("sources", {}):
            return 2
    elif args.command == "check":
        report = {"fetch": fetch_sources(config, args.source)}
        if args.notify_changes:
            report["notifications"] = process_incremental(
                list_events(config, {"include_resolved": True}),
                config,
            )
        print_json(report)
        if args.source and args.source not in config.get("sources", {}):
            return 2
    elif args.command == "list":
        query = {
            key: value for key, value in vars(args).items()
            if key in {"province", "municipality", "category", "road", "minimum_severity", "include_resolved"}
            and value
        }
        events = list_events(config, query)
        if args.json:
            print_json({"events": [event.to_dict() for event in events]})
        elif not events:
            print("Sin emergencias activas.")
        else:
            for event in events:
                location = " · ".join(part for part in (event.road, event.municipality, event.province) if part)
                print(f"{event.severity.upper():8} {event.category:22} {event.title}")
                if location:
                    print(f"         {location}")
    elif args.command == "history":
        print_json({"history": read_history(args.limit)})
    elif args.command == "status":
        print_json({
            "config": str(CONFIG_FILE), "current_exists": CURRENT_FILE.exists(),
            "events": len(list_events(config, {"include_resolved": True})),
            "state": load_state(),
        })
    elif args.command == "doctor":
        return _doctor(config)
    elif args.command == "serve":
        serve(config, args.host, args.port)
    elif args.command == "area":
        _areas(config, args)
    elif args.command == "category":
        _categories(config, args)
    elif args.command == "filters":
        return _filters(config, args)
    elif args.command == "source":
        return _sources(config, args)
    elif args.command == "notify":
        return _notifications(config, args)
    return 0


def _doctor(config: dict[str, Any]) -> int:
    problems: list[str] = []
    for source_id, source in config.get("sources", {}).items():
        if source.get("type") not in SOURCE_TYPES:
            problems.append(f"{source_id}: tipo desconocido")
        if source.get("enabled") and not str(source.get("url", "")).strip():
            problems.append(f"{source_id}: habilitada sin URL")
    checks = {
        "ok": not problems, "config": str(CONFIG_FILE), "data_dir": str(DATA_DIR),
        "current_exists": CURRENT_FILE.exists(),
        "enabled_sources": [
            source_id for source_id, source in config.get("sources", {}).items() if source.get("enabled")
        ],
        "areas": config.get("areas", []), "problems": problems,
    }
    print_json(checks)
    return 0 if not problems else 1


def _areas(config: dict[str, Any], args: argparse.Namespace) -> None:
    areas = config.setdefault("areas", [])
    if args.area_command == "list":
        print_json({"areas": areas})
        return
    if args.area_command == "remove":
        before = len(areas)
        config["areas"] = [area for area in areas if area.get("id") != _slug(args.name)]
        save_config(config)
        print_json({"ok": len(config["areas"]) < before, "areas": config["areas"]})
        return
    area: dict[str, Any] = {
        "id": _slug(args.name), "type": args.type, "name": args.name, "enabled": True,
    }
    if args.type == "radius":
        if args.lat is None or args.lon is None or args.km is None or args.km <= 0:
            raise SystemExit("radius requiere --lat, --lon y --km mayor que cero")
        area |= {"latitude": args.lat, "longitude": args.lon, "radius_km": args.km}
    config["areas"] = [item for item in areas if item.get("id") != area["id"]] + [area]
    save_config(config)
    print_json({"ok": True, "area": area})


def _categories(config: dict[str, Any], args: argparse.Namespace) -> None:
    enabled = set(config["filters"].get("categories", []))
    if args.category_command == "list":
        print_json({"categories": [
            {"name": name, "enabled": name in enabled} for name in sorted(VALID_CATEGORIES)
        ]})
        return
    if args.category_command == "enable":
        enabled.add(args.name)
    else:
        enabled.discard(args.name)
    config["filters"]["categories"] = sorted(enabled)
    save_config(config)
    print_json({"ok": True, "category": args.name, "enabled": args.name in enabled})


def _filters(config: dict[str, Any], args: argparse.Namespace) -> int:
    current = config.setdefault("notifications", {}).setdefault("propagation_filter", {
        "minimum_severity": "low",
        "categories": sorted(VALID_CATEGORIES),
    })
    if args.filters_command == "show":
        print_json({
            "minimum_severity": current.get("minimum_severity", "low"),
            "categories": [
                {"name": name, "enabled": name in set(current.get("categories", []))}
                for name in sorted(VALID_CATEGORIES)
            ],
        })
        return 0
    requested = {item.strip() for item in args.categories.split(",") if item.strip()}
    unknown = requested - VALID_CATEGORIES
    if unknown:
        print_json({"ok": False, "error": "categorías desconocidas", "categories": sorted(unknown)})
        return 2
    current["minimum_severity"] = args.minimum_severity
    current["categories"] = sorted(requested)
    save_config(config)
    print_json({
        "ok": True,
        "minimum_severity": args.minimum_severity,
        "categories": sorted(requested),
        "note": "El filtro se aplicará en las próximas comprobaciones y propagaciones.",
    })
    return 0


def _sources(config: dict[str, Any], args: argparse.Namespace) -> int:
    sources = config.setdefault("sources", {})
    if args.source_command == "list":
        print_json({"sources": sources})
        return 0
    if args.name not in sources:
        print_json({"ok": False, "error": "fuente desconocida", "source": args.name})
        return 2
    if args.source_command == "set-url":
        if args.name == "municipal_json" and "zaragoza.es/sede/servicio/via-publica/incidencia" in args.url:
            enabled = bool(sources[args.name].get("enabled", False))
            sources[args.name] = json.loads(json.dumps(DEFAULT_CONFIG["sources"]["municipal_json"]))
            sources[args.name]["enabled"] = enabled
        sources[args.name]["url"] = args.url
        save_config(config)
        print_json({"ok": True, "source": args.name, "url": args.url})
    elif args.source_command in {"enable", "disable"}:
        enabled = args.source_command == "enable"
        if enabled and not str(sources[args.name].get("url", "")).strip():
            print_json({"ok": False, "error": "configure primero la URL", "source": args.name})
            return 2
        sources[args.name]["enabled"] = enabled
        save_config(config)
        print_json({"ok": True, "source": args.name, "enabled": enabled})
    else:
        temporary = json.loads(json.dumps(config))
        temporary["sources"][args.name]["enabled"] = True
        report = fetch_sources(temporary, args.name)
        print_json(report)
        return 0 if report["sources"].get(args.name, {}).get("ok") else 1
    return 0


def _notifications(config: dict[str, Any], args: argparse.Namespace) -> int:
    notifications = config["notifications"]
    if args.notify_command == "status":
        incremental = (
            load_state()
            .get("notifications", {})
            .get("incremental", {})
        )
        print_json({
            "enabled": notifications.get("enabled", False),
            "transport": notifications.get("transport"),
            "routes": {
                route: {
                    **target_for(config, route),
                    "meshcore_channel": int(notifications["routes"][route]["meshcore_channel"]),
                    "meshtastic_channel": int(notifications["routes"][route]["meshtastic_channel"]),
                }
                for route in ROUTE_PREFIXES
            },
            "incremental": {
                "initialized": incremental.get("initialized", False),
                "baseline_at": incremental.get("baseline_at"),
                "last_check_at": incremental.get("last_check_at"),
                "observed": len(incremental.get("observed", {})),
                "delivered": len(incremental.get("delivered", {})),
                "pending": len(incremental.get("pending", [])),
            },
        })
        return 0
    if args.notify_command == "preview":
        events = list_events(config)
        print_json(preview_routes(events, config, args.route))
        return 0
    if args.notify_command == "set-channel":
        notifications["routes"][args.route][f"{args.network}_channel"] = args.channel
        save_config(config)
        print_json({
            "ok": True, "route": args.route, "network": args.network,
            "channel": args.channel,
        })
        return 0
    if args.notify_command == "set-transport":
        notifications["transport"] = args.network
        save_config(config)
        print_json({"ok": True, "transport": args.network})
        return 0
    if args.notify_command in {"enable", "disable"}:
        enabled = args.notify_command == "enable"
        if enabled:
            configured = [
                route for route in ROUTE_PREFIXES
                if target_for(config, route)["channel"] >= 0
            ]
            if not configured:
                print_json({
                    "ok": False,
                    "error": "configure al menos un canal antes de habilitar",
                })
                return 2
        notifications["enabled"] = enabled
        save_config(config)
        print_json({"ok": True, "enabled": enabled})
        return 0
    events = list_events(config)
    result = send_route(events, config, args.route, force=args.force)
    print_json(result)
    return 0 if result.get("sent") or result.get("reason") == "unchanged" else 2


def _slug(value: str) -> str:
    return "-".join(value.casefold().strip().split())


def print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
