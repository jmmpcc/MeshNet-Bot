#!/usr/bin/env python3
"""Extensión v7.0.54 de MeshNet Mobile API.

Esta capa se monta sobre la API v1 existente sin modificar su implementación A1.
Su objetivo es mantener la versión publicada sincronizada con MeshNet-Bot y
exponer capacidades para que MeshNet-Mobile pueda adaptar la interfaz sin
suponer funciones que el servidor todavía no permite.

Ejecución:
    python3 -m uvicorn tools.MobileAPI.mobile_api_v7054:app --host 0.0.0.0 --port 8791

Compatibilidad:
    - reutiliza exactamente la autenticación Bearer de mobile_api.py;
    - conserva todos los endpoints A1 existentes;
    - no añade operaciones mutantes;
    - no modifica ControlPanel, WebAdmin, dispatchers ni radio.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import Query

from tools.ControlPanel.emergency_province_view import build_emergency_snapshot
from tools.MobileAPI import mobile_api as base
from tools.emergencias_guardia.emergencias.storage import load_current


REPO_DIR = Path(__file__).resolve().parents[2]
CHANGELOG_DIR = REPO_DIR / "docs"
VERSION_RE = re.compile(r"^CHANGELOG_v(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\.md$")


def _version_key(path: Path) -> tuple[int, int, int, int]:
    """Convierte un nombre CHANGELOG_vX.Y.Z[.N].md en una clave comparable.

    Parámetros:
        path: fichero de changelog candidato.

    Retorno:
        Tupla numérica de cuatro componentes. Si el nombre no corresponde al
        formato esperado devuelve (-1, -1, -1, -1).
    """
    match = VERSION_RE.match(path.name)
    if not match:
        return (-1, -1, -1, -1)
    major, minor, patch, hotfix = match.groups()
    return (int(major), int(minor), int(patch), int(hotfix or 0))


def detect_meshnet_version() -> str:
    """Obtiene la versión que debe publicar la API móvil.

    Orden de resolución:
        1. MESHNET_BOT_VERSION, si está configurada explícitamente.
        2. Changelog con versión numérica más alta en docs/.
        3. v0.0.0 como fallback seguro.

    No lee secretos ni modifica archivos.
    """
    configured = os.getenv("MESHNET_BOT_VERSION", "").strip()
    if configured:
        return configured if configured.startswith("v") else f"v{configured}"

    candidates = [
        path
        for path in CHANGELOG_DIR.glob("CHANGELOG_v*.md")
        if _version_key(path) >= (0, 0, 0, 0)
    ]
    if not candidates:
        return "v0.0.0"

    latest = max(candidates, key=_version_key)
    return latest.stem.removeprefix("CHANGELOG_")


def _normalise_text(value: Any) -> str:
    """Normaliza un valor textual para comparaciones exactas sin distinguir mayúsculas.

    Parámetros:
        value: valor procedente de un evento o parámetro HTTP.

    Retorno:
        Texto sin espacios exteriores y convertido mediante ``casefold``.
    """
    return str(value or "").strip().casefold()


def _parse_event_datetime(value: Any) -> datetime | None:
    """Convierte una marca ISO de Emergencias a ``datetime`` UTC consciente de zona.

    Parámetros:
        value: timestamp ISO, normalmente terminado en ``Z`` o con offset explícito.

    Retorno:
        ``datetime`` UTC o ``None`` si el valor está vacío/no es interpretable.

    Las fechas sin zona se consideran UTC porque los timestamps persistidos por
    ``emergencias_guardia`` se manejan como referencias temporales de backend.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_activity_datetime(event: Any) -> datetime | None:
    """Devuelve la actividad temporal más reciente utilizable de una incidencia.

    Orden de prioridad:
        1. ``last_seen``: última observación real del colector.
        2. ``updated_at``: última actualización publicada.
        3. ``started_at``: comienzo conocido de la incidencia.

    Este orden coincide con el comportamiento ya validado en MeshNet-Mobile para
    los filtros de 24 h / 7 / 30 / 90 días.
    """
    for field in ("last_seen", "updated_at", "started_at"):
        parsed = _parse_event_datetime(getattr(event, field, ""))
        if parsed is not None:
            return parsed
    return None


def _filter_emergency_events(
    events: Iterable[Any],
    *,
    hours: int,
    province: str,
    severity: str,
    category: str,
    now: datetime | None = None,
) -> list[Any]:
    """Filtra la colección completa antes de cualquier paginación.

    Parámetros:
        events: incidencias devueltas por ``load_current().values()``.
        hours: ventana temporal en horas; ``0`` significa Todo.
        province: provincia exacta; vacío incluye todas.
        severity: severidad exacta; vacío incluye todas.
        category: código técnico exacto; vacío incluye todos.
        now: instante UTC opcional para pruebas deterministas.

    Retorno:
        Lista de eventos que cumplen todos los filtros.

    La función no modifica los objetos recibidos y, especialmente, aplica los
    filtros ANTES de ``limit``/``offset`` para evitar la regresión histórica en la
    que una provincia podía desaparecer al quedar fuera de los primeros 200 eventos.
    """
    expected_province = _normalise_text(province)
    expected_severity = _normalise_text(severity)
    expected_category = _normalise_text(category)

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)
    lower_bound = current_time - timedelta(hours=hours) if hours > 0 else None

    result: list[Any] = []
    for event in events:
        if expected_province and _normalise_text(getattr(event, "province", "")) != expected_province:
            continue
        if expected_severity and _normalise_text(getattr(event, "severity", "")) != expected_severity:
            continue
        if expected_category and _normalise_text(getattr(event, "category", "")) != expected_category:
            continue

        if lower_bound is not None:
            activity = _event_activity_datetime(event)
            if activity is None or not (lower_bound <= activity <= current_time):
                continue

        result.append(event)

    return result


# Los handlers originales consultan base.MESHNET_VERSION en tiempo de ejecución,
# por lo que actualizar este valor mantiene /health y /system/overview coherentes
# sin duplicar ni reescribir esos endpoints.
base.MESHNET_VERSION = detect_meshnet_version()
base.app.version = "1.1.0"
app = base.app


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, Any]:
    """Publica qué funciones puede utilizar de forma segura MeshNet-Mobile.

    La app Android debe usar este endpoint para activar u ocultar funciones. De
    este modo una versión móvil nueva puede seguir conectándose a servidores más
    antiguos sin intentar operaciones inexistentes.
    """
    return {
        "ok": True,
        "api_version": base.API_VERSION,
        "meshnet_version": base.MESHNET_VERSION,
        "mode": "read_only",
        "features": {
            "system_overview": True,
            "services_read": True,
            "messages_audit_read": True,
            "emergencies_overview": True,
            "emergencies_read": True,
            "emergencies_coordinates": True,
            "meshcore_nodes": False,
            "meshtastic_nodes": False,
            "message_send": False,
            "service_control": False,
            "configuration_write": False,
        },
        "backend": {
            "delivery_audit": True,
            "emergency_secondary_category_matrix": True,
            "firms_nearest_population": True,
            "lightweight_node_geolocation": True,
        },
    }


@app.get("/api/v1/emergencies/current-view")
def emergencies_current_view() -> dict[str, Any]:
    """Devuelve exactamente la instantánea completa usada por el Control Panel.

    Uso HTTP:
        GET /api/v1/emergencies/current-view

    Funcionalidad:
        Reutiliza `build_emergency_snapshot(load_current().values())`, igual que
        `/api/emergencias/current-view` del Control Panel. La ruta hereda el middleware
        Bearer existente de `mobile_api.py`, no aplica `limit=200`, no ejecuta
        colectores y no modifica `current.json`.
    """
    return build_emergency_snapshot(load_current().values())


@app.get("/api/v1/emergencies/query")
def emergencies_query(
    hours: int = Query(default=24, ge=0, le=24 * 365),
    province: str = Query(default="", max_length=120),
    severity: str = Query(default="", max_length=32),
    category: str = Query(default="", max_length=120),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Consulta incidencias dinámicamente aplicando filtros antes de paginar.

    Uso HTTP:
        GET /api/v1/emergencies/query?hours=24&province=Huesca&category=wildfire

    Parámetros:
        hours: 24 por defecto; 0 significa Todo.
        province: provincia exacta, opcional.
        severity: low/medium/high/critical, opcional.
        category: código técnico de categoría, opcional.
        limit: máximo de filas devueltas en esta página (1..500).
        offset: desplazamiento para paginación.

    Retorno:
        Eventos filtrados y paginados, total previo a paginación, ``has_more`` y
        catálogos globales de provincias/categorías obtenidos de la instantánea
        completa. Los catálogos no se estrechan al aplicar filtros para que Android
        pueda cambiar de selección sin descargar previamente todos los eventos.

    Seguridad/efectos:
        Ruta GET de sólo lectura; hereda la autenticación existente, no ejecuta
        colectores ni modifica el estado persistido.
    """
    all_events = list(load_current().values())
    all_snapshot = build_emergency_snapshot(all_events)
    filtered_events = _filter_emergency_events(
        all_events,
        hours=hours,
        province=province,
        severity=severity,
        category=category,
    )
    filtered_snapshot = build_emergency_snapshot(filtered_events)
    total = len(filtered_snapshot["events"])
    page_events = filtered_snapshot["events"][offset : offset + limit]

    return {
        "ok": True,
        "total": total,
        "returned": len(page_events),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(page_events) < total,
        "filters": {
            "hours": hours,
            "province": province.strip(),
            "severity": severity.strip(),
            "category": category.strip(),
        },
        "provinces": all_snapshot["provinces"],
        "categories": all_snapshot["categories"],
        "events": page_events,
    }
