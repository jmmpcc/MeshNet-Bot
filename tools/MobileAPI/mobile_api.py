#!/usr/bin/env python3
"""API REST v1 para clientes móviles de MeshNet-Bot.

Esta aplicación es deliberadamente independiente del ControlPanel y Web Admin.
No sustituye ni modifica sus rutas actuales: expone una fachada de solo lectura
para MeshNet-Mobile reutilizando los mismos datos y helpers del proyecto.

Ejecución de desarrollo:
    python3 -m uvicorn tools.MobileAPI.mobile_api:app --host 0.0.0.0 --port 8791

Variables:
    MESHNET_MOBILE_API_TOKEN
        Token Bearer obligatorio para todos los endpoints salvo /api/v1/health.

    MESHNET_MOBILE_API_VERSION
        Permite sobreescribir la versión publicada por la API. Por defecto 1.

    MESHNET_BOT_VERSION
        Versión MeshNet-Bot informada al cliente. Por defecto v7.0.49.

Seguridad:
    - /api/v1/health es público y no devuelve secretos.
    - El resto exige Authorization: Bearer <token>.
    - Si no existe token configurado, las rutas protegidas devuelven 503.
    - Nunca se devuelve el contenido de .env ni claves de servicios.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

REPO_DIR = Path(__file__).resolve().parents[2]

from tools.ControlPanel.web_admin import (
    BASE_DIR as CONTROLPANEL_BASE_DIR,
    EMERGENCIAS_CONFIG_FILE,
    ToolRegistry,
    _emergency_config,
    probe,
)
from shared.delivery_audit import query_operations
from tools.emergencias_guardia.emergencias.storage import load_current

API_VERSION = os.getenv("MESHNET_MOBILE_API_VERSION", "1").strip() or "1"
MESHNET_VERSION = os.getenv("MESHNET_BOT_VERSION", "v7.0.49").strip() or "v7.0.49"
TOKEN_ENV = "MESHNET_MOBILE_API_TOKEN"

app = FastAPI(
    title="MeshNet Mobile API",
    version="1.0.0",
    docs_url="/api/v1/docs",
    openapi_url="/api/v1/openapi.json",
)


def _configured_token() -> str:
    """Devuelve el token móvil configurado sin registrarlo ni exponerlo."""
    return os.getenv(TOKEN_ENV, "").strip()


def _bearer_token(request: Request) -> str:
    """Extrae el token de una cabecera Authorization Bearer."""
    authorization = request.headers.get("authorization", "")
    try:
        scheme, supplied = authorization.split(" ", 1)
    except ValueError:
        return ""
    if scheme.casefold() != "bearer":
        return ""
    return supplied.strip()


@app.middleware("http")
async def mobile_api_authentication(request: Request, call_next):
    """Protege exclusivamente la API móvil sin afectar ControlPanel/WebAdmin."""
    path = request.url.path
    if not path.startswith("/api/v1") or path == "/api/v1/health":
        return await call_next(request)

    configured = _configured_token()
    if not configured:
        return JSONResponse(
            {"detail": f"{TOKEN_ENV} no está configurado"},
            status_code=503,
        )

    supplied = _bearer_token(request)
    if not supplied or not secrets.compare_digest(supplied, configured):
        return JSONResponse(
            {"detail": "Token Bearer no válido"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


def _registry() -> ToolRegistry:
    """Crea el registro usando exactamente el state del ControlPanel actual."""
    state_path = Path(
        os.getenv(
            "CONTROLPANEL_STATE",
            str(CONTROLPANEL_BASE_DIR / "data/state.json"),
        )
    )
    return ToolRegistry(state_path)


def _uptime_seconds() -> float | None:
    """Obtiene el uptime Linux sin añadir dependencias como psutil."""
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def _system_snapshot() -> dict[str, Any]:
    """Genera un resumen local de host sin realizar operaciones mutantes."""
    disk = shutil.disk_usage(REPO_DIR)
    uptime = _uptime_seconds()
    load: list[float] = []
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        pass

    return {
        "hostname": platform.node() or None,
        "platform": platform.system(),
        "platform_release": platform.release(),
        "architecture": platform.machine() or None,
        "python": platform.python_version(),
        "uptime_seconds": round(uptime, 1) if uptime is not None else None,
        "load_average": load,
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0.0,
        },
    }


def _services_snapshot() -> list[dict[str, Any]]:
    """Consulta aplicaciones registradas sin ejecutar acciones administrativas."""
    registry = _registry()
    output: list[dict[str, Any]] = []
    for item in registry.items():
        service = {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "enabled": bool(item["enabled"]),
            "reachable": None,
            "http_status": None,
            "detail": None,
        }
        if item["enabled"]:
            try:
                health = probe(registry.get(item["id"]))
                service["reachable"] = bool(health.get("reachable"))
                service["http_status"] = health.get("status")
                service["detail"] = health.get("error")
            except (KeyError, OSError) as exc:
                service["reachable"] = False
                service["detail"] = str(exc)
        output.append(service)
    return output


def _emergencies_snapshot() -> dict[str, Any]:
    """Construye el resumen móvil desde los mismos config/state de emergencias."""
    registry = _registry()
    try:
        config = _emergency_config()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No se pudo leer emergencias") from exc

    try:
        state = json.loads(
            (EMERGENCIAS_CONFIG_FILE.parent / "state.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}

    source_states = state.get("sources", {})
    if not isinstance(source_states, dict):
        source_states = {}

    enabled_sources = [
        source_id
        for source_id, source in config.get("sources", {}).items()
        if isinstance(source, dict) and source.get("enabled")
    ]
    successes = [
        value.get("last_success")
        for value in source_states.values()
        if isinstance(value, dict) and value.get("last_success")
    ]
    incremental = state.get("notifications", {}).get("incremental", {})
    if not isinstance(incremental, dict):
        incremental = {}

    api_reachable: bool | None = None
    try:
        if registry.enabled("emergencias_guardia"):
            api_reachable = bool(probe(registry.get("emergencias_guardia")).get("reachable"))
    except KeyError:
        api_reachable = None

    return {
        "enabled": registry.enabled("emergencias_guardia"),
        "api_reachable": api_reachable,
        "collector": {
            "ok": any(
                isinstance(source_states.get(source_id), dict)
                and source_states[source_id].get("ok")
                for source_id in enabled_sources
            ),
            "last_success": max(successes) if successes else None,
        },
        "sources": {
            "enabled": len(enabled_sources),
            "total": len(config.get("sources", {})),
        },
        "notifications": {
            "enabled": bool(config.get("notifications", {}).get("enabled")),
            "pending": len(incremental.get("pending", [])),
        },
        "coverage": {
            "areas": len(
                [
                    area
                    for area in config.get("areas", [])
                    if isinstance(area, dict) and area.get("enabled", True)
                ]
            )
        },
    }


def _emergency_events_snapshot(
    *,
    source: str = "",
    severity: str = "",
    status: str = "",
    query: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """Devuelve incidencias actuales reutilizando ``load_current``.

    Esta función es estrictamente de lectura: no recalcula fuentes, no modifica
    ``current.json`` y no interviene en deduplicación, notificaciones ni envíos.
    Los filtros se aplican únicamente sobre la instantánea ya persistida por el
    motor de Emergencias.

    Args:
        source: identificador exacto de fuente; vacío incluye todas.
        severity: severidad exacta (low/medium/high/critical); vacío incluye todas.
        status: estado exacto del evento; vacío incluye todos.
        query: búsqueda de texto sobre título, descripción, municipio, provincia,
            carretera, identificador de evento y fuente.
        limit: máximo de eventos devueltos, limitado por el endpoint FastAPI.
    """
    try:
        events = list(load_current().values())
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="No se pudieron leer las incidencias") from exc

    source_value = source.strip().casefold()
    severity_value = severity.strip().casefold()
    status_value = status.strip().casefold()
    query_value = query.strip().casefold()

    def matches(event: Any) -> bool:
        if source_value and str(event.source or "").strip().casefold() != source_value:
            return False
        if severity_value and str(event.severity or "").strip().casefold() != severity_value:
            return False
        if status_value and str(event.status or "").strip().casefold() != status_value:
            return False
        if query_value:
            haystack = " ".join(
                str(value or "")
                for value in (
                    event.event_id,
                    event.source,
                    event.title,
                    event.description,
                    event.road,
                    event.municipality,
                    event.province,
                    event.autonomous_region,
                )
            ).casefold()
            if query_value not in haystack:
                return False
        return True

    filtered = [event for event in events if matches(event)]
    filtered.sort(
        key=lambda event: (
            str(event.updated_at or event.last_seen or event.started_at or ""),
            str(event.event_id or ""),
        ),
        reverse=True,
    )

    visible = filtered[:limit]
    with_coordinates = sum(
        1 for event in filtered if event.latitude is not None and event.longitude is not None
    )
    severity_summary = {
        level: sum(1 for event in filtered if str(event.severity or "").casefold() == level)
        for level in ("low", "medium", "high", "critical")
    }

    return {
        "ok": True,
        "events": [event.to_dict() for event in visible],
        "summary": {
            "total": len(filtered),
            "with_coordinates": with_coordinates,
            "severity": severity_summary,
        },
        "limit": limit,
        "has_more": len(filtered) > limit,
    }


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    """Endpoint público mínimo para detectar MeshNet Mobile API."""
    return {
        "ok": True,
        "service": "meshnet-mobile-api",
        "api_version": API_VERSION,
        "meshnet_version": MESHNET_VERSION,
        "authentication": "bearer",
        "time_unix": int(time.time()),
    }


@app.get("/api/v1/system/overview")
def system_overview() -> dict[str, Any]:
    """Resumen de host para el futuro Dashboard de MeshNet-Mobile."""
    services = _services_snapshot()
    return {
        "ok": True,
        "api_version": API_VERSION,
        "meshnet_version": MESHNET_VERSION,
        "system": _system_snapshot(),
        "services": {
            "registered": len(services),
            "enabled": sum(1 for item in services if item["enabled"]),
            "reachable": sum(1 for item in services if item["reachable"] is True),
        },
    }


@app.get("/api/v1/services")
def services() -> dict[str, Any]:
    """Lista estado de aplicaciones; no permite start/stop/restart en A1."""
    return {"ok": True, "services": _services_snapshot()}


@app.get("/api/v1/messages")
def messages(
    application: str = "",
    source: str = "",
    transport: str = "",
    result: str = "",
    q: str = "",
    hours: int = Query(default=24, ge=0, le=24 * 365),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Expone en lectura el journal común ya utilizado por ControlPanel."""
    return query_operations(
        application=application.strip().casefold(),
        source=source.strip(),
        transport=transport.strip().casefold(),
        result=result.strip().casefold(),
        query=q.strip(),
        hours=hours,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/emergencies/overview")
def emergencies_overview() -> dict[str, Any]:
    """Resumen de Emergencias para tarjetas del Dashboard móvil."""
    return {"ok": True, **_emergencies_snapshot()}


@app.get("/api/v1/emergencies")
def emergencies(
    source: str = "",
    severity: str = "",
    status: str = "",
    q: str = "",
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """Lista incidencias actuales para MeshNet-Mobile, siempre en solo lectura."""
    return _emergency_events_snapshot(
        source=source,
        severity=severity,
        status=status,
        query=q,
        limit=limit,
    )


def _nodes_placeholder(transport: str) -> dict[str, Any]:
    """Contrato estable de nodos sin inventar una fuente de datos."""
    return {
        "ok": True,
        "transport": transport,
        "available": False,
        "reason": "provider_not_linked_in_a1",
        "nodes": [],
    }


@app.get("/api/v1/nodes/meshcore")
def meshcore_nodes() -> dict[str, Any]:
    """Contrato inicial de nodos MeshCore; solo lectura."""
    return _nodes_placeholder("meshcore")


@app.get("/api/v1/nodes/meshtastic")
def meshtastic_nodes() -> dict[str, Any]:
    """Contrato inicial de nodos Meshtastic; solo lectura."""
    return _nodes_placeholder("meshtastic")
